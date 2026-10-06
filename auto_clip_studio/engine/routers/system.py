import logging
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import JSONResponse

from chopster.auto_clip_studio.engine.config import _base_dir, logger
from chopster.auto_clip_studio.engine.services.system_service import (
    cleanup_expired_temp_files,
    clear_temp_files,
    get_current_git_info,
    get_temp_storage_summary,
    run_git_command,
)

router = APIRouter(tags=["System"])
_LOCAL_SESSION_TOKEN = secrets.token_urlsafe(32)


def verify_admin_access(
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
    authorization: Optional[str] = Header(None)
) -> bool:
    """
    Verifies administrative authorization.
    If ADMIN_API_KEY or CHEAT_CLIP_API_KEY is configured in .env, requires matching token.
    If no administrator key is configured, requires this process's random session token.
    """
    admin_key = (os.environ.get("ADMIN_API_KEY") or os.environ.get("CHEAT_CLIP_API_KEY") or "").strip()
    provided = (x_api_key or "").strip()
    if not provided and authorization and authorization.startswith("Bearer "):
        provided = authorization[7:].strip()

    expected = admin_key or _LOCAL_SESSION_TOKEN
    if not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="Unauthorized: Invalid or missing administrator API key")
    return True


@router.get("/api/system/session")
async def get_local_session_token():
    """Return a per-process token to the trusted same-origin desktop frontend."""
    if os.environ.get("ADMIN_API_KEY") or os.environ.get("CHEAT_CLIP_API_KEY"):
        raise HTTPException(status_code=403, detail="Administrator API key is configured")
    return JSONResponse(
        {"token": _LOCAL_SESSION_TOKEN},
        headers={"Cache-Control": "no-store, private"},
    )


@router.get("/api/temp-storage-info")
async def get_temp_storage_info():
    """Returns total files, bytes, and formatted size of temp download storage."""
    return get_temp_storage_summary()


@router.post("/api/clear-temp")
async def clear_temp_folder(authorized: bool = Depends(verify_admin_access)):
    """
    Clears all temporary downloaded video clips, audio slices, ASS files, and frames
    from TEMP_DIR and backend/temp. Re-creates empty directories.
    PROTECTED: cookies.txt and any cookie files are strictly PRESERVED and NEVER deleted.
    """
    return clear_temp_files()


@router.post("/api/cleanup-expired-temp")
async def cleanup_expired_temp(max_age_hours: int = 48, authorized: bool = Depends(verify_admin_access)):
    """Deletes temporary frame images and slices older than max_age_hours."""
    return cleanup_expired_temp_files(max_age_hours=max_age_hours)


@router.get("/api/system/version")
def api_system_version():
    """Returns local git version information."""
    return get_current_git_info()


@router.get("/api/system/check-update")
def api_check_update():
    """Fetches origin and checks if updates are available."""
    root_dir = Path(_base_dir).parents[1]
    info = get_current_git_info()
    branch = info.get("branch", "master") or "master"

    # A distributed Chopster ZIP is intentionally not a Git working tree.
    # The packaged installer is the supported update mechanism.
    if not (root_dir / ".git").exists():
        remote = (info.get("remote_url") or "").strip()
        if not remote:
            return {
                **info, "update_available": False, "behind_count": 0,
                "remote_commit": info.get("current_commit_full", ""),
                "changelog": [],
                "message": "Updates are delivered with a new Chopster package; this build has no Git remote.",
            }
        rc_remote, remote_line, err_remote = run_git_command(
            ["ls-remote", remote, "HEAD"], cwd=root_dir, timeout=20
        )
        if rc_remote != 0 or not remote_line:
            return {
                **info, "update_available": False, "behind_count": 0,
                "remote_commit": info["current_commit"], "changelog": [],
                "error": f"Could not reach upstream: {err_remote or 'network error'}",
            }
        remote_full = remote_line.split()[0]
        available = remote_full != info.get("current_commit_full")
        return {
            **info, "update_available": available,
            "behind_count": 1 if available else 0,
            "remote_commit": remote_full[:7],
            "changelog": ([{"hash": remote_full[:7],
                            "message": "A newer Chopster package is available",
                            "date": ""}] if available else []),
        }

    # Fetch origin
    rc_fetch, _, err_fetch = run_git_command(["fetch", "origin", branch], cwd=root_dir, timeout=20)
    if rc_fetch != 0:
        return {
            **info,
            "update_available": False,
            "behind_count": 0,
            "changelog": [],
            "error": f"Failed to fetch updates from remote: {err_fetch or 'Network or remote error'}"
        }

    # Count commits behind
    rc_count, count_str, _ = run_git_command(["rev-list", "--count", "HEAD..FETCH_HEAD"], cwd=root_dir)
    behind_count = int(count_str) if rc_count == 0 and count_str.isdigit() else 0

    # Get changelog of new commits
    changelog = []
    if behind_count > 0:
        rc_log, log_str, _ = run_git_command(["log", "HEAD..FETCH_HEAD", "--pretty=format:%h|%s|%cd", "--date=short"], cwd=root_dir)
        if rc_log == 0 and log_str:
            for line in log_str.splitlines():
                p = line.strip().split("|")
                if len(p) >= 3:
                    changelog.append({"hash": p[0], "message": p[1], "date": p[2]})

    rc_remote_commit, remote_commit, _ = run_git_command(["rev-parse", "--short", "FETCH_HEAD"], cwd=root_dir)

    return {
        **info,
        "update_available": behind_count > 0,
        "behind_count": behind_count,
        "remote_commit": remote_commit if rc_remote_commit == 0 else info["current_commit"],
        "changelog": changelog
    }


