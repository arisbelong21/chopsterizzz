@echo off
setlocal EnableExtensions

REM Run the actual build in a child call and persist all output. This leaves a
REM usable log even when Explorer closes its temporary console after the script.
if /i "%CHOPSTER_BUILD_EXE_CHILD%"=="1" goto :build_main
set "CHOPSTER_BUILD_EXE_CHILD=1"
set "BUILD_LOG=%~dp0build_exe.log"
>"%BUILD_LOG%" echo Chopster Windows build started %DATE% %TIME%
echo Build sedang berjalan. Log lengkap: "%BUILD_LOG%"
call "%~f0" >>"%BUILD_LOG%" 2>&1
set "BUILD_RC=%ERRORLEVEL%"
>>"%BUILD_LOG%" echo Build exit code: %BUILD_RC%
echo.
echo ===== Ringkasan output build =====
type "%BUILD_LOG%"
echo.
echo Log tersimpan di: "%BUILD_LOG%"
pause
endlocal & exit /b %BUILD_RC%

:build_main
cd /d "%~dp0"

echo ========================================
echo  Chopster - Reproducible EXE build
echo ========================================

where py >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Python Launcher 'py' tidak ditemukan.
  echo Instal Python 3.12 64-bit dari python.org, lalu jalankan ulang build_exe.bat.
  goto :fail
)

REM Target utama build: Python 3.12 64-bit. Deteksi TIDAK memakai exit code launcher
REM (pada sebagian mesin, py -3.12 mengembalikan sukses walau runtime tidak ada).
REM Dipakai marker file: Python asli yang berjalan akan menuliskan sys.executable-nya.
set "PROBE312=%~dp0.build_py312_probe.txt"
set "PROBEDEF=%~dp0.build_py_default_probe.txt"
if exist "%PROBE312%" del "%PROBE312%"
if exist "%PROBEDEF%" del "%PROBEDEF%"

py -3.12 -c "import sys;open(r'%PROBE312%','w').write(sys.executable)" >nul 2>&1
if not exist "%PROBE312%" (
  echo [INFO] Python 3.12 belum terdeteksi. Mencoba pemasangan otomatis: py install 3.12 ...
  py install 3.12 >nul 2>&1
  py -3.12 -c "import sys;open(r'%PROBE312%','w').write(sys.executable)" >nul 2>&1
  if not exist "%PROBE312%" (
    echo [WARN] Python 3.12 tidak dapat dipasang otomatis di PC ini.
    echo        Build akan memakai Python default yang terpasang sebagai pengganti.
  ) else (
    echo [OK] Python 3.12 berhasil dipasang otomatis.
  )
)

REM Fallback: cari interpreter default (python atau py).
if not exist "%PROBE312%" (
  python -c "import sys;open(r'%PROBEDEF%','w').write(sys.executable)" >nul 2>&1
  if not exist "%PROBEDEF%" (
    py -c "import sys;open(r'%PROBEDEF%','w').write(sys.executable)" >nul 2>&1
  )
)

REM Tentukan interpreter build dari hasil probe.
set "BUILD_INTERP="
if exist "%PROBE312%" for /f "usebackq delims=" %%P in ("%PROBE312%") do if not defined BUILD_INTERP set "BUILD_INTERP=%%P"
if not defined BUILD_INTERP if exist "%PROBEDEF%" for /f "usebackq delims=" %%P in ("%PROBEDEF%") do if not defined BUILD_INTERP set "BUILD_INTERP=%%P"
if exist "%PROBE312%" del "%PROBE312%"
if exist "%PROBEDEF%" del "%PROBEDEF%"
if not defined BUILD_INTERP goto :no_python
echo [INFO] Interpreter build: %BUILD_INTERP%
"%BUILD_INTERP%" -c "import struct,sys; raise SystemExit(0 if struct.calcsize('P')==8 else 1)"
if errorlevel 1 (
  echo [ERROR] Hanya ditemukan Python 32-bit. Build memerlukan Python 64-bit.
  echo Instal Python 3.12 64-bit dari python.org, lalu jalankan ulang build_exe.bat.
  goto :fail
)

if not exist ".venv-build\Scripts\python.exe" (
  echo [INFO] Membuat lingkungan virtual build...
  "%BUILD_INTERP%" -m venv ".venv-build"
  if errorlevel 1 (
    echo [ERROR] Gagal membuat .venv-build.
    goto :fail
  )
)

set "PYTHON=.venv-build\Scripts\python.exe"
"%PYTHON%" -c "import struct,sys; print('Build Python:',sys.version.split()[0], '64-bit' if struct.calcsize('P')==8 else '32-bit')"
if errorlevel 1 (
  echo [ERROR] .venv-build tidak dapat dijalankan.
  echo Hapus folder .venv-build, lalu jalankan ulang build_exe.bat.
  goto :fail
)

