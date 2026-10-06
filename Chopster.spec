# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir build for the Chopster package layout in this folder."""
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules


# SPECPATH is supplied by PyInstaller. The spec and main.py live in the
# Chopster package root; imports use the parent directory as their package root.
ROOT = Path(SPECPATH).resolve()
PARENT = ROOT.parent


def data_file(source: str, destination: str):
    path = ROOT / source
    if not path.exists():
        raise FileNotFoundError(f"Required build asset is missing: {path}")
    return str(path), destination


datas = [
    data_file("resources", "chopster/resources"),
    data_file("VERSION.txt", "chopster"),
    data_file("auto_clip_studio/engine/fonts", "chopster/auto_clip_studio/engine/fonts"),
    data_file("auto_clip_studio/engine/cascades", "chopster/auto_clip_studio/engine/cascades"),
    data_file(
        "auto_clip_studio/engine/haarcascade_frontalface_default.xml",
        "chopster/auto_clip_studio/engine",
    ),
    data_file("auto_clip_studio/web_dist", "chopster/auto_clip_studio/web_dist"),
    data_file("auto_clip_studio/web_source", "chopster/auto_clip_studio/web_source"),
]

hiddenimports = [
    "yt_dlp",
    "PIL",
    "av",
    "ctranslate2",
    "faster_whisper",
    "cv2",
    "chopster.ai.orchestrator",
    "chopster.ai.local_provider",
    "chopster.ai.embedded_gemini",
    "fastapi",
    "uvicorn",
    "python_multipart",
    "google.genai",
    "youtube_transcript_api",
    "dotenv",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
]
hiddenimports += collect_submodules("yt_dlp")
hiddenimports += collect_submodules("chopster.auto_clip_studio.engine")
# ASGI stack lazy imports. uvicorn resolves several classes by string at
# runtime (e.g. "uvicorn.logging.DefaultFormatter"), so every uvicorn submodule
# must be bundled or the embedded Auto Clip Studio backend fails to start with
# "unable to configure formatter 'default'" inside the frozen EXE.
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("fastapi")
hiddenimports += collect_submodules("starlette")
hiddenimports += collect_submodules("python_multipart")

try:
    datas += collect_data_files("cv2")
except Exception as exc:
    raise RuntimeError("Could not collect required OpenCV data files") from exc

binaries = []
for package in ("faster_whisper", "ctranslate2", "av", "curl_cffi"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(PARENT), str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=list(dict.fromkeys(hiddenimports)),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Chopster",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # Leave native Python/Qt/CTranslate2 libraries uncompressed for safer
    # Windows DLL loading.
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ROOT / "resources" / "icons" / "app.ico"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Chopster",
)