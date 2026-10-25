@echo off
REM Jalankan Chopster dari source TANPA jendela console hitam.
REM Dipakai saat merekam video demo (console window bikin rekaman kurang rapi).
REM Setara dengan run_chopster.bat, tapi memakai pythonw.exe + start.
setlocal
cd /d "%~dp0"
set "VERSION=unknown"
if exist "VERSION.txt" set /p VERSION=<"VERSION.txt"
set "QT_LOGGING_RULES=qt.multimedia.ffmpeg=false;qt.multimedia.ffmpeg.*=false"
set "QT_FFMPEG_DEBUG=0"
set "QT_MEDIA_BACKEND=ffmpeg"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"
set "PYTHONWARNINGS=ignore::UserWarning:huggingface_hub.file_download"
if not exist ".venv-source\Scripts\pythonw.exe" (
  echo [ERROR] Environment source belum terpasang.
  echo Jalankan install_dependencies.bat terlebih dahulu, lalu coba lagi.
  pause
  exit /b 1
)
start "" ".venv-source\Scripts\pythonw.exe" main.py
endlocal
