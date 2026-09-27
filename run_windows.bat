@echo off
setlocal
cd /d "%~dp0"
if not exist "%~dp0release\FLP_Note_Merger\FLP_Note_Merger.exe" (
    echo The C# application is not built yet. Run build_windows.bat first.
    pause
    exit /b 1
)
start "FLP Note Merger" "%~dp0release\FLP_Note_Merger\FLP_Note_Merger.exe"
endlocal
