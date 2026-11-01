@echo off
rem Check this machine; writes data\doctor-report.txt. Add --ping to send one tiny request to every LLM endpoint.
call "%~dp0llmwiki.bat" doctor %*
