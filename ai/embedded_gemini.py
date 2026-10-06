"""Embedded Gemini compatibility shim.

Security note: previous builds bundled a third-party gateway endpoint and API
key directly in this module. Those credentials were removed and are **not**
replaced with any vendor-owned key. A bare ``EmbeddedGeminiProvider()`` is now
unavailable; the visual/editor AI is instead provided by the global AI
configured in Settings (Gateway OpenAI-compatible or Google AI Studio / Gemini),
with the existing local vision/tracking engine as the automatic fallback.
"""
from __future__ import annotations

from typing import Any

from chopster.ai.api_provider import APIProvider
from chopster.ai.provider_interface import AIResponse

# Intentionally empty: no bundled credentials.
EMBEDDED_GEMINI_ENDPOINT = ""
EMBEDDED_GEMINI_API_KEY = ""
EMBEDDED_GEMINI_MODEL = ""
EMBEDDED_GEMINI_FALLBACK_MODELS: tuple[str, ...] = ()


class EmbeddedGeminiProvider(APIProvider):
    """Backward-compatible name.

    Requires explicit credentials (endpoint + API key) supplied by the caller;
    with no credentials it reports unavailable so the local fallback is used.
    """

    def __init__(self, timeout: int = 45, endpoint: str = "", api_key: str = "", model: str = EMBEDDED_GEMINI_MODEL):
        super().__init__(
            endpoint=endpoint or EMBEDDED_GEMINI_ENDPOINT,
            api_key=api_key or EMBEDDED_GEMINI_API_KEY,
            model=model,
            timeout=timeout,
            vision_model=model,
            retry_5xx=False,
        )
        self.embedded_model = model or ""
        self.last_attempts: list[str] = []

    @property
    def name(self) -> str:
        return "embedded-gemini"

    def is_available(self) -> tuple[bool, str]:
        if not self.api_key or not self.base_url:
            return False, "Kredensial Embedded Gemini dicabut. Gunakan AI global di Settings, atau Local Vision/Tracking."
        return super().is_available()

    def health_probe(self) -> dict[str, Any]:
        return {
            "ok": False,
            "provider": self.name,
            "model": self.embedded_model,
            "vision": False,
            "error": "Embedded Gemini kredensial bawaan telah dicabut (keamanan). Konfigurasikan AI global di Settings untuk layanan remote.",
            "models": [],
            "vision_models": [],
        }

    # generate()/generate_vision() inherit APIProvider behaviour, but since a
    # bare instance is unavailable they will raise an informative RuntimeError
    # through is_available() before any network request is made.
