@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ============================================
echo   txt-to-epub — Setup
echo ============================================
echo.

:: Check Python
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Please install Python 3.9+ from https://python.org
    echo         Make sure to check "Add Python to PATH" during installation.
    pause
    exit /b 1
)
echo [OK] Python found

:: Install dependencies
echo.
echo [*] Installing Python dependencies...
pip install -r "%~dp0requirements.txt" --quiet
if errorlevel 1 (
    echo [ERROR] pip install failed. Check your network or run manually:
    echo         pip install -r requirements.txt
    pause
    exit /b 1
)
echo [OK] Dependencies installed

:: Check Edge
set EDGE_FOUND=0
if exist "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" set EDGE_FOUND=1
if exist "C:\Program Files\Microsoft\Edge\Application\msedge.exe" set EDGE_FOUND=1
if %EDGE_FOUND%==1 (
    echo [OK] Microsoft Edge found
) else (
    echo [WARN] Microsoft Edge not found at standard paths
    echo       syosetu/wenku8/lightnovel CDP fetchers need Edge
    echo       Set edge_path in config.json if Edge is installed elsewhere
)

:: Check Dart (optional, only needed if exe is missing)
set DART_FOUND=0
dart --version >nul 2>&1
if not errorlevel 1 (
    echo [OK] Dart SDK found (optional, for bridge recompilation)
)

:: Setup config
if not exist "%~dp0config.json" (
    echo.
    echo [*] Creating config.json from template...
    copy "%~dp0config.example.json" "%~dp0config.json" >nul
    echo [INFO] Edit config.json and add your lightnovel.app refresh_token
    echo       Get it from: DevTools - Application - Local Storage - lightnovel.app
    echo       Key: sb-yywiuxedvyfxdpznoyqy-auth-token
) else (
    echo [OK] config.json exists
)

echo.
echo ============================================
echo   Setup complete!
echo ============================================
echo.
echo Usage:
echo   python ebook.py lightnovel --bid 17028 --all
echo   python ebook.py syosetu -u "https://syosetu.org/novel/XXXXX/"
echo   python ebook.py wenku8 -u "https://www.wenku8.net/novel/X/XXX/index.htm"
echo   python ebook.py pack "novel_dir" --author "Author Name"
echo   python ebook.py convert "novel.txt" -o "novel.epub" --title "Title"
echo.
echo For lightnovel.app: set refresh_token in config.json first!
echo.
pause
