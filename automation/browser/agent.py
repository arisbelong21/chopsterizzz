"""AI Browser Agent: OBSERVE → REASON → VALIDATE → ACT → VERIFY loop.

Contract (spec §16-§25, §56-§58, §93):
  * the model only returns structured JSON actions from the allowed schema,
  * a deterministic validator gates every action (domain + file allowlist),
  * the executor never uses coordinates,
  * CAPTCHA/MFA/checkpoints pause for the human — never bypassed,
  * bounded retries (3 strategies then ACTION_REQUIRED / PLATFORM_UI_CHANGED),
  * reasoning uses the existing Chopster AI orchestrator (no hardcoded vendor),
  * no chain-of-thought is surfaced to the UI — only operational status.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable

from chopster.automation.browser import executor
from chopster.automation.browser import session as browser_session
from chopster.automation.browser.actions import ActionValidator, looks_like_security_challenge
from chopster.automation.browser.session import BrowserSession, logs_dir
from chopster.automation.models import AgentState, NeedsActionError

AGENT_SYSTEM = """You are the UI reasoning component of Chopster Automation.

You are NOT allowed to invent user intent.
You are NOT allowed to change schedule, caption meaning, visibility, or account destination.
You must inspect the current browser state before selecting an action.
You must prefer semantic targets over coordinates.
You must act only through the allowed action schema.
You must stop on authentication/security challenges.
You must never attempt to bypass CAPTCHA, MFA, rate limits, or platform security controls.
You must verify important transitions.
When the interface is ambiguous, request a new observation or pause for the user.
When you cannot safely continue, return ACTION_REQUIRED instead of guessing.

Respond with ONE JSON object only, schema:
{"state":"...","goal":"...","confidence":0.0,"action":{"type":"CLICK|TYPE|SELECT|UPLOAD_FILE|SCROLL|WAIT|NAVIGATE|PRESS_KEY|FOCUS|READ|VERIFY|REQUEST_USER_ACTION|STOP","target":{"role":"...","name":"...","text":"...","selector":"..."}|null,"value":"..."},"reason":"short rationale","expected_change":"..."}

