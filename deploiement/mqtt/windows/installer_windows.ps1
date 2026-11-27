# =====================================================================
# installer_windows.ps1 — configure Mosquitto (deja installe par winget)
# sur le PC de la passerelle, pour le suivi temps reel des DT.
#
#   A lancer EN ADMINISTRATEUR (une fenetre UAC le demande) :
#   powershell -ExecutionPolicy Bypass -File installer_windows.ps1 -Python <chemin de python.exe>
#
# - configuration et donnees dans C:\ProgramData\OreTracking\mosquitto
#   (dossier reserve a SYSTEM et aux administrateurs) ;
# - C:\Program Files\mosquitto\mosquitto.conf ne fait plus qu'inclure ce
#   dossier (original garde en mosquitto.conf.origine) ;
# - comptes « controle » et « DT-TEST » (re)generes : mots de passe ecrits
#   dans un fichier du Bureau, JAMAIS affiches ;
# - redemarrage du service puis recette locale (verifier_broker.py local),
#   resultat dans derniere_verification.log a cote de ce script.
# Relancable sans risque.
# =====================================================================
param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$kit = $PSScriptRoot
$journal = Join-Path $kit "derniere_verification.log"
function Ecrire($t) { Add-Content -Path $journal -Value $t -Encoding utf8 }
Set-Content -Path $journal -Value ("Installation du " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss")) -Encoding utf8

try {
    $estAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $estAdmin) { throw "A lancer en administrateur." }

    $mosq = "C:\Program Files\mosquitto"
    if (-not (Test-Path "$mosq\mosquitto.exe")) { throw "Mosquitto introuvable dans $mosq (winget install EclipseFoundation.Mosquitto)." }
    $base = "C:\ProgramData\OreTracking\mosquitto"

    # 1. dossiers, fichiers de configuration et droits
    New-Item -ItemType Directory -Force -Path "$base\conf.d", "$base\data" | Out-Null
    Copy-Item "$kit\oretracking.conf" "$base\conf.d\oretracking.conf" -Force
    Copy-Item "$kit\..\acl" "$base\acl" -Force
    Copy-Item "$kit\acl_local" "$base\acl_local" -Force
    if (-not (Test-Path "$base\passwd")) { New-Item -ItemType File -Path "$base\passwd" | Out-Null }
    # dossier reserve a SYSTEM (service) et aux administrateurs : il contient les empreintes des mots de passe
    & icacls $base /inheritance:r /grant:r "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" | Out-Null
    Ecrire "1. Configuration copiee dans $base (acces : SYSTEM + Administrateurs)"

    # 2. configuration principale : simple inclusion
    if ((Test-Path "$mosq\mosquitto.conf") -and -not (Test-Path "$mosq\mosquitto.conf.origine")) {
        Copy-Item "$mosq\mosquitto.conf" "$mosq\mosquitto.conf.origine"
    }
    Set-Content -Path "$mosq\mosquitto.conf" -Encoding ascii -Value @(
        "# OreTracking : configuration dans C:\ProgramData\OreTracking\mosquitto\conf.d",
        "include_dir C:/ProgramData/OreTracking/mosquitto/conf.d")
    Ecrire "2. mosquitto.conf principal : inclusion de conf.d (original : mosquitto.conf.origine)"

    # 3. comptes de recette (mots de passe aleatoires, jamais affiches)
    function Nouveau-Mdp { -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 24 | ForEach-Object { [char]$_ }) }
    $comptes = [ordered]@{ "controle" = (Nouveau-Mdp); "DT-TEST" = (Nouveau-Mdp) }
    foreach ($k in $comptes.Keys) { & "$mosq\mosquitto_passwd.exe" -b "$base\passwd" $k $comptes[$k] }
    $fichier = Join-Path ([Environment]::GetFolderPath("Desktop")) "OreTracking-MQTT-comptes.txt"
    $lignes = @("Comptes du broker MQTT OreTracking - $(Get-Date -Format 'yyyy-MM-dd HH:mm')",
                "A ranger dans le gestionnaire de mots de passe puis SUPPRIMER ce fichier.", "")
    foreach ($k in $comptes.Keys) { $lignes += ("{0,-12} {1}" -f $k, $comptes[$k]) }
    Add-Content -Path $fichier -Value ($lignes + "") -Encoding utf8
    Ecrire "3. Comptes controle et DT-TEST (re)generes ; mots de passe dans : $fichier"

    # 4. service
    Restart-Service mosquitto
    Start-Sleep -Seconds 2
    $svc = Get-Service mosquitto
    Ecrire ("4. Service mosquitto : " + $svc.Status)
    if ($svc.Status -ne "Running") { throw "Le service ne demarre pas : voir $base\mosquitto.log" }
    $ecoute = Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -in 1883, 1884 } |
        ForEach-Object { "{0}:{1}" -f $_.LocalAddress, $_.LocalPort }
    Ecrire ("   Ports a l'ecoute : " + ($ecoute -join ", "))

    # 5. recette locale (sans afficher les mots de passe)
    $env:OT_MQTT_CONTROLE = $comptes["controle"]
    $env:OT_MQTT_DT_TEST = $comptes["DT-TEST"]
    Ecrire "5. Recette locale :"
    $sortie = & $Python -W ignore "$kit\verifier_broker.py" local 2>&1
    $sortie | ForEach-Object { Ecrire ("   " + $_) }
    Remove-Item Env:OT_MQTT_CONTROLE, Env:OT_MQTT_DT_TEST -ErrorAction SilentlyContinue
} catch {
    Ecrire ("ERREUR : " + $_.Exception.Message)
}
Ecrire "FIN"
