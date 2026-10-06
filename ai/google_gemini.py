"""Google AI Studio / Gemini direct provider adapter.

Talks to Google's own Generative Language API through the official
``google-genai`` SDK (already a Chopster dependency used by Auto Clip Studio).
This adapter is intentionally separate from the OpenAI-compatible Gateway
adapter: Google requests are sent in Google's native format and never through a
``/chat/completions`` gateway, and vice versa.

The dependency is imported lazily so this module stays importable even when
``google-genai`` is not installed (the adapter simply reports unavailable).
"""
from __future__ import annotations

import base64
import json
from typing import Any

from chopster.ai.provider_interface import AIProvider, AIRequest, AIResponse

DEFAULT_MODEL = "gemini-2.5-flash"

# Model families that are known NOT to accept visual input. This list is used
# only as a conservative exclusion (it never enables vision), so a model is never
# claimed multimodal from its name alone.
_KNOWN_NON_VISUAL_TOKENS = (
    "embedding", "embed", "tts", "speech", "audio", "imagen", "image-", "video-",
    "realtime", "robotics", "customtools", "translation", "text-embedding",
)


def _is_visual_candidate(model_id: str) -> bool:
    mid = str(model_id or "").lower()
    return not any(token in mid for token in _KNOWN_NON_VISUAL_TOKENS)


