@echo off
setlocal
cd /d "%~dp0"

if not exist "chat.py" (
  echo ERROR: chat.py not found in:
  echo   %cd%
  echo.
  echo Make sure you extracted the full zip and are in the project root.
  echo Expected files: chat.py, app\, requirements.txt, local.settings.json
  dir /b
  pause
  exit /b 1
)

python -m pip install -r requirements.txt
python chat.py
pause
