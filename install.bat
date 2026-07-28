@echo off
:: 1-Click Installer for Study Notes Automation
:: Run from Google Drive or local directory on any Windows machine

echo ====================================================================
echo  Study Notes Automation - 1-Click Installation Launcher
echo ====================================================================

powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Process powershell -ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"%~dp0scripts\win-install-setup.ps1\"' -Verb RunAs"

echo.
echo Setup script initiated in elevated window.
pause
