@echo off
rem Start the portal + API + scheduler (one process). Local PC: http://127.0.0.1:8000  Server: see settings (host 0.0.0.0).
setlocal
cd /d "%~dp0"
if "%LLMWIKI_DATA%"=="" set LLMWIKI_DATA=%~dp0data
set HF_HUB_OFFLINE=1
if exist .venv\Scripts\python.exe (set PY=.venv\Scripts\python) else (set PY=python)
%PY% manage.py serve %*
endlocal
