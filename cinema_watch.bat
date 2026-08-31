@echo off
rem cinema-watch launcher.
rem %~dp0 is this file's own folder, so the project can be moved anywhere.
rem %* passes arguments through, so headless mode (--radar etc.) works too.
rem Comments are kept ASCII on purpose: cmd reads this file in the console
rem codepage, and Korean text here would be mangled into bogus commands.
chcp 65001 >nul 2>&1
set PYTHONUTF8=1
where py >nul 2>&1
if %errorlevel%==0 (
  py "%~dp0cinema_watch.py" %*
) else (
  python "%~dp0cinema_watch.py" %*
)
