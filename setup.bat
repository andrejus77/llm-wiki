@echo off
rem Setup on a machine WITH internet (dev laptop, or a PC that can reach PyPI and Hugging Face / the internal mirror).
rem For the offline server use tools\package.bat on the laptop and install.bat on the server instead.
rem   setup.bat            -> data\ with default settings
rem   setup.bat --laptop   -> work laptop: .pst backlog, internal APIs for mail, Gemini/Claude/Codex for the wiki lint
rem   setup.bat --home     -> work laptop at home: the synthetic test mailbox processed with Gemini CLI (data_gemini\)
setlocal
cd /d "%~dp0"
where py >nul 2>nul && (set PY=py -3.11) || (set PY=python)
if not exist .venv %PY% -m venv .venv
.venv\Scripts\python -m pip install -r requirements.lock || exit /b 1
if "%1"=="--home" if "%LLMWIKI_DATA%"=="" set LLMWIKI_DATA=%~dp0data_gemini
if "%LLMWIKI_DATA%"=="" set LLMWIKI_DATA=%~dp0data
if not exist "%LLMWIKI_DATA%" mkdir "%LLMWIKI_DATA%"
if "%1"=="--laptop" if not exist "%LLMWIKI_DATA%\settings.yaml" copy /Y tools\settings.laptop.yaml "%LLMWIKI_DATA%\settings.yaml" >nul
if "%1"=="--laptop" if not exist "%LLMWIKI_DATA%\company_adapter.py" copy /Y tools\company_adapter.example.py "%LLMWIKI_DATA%\company_adapter.py" >nul
if "%1"=="--home" if not exist "%LLMWIKI_DATA%\settings.yaml" copy /Y tools\settings.home.yaml "%LLMWIKI_DATA%\settings.yaml" >nul
.venv\Scripts\python manage.py migrate
.venv\Scripts\python manage.py models --download
.venv\Scripts\python manage.py doctor --offline
.venv\Scripts\python tools\patch.py manifest >nul
echo.
echo Data folder: %LLMWIKI_DATA%
if "%1"=="--laptop" echo Next: edit %LLMWIKI_DATA%\settings.yaml and company_adapter.py, log in to gemini / claude / codex once, then see docs\OPERATIONS.md "Work laptop".
if "%1"=="--home" echo Next: log in to Gemini CLI once (run: gemini), then: set LLMWIKI_DATA=%LLMWIKI_DATA% ^& llmwiki fetch --eml testdata\eml ^& llmwiki process --mode backfill
echo Start the portal with run.bat (opens http://127.0.0.1:8000).
endlocal