class GoogleGeminiProvider(AIProvider):
    """Direct Google AI Studio / Gemini provider backed by the GenAI SDK."""

    def __init__(self, api_key: str = "", model: str = "", timeout: int = 60):
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip() or DEFAULT_MODEL
        self.timeout = max(5, int(timeout or 60))
        self.models_cache: list[dict[str, Any]] = []
        self._client = None

    @property
    def name(self) -> str:
        return "google-gemini"

    def is_available(self) -> tuple[bool, str]:
        if not self.api_key:
            return False, "API key belum diatur"
        return True, "Siap"

    def _sdk(self):
        if self._client is None:
            from google import genai
            self._client = genai.Client(api_key=self.api_key)
        return self._client

    # ------------------------------------------------------------------ models

    def discover_models(self) -> list[dict[str, Any]]:
        """List generative Gemini models visible to this API key."""
        client = self._sdk()
        rows: list[dict[str, Any]] = []
        try:
            page = client.models.list()
        except Exception as exc:
            raise RuntimeError(f"Gagal mengambil daftar model Google: {exc}") from exc
        for m in page or []:
            name = str(getattr(m, "name", "") or "")
            mid = name.split("/")[-1]
            if "gemini" not in mid.lower():
                continue
            actions = [str(x) for x in (getattr(m, "supported_actions", None) or [])]
            if actions and "generateContent" not in actions:
                continue
            row: dict[str, Any] = {
                "id": mid,
                "name": mid,
                "owned_by": "google",
                "capabilities": [],
                "input_modalities": [],
            }
            row["_raw_meta"] = {
                "id": mid,
                "supported_actions": actions,
            }
            from chopster.ai.config_helpers import model_scope
            row["_chopster_scope"] = model_scope("google")
            rows.append(row)
        if not rows:
            rows = [{
                "id": self.model.split("/")[-1],
                "name": self.model.split("/")[-1],
                "owned_by": "google",
                "capabilities": [],
                "input_modalities": [],
                "_raw_meta": {"id": self.model.split("/")[-1]},
                "_chopster_scope": {"provider": "google", "endpoint": ""},
            }]
        self.models_cache = rows
        return rows

    # ---------------------------------------------------------------- generate

    def _config(self, req: AIRequest, max_tokens: int | None = None):
        from google.genai import types
        kw: dict[str, Any] = {"response_modalities": ["TEXT"]}
        if req.system:
            kw["system_instruction"] = req.system
        kw["temperature"] = req.temperature
        if max_tokens:
            kw["max_output_tokens"] = max_tokens
        if req.max_tokens:
            kw["max_output_tokens"] = max(
                int(req.max_tokens), int(kw.get("max_output_tokens") or 0)
            )
        return types.GenerateContentConfig(**kw)

    def generate(self, req: AIRequest) -> AIResponse:
        ok, msg = self.is_available()
        if not ok:
            raise RuntimeError(msg)
        client = self._sdk()
        model = str(req.model or "").strip() or self.model
        try:
            resp = client.models.generate_content(
                model=model,
                contents=req.prompt,
                config=self._config(req),
            )
        except Exception as exc:
            raise RuntimeError(f"Google AI tidak merespons: {exc}") from exc
        text = str(getattr(resp, "text", None) or "").strip()
        if not text and getattr(resp, "candidates", None) is None:
            raise RuntimeError("Google AI tidak mengembalikan jawaban")
        return AIResponse(
            text=text,
            raw={"provider": self.name, "model": model, "raw": _safe_raw(resp)},
            usage={"prompt": None, "completion": None, "total": None},
        )

    def generate_vision(
        self,
        prompt: str,
        image_paths: list[str],
        *,
        system: str = "",
        model: str = "",
        temperature: float = 0.2,
        max_tokens: int = 4096,
        timeout: int | None = None,
    ) -> AIResponse:
        ok, msg = self.is_available()
        if not ok:
            raise RuntimeError(msg)
        from google.genai import types
        client = self._sdk()
        selected = str(model or "").strip() or self.model
        parts: list[Any] = [types.Part(text=prompt)]
        for path in image_paths or []:
            p = str(path)
            with open(p, "rb") as fh:
                raw_bytes = fh.read()
            suffix = p.lower().rsplit(".", 1)[-1] if "." in p else "png"
            mime = {
                "jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                "webp": "image/webp", "gif": "image/gif",
            }.get(suffix, "image/png")
            parts.append(types.Part(
                inline_data=types.Blob(
                    mime_type=mime,
                    data=base64.b64encode(raw_bytes).decode("ascii"),
                )
            ))
        req = AIRequest(
            prompt=prompt, system=system, model=selected,
            temperature=temperature, max_tokens=max_tokens,
            timeout=timeout or self.timeout,
        )
        try:
            resp = client.models.generate_content(
                model=selected,
                contents=types.Content(role="user", parts=parts),
                config=self._config(req, max_tokens=max_tokens),
            )
        except Exception as exc:
            raise RuntimeError(f"Google AI Vision tidak merespons: {exc}") from exc
        text = str(getattr(resp, "text", None) or "").strip()
        if not text:
            raise RuntimeError("Google AI Vision tidak mengembalikan jawaban")
        return AIResponse(
            text=text,
            raw={"provider": f"{self.name}-vision", "model": selected, "raw": _safe_raw(resp)},
            usage={"prompt": None, "completion": None, "total": None},
        )

    def test_inference(self, model: str | None = None) -> AIResponse:
        selected = str(model or "").strip() or self.model
        return self.generate(AIRequest(
            prompt="Balas tepat satu kata: OK",
            system="Kamu sedang diuji koneksinya. Jangan jelaskan apa pun.",
            model=selected, max_tokens=32, timeout=self.timeout,
        ))

    def connection_probe(self, preferred_model: str = "") -> dict[str, Any]:
        """Probe model discovery; model routing is validated by the SDK."""
        result: dict[str, Any] = {"ok": False, "models": [], "model_route_ok": False, "inference_ok": False}
        try:
            models = self.discover_models()
            result.update({"ok": True, "models": models, "model_route_ok": True, "message": f"{len(models)} model"})
        except Exception as exc:
            result["model_error"] = str(exc)[:900]
            result["message"] = result.get("model_error") or "Google AI tidak merespons"
        return result


def _safe_raw(resp: Any) -> dict[str, Any] | None:
    try:
        return json.loads(json.dumps({k: str(v) for k, v in vars(resp).items()}, default=str)) if hasattr(resp, "__dict__") else None
    except Exception:
        return None
