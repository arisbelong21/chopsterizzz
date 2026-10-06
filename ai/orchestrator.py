"""Central AI orchestration for Chopster.

The global AI configured in Settings is provided through two explicit adapters
behind one uniform interface:

1) Gateway OpenAI-compatible (``APIProvider``) – own base URL + API key.
2) Google AI Studio / Gemini (``GoogleGeminiProvider``) – API key, Google
   protocol via the official GenAI SDK.

Text/reasoning tasks (transcript analysis, captions, B-roll, subtitle cues,
camera text decisions, …) use the configured global adapter when present.

Visual tasks (frame/scene/camera) use the global adapter **only** when the
selected model's multimodal capability is verified; otherwise they go through
the existing Local Vision/Tracking engine. Local OpenCV/heuristics remain the
last-resort safety net, exactly as before.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from chopster.ai import config_helpers
from chopster.ai.api_provider import APIProvider
from chopster.ai.google_gemini import GoogleGeminiProvider
from chopster.ai.local_provider import LocalProvider
from chopster.ai.provider_interface import AIRequest, AIResponse, AIProvider


@dataclass
class AIHealth:
    state: str = "ready"  # ready | online | degraded | offline | local
    failures: int = 0
    last_error: str = ""
    last_success: float = 0.0
    disabled_until: float = 0.0

    @property
    def circuit_open(self) -> bool:
        return self.disabled_until > time.time()


class AIOrchestrator:
    """Single gateway between all features and all AI layers."""

    _settings_health: dict[str, AIHealth] = {}

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = dict(config or {})
        self.provider = config_helpers.normalize_provider(self.config.get("ai_provider"))
        self.endpoint = str(self.config.get("ai_endpoint") or "").strip()
        self.api_key = str(self.config.get("ai_api_key") or "").strip()
        self.model = str(self.config.get("ai_model") or "").strip()
        self.vision_model = str(self.config.get("ai_vision_model") or "").strip()
        self.settings_enabled = config_helpers.settings_ai_enabled(self.provider, self.endpoint, self.api_key)
        self.failover_local = bool(self.config.get("ai_failover_local", True))
        self.cooldown = max(5.0, float(self.config.get("ai_failover_cooldown", 60) or 60))
        self.models_cache = list(self.config.get("ai_models_cache") or [])
        self.local = LocalProvider()
        self._remote: AIProvider | None = None
        self._settings_health_state = self._settings_health.setdefault(self.key, AIHealth())
        self._vision_verification: dict[str, bool] = {}

    @property
    def key(self) -> str:
        return f"settings|{self.provider}|{self.endpoint}|{self.model}|{self.vision_model}"

    @property
    def health(self) -> AIHealth:
        return self._settings_health_state

    # ------------------------------------------------------------- providers

    def _make_remote_provider(self) -> AIProvider:
        if self.provider == config_helpers.PROVIDER_GOOGLE:
            return GoogleGeminiProvider(
                api_key=self.api_key,
                model=self.model,
                timeout=int(self.config.get("ai_timeout") or 60),
            )
        return APIProvider(
            endpoint=self.endpoint,
            api_key=self.api_key,
            model=self.model,
            timeout=int(self.config.get("ai_timeout") or 60),
            vision_model=self.vision_model,
        )

    def remote_provider(self, *, require_vision: bool = False) -> AIProvider | None:
        """Return the configured global AI adapter (gateway or Google)."""
        if not self.settings_enabled or self.health.circuit_open:
            return None
        if self._remote is None:
            try:
                self._remote = self._make_remote_provider()
            except Exception as exc:
                self.health.last_error = str(exc)[:700]
                return None
        ok, _ = self._remote.is_available()
        if not ok:
            self.health.last_error = str(self._remote.is_available()[1])[:700]
            return None
        if require_vision and not self._remote_vision_available():
            return None
        return self._remote

    def _remote_vision_available(self) -> bool:
        """Whether the configured provider/model is verified as multimodal."""
        provider = self.provider
        if provider == config_helpers.PROVIDER_GOOGLE:
            model_id = (self.vision_model or self.model or "").strip()
        else:
            model_id = (self.vision_model or self.model or "").strip()
        return config_helpers.can_send_vision(provider, model_id, self.models_cache, self.endpoint)

    def _embedded_state(self) -> AIHealth:
        # Kept for API compatibility: the old embedded channel no longer carries
        # bundled credentials; its availability is now the global vision path.
        h = AIHealth()
        h.state = "online" if self._remote_vision_available() else "offline"
        h.last_error = "" if h.state == "online" else "Global AI tidak menyediakan mode visual terverifikasi; Local Vision siap."
        return h

    # ---------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        sh = self.health
        if not self.settings_enabled:
            settings_state = "not-configured"
        elif sh.circuit_open:
            settings_state = "degraded"
        elif sh.last_success:
            settings_state = "online"
        else:
            settings_state = "ready"
        eh = self._embedded_state()
        remote = self.remote_provider()
        remote_name = (remote.name if remote is not None else self.provider)
        vision_ok = self._remote_vision_available()
        overall = "online" if settings_state == "online" else (
            "degraded" if settings_state == "degraded" else (
                "online" if vision_ok else "ready"))
        return {
            "state": overall,
            "failures": sh.failures,
            "last_error": sh.last_error or eh.last_error,
            "last_success": sh.last_success,
            "cooldown_remaining": max(0.0, sh.disabled_until - time.time()),
            "provider": remote_name if self.settings_enabled else "none",
            "model": self.model or self.vision_model or "Auto",
            "settings": {
                "state": settings_state,
                "provider": self.provider if self.settings_enabled else "none",
                "model": self.model,
                "vision_model": self.vision_model,
                "last_error": sh.last_error,
            },
            "global_vision": {
                "state": "online" if vision_ok else ("offline" if self.settings_enabled else "not-configured"),
                "verified": vision_ok,
                "model": (self.vision_model or self.model or ""),
            },
            "fallback_local": self.failover_local,
        }

    # ------------------------------------------------------------- recording

    def _record_settings_success(self) -> None:
        h = self.health
        h.failures = 0; h.last_error = ""; h.last_success = time.time(); h.disabled_until = 0.0; h.state = "online"

    def _record_settings_failure(self, exc: Exception | str) -> None:
        h = self.health
        h.failures += 1; h.last_error = str(exc)[:700]; h.disabled_until = time.time() + self.cooldown; h.state = "degraded"

    # ------------------------------------------------------------------ local

    def local_response(self, fn: Callable[[], AIResponse | str]) -> AIResponse:
        result = fn()
        if isinstance(result, AIResponse):
            return result
        return AIResponse(text=str(result or ""), raw={"provider": "local", "fallback": True})

    def _bounded_request(self, req: AIRequest) -> AIRequest:
        if not self.failover_local:
            return req
        return AIRequest(
            prompt=req.prompt, system=req.system, model=req.model,
            temperature=req.temperature, max_tokens=req.max_tokens,
            timeout=min(int(req.timeout or 60), max(5, int(self.config.get("ai_failover_timeout") or 15))),
        )

    # ----------------------------------------------------------------- text

    def generate(self, req: AIRequest, *, local_fallback: Callable[[], AIResponse | str] | None = None) -> AIResponse:
        """Text/reasoning route: global Settings AI first; local fallback when absent/failed."""
        bounded = self._bounded_request(req)
        errors: list[str] = []

        if self.settings_enabled:
            provider = self.remote_provider()
            if provider is not None and not self.health.circuit_open:
                try:
                    reviewed = provider.generate(bounded)
                    self._record_settings_success()
                    if isinstance(reviewed.raw, dict):
                        reviewed.raw.setdefault("provider", "global-ai")
                        reviewed.raw["ai_layer"] = "remote"
                        reviewed.raw["provider_chain"] = [provider.name]
                    return reviewed
                except Exception as exc:
                    self._record_settings_failure(exc)
                    errors.append(f"{provider.name}: {exc}")
            else:
                errors.append(self.health.last_error or "AI global tidak tersedia")
        else:
            errors.append("AI global belum dikonfigurasi")

        if local_fallback and self.failover_local:
            resp = self.local_response(local_fallback)
            if isinstance(resp.raw, dict):
                resp.raw["provider"] = "local-fallback"
                resp.raw["ai_layer"] = "local"
                resp.raw["ai_error"] = " | ".join(errors)[-900:]
            return resp
        return self.local.generate(bounded)

    def generate_json(
        self,
        req: AIRequest,
        *,
        local_fallback: Callable[[], Any],
        validator: Callable[[Any], Any] | None = None,
    ) -> tuple[Any, AIResponse]:
        def fallback_response() -> AIResponse:
            value = local_fallback()
            return AIResponse(text=json.dumps(value, ensure_ascii=False), raw={"provider": "local-fallback", "fallback": True})

        resp = self.generate(req, local_fallback=fallback_response)
        raw = (resp.text or "").strip()
        if "```" in raw:
            m = re.search(r"```(?:json)?\s*(.*?)\s*```", raw, re.S | re.I)
            if m:
                raw = m.group(1).strip()
        try:
            value = json.loads(raw)
            if validator:
                value = validator(value)
            return value, resp
        except Exception as exc:
            value = local_fallback()
            return value, AIResponse(text=json.dumps(value, ensure_ascii=False), raw={"provider": "local-fallback", "ai_error": str(exc)[:400], "fallback": True})

    # --------------------------------------------------------------- vision

    def generate_vision(
        self, prompt: str, image_paths: list[str], *, system: str = "", model: str = "",
        temperature: float = 0.2, max_tokens: int = 4096, timeout: int | None = None,
        local_fallback: Callable[[], AIResponse | str] | None = None,
    ) -> AIResponse:
        """Visual route.

        Local evidence is gathered first when the user prefers local-first,
        then the global AI is used only when its selected model is verified as
        multimodal; otherwise the existing Local Vision/Tracking engine answers.
        """
        local_resp: AIResponse | None = None
        local_evidence = ""
        collect_local_first = bool(self.config.get("ai_local_vision", True)) and self._vision_prefers_local_evidence()
        if collect_local_first:
            try:
                local_resp = self.local.generate_vision(prompt, image_paths, system=system, model=model,
                                                        temperature=temperature, max_tokens=max_tokens, timeout=timeout)
                local_evidence = (local_resp.text or "")[:9000]
            except Exception as exc:
                local_resp = AIResponse(text="", raw={"provider": "local-vision-ai", "error": str(exc)[:300]})

        evidence_prompt = prompt
        if local_evidence:
            evidence_prompt += "\n\nLOCAL VISION EVIDENCE (verify visually; do not blindly trust):\n" + local_evidence

        errors: list[str] = []
        provider: AIProvider | None = None
        if self.settings_enabled and not self.health.circuit_open:
            provider = self.remote_provider()
        vision_requested_model = (model or self.vision_model or self.model or "").strip()
        remote_allowed = (provider is not None) and config_helpers.can_send_vision(
            self.provider, vision_requested_model or provider.name, self.models_cache, self.endpoint,
        )
        # A verified vision model id lets routing proceed. When none is
        # configured explicitly, fall back to the provider's own verified
        # discovery only (never name inference).
        if remote_allowed:
            try:
                remote_resp = provider.generate_vision(
                    evidence_prompt, image_paths,
                    system=system, model=vision_requested_model,
                    temperature=temperature, max_tokens=max_tokens,
                    timeout=self._bounded_request(AIRequest(prompt="", timeout=timeout or self.config.get("ai_timeout") or 60)).timeout,
                )
                self._record_settings_success()
                if isinstance(remote_resp.raw, dict):
                    remote_resp.raw.setdefault("provider", provider.name)
                    remote_resp.raw["ai_layer"] = "remote"
                    remote_resp.raw["provider_chain"] = (["local-vision-ai", provider.name] if local_evidence else [provider.name])
                    remote_resp.raw["local_vision_evidence"] = local_evidence
                return remote_resp
            except Exception as exc:
                self._record_settings_failure(exc)
                errors.append(f"{provider.name}: {exc}")
        else:
            if provider is not None:
                errors.append("Model AI global belum terverifikasi untuk gambar; visual memakai Local Vision/Tracking.")
            else:
                errors.append("AI global belum dikonfigurasi; visual memakai Local Vision/Tracking.")

        if local_fallback and self.failover_local:
            resp = self.local_response(local_fallback)
        else:
            if local_resp is None and bool(self.config.get("ai_local_vision", True)):
                try:
                    local_resp = self.local.generate_vision(prompt, image_paths, system=system, model=model,
                                                            temperature=temperature, max_tokens=max_tokens, timeout=timeout)
                except Exception as exc:
                    local_resp = AIResponse(text="", raw={"provider": "local-vision-ai", "error": str(exc)[:300]})
            resp = local_resp if local_resp is not None else self.local.generate_vision(prompt, image_paths, system=system,
                                                                                        model=model, temperature=temperature,
                                                                                        max_tokens=max_tokens, timeout=timeout)
        if isinstance(resp.raw, dict):
            resp.raw["provider"] = "local-fallback"
            resp.raw["ai_layer"] = "local"
            resp.raw["ai_error"] = " | ".join(errors)[-900:]
            resp.raw["local_vision_evidence"] = local_evidence
        return resp

    def _vision_prefers_local_evidence(self) -> bool:
        pref = str(self.config.get("ai_vision_preference") or "").strip().lower()
        return pref in {"local_first", "hybrid", "evidence_first"}


def orchestrator_from_config(config: dict[str, Any] | None = None) -> AIOrchestrator:
    return AIOrchestrator(config or {})
