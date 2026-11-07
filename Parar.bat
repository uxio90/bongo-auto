@echo off
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'python.exe' -and $_.CommandLine -match 'bongo_auto' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
