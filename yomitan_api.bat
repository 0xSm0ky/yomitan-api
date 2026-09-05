@echo off
rem Run the yomitan_api.py script using the python on PATH and a repo-relative path.
rem This avoids hardcoded absolute paths and works when Python is added to PATH.
setlocal enabledelayedexpansion
set DIR=%~dp0
python -u "%DIR%yomitan_api.py"