@router.post("/api/system/update")
async def api_perform_update(authorized: bool = Depends(verify_admin_access)):
    """Update only a development Git checkout; packaged builds require a new installer."""
    root_dir = Path(_base_dir).parents[1]
    if not (root_dir / ".git").is_dir() or os.environ.get("CHOPSTER_DEV_MODE") != "1":
        raise HTTPException(
            status_code=409,
            detail="Self-update is unavailable in packaged builds. Install a newer Chopster package instead.",
        )
    info = get_current_git_info()
    branch = info.get("branch", "master") or "master"
    old_head = info.get("current_commit_full", "HEAD")

    # 1. Fetch latest
    rc_fetch, _, err_fetch = run_git_command(["fetch", "origin", branch], cwd=root_dir, timeout=25)
    if rc_fetch != 0:
        raise HTTPException(status_code=500, detail=f"Failed to fetch updates: {err_fetch}")

    # 2. Check if working tree has tracked changes
    rc_status, status_out, _ = run_git_command(["status", "--porcelain"], cwd=root_dir)
    has_local_changes = False
    if rc_status == 0 and status_out:
        for line in status_out.splitlines():
            if not line.startswith("??"):
                has_local_changes = True
                break

    if has_local_changes:
        logger.info("Local changes detected. Stashing before update...")
        run_git_command(["stash", "save", "Auto-stash before Chopster package update"], cwd=root_dir)

    # 3. Pull latest changes
    rc_pull, pull_out, err_pull = run_git_command(["pull", "origin", branch], cwd=root_dir, timeout=40)
    if rc_pull != 0:
        if has_local_changes:
            run_git_command(["stash", "pop"], cwd=root_dir)
        raise HTTPException(status_code=500, detail=f"Git pull failed: {err_pull or pull_out}")

    if has_local_changes:
        logger.info("Reapplying stashed local changes...")
        run_git_command(["stash", "pop"], cwd=root_dir)

    # 4. Check what files changed between old_head and new HEAD
    rc_diff, diff_files, _ = run_git_command(["diff", f"{old_head}..HEAD", "--name-only"], cwd=root_dir)
    changed_files = diff_files.splitlines() if rc_diff == 0 and diff_files else []

    updated_deps = []
    # If package.json changed, run npm install
    web_source = root_dir / "auto_clip_studio" / "web_source"
    if "auto_clip_studio/web_source/package.json" in changed_files:
        logger.info("package.json changed. Running npm install...")
        try:
            import shutil
            npm_bin = shutil.which("npm.cmd") or shutil.which("npm") or "npm"
            subprocess.run([npm_bin, "install"], shell=False, cwd=str(web_source), timeout=120, check=True)
            updated_deps.append("Node modules (npm install)")
        except Exception as e:
            logger.warning(f"npm install warning: {e}")

    # If requirements.txt changed, run pip install
    if "requirements.txt" in changed_files:
        if getattr(sys, "frozen", False):
            # Frozen EXE: sys.executable is the app EXE, so `-m pip` would relaunch
            # the GUI. Dependency install is a dev-mode action and is skipped here.
            logger.warning("pip install dilewati pada build EXE (dev-only action).")
        else:
            logger.info("requirements.txt changed. Running pip install...")
            try:
                req_file = root_dir / "requirements.txt"
                subprocess.run([sys.executable, "-m", "pip", "install", "-r", str(req_file)], shell=False, cwd=str(root_dir), timeout=180)
                updated_deps.append("Python dependencies (pip install)")
            except Exception as e:
                logger.warning(f"pip install warning: {e}")

    # 5. Trigger detached restart runner
    from chopster.auto_clip_studio.engine.services.system_service import trigger_detached_restart
    trigger_detached_restart(delay=2.5)

    new_info = get_current_git_info()
    return {
        "success": True,
        "status": "restarting",
        "previous_commit": info["current_commit"],
        "new_commit": new_info["current_commit"],
        "updated_deps": updated_deps,
        "message": "Chopster Auto Clip Studio has been updated. The server is restarting..."
    }


@router.post("/api/system/restart")
async def api_restart_app(authorized: bool = Depends(verify_admin_access)):
    """Restart the frontend/backend pair only while running in development mode."""
    if os.environ.get("CHOPSTER_DEV_MODE") != "1":
        raise HTTPException(status_code=501, detail="Restart is controlled by the Chopster desktop application.")
    from chopster.auto_clip_studio.engine.services.system_service import trigger_detached_restart
    trigger_detached_restart(delay=2.5)
    return {
        "success": True,
        "status": "restarting",
        "message": "Chopster Auto Clip Studio is restarting..."
    }
