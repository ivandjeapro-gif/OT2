@echo off
rem OreTracking V2 - ARRETER volontairement les serveurs (le superviseur ne les relance pas).
rem Pour les redemarrer : poste\relancer.cmd, ou redemarrer le PC.
setlocal
cd /d "%~dp0.."
echo arret > "data\arret_serveurs"
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -match 'lanceur\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
echo Serveurs OreTracking arretes. Pour les relancer : poste\relancer.cmd
timeout /t 5 >nul
