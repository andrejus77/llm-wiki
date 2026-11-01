@echo off
rem Build a code-only patch of this checkout for another machine (work laptop or server). No data goes into it.
rem   tools\patch.bat                          -> dist\llmwiki-patch-<version>.zip
rem   tools\patch.bat --note "page editor"     -> with a note shown when it is applied
rem   tools\patch.bat --wheels                 -> also the Python wheels (when requirements.lock changed; ~300 MB)
setlocal
cd /d "%~dp0.."
if exist .venv\Scripts\python.exe (set PY=.venv\Scripts\python) else (set PY=python)
%PY% tools\patch.py build %*
endlocal
