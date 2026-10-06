@echo off
setlocal
cd /d "%~dp0"
echo ================================================
echo Chopster - Optional Speaker Diarization Setup
echo ================================================
echo.
echo This installs pyannote.audio. It is optional and can be large.
echo After installation, set HUGGINGFACE_TOKEN or HF_TOKEN in Windows.
echo.
python -m pip install --upgrade pip
if errorlevel 1 goto :fail
python -m pip install -r requirements_speaker_diarization.txt
if errorlevel 1 goto :fail
echo.
echo Speaker diarization package installed.
echo Set your Hugging Face token before using real diarization.
pause
exit /b 0
:fail
echo.
echo Speaker diarization installation failed. The main Chopster app remains usable without it.
pause
exit /b 1
