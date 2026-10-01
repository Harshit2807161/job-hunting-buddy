@echo off
REM Runs one poll cycle. Called by Windows Task Scheduler every 15 minutes.
REM Calls the env's python.exe directly -- no conda activation needed.
cd /d "%~dp0"
"C:\Users\Harshit\miniconda3\envs\jhb\python.exe" -m jhb.poll >> "%~dp0data\poll.log" 2>&1
