@echo off
chcp 65001 >nul
title ExcelFlow
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\Start-ExcelFlow-Postgres.ps1" %*
if errorlevel 1 (
    echo.
    echo ExcelFlow startup failed. See the message above and var\logs.
    pause
)
