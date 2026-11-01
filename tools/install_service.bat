@echo off
rem Start LLM Wiki at boot via the Task Scheduler ("run whether the user is logged on or not").
rem Run this once in an elevated command prompt. It asks for the password of the account that will run it
rem (that account needs the environment variables with the API keys, and Outlook if the .pst is read via COM).
setlocal
set TASK=LLMWiki
schtasks /Create /TN %TASK% /TR "\"%~dp0run.bat\"" /SC ONSTART /RU "%USERDOMAIN%\%USERNAME%" /RP * /RL HIGHEST /F
echo Created task %TASK%. Start it now with: schtasks /Run /TN %TASK%
endlocal
