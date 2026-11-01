@echo off
rem Apply this LLM Wiki code patch. Keeps data\ (database, settings.yaml, company_adapter.py, models) untouched.
rem   apply_patch.bat                      -> the install this folder was unzipped into (the folder above)
rem   apply_patch.bat D:\llmwiki           -> that install
rem   apply_patch.bat --dry-run            -> only list what would change
rem   options: --force (overwrite files changed on that machine; they stay in the backup)  --no-restart  --online
setlocal
set "TARGET=%~dp0.."
set "A1=%~1"
if not "%A1%"=="" if not "%A1:~0,1%"=="-" set "TARGET=%~1"
if exist "%TARGET%\.venv\Scripts\python.exe" (set "PY=%TARGET%\.venv\Scripts\python.exe") else (set "PY=python")
"%PY%" "%~dp0files\tools\patch.py" apply "%~dp0." --target "%TARGET%" %*
endlocal
