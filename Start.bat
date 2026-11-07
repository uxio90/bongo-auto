@echo off
rem Restarts the script, so the cat is put back in position on every launch.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'bongo_auto' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
start "Bongo Auto" /min python "%~dp0bongo_auto.py"
