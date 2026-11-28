@echo off
rem OreTracking V2 - demarrage des serveurs (dashboard :8100 + passerelle :8101) par la tache planifiee.
rem Lance poste\superviseur.py, qui lance lanceur.py SANS console et le RELANCE s'il s'arrete (28/11).
rem Journaux : data\lanceur.log (serveurs), data\superviseur.log (erreurs du superviseur seulement). %1 = chemin complet de python.exe (pose par installer_poste.ps1).
setlocal
cd /d "%~dp0.."
set "PY=%~1"
if "%PY%"=="" set "PY=python"
"%PY%" -u poste\superviseur.py < nul >> "data\superviseur.log" 2>&1
exit /b %errorlevel%
