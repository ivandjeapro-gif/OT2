# =====================================================================
# ajouter_dt.ps1 — cree (ou renouvelle) des comptes du broker OreTracking
# (Windows, PC de la passerelle). A lancer en ADMINISTRATEUR :
#
#   powershell -ExecutionPolicy Bypass -File ajouter_dt.ps1 DT-01 DT-02 DT-18
#
# L'identifiant = le code du DT, exactement celui des remontees (dt.id) :
# les droits limitent chaque compte a camions/<son code>/...
# Les mots de passe (aleatoires) sont AJOUTES au fichier du Bureau
# OreTracking-MQTT-comptes.txt, jamais affiches. Relancer pour un compte
# existant = nouveau mot de passe (l'ancien ne marche plus).
# Le service est redemarre (les tablettes se reconnectent seules en
# quelques secondes).
# =====================================================================
param([Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)][string[]]$Comptes)
$ErrorActionPreference = "Stop"
$estAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $estAdmin) { Write-Host "A lancer en administrateur."; exit 1 }

$passwd = "C:\ProgramData\OreTracking\mosquitto\passwd"
$outil = "C:\Program Files\mosquitto\mosquitto_passwd.exe"
$fichier = Join-Path ([Environment]::GetFolderPath("Desktop")) "OreTracking-MQTT-comptes.txt"
function Nouveau-Mdp { -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 24 | ForEach-Object { [char]$_ }) }

$lignes = @("Comptes ajoutes le $(Get-Date -Format 'yyyy-MM-dd HH:mm') :")
$faits = @()
foreach ($c in $Comptes) {
    if ($c -notmatch '^[A-Za-z0-9_-]{2,40}$') { Write-Host "Identifiant refuse : $c"; continue }
    $mdp = Nouveau-Mdp
    & $outil -b $passwd $c $mdp
    $lignes += ("{0,-12} {1}" -f $c, $mdp)
    $faits += $c
}
Add-Content -Path $fichier -Value ($lignes + "") -Encoding utf8
Restart-Service mosquitto
Write-Host ("Comptes crees : " + ($faits -join ", "))
Write-Host "Mots de passe ajoutes a : $fichier (a ranger puis supprimer)"
