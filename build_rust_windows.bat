@echo off
setlocal

rem Build the Rust merge engine for Windows without MSVC link.exe.
rem Uses the GNU Windows target and MinGW-w64 gcc.

where cargo >nul 2>nul
if errorlevel 1 (
    echo Rust is not installed. Get it from https://rustup.rs and re-run this script.
    exit /b 1
)

where gcc >nul 2>nul
if errorlevel 1 (
    echo MinGW-w64 gcc was not found.
    echo Install MinGW-w64 and add its bin folder to PATH.
    echo This build intentionally uses the GNU target and does not require link.exe.
    exit /b 1
)

rustup target add x86_64-pc-windows-gnu
if errorlevel 1 (
    echo Could not install the Rust GNU Windows target.
    exit /b 1
)

pushd "%~dp0rust" || exit /b 1

echo Building the release binary...
cargo build --release --target x86_64-pc-windows-gnu
if errorlevel 1 (
    echo Build failed.
    popd
    exit /b 1
)

popd

if not exist "%~dp0release" mkdir "%~dp0release"
copy /Y "%~dp0rust\target\x86_64-pc-windows-gnu\release\flp-note-merger.exe" "%~dp0release\flp-note-merger.exe" >nul
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
