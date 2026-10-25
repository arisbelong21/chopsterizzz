"""DPAPI-protected credential storage for Automation tokens.

Separate file (automation_secrets.dpapi) from Configuration's secrets.dpapi so
the two stores can never overwrite each other. Tokens are never written into
settings.json, job rows, or logs (spec §7).
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from chopster.app.paths import user_data_dir
from chopster.app.secret_store import WindowsSecretStore


class AutomationSecrets:
    def __init__(self, data_dir: str | Path | None = None):
        base = Path(data_dir) if data_dir is not None else user_data_dir()
        self._store = WindowsSecretStore(base, filename="automation_secrets.dpapi")
        self._lock = threading.RLock()

    @property
    def available(self) -> bool:
        return self._store.available

    def _load(self) -> dict[str, str]:
        try:
            return dict(self._store.load())
        except Exception:
            return {}

    def get(self, key: str, default: str = "") -> str:
        with self._lock:
            return str(self._load().get(str(key), default))

    def get_json(self, key: str) -> dict[str, Any]:
        raw = self.get(key, "")
        if not raw:
            return {}
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def set(self, key: str, value: Any) -> bool:
        with self._lock:
            data = self._load()
            data[str(key)] = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            try:
                self._store.save(data)
                return True
            except Exception:
                return False

    def set_json(self, key: str, value: dict[str, Any]) -> bool:
        return self.set(key, json.dumps(value, ensure_ascii=False))

    def delete(self, key: str) -> None:
        with self._lock:
            data = self._load()
            if str(key) in data:
                data.pop(str(key), None)
                try:
                    self._store.save(data)
                except Exception:
                    pass

    def account_key(self, account_id: str) -> str:
        return f"aut_tok_{account_id}"
