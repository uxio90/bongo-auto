@echo off
rem Restarts the web panel and opens it in the browser.
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'panel\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
start "Bongo Panel" /min python "%~dp0panel.py"
timeout /t 2 /nobreak >nul
start "" http://localhost:8765
