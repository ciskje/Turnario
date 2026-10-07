@echo off
cd /d "%~dp0"
set DB=%1
if "%DB%"=="" set DB=turnario.db

:: kill anything already listening on 8080
powershell -NoProfile -Command "Get-NetTCPConnection -LocalPort 8080 -State Listen -ErrorAction SilentlyContinue | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }"

start "Turnario" python app.py --port 8080 --db "%DB%"

:: wait for the server before opening the browser
powershell -NoProfile -Command "for($i=0;$i -lt 30;$i++){ try { Invoke-WebRequest http://127.0.0.1:8080/ -UseBasicParsing -TimeoutSec 1 | Out-Null; break } catch { Start-Sleep -Milliseconds 300 } }"

start "" http://127.0.0.1:8080/
