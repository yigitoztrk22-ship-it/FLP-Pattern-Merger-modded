@echo off
setlocal

rem Build the Rust merge engine for Windows.
rem Produces release\flp-note-merger.exe (no Python required to run it).

where cargo >nul 2>nul
if errorlevel 1 (
    echo Rust is not installed. Get it from https://rustup.rs and re-run this script.
    exit /b 1
)

pushd "%~dp0rust" || exit /b 1

echo Building the release binary...
cargo build --release
if errorlevel 1 (
    echo Build failed.
    popd
    exit /b 1
)

popd

if not exist "%~dp0release" mkdir "%~dp0release"
copy /Y "%~dp0rust\target\release\flp-note-merger.exe" "%~dp0release\flp-note-merger.exe" >nul
if errorlevel 1 (
    echo Could not copy the built binary.
    exit /b 1
)

echo.
echo Done: release\flp-note-merger.exe
echo.
echo Usage:
echo     release\flp-note-merger.exe "C:\Music\song.flp" "C:\Music\song_merged_notes.flp"
echo.
