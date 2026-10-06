"""Resolution of the Chopster *global* AI for Auto Clip Studio use.

Auto Clip Studio keeps its own Gemini API key (and the existing "Get free key"
button). When that key is empty, ACS may use the global AI configured in
Settings **only** when the global provider/model is verified as vision-capable.
This module is deliberately dependency-light (stdlib + chopster config helpers)
so it stays importable even when FastAPI/google-genai are not installed.
"""
from __future__ import annotations

import json
from typing import Any


def _load_global_settings() -> dict[str, Any]:
    """Load preferences and DPAPI-backed secrets through Configuration."""
    try:
        from chopster.app import paths
        from chopster.app.configuration import Configuration
        return Configuration(data_dir=paths.user_data_dir()).as_dict()
    except Exception:
        return {}


def _load_acs_key() -> str:
    try:
        return str(_load_global_settings().get("acs_gemini_key") or "").strip()
    except Exception:
        return ""


def _vision_verified(provider: str, model_id: str, cache: list, endpoint: str = "") -> bool:
    try:
        from chopster.ai.config_helpers import can_send_vision
        return can_send_vision(provider, model_id, cache, endpoint)
    except Exception:
        return False


def get_global_ai_context() -> dict[str, Any]:
    """Return a safe snapshot of the global AI usable as an ACS fallback.

    Returns:
      provider           "none" | "gateway" | "google"
      google_api_key     set only when provider == "google" AND vision verified
      gateway            dict {endpoint, api_key, model, vision_model, timeout}
                         set only when provider == "gateway" AND vision verified
      vision_verified    whether the global model passed the multimodal check
    """
    result: dict[str, Any] = {
        "provider": "none",
        "google_api_key": "",
        "gateway": None,
        "vision_verified": False,
    }
    try:
        from chopster.ai.config_helpers import normalize_provider, settings_ai_enabled
    except Exception:
        return result

    data = _load_global_settings()
    provider = normalize_provider(data.get("ai_provider"))
    endpoint = str(data.get("ai_endpoint") or "").strip()
    api_key = str(data.get("ai_api_key") or "").strip()
    model = str(data.get("ai_model") or "").strip()
    vision_model = str(data.get("ai_vision_model") or "").strip()
    if not settings_ai_enabled(provider, endpoint, api_key):
        return result

    result["provider"] = provider
    effective_model = (vision_model or model or "")
    cache = data.get("ai_models_cache") or []
    verified = _vision_verified(provider, effective_model, cache, endpoint)
    result["vision_verified"] = bool(verified)
    if not verified:
        return result

    if provider == "google":
        result["google_api_key"] = api_key
    else:
        result["gateway"] = {
            "endpoint": endpoint,
            "api_key": api_key,
            "model": effective_model,
            "vision_model": vision_model,
            "timeout": max(5, int(data.get("ai_timeout") or 90) or 90),
        }
    return result


def resolve_ai_source(acs_key: str = "", env_gemini_key: str = "") -> dict[str, Any]:
    """Decide which AI source an Auto Clip Studio analysis request should use.

    Priority:
      1. ``mock``              – the ACS key is literally "mock"
      2. ``acs``               – ACS-specific Gemini key (or env GEMINI_API_KEY)
      3. ``google_global``     – global Google AI Studio key (vision-verified)
      4. ``gateway``           – global Gateway (vision-verified model)
      5. ``none``              – nothing usable; caller shows the key-required hint

    Returns: {source, gemini_key, gateway, is_mock}
    """
    # The UI stores its ACS-only key in DPAPI; accepting a one-off request key
    # remains for standalone development and backwards compatibility.
    key = (acs_key or _load_acs_key() or env_gemini_key or "").strip()
    is_mock = key.lower() == "mock"
    if is_mock:
        return {"source": "mock", "gemini_key": key, "gateway": None, "is_mock": True}
    if key:
        return {"source": "acs", "gemini_key": key, "gateway": None, "is_mock": False}
    ctx = get_global_ai_context()
    google_key = str(ctx.get("google_api_key") or "")
    gateway = ctx.get("gateway")
    if google_key:
        return {"source": "google_global", "gemini_key": google_key, "gateway": None, "is_mock": False}
    if gateway:
        return {"source": "gateway", "gemini_key": "", "gateway": gateway, "is_mock": False}
    return {"source": "none", "gemini_key": "", "gateway": None, "is_mock": False}
