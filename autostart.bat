@echo off
rem Starts the app hidden each time you sign in to Windows, so subscriptions are checked and scheduled downloads run
rem without anyone opening start.bat. Run start.bat once first, to set the app up.
rem   autostart.bat          adds the app to your Startup folder
rem   autostart.bat remove   takes it out again
setlocal
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT=%STARTUP%\YouTube VideoCap.lnk"
if /i "%~1"=="remove" goto remove
if not "%~1"=="" goto usage

set "APP_DIR=%~dp0"
if not exist "%APP_DIR%.venv\Scripts\pythonw.exe" (
    echo The app is not set up yet. Run start.bat once, then run autostart.bat again.
    goto stop
)
if not exist "%STARTUP%" mkdir "%STARTUP%"
rem pythonw opens no window. Only this computer can connect, whatever VIDEOCAP_ADDRESS says, since nobody sees this copy run.
rem PowerShell reads the paths from the environment, so quotes or brackets in a folder name cannot break the command.
rem The shortcut fills in a variable written between percent signs in its target, which cannot be escaped, so the
rem saved target is read back and a wrong one removed.
rem A target longer than Windows' 260-character path limit is quietly cut by the shortcut object, so it is refused first.
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -Command "$ErrorActionPreference = 'Stop'; $shell = New-Object -ComObject WScript.Shell; $target = $env:APP_DIR + '.venv\Scripts\pythonw.exe'; if ($target.Length -gt 259) { exit 4 }; $s = $shell.CreateShortcut($env:SHORTCUT); $s.TargetPath = $target; $s.Arguments = '-m videocap --server.headless true --server.address localhost'; $s.WorkingDirectory = $env:APP_DIR; $s.Save(); if ($shell.CreateShortcut($env:SHORTCUT).TargetPath -ne $target) { Remove-Item -LiteralPath $env:SHORTCUT; if ($target.Contains('%')) { exit 3 } else { exit 5 } }"
if errorlevel 5 goto changed
if errorlevel 4 goto toolong
if errorlevel 3 goto percent
if errorlevel 1 goto failed
echo Added. From your next sign-in, the app starts hidden in the background at http://localhost:8501
echo While it runs, open that address in your browser instead of running start.bat again.
echo To stop starting it with Windows, run: autostart.bat remove
goto done

:remove
if not exist "%SHORTCUT%" (
    echo Nothing to remove: the app was not set to start with Windows.
    goto done
)
del "%SHORTCUT%"
if exist "%SHORTCUT%" goto failed
echo Removed. The app no longer starts when you sign in to Windows.
echo If it is running now, it keeps running until you sign out or restart the computer.
goto done

:usage
echo Usage: autostart.bat          start the app hidden each time you sign in to Windows
echo        autostart.bat remove   stop starting it with Windows
goto stop

:percent
echo.
echo The folder path "%APP_DIR%" contains %%, which a Windows shortcut reads as a variable,
echo so the app would never start at sign-in. Please rename the folder without "%%" and run autostart.bat again.
pause
exit /b 3

:toolong
echo.
echo The app's folder path "%APP_DIR%" is too long: a Windows shortcut can hold at most 259 characters,
echo so the app would never start at sign-in. Please move the app to a shorter path and run autostart.bat again.
pause
exit /b 4

:changed
echo.
echo Windows saved a different target than the app's path, so the shortcut was removed.
echo Please move the app to a shorter path without special characters and run autostart.bat again.
pause
exit /b 5

:failed
echo.
echo Could not change the Startup folder "%STARTUP%". The messages above say why.
goto stop

:done
pause
exit /b 0

:stop
pause
exit /b 1
