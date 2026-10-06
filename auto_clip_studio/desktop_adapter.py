"""Native desktop bridge for the Chopster Auto Clip Studio engine.

No HTTP server or browser is used: FastAPI route functions are invoked in-process and
SSE payloads are converted to ordinary Python callbacks.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path
from threading import Event
from typing import Any, Callable

Progress = Callable[[dict[str, Any]], None]


def _decode_sse(chunk: Any) -> list[dict[str, Any]]:
    if isinstance(chunk, bytes):
        chunk = chunk.decode("utf-8", "replace")
    events = []
    for block in str(chunk).replace("\r\n", "\n").split("\n\n"):
        data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
        if data:
            try:
                events.append(json.loads(data))
            except json.JSONDecodeError:
                pass
    return events


def analyze(payload: dict[str, Any], progress: Progress | None = None,
            cancel: Event | None = None) -> dict[str, Any]:
    """Run the original seven-tier analysis pipeline synchronously."""
    async def _run() -> dict[str, Any]:
        from .engine.routers.analyze import analyze_video
        from .engine.schemas.analyze import AnalyzeRequest

        response = await analyze_video(AnalyzeRequest(**payload))
        result = None
        async for chunk in response.body_iterator:
            if cancel and cancel.is_set():
                raise RuntimeError("Analisis dibatalkan pengguna.")
            for event in _decode_sse(chunk):
                if progress:
                    progress(event)
                if event.get("error"):
                    raise RuntimeError(str(event["error"]))
                if event.get("done"):
                    result = event.get("result")
        if not result:
            raise RuntimeError("Mesin analisis selesai tanpa hasil.")
        # Preserve direct local path; the web build returns /api/video/... instead.
        source = str(payload.get("url") or "")
        if Path(source).is_file():
            result["video_url"] = str(Path(source).resolve())
        return result

    return asyncio.run(_run())


def render_batch(result: dict[str, Any], selected_clips: list[dict[str, Any]],
                 settings: dict[str, Any], progress: Progress | None = None,
                 cancel: Event | None = None) -> dict[str, Any]:
    """Run the original render queue directly and return its final batch state."""
    async def _run() -> dict[str, Any]:
        from .engine.schemas.render import RenderBatchRequest, RenderSettingsModel
        from .engine.services.render_service import (
            BATCH_REQUESTS, RENDER_BATCHES, process_batch_rendering,
        )

        batch_id = f"batch_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        request = RenderBatchRequest(
            video_url=result.get("video_url") or result.get("source_url") or "",
            video_id=result.get("video_id") or "",
            clips=selected_clips,
            settings=RenderSettingsModel(**settings),
            transcript=result.get("transcript") or [],
        )
        status_clips = []
        for idx, clip in enumerate(selected_clips):
            title = (clip.get("custom_title") or clip.get("title_suggestion")
                     or clip.get("title") or f"Clip {idx + 1}")
            status_clips.append({"clip_index": idx, "title": title,
                                 "base_title": title, "status": "pending",
                                 "progress_percent": 0})
        RENDER_BATCHES[batch_id] = {
            "batch_id": batch_id, "total_clips": len(selected_clips),
            "current_clip_index": 0, "overall_status": "running",
            "clips": status_clips, "zip_url": None,
        }
        BATCH_REQUESTS[batch_id] = request
        task = asyncio.create_task(process_batch_rendering(batch_id, request))
        last = ""
        while not task.done():
            if cancel and cancel.is_set():
                task.cancel()
                RENDER_BATCHES[batch_id]["overall_status"] = "cancelled"
                raise RuntimeError("Render dibatalkan pengguna.")
            snapshot = json.loads(json.dumps(RENDER_BATCHES[batch_id], default=str))
            encoded = json.dumps(snapshot, sort_keys=True)
            if encoded != last and progress:
                progress(snapshot)
                last = encoded
            await asyncio.sleep(.25)
        await task
        final = RENDER_BATCHES[batch_id]
        if progress:
            progress(final)
        return final

    return asyncio.run(_run())


def retry_failed(batch_id: str, progress: Progress | None = None) -> dict[str, Any]:
    async def _run():
        from .engine.services.render_service import RENDER_BATCHES, process_batch_retry
        batch = RENDER_BATCHES.get(batch_id)
        if not batch:
            raise RuntimeError("Batch tidak ditemukan.")
        indices = [i for i, c in enumerate(batch["clips"]) if c.get("status") == "error"]
        if not indices:
            return batch
        await process_batch_retry(batch_id, indices)
        if progress:
            progress(batch)
        return batch
    return asyncio.run(_run())


def output_directories() -> tuple[Path, Path]:
    from .engine.video_engine import EXPORTS_DIR, TEMP_DIR
    return Path(EXPORTS_DIR), Path(TEMP_DIR)


def download_raw_full(result: dict[str, Any], progress: Progress | None = None) -> Path:
    """Download/copy the complete source as a high-quality video+audio MP4."""
    async def _run():
        import re
        from .engine.services.download_service import raw_download_jobs, run_raw_download_job
        from .engine.video_engine import EXPORTS_DIR
        job_id = f"raw_{uuid.uuid4().hex[:10]}"
        clean = re.sub(r'[\\/*?:"<>|]', "", result.get("title") or "full_video")[:100]
        filename = f"{clean or 'full_video'}_raw.mp4"
        out = Path(EXPORTS_DIR) / filename
        raw_download_jobs[job_id] = {"status": "pending", "progress_percent": 0}
        task = asyncio.create_task(run_raw_download_job(
            job_id, result.get("video_url") or result.get("source_url") or "",
            str(out), filename, clean,
        ))
        while not task.done():
            if progress:
                progress(dict(raw_download_jobs[job_id]))
            await asyncio.sleep(.25)
        await task
        state = raw_download_jobs[job_id]
        if state.get("status") != "ready":
            raise RuntimeError(state.get("error") or "Raw download failed")
        return out
    return asyncio.run(_run())


def download_raw_clips(result: dict[str, Any], clips: list[dict[str, Any]],
                       progress: Progress | None = None) -> list[Path]:
    """Export selected unstyled segments with their original video and audio."""
    async def _run():
        from .engine.services.download_service import (
            raw_clip_download_jobs, run_raw_clip_download_job,
        )
        from .engine.video_engine import EXPORTS_DIR
        outputs = []
        for index, clip in enumerate(clips):
            job_id = f"rawclip_{uuid.uuid4().hex[:10]}"
            raw_clip_download_jobs[job_id] = {"status": "pending", "progress_percent": 0}
            await run_raw_clip_download_job(
                job_id, result.get("video_url") or result.get("source_url") or "",
                result.get("video_id") or "video", float(clip.get("start_time", 0)),
                float(clip.get("end_time", 0)), clip.get("custom_title")
                or clip.get("title_suggestion") or clip.get("title") or f"clip_{index+1}",
            )
            state = raw_clip_download_jobs[job_id]
            if progress:
                progress({"clip_index": index, **state})
            if state.get("status") != "ready":
                raise RuntimeError(state.get("error") or f"Raw clip {index+1} failed")
            # The service writes a unique file; locate it through download_url.
            name = str(state["download_url"]).split("?")[0].rsplit("/", 1)[-1]
            outputs.append(Path(EXPORTS_DIR) / name)
        return outputs
    return asyncio.run(_run())
