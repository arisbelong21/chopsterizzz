"""Shared AI configuration helpers for Chopster.

Visual capability is conservative: a model may receive images only when its
provider metadata explicitly says it accepts image input or a successful safe
vision test verified it. Cached results are also bound to the provider and,
for gateways, the exact normalized endpoint.
"""
from __future__ import annotations

from typing import Any

PROVIDER_NONE = "none"
PROVIDER_GATEWAY = "gateway"
PROVIDER_GOOGLE = "google"
_LEGACY_GATEWAY_PROVIDERS = {"openai-compatible", "custom", "openai", "gateway"}
VISION_VERIFIED = "vision"
VISION_TEXT_ONLY = "text"
VISION_UNKNOWN = "unknown"


def normalize_provider(value: Any) -> str:
    """Map persisted provider values onto none|gateway|google."""
    provider = str(value or "").strip().lower()
    if provider in ("", "none", "local", "off", "disabled"):
        return PROVIDER_NONE
    if provider in _LEGACY_GATEWAY_PROVIDERS or provider in ("gateway-openai",):
        return PROVIDER_GATEWAY
    if provider in (PROVIDER_GOOGLE, "google-gemini", "google_ai_studio", "googleaistudio", "gemini"):
        return PROVIDER_GOOGLE
    # Preserve the existing safe default for an unknown explicit provider.
    return PROVIDER_GATEWAY


def _normalize_endpoint(endpoint: Any) -> str:
    value = str(endpoint or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/models"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
    if value.endswith("/v1"):
        return value
    if "/v1/" in value:
        return value.split("/v1/", 1)[0] + "/v1"
    return value


def model_scope(provider: Any, endpoint: Any = "") -> dict[str, str]:
    """Return a non-secret identity for model metadata and verification cache."""
    p = normalize_provider(provider)
    return {
        "provider": p,
        "endpoint": _normalize_endpoint(endpoint) if p == PROVIDER_GATEWAY else "",
    }


def model_matches_scope(row: Any, provider: Any, endpoint: Any = None) -> bool:
    """Whether a cached model row belongs to the active provider/endpoint."""
    if not isinstance(row, dict):
        return False
    scope = row.get("_chopster_scope")
    if not isinstance(scope, dict):
        # Legacy unscoped cache entries must not authorize image transmission.
        return False
    expected = model_scope(provider, endpoint or "")
    if normalize_provider(scope.get("provider")) != expected["provider"]:
        return False
    if expected["provider"] == PROVIDER_GATEWAY:
        # Gateway capability is never trusted without an exact endpoint match.
        if endpoint is None:
            return False
        return _normalize_endpoint(scope.get("endpoint")) == expected["endpoint"]
    return True


def settings_ai_enabled(provider: Any, endpoint: Any = "", api_key: Any = "") -> bool:
    """Whether the global Settings AI is usable."""
    p = normalize_provider(provider)
    key = str(api_key or "").strip()
    if p == PROVIDER_GOOGLE:
        return bool(key)
    if p == PROVIDER_GATEWAY:
        return bool(key) and bool(str(endpoint or "").strip())
    return False


def _flag_enabled(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "supported"}
    return bool(value)


def _listify(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(x) for x in value]
    return []


def _raw_row(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("_raw_meta")
    return raw if isinstance(raw, dict) else row


def explicit_vision_status(row: Any) -> str:
    """Capability from raw provider metadata or an actual test, never model name."""
    if not isinstance(row, dict):
        return VISION_UNKNOWN
    verified = row.get("vision_verified")
    if verified is True or str(verified).lower() in ("true", "1", "yes"):
        return VISION_VERIFIED
    checked = row.get("vision_checked")
    if checked is True or str(checked).lower() in ("true", "1", "yes"):
        return VISION_TEXT_ONLY

    raw = _raw_row(row)
    caps = {x.lower() for x in _listify(raw.get("capabilities"))}
    if caps & {"vision", "image", "images", "multimodal"}:
        return VISION_VERIFIED
    for key in ("modalities", "input_modalities", "supported_modalities", "capability", "features"):
        values = {str(x).lower() for x in _listify(raw.get(key))}
        if values & {"image", "images", "vision", "multimodal"}:
            return VISION_VERIFIED
    for flag in ("vision", "supports_vision", "vision_capable"):
        if _flag_enabled(raw.get(flag)):
            return VISION_VERIFIED
    if caps or any(raw.get(key) for key in (
        "modalities", "input_modalities", "supported_modalities", "features"
    )):
        return VISION_TEXT_ONLY
    return VISION_UNKNOWN


def can_send_vision(provider: str, model_id: Any, models_cache: Any = None,
                    endpoint: Any = None) -> bool:
    """Only allow image routing for a verified model in the active cache scope."""
    p = normalize_provider(provider)
    if p == PROVIDER_NONE:
        return False
    wanted = str(model_id or "").strip()
    if not wanted:
        return False
    for row in (models_cache or []):
        if not isinstance(row, dict) or not model_matches_scope(row, p, endpoint):
            continue
        mid = str(row.get("id") or row.get("name") or row.get("model") or "").strip().split("/")[-1]
        if mid == wanted and explicit_vision_status(row) == VISION_VERIFIED:
            return True
    return False


def model_picker_label(provider: str, row: Any, *, recommended: bool = True,
                       endpoint: Any = None) -> str:
    """Label current-scope model capability in Settings."""
    if not model_matches_scope(row, provider, endpoint):
        return "Kemampuan visual belum diketahui"
    status = explicit_vision_status(row)
    if status == VISION_VERIFIED:
        return "⭐ Direkomendasikan · Teks + gambar"
    if status == VISION_TEXT_ONLY:
        return "Teks saja"
    return "Kemampuan visual belum diketahui"


def effective_vision_model(config: dict[str, Any]) -> str:
    return str(config.get("ai_vision_model") or config.get("ai_model") or "").strip()


def provider_display(provider: Any) -> str:
    p = normalize_provider(provider)
    return {
        PROVIDER_NONE: "Tidak digunakan",
        PROVIDER_GATEWAY: "Gateway OpenAI-compatible",
        PROVIDER_GOOGLE: "Google AI Studio / Gemini",
    }.get(p, p)
