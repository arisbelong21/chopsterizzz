@echo off
setlocal
cd /d "%~dp0"
set "VERSION=unknown"
if exist "VERSION.txt" set /p VERSION=<"VERSION.txt"
set "QT_LOGGING_RULES=qt.multimedia.ffmpeg=false;qt.multimedia.ffmpeg.*=false"
set "QT_FFMPEG_DEBUG=0"
set "QT_MEDIA_BACKEND=ffmpeg"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"
set "PYTHONWARNINGS=ignore::UserWarning:huggingface_hub.file_download"
echo ========================================
echo   Chopster
echo ========================================
if not exist ".venv-source\Scripts\python.exe" (
  echo [ERROR] Environment source belum terpasang.
  echo Jalankan install_dependencies.bat terlebih dahulu, lalu coba lagi.
  pause
  exit /b 1
)
".venv-source\Scripts\python.exe" main.py
set "RC=%errorlevel%"
if not "%RC%"=="0" (
  echo.
  echo [ERROR] Chopster berhenti dengan kode %RC%.
  echo Crash log: %APPDATA%\ChopsterByAris\logs\crash.log
  echo Jika file belum ada, error mungkin terjadi sebelum crash handler aktif.
  pause
)
endlocal