Allowed action types: CLICK, TYPE, SELECT, UPLOAD_FILE, SCROLL, WAIT, NAVIGATE, PRESS_KEY, FOCUS, READ, VERIFY, REQUEST_USER_ACTION, STOP."""

START_URLS = {
    "instagram": "https://www.instagram.com/reels/create/",
    "facebook": "https://www.facebook.com/",
    "tiktok": "https://www.tiktok.com/upload",
    "youtube": "https://studio.youtube.com/",
}

MAX_STEPS = 30
MAX_STRATEGIES = 3


def _extract_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    candidates = [text]
    if "{" in text and "}" in text:
        candidates.append(text[text.find("{"):text.rfind("}") + 1])
    for cand in candidates:
        try:
            data = json.loads(cand)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return None


def _build_observation(page: Any, task: dict[str, Any], completed: list[str],
                       last_action: dict[str, Any] | None) -> dict[str, Any]:
    try:
        url = page.url or ""
    except Exception:
        url = ""
    try:
        title = page.title() or ""
    except Exception:
        title = ""
    visible = ""
    try:
        visible = str(page.evaluate("() => document.body ? document.body.innerText : ''") or "")[:4000]
    except Exception:
        visible = ""
    a11y = ""
    try:
        snap = page.accessibility.snapshot()
        a11y = json.dumps(snap, ensure_ascii=False)[:6000] if snap else ""
    except Exception:
        a11y = ""
    allowed = ["CLICK", "TYPE", "SELECT", "UPLOAD_FILE", "SCROLL", "WAIT", "NAVIGATE", "PRESS_KEY", "FOCUS", "READ", "VERIFY", "REQUEST_USER_ACTION", "STOP"]
    return {"current_url": url, "page_title": title, "accessibility_tree": a11y, "visible_text": visible, "task": task.get("name", "publish_short_video"), "platform": task.get("platform", ""), "user_intent": task.get("intent", {}), "completed_steps": completed[-8:], "last_action": last_action, "allowed_actions": allowed, "constraints": ["no coordinates", "domain allowlist", "no security bypass", "user intent is authoritative"]}


def _reason(app: Any, observation: dict[str, Any]) -> dict[str, Any] | None:
    try:
        from chopster.ai.orchestrator import orchestrator_from_config
        from chopster.ai.provider_interface import AIRequest
        config = app.config.as_dict()
        orchestrator = orchestrator_from_config(config)
        prompt = "OBSERVATION:\n" + json.dumps(observation, ensure_ascii=False)[:12000] + "\n\nChoose the single next action. JSON only."
        req = AIRequest(prompt=prompt, system=AGENT_SYSTEM, model=str(config.get("ai_model") or ""), temperature=0.1, max_tokens=1200, timeout=int(config.get("ai_timeout") or 60))

        def _fallback() -> str:
            return json.dumps(_local_reason(observation))

        resp = orchestrator.generate(req, local_fallback=_fallback)
        return _extract_json(resp.text)
    except Exception:
        try:
            return _local_reason(observation)
        except Exception:
            return None


def _local_reason(observation: dict[str, Any]) -> dict[str, Any]:
    """Deterministic local heuristic used when no AI provider is configured.

    Deliberately conservative: find a visible upload control, else wait.
    """
    text = str(observation.get("visible_text") or "").lower()
    for label in ("upload file", "upload video", "select file", "choose file", "add media", "create"):
        if label in text:
            return {"state": "finding_upload", "goal": "upload_video", "confidence": 0.6, "action": {"type": "CLICK", "target": {"text": label}, "value": None}, "expected_change": "upload control activates"}
    if "file" in text:
        video_path = (observation.get("user_intent") or {}).get("video_path")
        return {"state": "upload_screen", "goal": "upload_video", "confidence": 0.55, "action": {"type": "UPLOAD_FILE", "target": {"selector": "input[type='file']"}, "value": video_path}, "expected_change": "video attached"}
    return {"state": "unknown", "goal": "observe", "confidence": 0.3, "action": {"type": "WAIT", "target": None, "value": 2}, "expected_change": "page settles"}


def _save_diagnostics(platform: str, page: Any, url: str, note: str, keep: bool) -> str:
    """Failure-only screenshot + context (spec §24/§93)."""
    path = ""
    try:
        logs_dir().mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        base = logs_dir() / f"{stamp}_{platform}"
        path_png = str(base.with_suffix(".png"))
        try:
            page.screenshot(path=path_png, full_page=False)
            path = path_png
        except Exception:
            path = ""
        (base.with_suffix(".txt")).write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\nurl: {url}\nnote: {note}\n", encoding="utf-8")
        if not keep:
            _cleanup_old_logs()
    except Exception:
        pass
    return path


def _cleanup_old_logs(max_age_hours: int = 48) -> None:
    try:
        root = logs_dir()
        if not root.exists():
            return
        cutoff = time.time() - max_age_hours * 3600
        for p in root.glob("*"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
            except Exception:
                pass
    except Exception:
        pass


def _friendly_step(atype: str) -> str:
    return {"CLICK": "Mengklik kontrol…", "TYPE": "Mengisi teks…", "SELECT": "Memilih opsi…", "UPLOAD_FILE": "Mengunggah media…", "SCROLL": "Menelusuri halaman…", "WAIT": "Menunggu…", "NAVIGATE": "Membuka halaman…", "PRESS_KEY": "Menekan tombol…", "FOCUS": "Memfokuskan field…", "READ": "Membaca halaman…"}.get(atype, "Bekerja…")


def publish_via_agent(*, app: Any, platform: str, media_path: str, caption: str,
                      privacy: str, progress: Callable[[int, str], None],
                      cancel_check: Callable[[], bool], account_id: str = "") -> dict[str, Any]:
    """Drive the platform UI to publish one video via the AI agent loop."""
    # Fail fast (and without touching the browser) when the Playwright package
    # is not importable: the caller gets an actionable install hint instead of
    # a generic "browser agent failed" after a failed launch attempt.
    if not browser_session.playwright_installed():
        return {"ok": False, "category": "UNSUPPORTED_OPERATION",
                "error": "playwright unavailable",
                "message": browser_session.INSTALL_HINT}
    try:
        headless = bool(app.config.get("aut_browser_headless"))
    except Exception:
        headless = False
    try:
        nav_ms = int(app.config.get("aut_browser_nav_timeout_ms") or 30000)
    except Exception:
        nav_ms = 30000
    try:
        action_ms = int(app.config.get("aut_browser_action_timeout_ms") or 15000)
    except Exception:
        action_ms = 15000
    try:
        keep_diag = bool(app.config.get("aut_keep_diagnostics"))
    except Exception:
        keep_diag = False
    try:
        channel = str(app.config.get("aut_browser_channel") or "")
    except Exception:
        channel = ""
    try:
        minimize = bool(app.config.get("aut_browser_minimize"))
    except Exception:
        minimize = False
    intent = {"video_path": media_path, "caption": caption, "schedule": None, "visibility": privacy}
    task = {"name": "publish_short_video", "platform": platform, "intent": intent}
    completed: list[str] = []
    last_action: dict[str, Any] | None = None
    strategy_misses = 0

    def _emit(label: str, pct: int) -> None:
        progress(pct, f"AI Browser Agent: {label}")

    try:
        with BrowserSession(platform, headless=headless, nav_timeout_ms=nav_ms, action_timeout_ms=action_ms, channel=channel, account_id=account_id) as session:
            _emit("Membuka browser…", 6)
            try:
                session.goto(START_URLS.get(platform, f"https://www.{platform}.com/"))
            except Exception:
                pass
            if minimize:
                # Opt-in only: keeps the upload window out of the way without
                # changing the default (hidden tabs can throttle page timers).
                session.minimize_window()
            page = session.page
            if page is None:
                return {"ok": False, "category": "PLATFORM_UI_CHANGED", "error": "no page", "message": "Browser tidak bisa membuka halaman."}
            try:
                page.set_default_timeout(action_ms)
            except Exception:
                pass
            validator = ActionValidator(platform, allowed_files=[media_path])
            for step in range(1, MAX_STEPS + 1):
                if cancel_check():
                    return {"ok": False, "category": "CANCELLED", "error": "cancelled", "message": "Dibatalkan."}
                _emit("Menganalisis halaman…", min(60, 6 + step * 2))
                obs = _build_observation(page, task, completed, last_action)
                challenge = looks_like_security_challenge(str(obs.get("current_url") or ""), str(obs.get("visible_text") or ""))
                if challenge:
                    shot = _save_diagnostics(platform, page, str(obs.get("current_url")), f"security challenge: {challenge}", keep_diag)
                    marker = str(challenge).lower()
                    reason = "login_required" if any(k in marker for k in ("login", "checkpoint", "suspicious", "two-factor", "2fa", "identity", "verify it")) else "captcha"
                    return {"ok": False, "category": "USER_ACTION_REQUIRED", "reason": reason, "error": f"security challenge: {challenge}", "message": "Platform meminta verifikasi keamanan. Buka Automation > Accounts > Kelola > 'Login / Verifikasi browser' untuk menyelesaikannya di jendela browser Chopster, lalu klik '↻ Ulangi' di Queue.", "screenshot": shot, "state": AgentState.WAITING_USER.value}
                _emit("Merencanakan aksi…", min(62, 8 + step * 2))
                decision = _reason(app, obs)
                if not decision:
                    strategy_misses += 1
                    if strategy_misses >= MAX_STRATEGIES:
                        break
                    continue
                validated, why = validator.validate_model_output(decision)
                if validated is None:
                    strategy_misses += 1
                    try:
                        app.logger.info("[Automation][agent] rejected action: %s", why)
                    except Exception:
                        pass
                    if strategy_misses >= MAX_STRATEGIES:
                        break
                    continue
                action = validated["action"]
                atype = str(action.get("type") or "").upper()
                if atype in ("REQUEST_USER_ACTION", "STOP"):
                    return {"ok": False, "category": "USER_ACTION_REQUIRED", "error": str(action.get("value") or "agent stopped"), "message": str(action.get("value") or "Bantuan pengguna diminta agent."), "state": AgentState.WAITING_USER.value}
                if atype == "VERIFY":
                    try:
                        executor.execute(page, action)
                        completed.append("verified")
                        _emit("Terverifikasi", 96)
                        session.mark_verified()
                        return {"ok": True, "external_id": "", "external_url": "", "verification": "ui_confirmed", "state": AgentState.COMPLETED.value}
                    except executor.ExecutorError:
                        strategy_misses += 1
                        if strategy_misses >= MAX_STRATEGIES:
                            break
                        continue
                _emit(_friendly_step(atype), min(70, 12 + step * 2))
                try:
                    outcome = executor.execute(page, action)
                except executor.ExecutorError as exc:
                    strategy_misses += 1
                    try:
                        app.logger.info("[Automation][agent] exec failed: %s", exc)
                    except Exception:
                        pass
                    if strategy_misses >= MAX_STRATEGIES:
                        break
                    continue
                strategy_misses = 0
                last_action = {"type": atype, "outcome": outcome, "expected": validated.get("expected_change", "")}
                completed.append(f"step{step}:{atype}:{outcome}")
                try:
                    page.wait_for_timeout(700)
                except Exception:
                    time.sleep(0.7)
            url_now = ""
            try:
                url_now = page.url or ""
            except Exception:
                pass
            shot = _save_diagnostics(platform, page, url_now, "agent exhausted retries (possible UI change)", keep_diag)
            return {"ok": False, "category": "PLATFORM_UI_CHANGED", "error": "semantic action not found after bounded retries", "message": "Automation dijeda: Chopster tidak bisa mengenali kontrol publish dengan aman. Coba Retry With AI atau selesaikan manual.", "screenshot": shot, "state": AgentState.BLOCKED.value}
    except NeedsActionError as exc:
        return {"ok": False, "category": exc.category, "reason": getattr(exc, "reason", ""),
                "error": str(exc), "message": exc.user_message}
    except Exception as exc:
        return {"ok": False, "category": "PLATFORM_UI_CHANGED", "error": str(exc), "message": "Browser agent gagal. Detail tersimpan di log."}
