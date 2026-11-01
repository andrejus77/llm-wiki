@echo off
rem Backfill: import older mail from the .pst (dates inclusive/exclusive), then process it week by week.
rem   backfill.bat 2026-09-01 2026-10-01
rem Stop the server first (run.bat), or use the portal (Agent > Logs) while it runs. Safe to re-run: nothing is processed twice.
setlocal
if "%~1"=="" (echo usage: backfill.bat SINCE UNTIL   e.g. backfill.bat 2026-09-01 2026-10-01 & exit /b 1)
call "%~dp0llmwiki.bat" fetch --pst --since %1 --until %2
call "%~dp0llmwiki.bat" fetch
call "%~dp0llmwiki.bat" process --mode backfill
endlocal
