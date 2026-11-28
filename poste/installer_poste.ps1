# OreTracking V2 - mise en service continue du poste de site (28/11).
# A lancer UNE FOIS, dans PowerShell EN ADMINISTRATEUR :
#   powershell -ExecutionPolicy Bypass -File "<dossier du projet>\poste\installer_poste.ps1"
#
# Ce que fait ce script (et rien d'autre) :
#   1. Alimentation SUR SECTEUR : plus de mise en veille ni de veille prolongee, et fermer le capot ne fait rien.
#      Sur batterie, les reglages ne changent pas (pour ne pas vider la batterie pendant une coupure).
#   2. Tache planifiee "OreTracking - serveurs" : lance dashboard + passerelle (lanceur.py), sans fenetre, des que
#      la session Windows de cet utilisateur s'ouvre (30 s apres) ; relancee si elle s'arrete. Aucun mot de passe
#      demande (choix du 28/11). Apres un redemarrage du PC, il faut donc OUVRIR LA SESSION pour que tout reparte.
#      (Le mode "sans session" S4U restait bloque "Queued" sur ce poste ; le mode avec mot de passe stocke a ete ecarte.)
#   3. Tache planifiee "OreTracking - sauvegarde" : sauvegarde de data\ chaque nuit a 02:00
#      (D:\Sauvegardes\OreTracking, 14 jours gardes).
#   4. Arrete le lanceur ouvert a la main (fenetre noire) et demarre la tache a sa place, puis verifie.
# Pour tout defaire : poste\desinstaller_poste.ps1

$ErrorActionPreference = 'Stop'
$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
    Write-Host "Ce script doit etre lance dans PowerShell EN ADMINISTRATEUR." -ForegroundColor Red
    exit 1
}

$racine = Split-Path -Parent $PSScriptRoot
$utilisateur = "$env:USERDOMAIN\$env:USERNAME"
$python = $null
try { $python = (Get-Command python -ErrorAction Stop).Source } catch { }
if (-not $python -or $python -like '*WindowsApps*') {
    $python = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
}
if (-not (Test-Path $python)) { Write-Host "python.exe introuvable ($python)." -ForegroundColor Red; exit 1 }
if (-not (Test-Path (Join-Path $racine 'lanceur.py'))) { Write-Host "lanceur.py introuvable dans $racine" -ForegroundColor Red; exit 1 }

Write-Host "Dossier du projet : $racine"
Write-Host "Compte utilise    : $utilisateur"
Write-Host "Python            : $python"
Write-Host ""

# ---- 1. Alimentation (secteur seulement) ----
Write-Host "1. Alimentation sur secteur : pas de veille, capot ferme sans effet..."
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
Write-Host "   OK"

# ---- 2. Tache des serveurs ----
Write-Host "2. Tache planifiee 'OreTracking - serveurs'..."
$principal = New-ScheduledTaskPrincipal -UserId $utilisateur -LogonType Interactive -RunLevel Limited
$demarrer = Join-Path $racine 'poste\demarrer.cmd'
$action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c ""' + $demarrer + '" "' + $python + '""') -WorkingDirectory $racine
$declencheur = New-ScheduledTaskTrigger -AtLogOn -User $utilisateur
$declencheur.Delay = 'PT30S'          # laisser le reseau et Mosquitto demarrer
$reglages = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -Hidden
Register-ScheduledTask -TaskName 'OreTracking - serveurs' -Action $action -Trigger $declencheur -Principal $principal `
    -Settings $reglages -Description 'OreTracking V2 : dashboard (:8100) et passerelle (:8101). Journal : data\lanceur.log. Relancer : poste\relancer.cmd' -Force | Out-Null
Write-Host "   OK"

# ---- 3. Tache de sauvegarde ----
Write-Host "3. Tache planifiee 'OreTracking - sauvegarde' (chaque nuit a 02:00)..."
$sauvegarde = Join-Path $racine 'poste\sauvegarde.py'
$journal = Join-Path $racine 'data\sauvegarde.log'
$actionS = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/c ""' + $python + '" "' + $sauvegarde + '" >> "' + $journal + '" 2>&1"') -WorkingDirectory $racine
$declencheurS = New-ScheduledTaskTrigger -Daily -At '02:00'
$reglagesS = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName 'OreTracking - sauvegarde' -Action $actionS -Trigger $declencheurS -Principal $principal `
    -Settings $reglagesS -Description 'OreTracking V2 : sauvegarde de data\ vers D:\Sauvegardes\OreTracking (14 jours). Journal : data\sauvegarde.log' -Force | Out-Null
Write-Host "   OK"

# ---- 4. Bascule : lanceur manuel -> tache ----
Write-Host "4. Arret du lanceur ouvert a la main, demarrage par la tache..."
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'lanceur\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 3
Start-ScheduledTask -TaskName 'OreTracking - serveurs'
$ok = @{ 8100 = $false; 8101 = $false }
for ($i = 0; $i -lt 30 -and -not ($ok[8100] -and $ok[8101]); $i++) {
    Start-Sleep -Seconds 2
    foreach ($p in 8100, 8101) {
        if (-not $ok[$p]) {
            try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 ("http://127.0.0.1:$p/api/health") | Out-Null; $ok[$p] = $true } catch { }
        }
    }
}
if (-not ($ok[8100] -and $ok[8101])) {
    Write-Host "   La tache n'a pas demarre les serveurs : relance directe (les serveurs ne restent jamais arretes)." -ForegroundColor Yellow
    Start-Process cmd.exe -ArgumentList ('/c ""' + $demarrer + '" "' + $python + '""') -WorkingDirectory $racine -WindowStyle Hidden | Out-Null
    Start-Sleep -Seconds 12
    foreach ($p in 8100, 8101) { try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 ("http://127.0.0.1:$p/api/health") | Out-Null; $ok[$p] = $true } catch { } }
}
foreach ($p in 8100, 8101) {
    if ($ok[$p]) { Write-Host "   port $p : OK" -ForegroundColor Green }
    else { Write-Host "   port $p : NE REPOND PAS - voir $racine\data\lanceur.log" -ForegroundColor Red }
}

Write-Host "5. Premiere sauvegarde..."
& $python $sauvegarde

Write-Host ""
Write-Host "Termine." -ForegroundColor Green
Write-Host " - Les serveurs tournent sans fenetre. Journal : data\lanceur.log"
Write-Host " - Pour RELANCER apres une mise a jour : double-clic sur poste\relancer.cmd"
Write-Host " - Le tunnel Cloudflare est deja un service Windows (cloudflared)."
