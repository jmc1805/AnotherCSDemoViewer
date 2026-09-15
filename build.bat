@echo off
rem build.bat - convenience wrapper for build.ps1 (the maintained Windows build).
rem Usage: build.bat [-Help]
rem   build.bat        build the parser + overwatch binaries (the whole app)
rem   build.bat -Help  full help
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0build.ps1" %*
exit /b %ERRORLEVEL%
