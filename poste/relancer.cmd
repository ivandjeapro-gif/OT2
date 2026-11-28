@echo off
rem OreTracking V2 - RELANCER les serveurs (dashboard + passerelle) apres une mise a jour du code.
rem Arrete lanceur.py : le superviseur (poste\superviseur.py) le relance tout seul en 10 s.
rem Si le superviseur ne tourne pas, la tache "OreTracking - serveurs" est demarree.
setlocal
echo Relance des serveurs OreTracking...
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'lanceur\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
powershell -NoProfile -Command "if (-not (Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'superviseur\.py' })) { Start-ScheduledTask -TaskName 'OreTracking - serveurs' }"
echo Attente du redemarrage (environ 20 s)...
timeout /t 20 /nobreak >nul
powershell -NoProfile -Command "foreach ($p in 8100,8101) { try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 ('http://127.0.0.1:' + $p + '/api/health') | Out-Null; Write-Host ('  port ' + $p + ' : OK') } catch { Write-Host ('  port ' + $p + ' : pas encore pret (voir data\lanceur.log)') } }"
echo.
timeout /t 5 >nul
