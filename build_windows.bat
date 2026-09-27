@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo  Building FLP Note Merger C# + Rust Windows app
echo ============================================================

call "%~dp0build_rust_windows.bat"
if errorlevel 1 goto :failed

where dotnet >nul 2>nul
if errorlevel 1 (
    echo ERROR: .NET SDK was not found.
    echo Install the .NET 8 SDK or newer from https://dotnet.microsoft.com/download
    goto :failed
)

if exist "%~dp0release\FLP_Note_Merger" rmdir /s /q "%~dp0release\FLP_Note_Merger"
dotnet publish "%~dp0dotnet\FLPNoteMerger.Gui.csproj" -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true -o "%~dp0release\FLP_Note_Merger"
if errorlevel 1 goto :failed
copy /Y "%~dp0release\flp-note-merger.exe" "%~dp0release\FLP_Note_Merger\flp-note-merger.exe" >nul
if errorlevel 1 goto :failed

echo.
echo Fast build complete:
echo   %~dp0release\FLP_Note_Merger\FLP_Note_Merger.exe
echo.
echo Keep the complete FLP_Note_Merger folder together when copying it.
echo.
pause
exit /b 0

:failed
echo.
echo ERROR: Build failed. Review the messages above.
pause
exit /b 1
