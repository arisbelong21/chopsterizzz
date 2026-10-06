"""Run an Auto Clip Studio analysis prompt through the global Gateway AI.

Used only when the global Settings AI is a Gateway OpenAI-compatible provider
whose selected model has been verified as vision-capable, and the ACS-specific
Gemini key is empty. The caller (analyze router) keeps its own parse/retry/
fallback logic: this returns a small object with ``.text`` containing the JSON
payload, which the existing pipeline parses exactly like a Gemini response.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from chopster.ai.api_provider import APIProvider
from chopster.ai.provider_interface import AIRequest


@dataclass
class GatewayResponse:
    text: str = ""
    parsed: Any = None
    raw: dict[str, Any] = field(default_factory=dict)


def run_gateway_analysis(gateway_ctx: dict, prompt: str, model: str, max_output_tokens: int) -> GatewayResponse:
    """Send the ACS analysis prompt through the global Gateway adapter.

    Raises RuntimeError with recognisable codes (429/401/403/404/422/…) so the
    caller's existing error classification keeps working.
    """
    provider = APIProvider(
        endpoint=str(gateway_ctx.get("endpoint") or ""),
        api_key=str(gateway_ctx.get("api_key") or ""),
        model=str(model or ""),
        timeout=int(gateway_ctx.get("timeout") or 90 or 90),
        vision_model=str(gateway_ctx.get("vision_model") or model or ""),
    )
    resp = provider.generate(AIRequest(
        prompt=prompt,
        system="You are an expert viral video clip editor. Follow the user instructions exactly and return ONLY valid JSON (no markdown fences, no commentary).",
        model=str(model or ""),
        temperature=0.2,
        max_tokens=int(max_output_tokens or 8192),
        timeout=int(gateway_ctx.get("timeout") or 90 or 90),
    ))
    text = (resp.text or "").strip()
    if not text:
        raise RuntimeError("Gateway AI mengembalikan respons kosong")
    return GatewayResponse(text=text, raw=resp.raw if isinstance(resp.raw, dict) else {})
