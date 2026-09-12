@echo off
REM FAT3Tool launcher - double-click this file to run the tool.
REM You can also drag-and-drop a .fat file directly onto this .bat file.

setlocal

where python >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    where py >nul 2>nul
    if %ERRORLEVEL% NEQ 0 (
        echo.
        echo ============================================================
        echo   Python was not found on this computer.
        echo.
        echo   FAT3Tool needs Python 3 to run. It's free and only takes
        echo   a minute to install:
        echo.
        echo       1. Go to https://www.python.org/downloads/
        echo       2. Download and run the installer
        echo       3. IMPORTANT: tick "Add python.exe to PATH" during setup
        echo       4. Run this file again afterwards
        echo ============================================================
        echo.
        pause
        exit /b 1
    ) else (
        py "%~dp0fat3tool.py" %*
        goto :end
    )
)

python "%~dp0fat3tool.py" %*

:end
endlocal
