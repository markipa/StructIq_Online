@echo off
echo ============================================
echo   StructIQ — Build .exe
echo ============================================

REM Activate virtual environment
call ..\.venv\Scripts\activate.bat

REM Install PyInstaller if not present
pip install pyinstaller --quiet

REM Clean previous build
if exist dist\StructIQ rmdir /s /q dist\StructIQ
if exist build\StructIQ rmdir /s /q build\StructIQ

REM Build
echo Building... (this takes 1-3 minutes)
pyinstaller structiq.spec --noconfirm

echo.
echo ============================================
echo   Done! Find your app in:
echo   dist\StructIQ\StructIQ.exe
echo ============================================
pause
