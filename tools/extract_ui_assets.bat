@echo off
rem extract_ui_assets.bat - convenience wrapper for extract_ui_assets.ps1.
rem   extract_ui_assets.bat               extract every kind + manifest
rem   extract_ui_assets.bat equipment     just re-extract equipment icons
rem   extract_ui_assets.bat -Manifest     re-index existing PNGs (no CS2 files)
rem   extract_ui_assets.bat -List         list the kinds and their VPK subtrees
rem   extract_ui_assets.bat -Help         full flag/config help
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0extract_ui_assets.ps1" %*
exit /b %ERRORLEVEL%
