"""Windows DPAPI-backed storage for user API credentials.

Secrets are encrypted for the current Windows user and kept outside the JSON
settings file. On platforms without Windows DPAPI, Configuration refuses to
persist non-empty API secrets rather than writing them as plaintext.
"""
from __future__ import annotations

import ctypes
import json
import os
import tempfile
from ctypes import wintypes
from pathlib import Path
from typing import Any

_MAGIC = b"CHOPSTER-DPAPI-SECRETS-V1\n"
_ENTROPY = b"ChopsterByAris:user-api-credentials:v1"


class SecretStoreError(RuntimeError):
    """Raised when the encrypted Windows credential store cannot be read/written."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _input_blob(data: bytes):
    buf = ctypes.create_string_buffer(data, max(1, len(data)))
    blob = _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buf


def _dpapi(data: bytes, *, protect: bool) -> bytes:
    if os.name != "nt":
        raise SecretStoreError("Windows DPAPI is available only on Windows.")
    try:
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        source, source_buf = _input_blob(data)
        entropy, entropy_buf = _input_blob(_ENTROPY)
        output = _DataBlob()
        description = wintypes.LPWSTR()
        if protect:
            fn = crypt32.CryptProtectData
            fn.argtypes = [
                ctypes.POINTER(_DataBlob), wintypes.LPCWSTR,
                ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                wintypes.DWORD, ctypes.POINTER(_DataBlob),
            ]
            fn.restype = wintypes.BOOL
            ok = fn(ctypes.byref(source), "Chopster user API credentials",
                    ctypes.byref(entropy), None, None, 0x1, ctypes.byref(output))
        else:
            fn = crypt32.CryptUnprotectData
            fn.argtypes = [
                ctypes.POINTER(_DataBlob), ctypes.POINTER(wintypes.LPWSTR),
                ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                wintypes.DWORD, ctypes.POINTER(_DataBlob),
            ]
            fn.restype = wintypes.BOOL
            ok = fn(ctypes.byref(source), ctypes.byref(description),
                    ctypes.byref(entropy), None, None, 0x1, ctypes.byref(output))
        if not ok:
            raise ctypes.WinError(ctypes.get_last_error())
        return ctypes.string_at(output.pbData, output.cbData)
    except SecretStoreError:
        raise
    except Exception as exc:
        raise SecretStoreError(f"Windows secure credential operation failed: {exc}") from exc
    finally:
        # DPAPI allocates these buffers with LocalAlloc.
        try:
            if output.pbData:
                kernel32.LocalFree(output.pbData)
        except Exception:
            pass
        try:
            if description:
                kernel32.LocalFree(description)
        except Exception:
            pass


class WindowsSecretStore:
    """Small DPAPI encrypted map stored under the current user's app-data folder."""

    def __init__(self, data_dir: str | Path):
        self.path = Path(data_dir) / "secrets.dpapi"

    @property
    def available(self) -> bool:
        return os.name == "nt"

    def load(self) -> dict[str, str]:
        if not self.available or not self.path.exists():
            return {}
        try:
            raw = self.path.read_bytes()
            if not raw.startswith(_MAGIC):
                raise SecretStoreError("The secure credential file has an unsupported format.")
            payload = json.loads(_dpapi(raw[len(_MAGIC):], protect=False).decode("utf-8"))
            if not isinstance(payload, dict):
                raise SecretStoreError("The secure credential file is invalid.")
            return {str(k): str(v or "") for k, v in payload.items()}
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError(f"Could not read Windows secure credentials: {exc}") from exc

    def save(self, values: dict[str, Any]) -> None:
        if not self.available:
            raise SecretStoreError("Windows DPAPI is unavailable on this platform.")
        payload = {str(k): str(v or "") for k, v in values.items() if str(v or "")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not payload:
            self.path.unlink(missing_ok=True)
            return
        clear = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        encrypted = _MAGIC + _dpapi(clear, protect=True)
        fd, tmp_name = tempfile.mkstemp(prefix=".secrets-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(encrypted)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
