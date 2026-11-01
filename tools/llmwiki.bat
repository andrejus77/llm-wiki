@echo off
rem Run any management command with the right environment, e.g.:
rem   llmwiki fetch --pst --since 2026-09-01      llmwiki process --mode backfill      llmwiki eval --answers 20
setlocal
cd /d "%~dp0"
if "%LLMWIKI_DATA%"=="" set LLMWIKI_DATA=%~dp0data
set HF_HUB_OFFLINE=1
if exist .venv\Scripts\python.exe (set PY=.venv\Scripts\python) else (set PY=python)
%PY% manage.py %*
endlocal
