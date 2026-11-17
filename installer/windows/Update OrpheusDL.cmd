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
rem The old install is the folder above when it has the program and config.
rem Otherwise this folder itself (the ZIP was unpacked straight over the old
rem folder); the EXE refuses when neither is an OrpheusDL folder.
set "TARGET=%~dp0."
if exist "%~dp0..\OrpheusDL_GUI.exe" if exist "%~dp0..\config\" set "TARGET=%~dp0.."

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
