@echo off
rem Posture Puck setup: installs the Python packages, adds the app to Windows
rem startup (hidden, no console window) and starts it now.
cd /d "%~dp0"

python --version >nul 2>&1 || (
  echo Python wasn't found. Install it from python.org and tick "Add python.exe to PATH".
  pause
  exit /b 1
)

echo Installing Python packages...
python -m pip install --upgrade -r requirements.txt || (
  echo Package install failed - see the message above.
  pause
  exit /b 1
)

for /f "delims=" %%i in ('python -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))"') do set "PYW=%%i"

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
> "%STARTUP%\PosturePuck.vbs" echo CreateObject("WScript.Shell").Run """%PYW%"" ""%~dp0posture_alert.py""", 0

echo.
echo Added to startup: %STARTUP%\PosturePuck.vbs
echo (delete that file to stop it starting with Windows)
echo.
echo Starting Posture Puck - look for the coloured dot in the system tray.
start "" "%PYW%" "%~dp0posture_alert.py"
pause
