@echo off
if not defined GOGOGOOGLE_WORKER_PYTHON (
  >&2 echo GOGOGOOGLE_WORKER_PYTHON is not configured.
  exit /b 1
)
"%GOGOGOOGLE_WORKER_PYTHON%" "%~dp0remote_supervisor.py"
exit /b %errorlevel%
