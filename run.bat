@echo off
rem run.bat - one-click launcher (Windows). Double-click opens this in a cmd
rem window; closing the window or Ctrl+C stops the server. Keep in lockstep
rem with run.sh.
call "%~dp0.venv\Scripts\activate.bat"
cd /d "%~dp0"
python serve.py
pause
