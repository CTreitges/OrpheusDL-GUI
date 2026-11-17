@echo off
setlocal
rem Update OrpheusDL without setting it up again.
rem 1. Unpack the new release ZIP INTO your old OrpheusDL folder,
rem    the one with OrpheusDL_GUI.exe, config and modules.
rem 2. Close OrpheusDL, then double-click this file.
rem Settings, logins, queue, downloads and extensions stay as they are.
rem The old EXE is kept as OrpheusDL_GUI.old.exe, the old modules in
rem modules.backup-VERSION. All the work is done by the new EXE itself
rem (--install-into), the same code the in-app update uses.

set "RC=2"
set "TARGET=%~dp0.."
rem Unpacked straight over the old folder: update that folder itself.
if exist "%~dp0config\" set "TARGET=%~dp0."

if not exist "%~dp0OrpheusDL_GUI.exe" (
  echo OrpheusDL_GUI.exe is missing next to this script. Unpack the whole ZIP.
  goto :done
)
echo Updating OrpheusDL ...
start "" /wait "%~dp0OrpheusDL_GUI.exe" --install-into "%TARGET%" %*
set "RC=%errorlevel%"
if "%RC%"=="0" (
  echo Done.
) else (
  echo The update did not run. The message window said why, nothing was replaced.
)
:done
if not defined ORPHEUSDL_NO_PAUSE pause
exit /b %RC%
