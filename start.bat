@echo off
rem Double-click to run. Sets up its own Python environment the first time, and repairs or updates it when needed.
setlocal
rem Unlike cd, pushd also works in a network folder (\\server\share): it maps a temporary drive letter.
pushd "%~dp0" || (
    echo Could not open the folder "%~dp0".
    pause
    exit /b 1
)

rem Set up again when the environment is missing or broken, or requirements.txt changed since the last install that finished.
set "STAMP=.venv\installed-requirements.txt"
fc /b requirements.txt "%STAMP%" >nul 2>nul || goto setup
".venv\Scripts\python.exe" -c "import streamlit, yt_dlp" >nul 2>nul || goto setup
goto launch

:setup
echo Setting up, this takes a few minutes the first time...
del "%STAMP%" >nul 2>nul
rem An environment whose Python was uninstalled, or is older than 3.11, cannot be repaired, only rebuilt.
".venv\Scripts\python.exe" -c "import sys, pip; sys.exit(sys.version_info < (3, 11))" >nul 2>nul && goto install
call :find_python || goto no_python
call %BASE_PYTHON% -m venv --clear .venv || goto failed

:install
".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto failed
copy /y requirements.txt "%STAMP%" >nul

:launch
where ffmpeg >nul 2>nul || echo ffmpeg was not found. Downloads will be limited to about 360p. To install it: winget install Gyan.FFmpeg

rem Only this computer can connect. Set VIDEOCAP_ADDRESS=0.0.0.0 to allow other devices on the network.
if not defined VIDEOCAP_ADDRESS set "VIDEOCAP_ADDRESS=localhost"
if not defined VIDEOCAP_HEADLESS set "VIDEOCAP_HEADLESS=false"
rem Through the app's launcher, so subscriptions and unfinished downloads resume before anyone opens the page.
".venv\Scripts\python.exe" -m videocap --server.address %VIDEOCAP_ADDRESS% --server.headless %VIDEOCAP_HEADLESS%
popd
pause
exit /b

:no_python
echo This app needs Python 3.11 or newer. Install it from https://www.python.org, then run start.bat again.
goto stop

:failed
echo.
echo Setup did not finish. The messages above say why, for example no internet connection.
echo After fixing that, run start.bat again: setup picks up where it stopped.

:stop
popd
pause
exit /b 1

:find_python
rem py -3 picks the newest installed Python. call lets these names be .bat or .cmd files too.
for %%P in ("py -3" "python") do (
    call %%~P -c "import sys; sys.exit(sys.version_info < (3, 11))" >nul 2>nul && (
        set "BASE_PYTHON=%%~P"
        exit /b 0
    )
)
exit /b 1
