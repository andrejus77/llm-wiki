@echo off
rem Offline install on the server (Windows Server 2016+). Run from the unzipped llmwiki folder.
setlocal
cd /d "%~dp0"
echo === LLM Wiki install ===
where py >nul 2>nul && (set PY=py -3.11) || (set PY=python)
if not exist .venv (
  %PY% -m venv .venv || (echo Python 3.11+ is required & exit /b 1)
)
.venv\Scripts\python -m pip install --no-index --find-links wheelhouse -r requirements.lock || (echo pip install failed & exit /b 1)
if "%LLMWIKI_DATA%"=="" set LLMWIKI_DATA=%~dp0data
if not exist "%LLMWIKI_DATA%" mkdir "%LLMWIKI_DATA%"
if not exist "%LLMWIKI_DATA%\models" if exist models xcopy /E /I /Y /Q models "%LLMWIKI_DATA%\models" >nul
if not exist "%LLMWIKI_DATA%\settings.yaml" copy /Y tools\settings.server.yaml "%LLMWIKI_DATA%\settings.yaml" >nul
if not exist "%LLMWIKI_DATA%\company_adapter.py" copy /Y tools\company_adapter.example.py "%LLMWIKI_DATA%\company_adapter.py" >nul
set HF_HUB_OFFLINE=1
.venv\Scripts\python manage.py migrate
if exist import\llmwiki-data.zip (
  echo Importing the processed data from the work laptop ...
  .venv\Scripts\python manage.py import_data import\llmwiki-data.zip || echo Data not imported. To replace existing data: .venv\Scripts\python manage.py import_data import\llmwiki-data.zip --force
)
.venv\Scripts\python manage.py doctor --offline
.venv\Scripts\python tools\patch.py manifest >nul
echo.
echo Next:
echo  1. Edit %LLMWIKI_DATA%\settings.yaml (pop3_accounts: one per department mailbox; department addresses)
echo  2. Check %LLMWIKI_DATA%\company_adapter.py (it came with the data package, or paste your API call code)
echo  3. Store the secrets as the account that runs the portal: .venv\Scripts\python manage.py secret set api-a  (also api-b, pop3-a ...; 'secret list' shows what is missing)
echo  4. Create a login: .venv\Scripts\python manage.py createsuperuser
echo  5. doctor.bat --ping   then   run.bat
echo Later code updates: unzip a patch (tools\patch.bat on the laptop) into this folder and run its apply_patch.bat.
endlocal
