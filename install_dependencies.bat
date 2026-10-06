@echo off
setlocal EnableExtensions
cd /d "%~dp0"
echo ========================================
echo   Chopster - Install Dependencies
echo ========================================
call "%~dp0install_runtime_dependencies.bat" /nopause
if errorlevel 1 goto :fail

set "PY312=py -3.12"
py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,12) else 1)" >nul 2>&1
if errorlevel 1 (
  where py >nul 2>&1
  if not errorlevel 1 (
    echo [INFO] Mencoba memasang Python 3.12 melalui Python Launcher...
    py install 3.12 >nul 2>&1
    py -3.12 -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3,12) else 1)" >nul 2>&1
    if errorlevel 1 set "PY312="
  ) else (
    set "PY312="
  )
)
if not defined PY312 (
  where winget >nul 2>&1
  if errorlevel 1 (
    echo [ERROR] Python 3.12 tidak ditemukan; pasang Python 3.12 atau jalankan build_exe.bat.
    goto :fail
  )
  echo [INFO] Memasang Python 3.12 melalui WinGet...
  winget install --exact --id Python.Python.3.12 --scope user --accept-source-agreements --accept-package-agreements
  if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" (
    set "PY312_EXE=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  ) else (
    echo [WARN] Python baru terpasang; buka ulang sesi Windows lalu jalankan installer ini lagi.
    goto :fail
  )
)

if not exist ".venv-source\Scripts\python.exe" (
  if defined PY312_EXE (
    "%PY312_EXE%" -m venv ".venv-source"
  ) else (
    py -3.12 -m venv ".venv-source"
  )
)
if errorlevel 1 goto :fail
set "VENV_PY=%~dp0.venv-source\Scripts\python.exe"
"%VENV_PY%" -m pip install --upgrade pip
if errorlevel 1 goto :fail
"%VENV_PY%" -m pip install -r requirements.txt
if errorlevel 1 goto :fail
"%VENV_PY%" -c "import yt_dlp,yt_dlp_ejs,curl_cffi; print('yt-dlp OK:', yt_dlp.version.__version__, '| curl-cffi OK')"
if errorlevel 1 goto :fail

echo.
echo [OK] Dependency source siap. Jalankan run_chopster.bat.
echo Untuk membuat EXE onedir, jalankan build_exe.bat.
pause
endlocal
exit /b 0

:fail
echo.
echo [ERROR] Instalasi dependency belum lengkap. Periksa pesan di atas.
pause
endlocal
exit /b 1
