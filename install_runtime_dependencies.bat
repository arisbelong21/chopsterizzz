@echo off
setlocal EnableExtensions
cd /d "%~dp0"
set "NOPAUSE="
if /i "%~1"=="/nopause" set "NOPAUSE=1"
echo ========================================
echo   Chopster - Dependency Setup
echo ========================================
set "NEED_FFMPEG=0"
set "NEED_DENO=0"
set "NEED_VC=0"

if exist "%~dp0ffmpeg.exe" (set "HAVE_FFMPEG=1") else (where ffmpeg >nul 2>&1 && set "HAVE_FFMPEG=1")
if exist "%~dp0ffprobe.exe" (set "HAVE_FFPROBE=1") else (where ffprobe >nul 2>&1 && set "HAVE_FFPROBE=1")
if not defined HAVE_FFMPEG set "NEED_FFMPEG=1"
if not defined HAVE_FFPROBE set "NEED_FFMPEG=1"

if exist "%~dp0deno.exe" (
  set "DENO_CMD=%~dp0deno.exe"
) else (
  for /f "delims=" %%D in ('where deno 2^>nul') do if not defined DENO_CMD set "DENO_CMD=%%D"
)
if defined DENO_CMD (
  powershell -NoProfile -Command "$line = (& '%DENO_CMD%' --version 2>$null | Select-Object -First 1); if ($line -match '^deno\s+(\d+)\.(\d+)\.(\d+)') { if ([int]$matches[1] -gt 2 -or ([int]$matches[1] -eq 2 -and [int]$matches[2] -ge 3)) { exit 0 } }; exit 1" >nul 2>&1
  if errorlevel 1 set "DENO_CMD="
)
if not defined DENO_CMD (
  where node >nul 2>&1
  if not errorlevel 1 node -e "process.exit(Number(process.versions.node.split('.')[0]) >= 22 ? 0 : 1)" >nul 2>&1
  if errorlevel 1 set "NEED_DENO=1"
)

reg query "HKLM\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" /v Installed 2>nul | findstr /i "0x1" >nul
if errorlevel 1 set "NEED_VC=1"

if "%NEED_FFMPEG%%NEED_DENO%%NEED_VC%"=="000" goto :verify
where winget >nul 2>&1
if errorlevel 1 (
  echo [ERROR] WinGet tidak tersedia. Pasang App Installer/WinGet atau pasang dependency secara manual.
  goto :fail
)
if "%NEED_FFMPEG%"=="1" (
  echo [INFO] Memasang FFmpeg/FFprobe...
  winget install --exact --id Gyan.FFmpeg --accept-source-agreements --accept-package-agreements
  if errorlevel 1 echo [WARN] Pemasangan FFmpeg melalui WinGet gagal; periksa pesan WinGet.
)
if "%NEED_DENO%"=="1" (
  echo [INFO] Memasang Deno untuk ekstraksi YouTube...
  winget install --exact --id DenoLand.Deno --scope user --accept-source-agreements --accept-package-agreements
  if errorlevel 1 echo [WARN] Pemasangan Deno melalui WinGet gagal; Node.js 22+ juga dapat dipakai.
)
if "%NEED_VC%"=="1" (
  echo [INFO] Memasang Microsoft Visual C++ Runtime x64...
  winget install --exact --id Microsoft.VCRedist.2015+.x64 --accept-source-agreements --accept-package-agreements
  if errorlevel 1 echo [WARN] Pemasangan VC++ Runtime melalui WinGet gagal.
)

:verify
set "VERIFY_FAIL=0"
if not exist "%~dp0ffmpeg.exe" where ffmpeg >nul 2>&1
if errorlevel 1 if not exist "%~dp0ffmpeg.exe" (
  echo [WARN] FFmpeg belum terdeteksi di PATH. Buka ulang sesi Windows lalu jalankan setup lagi.
  set "VERIFY_FAIL=1"
)
if not exist "%~dp0ffprobe.exe" where ffprobe >nul 2>&1
if errorlevel 1 if not exist "%~dp0ffprobe.exe" (
  echo [WARN] FFprobe belum terdeteksi di PATH. Buka ulang sesi Windows lalu jalankan setup lagi.
  set "VERIFY_FAIL=1"
)
if "%VERIFY_FAIL%"=="1" goto :fail

echo [OK] Dependensi eksternal terpasang/tersedia.
echo Jika baru memasang runtime, tutup lalu buka kembali Chopster.
echo YouTube mungkin memerlukan cookie login; setup tidak melewati batasan akun/platform.
if not defined NOPAUSE pause
exit /b 0

:fail
echo.
echo [ERROR] Setup belum lengkap. Periksa pesan di atas dan jalankan setup kembali.
if not defined NOPAUSE pause
exit /b 1
