@echo off
setlocal enabledelayedexpansion
title IISER Bhopal - Exam Scanner & AI Grading Hub

echo ==============================================================================
echo   IISER Bhopal - 100%% Laptop-Hosted Exam Scanner & AI Grading Hub
echo ==============================================================================
echo.

cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [INFO] Virtual environment not found at .\venv.
    echo [INFO] Creating virtual environment and installing requirements...
    python -m venv venv
    if errorlevel 1 (
        echo [ERROR] Could not create virtual environment. Ensure Python 3.10+ is installed and on PATH.
        pause
        exit /b 1
    )
    echo [INFO] Installing dependencies from requirements.txt...
    ".\venv\Scripts\pip.exe" install -r requirements.txt
    if errorlevel 1 (
        echo [ERROR] Failed to install dependencies.
        pause
        exit /b 1
    )
    echo [SUCCESS] Virtual environment initialized successfully!
    echo.
)

echo Starting Hub on http://localhost:8000 and opening browser...
echo.
".\venv\Scripts\python.exe" local_hub.py --port 8000 --open-browser %*
if errorlevel 1 (
    echo.
    echo [ERROR] Server stopped with error code %errorlevel%.
    pause
)