echo [INFO] Memastikan dependensi build...
"%PYTHON%" -m pip install -r requirements.txt
if errorlevel 1 (
  echo [ERROR] Instalasi dependensi build gagal.
  goto :fail
)

if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [INFO] Membangun EXE onedir...
"%PYTHON%" -m PyInstaller --noconfirm --clean "Chopster.spec"
if errorlevel 1 (
  echo [ERROR] Build gagal. Periksa pesan PyInstaller di atas.
  goto :fail
)

set "DIST_DIR=dist\Chopster"
if not exist "%DIST_DIR%\Chopster.exe" (
  echo [ERROR] EXE tidak ditemukan di "%DIST_DIR%".
  goto :fail
)

if exist "%DIST_DIR%\_internal\chopster\auto_clip_studio\web_dist\index.html" (
  set "WEB_DIST=%DIST_DIR%\_internal\chopster\auto_clip_studio\web_dist"
) else if exist "%DIST_DIR%\chopster\auto_clip_studio\web_dist\index.html" (
  set "WEB_DIST=%DIST_DIR%\chopster\auto_clip_studio\web_dist"
) else (
  echo [ERROR] Bundle Auto Clip Studio tidak ditemukan pada hasil build.
  goto :fail
)
if not exist "%WEB_DIST%\assets" (
  echo [ERROR] Aset frontend Auto Clip Studio tidak ditemukan.
  goto :fail
)

if exist "%DIST_DIR%\_internal\chopster\auto_clip_studio" (
  set "ACS_DATA=%DIST_DIR%\_internal\chopster\auto_clip_studio"
) else (
  set "ACS_DATA=%DIST_DIR%\chopster\auto_clip_studio"
)
if not exist "%ACS_DATA%\engine\fonts\Anton.ttf" (
  echo [ERROR] Font Auto Clip Studio tidak ditemukan.
  goto :fail
)
if not exist "%ACS_DATA%\engine\cascades\face_detection_yunet.onnx" (
  echo [ERROR] Model deteksi wajah Auto Clip Studio tidak ditemukan.
  goto :fail
)

if not exist "browser_extension\manifest.json" (
  echo [ERROR] Browser extension tidak lengkap: manifest.json tidak ditemukan.
  goto :fail
)
xcopy /E /I /Y "browser_extension" "%DIST_DIR%\browser_extension" >nul
if exist "install_runtime_dependencies.bat" copy /Y "install_runtime_dependencies.bat" "%DIST_DIR%\SETUP_DEPENDENCIES.bat" >nul
if not exist "%DIST_DIR%\browser_extension\manifest.json" (
  echo [ERROR] Browser extension gagal disalin ke folder distribusi.
  goto :fail
)

REM Deno is optional. Never ship a local copy that fails its runtime/version check.
if exist "deno.exe" call :copy_valid_deno

REM Optional FFmpeg helpers are copied beside the app when supplied.
for %%F in (ffmpeg.exe ffprobe.exe) do (
  if exist "%%F" copy /Y "%%F" "%DIST_DIR%\%%F" >nul
)

echo [OK] EXE dan aset utama terverifikasi.
echo.
echo Build selesai. Jalankan:
echo   "%DIST_DIR%\Chopster.exe"
echo.
echo Distribusikan seluruh folder "%DIST_DIR%", termasuk _internal.
echo Jangan jalankan EXE dari folder build: gunakan hanya EXE dari dist.
echo Jika Windows melaporkan DLL belum tersedia, pasang Microsoft Visual C++ 2015-2022 Redistributable x64.
echo Untuk FFmpeg, letakkan ffmpeg.exe dan ffprobe.exe di samping EXE atau tambahkan ke PATH.
endlocal
exit /b 0

:copy_valid_deno
"%PYTHON%" -c "import pathlib,re,subprocess,sys; p=subprocess.run([str(pathlib.Path('deno.exe').resolve()),'--version'],capture_output=True,text=True,timeout=5); m=re.search(r'(?m)^deno\s+(\d+)\.(\d+)\.(\d+)',p.stdout or ''); sys.exit(0 if p.returncode==0 and m and tuple(map(int,m.groups())) >= (2,3,0) else 1)" >nul 2>&1
if errorlevel 1 (
  echo [WARN] deno.exe lokal gagal pemeriksaan atau versi di bawah 2.3; file tidak disalin.
  echo        Runtime YouTube memerlukan Node ^>=22 atau Deno ^>=2.3.
  exit /b 0
)
copy /Y "deno.exe" "%DIST_DIR%\deno.exe" >nul
echo [OK] Deno 2.3+ lolos pemeriksaan dan disertakan.
exit /b 0

:no_python
echo [ERROR] Tidak ada interpreter Python 64-bit yang tersedia untuk membangun.
echo Instal Python 3.12 64-bit dari python.org, lalu jalankan ulang build_exe.bat.
goto :fail

:fail
echo.
echo Build dihentikan. Output lengkap tersimpan dalam build_exe.log.
endlocal
exit /b 1