"""Typed application configuration — JSON backed, with defaults."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from chopster.app.paths import app_dir, user_data_dir


# ------------------------------------------------------------------ defaults

DEFAULTS: dict[str, Any] = {
    # general
    "theme": "dark",  # dark | light
    "language": "id",
    "window_width": 1280,
    "window_height": 860,
    "last_workspace": "dashboard",
    # download
    "out_dir": str(Path.home() / "Downloads"),
    "quality": "Terbaik yang tersedia",
    "output_format": "MP4 (Video + Audio)",
    "audio_language": "Auto — Indonesia jika tersedia",
    "h264": True,
    "download_playlist": False,
    "playlist_items": "",
    "parallel": 2,
    "speed_limit": "Tidak terbatas",
    "filename_template": "[Platform] Judul",
    "subfolders": True,
    "subtitles": False,
    "subtitle_langs": "id,en",
    "thumbnail": False,
    "cookies_file": "",
    "browser_cookie_source": "Auto",  # Auto | Chrome | Edge | Firefox | Brave | Chromium | None
    "proxy": "",
    "ask_duplicates": True,
    "browser_bridge": True,
    "browser_bridge_port": 18421,
    # clipper
    "clip_default_duration": 30,
    "clip_overlap": 0,
    "clip_aspect": "9:16",
    "clip_quality": "balanced",  # fast | balanced | high
    "clip_output_dir": "",
    "clip_parallel": 1,
    "clip_burn_subtitle": False,
    "clip_watermark_position": "Bottom right",
    "clip_watermark_opacity": 0.75,
    "clip_watermark_font": 28,
    # transcription
    "transcribe_model": "tiny",
    "transcribe_model_dir": "",
    "transcribe_device": "auto",  # auto | cpu | cuda
    "transcribe_cuda_preflight": True,
    "transcribe_language": "auto",
    "word_timestamps": False,
    # ai
    "ai_provider": "none",  # optional user AI; embedded Gemini is always separate
    "ai_endpoint": "",
    "ai_model": "",  # optional user AI model
    "ai_vision_model": "",  # optional user AI Vision model
    "ai_vision_preference": "",
    "ai_local_enabled": True,
    "ai_local_vision": True,
    "ai_api_key": "",
    "ai_timeout": 60,
    "ai_profiles": [],
    "ai_models_cache": [],
    "ai_auto_model": True,
    "ai_auto_visual": True,
    "ai_last_connection": "",
    "ai_temperature": 0.2,
    "ai_broll_enabled": True,
    "ai_voiceover_enabled": False,
    "ai_failover_local": True,
    "ai_embedded_gemini_enabled": True,
    "ai_failover_cooldown": 60,
    "ai_failover_timeout": 15,
    "config_schema_version": 9,
    "ai_settings_user_configured": False,
    # Auto Clip Studio (isolated from Content Clipper AI)
    "acs_last_source": "",
    "acs_gemini_key": "",
    "acs_proxy": "",
    "acs_supadata_keys": "",
    "acs_cookies_file": "",
    # editor / production
    "subtitle_preset": "Bold creator",
    "subtitle_animation": "pop",
    "subtitle_max_words": 8,
    "reframe_mode": "Smart",
    "reframe_smoothing": 0.75,
    "reframe_focus": "auto",
    "watermark_enabled": False,
    "watermark_type": "text",
    "watermark_text": "",
    "watermark_image": "",
    "watermark_scale": 0.22,  # image watermark width as fraction of output width
    "watermark_image_scale_percent": 22,
    "watermark_opacity": 0.75,
    "watermark_position": "Bottom right",
    "audio_enhance": False,
    "audio_preset": "Voice Clear",
    "export_format": "MP4",
    "export_fps": 30,
    "export_parallel": 1,
    "analytics_enabled": True,
    "publish_platform": "YouTube Shorts",
    # storage
    "project_dir": "",
    "cache_dir": "",
    "temp_dir": "",
    # tray
    "minimize_to_tray": True,
}

# Map filename templates
FILENAME_TEMPLATES: dict[str, str] = {
    "[Platform] Judul": "[%(extractor)s] %(title)s.%(ext)s",
    "Judul - tanggal": "%(title)s - %(upload_date>%Y-%m-%d|unknown)s.%(ext)s",
    "Judul": "%(title)s.%(ext)s",
    "Klasik (extractor_uploader_id)": "%(extractor)s_%(uploader)s_%(id)s.%(ext)s",
}

QUALITIES: dict[str, int | None] = {
    "Terbaik yang tersedia": None,
    "Maks 4K (2160p)": 2160,
    "Maks 1440p": 1440,
    "Maks 1080p": 1080,
    "Maks 720p": 720,
    "Maks 540p": 540,
}

FORMATS = ("MP4 (Video + Audio)", "MP3 (Audio saja)", "M4A (Audio saja)")

SPEED_PRESETS: dict[str, int] = {
    "Tidak terbatas": 0,
    "512 KB/s": 512 * 1024,
    "1 MB/s": 1024 * 1024,
    "2 MB/s": 2 * 1024 * 1024,
    "5 MB/s": 5 * 1024 * 1024,
}


class Configuration:
    """JSON-backed configuration with migration from legacy settings.json."""

    _SECRET_KEYS = ("ai_api_key", "acs_gemini_key")

    def __init__(self, data_dir: str | Path | None = None) -> None:
        self._data_dir = Path(data_dir) if data_dir is not None else user_data_dir()
        self._path = self._data_dir / "settings.json"
        self._legacy = app_dir() / "settings.json"
        self._data: dict[str, Any] = {}
        from chopster.app.secret_store import WindowsSecretStore
        self._secret_store = WindowsSecretStore(self._data_dir)
        self._secret_store_error = False
        self.load()

    # -- load / save ------------------------------------------------

    def load(self) -> None:
        data: dict[str, Any] = dict(DEFAULTS)
        loaded_schema_version = 0
        loaded_from_legacy = False
        # migrate legacy file if new location missing
        if not self._path.exists() and self._legacy.exists():
            loaded_from_legacy = True
            try:
                with open(self._legacy, "r", encoding="utf-8") as fh:
                    legacy = json.load(fh)
                if isinstance(legacy, dict):
                    loaded_schema_version = int(legacy.get("config_schema_version") or 0)
                    data.update({k: v for k, v in legacy.items() if k in DEFAULTS})
            except Exception:
                pass
        elif self._path.exists():
            try:
                with open(self._path, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    loaded_schema_version = int(loaded.get("config_schema_version") or 0)
                    data.update({k: v for k, v in loaded.items() if k in DEFAULTS})
            except Exception:
                pass
        # Migrations must never discard user-entered AI credentials. The old
        # schema reset removed valid gateway keys, so schema upgrades now keep
        # all recognized AI settings and only advance the schema marker.
        try:
            version = int(loaded_schema_version or 0)
        except Exception:
            version = 0
        if version < 7:
            data["config_schema_version"] = 7
        data["ai_embedded_gemini_enabled"] = bool(data.get("ai_embedded_gemini_enabled", True))
        data["ai_settings_user_configured"] = bool(data.get("ai_settings_user_configured", False))

        # Secure-store values take precedence. Plaintext values from older
        # settings.json files are migrated to DPAPI on Windows before the JSON
        # copy is scrubbed; if DPAPI fails, leave the old file intact.
        try:
            secured = self._secret_store.load()
        except Exception:
            secured = {}
            self._secret_store_error = True
        needs_secret_migration = False
        for key in self._SECRET_KEYS:
            secure_value = secured.get(key)
            legacy_value = data.get(key)
            if secure_value:
                data[key] = secure_value
            elif legacy_value and self._secret_store.available:
                needs_secret_migration = True

        # v8.5.4: isolate the Chopster browser bridge from legacy/other downloader tools.
        # Older builds used 17321+, so a stale persisted port could make the extension
        # send links to another local application that happened to answer first.
        # The extension only probes 18421-18425; force the app onto that same range.
        _extension_ports = {18421, 18422, 18423, 18424, 18425}
        try:
            current_bridge_port = int(data.get("browser_bridge_port") or 0)
        except Exception:
            current_bridge_port = 0
        if version < 8 or current_bridge_port not in _extension_ports:
            data["browser_bridge_port"] = 18421
            if version >= 7:
                data["config_schema_version"] = 8
        # v8.5.x: explicit global AI provider selection. Map legacy provider
        # labels onto the two supported adapters without touching the user's
        # saved endpoint/key, so previously-entered gateway credentials keep
        # working.
        try:
            from chopster.ai.config_helpers import normalize_provider
            prov = str(data.get("ai_provider") or "none")
            data["ai_provider"] = normalize_provider(prov)
        except Exception:
            pass
        # v9 stores user API secrets through Windows DPAPI and scopes cached
        # visual capabilities to their provider/endpoint.
        data["config_schema_version"] = 9
        self._data = data
        if needs_secret_migration and not self._secret_store_error:
            saved = self.save()
            if saved and loaded_from_legacy and self._secret_store.available:
                self._scrub_legacy_secrets()

    def _scrub_legacy_secrets(self) -> None:
        """Remove plaintext secrets from the former settings.json after DPAPI save."""
        try:
            with open(self._legacy, "r", encoding="utf-8") as fh:
                legacy = json.load(fh)
            if not isinstance(legacy, dict):
                return
            changed = False
            for key in self._SECRET_KEYS:
                if key in legacy:
                    legacy.pop(key, None)
                    changed = True
            if changed:
                with open(self._legacy, "w", encoding="utf-8") as fh:
                    json.dump(legacy, fh, ensure_ascii=False, indent=2)
        except Exception:
            # The new secure store has already been written; leave the old file
            # untouched if the installation directory is not writable.
            return

    def save(self) -> bool:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            persisted = dict(self._data)
            if self._secret_store.available:
                if self._secret_store_error:
                    # Fail closed: do not overwrite a legacy settings file if a
                    # prior encrypted store could not be decrypted.
                    return False
                self._secret_store.save({key: self._data.get(key, "") for key in self._SECRET_KEYS})
            elif any(str(self._data.get(key) or "").strip() for key in self._SECRET_KEYS):
                # Never add new plaintext API keys to JSON outside Windows DPAPI.
                # Preserve any already-existing file untouched on unsupported OSes.
                return False
            for key in self._SECRET_KEYS:
                persisted.pop(key, None)
            with open(self._path, "w", encoding="utf-8") as fh:
                json.dump(persisted, fh, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    # -- accessors --------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default if default is not None else DEFAULTS.get(key))

    def set(self, key: str, value: Any) -> bool:
        self._data[key] = value
        return self.save()

    def update(self, values: dict[str, Any]) -> bool:
        self._data.update(values)
        return self.save()

    def as_dict(self) -> dict[str, Any]:
        return dict(self._data)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)
