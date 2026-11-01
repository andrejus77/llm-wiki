@echo off
rem On the company laptop (has pip + the Hugging Face mirror): build the offline zip for the Windows Server.
rem   package.bat                 -> win_amd64, Python 3.11
rem   package.bat --python 3.12   -> if the server gets a newer Python
setlocal
cd /d "%~dp0.."
where py >nul 2>nul && (set PY=py -3.11) || (set PY=python)
if not exist .venv-pkg (
  %PY% -m venv .venv-pkg
  .venv-pkg\Scripts\python -m pip install -q huggingface_hub
)
.venv-pkg\Scripts\python tools\package_offline.py %*
endlocal
