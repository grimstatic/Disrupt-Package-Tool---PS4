@echo off
REM Optional: build a single FAT3Tool.exe that runs with NO Python install
REM required on other computers. Run this file ONCE, on a Windows PC that
REM already has Python installed. It only needs to be done one time - the
REM resulting FAT3Tool.exe (in the "dist" folder) can then be copied
REM anywhere and shared with people who don't have Python at all.

setlocal

where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo Python is required to BUILD the .exe ^(but not to run the .bat
    echo version - see FAT3Tool.bat instead if you just want to use the tool^).
    pause
    exit /b 1
)

echo Installing PyInstaller (one-time)...
python -m pip install --upgrade pyinstaller

echo.
echo Building FAT3Tool.exe ...
python -m PyInstaller --onefile --name FAT3Tool --console "%~dp0fat3tool.py"

echo.
echo ============================================================
echo Done! Your standalone FAT3Tool.exe is in the "dist" folder.
echo You can copy just that one .exe file anywhere - it does not
echo need Python installed to run.
echo ============================================================
pause
