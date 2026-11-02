@echo off
rem PS3revamp command line. Uses the bundled Python; no network access needed.
rem   ps3revamp.cmd port "C:\...\EBOOT.elf" --build
rem   ps3revamp.cmd analyze "C:\...\EBOOT.elf"
rem   ps3revamp.cmd doctor
setlocal
cd /d "%~dp0"
if exist "%~dp0python\python.exe" (
  "%~dp0python\python.exe" "%~dp0ps3revamp.py" %*
) else (
  python "%~dp0ps3revamp.py" %*
)
