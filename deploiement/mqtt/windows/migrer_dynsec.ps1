# =====================================================================
# migrer_dynsec.ps1 — passe le broker OreTracking au module « Dynamic
# Security » (comptes geres a chaud). A lancer EN ADMINISTRATEUR :
#
#   powershell -ExecutionPolicy Bypass -File migrer_dynsec.ps1 -Python <python.exe> [-Utilisateur <compte Windows>]
#
# - configuration : oretracking.conf (un seul port, 127.0.0.1:1883, compte
#   obligatoire) ; dynamic-security.json cree s'il n'existe pas ;
# - roles : dt (camions/%u/...), passerelle (lit tout, publie mission/alerte),
#   controle (lecture seule) ;
# - comptes techniques : admin-ot (administration, utilise par la passerelle),
#   passerelle, controle, DT-TEST. Les mots de passe de controle et DT-TEST
#   deja distribues sont REPRIS du fichier du Bureau s'il existe ;
# - identifiants de la passerelle dans C:\ProgramData\OreTracking\passerelle\mqtt.json,
#   lisible par SYSTEM, les administrateurs et l'utilisateur qui lance la passerelle ;
# - recette locale ; resultat dans derniere_verification.log.
# Relancable : un compte, un role ou un droit existant est laisse tel quel.
# =====================================================================
param([string]$Python = "python", [string]$Utilisateur = "", [switch]$Reinitialiser)
$ErrorActionPreference = "Stop"
$kit = $PSScriptRoot
$journal = Join-Path $kit "derniere_verification.log"
function Ecrire($t) { Add-Content -Path $journal -Value $t -Encoding utf8 }
Set-Content -Path $journal -Value ("Migration Dynamic Security du " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss")) -Encoding utf8

try {
    $estAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
    if (-not $estAdmin) { throw "A lancer en administrateur." }
    $mosq = "C:\Program Files\mosquitto"
    $ctrl = "$mosq\mosquitto_ctrl.exe"
    $base = "C:\ProgramData\OreTracking\mosquitto"
    $json = "$base\dynamic-security.json"
    $dossierPasserelle = "C:\ProgramData\OreTracking\passerelle"
    $fichierPasserelle = "$dossierPasserelle\mqtt.json"
    $bureau = Join-Path ([Environment]::GetFolderPath("Desktop")) "OreTracking-MQTT-comptes.txt"
    function Nouveau-Mdp { -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 24 | ForEach-Object { [char]$_ }) }

    # --- mots de passe : repris s'ils existent, sinon generes ---
    $mdp = @{}
    $ancien = $null
    if (Test-Path $fichierPasserelle) {
        $ancien = Get-Content $fichierPasserelle -Raw | ConvertFrom-Json
        $mdp["admin-ot"] = $ancien.admin.mdp; $mdp["passerelle"] = $ancien.passerelle.mdp
    }
    if (Test-Path $bureau) {
        foreach ($l in Get-Content $bureau -Encoding utf8) {
            if ($l -match '^(controle|DT-TEST)\s+(\S+)\s*$') { $mdp[$Matches[1]] = $Matches[2] }   # la derniere valeur gagne
        }
    }
    foreach ($c in "passerelle", "controle", "DT-TEST") { if (-not $mdp[$c]) { $mdp[$c] = Nouveau-Mdp } }

    # --- 1. configuration + fichier dynsec ---
    New-Item -ItemType Directory -Force -Path "$base\conf.d", "$base\data", $dossierPasserelle | Out-Null
    Copy-Item "$kit\oretracking.conf" "$base\conf.d\oretracking.conf" -Force
    # Le module refuse un fichier cree par mosquitto_ctrl (« File is not readable -
    # check permissions ») : on laisse le BROKER generer son fichier (droits qu'il
    # attend), avec un administrateur « admin » dont il ecrit le mot de passe dans
    # dynamic-security.json.pw. On lit ce fichier, puis on le supprime.
    $ErrorActionPreference = "Continue"
    $pw = $json + ".pw"
    if ($Reinitialiser -or -not (Test-Path $json) -or -not $mdp["admin-ot"]) {
        Stop-Service mosquitto -Force
        (Get-Service mosquitto).WaitForStatus("Stopped", [TimeSpan]::FromSeconds(20))
        Start-Sleep -Seconds 1
        $suffixe = ".avant-" + (Get-Date -Format "yyyyMMdd-HHmmss")
        foreach ($f in $json, $pw) {
            if (Test-Path $f) {
                # fichier cree par le broker (SYSTEM, droits restreints) : on en reprend la propriete pour le mettre de cote
                & takeown /f $f /a 2>&1 | Out-Null
                & icacls $f /grant "*S-1-5-32-544:F" 2>&1 | Out-Null
                Move-Item $f ($f + $suffixe) -Force -ErrorAction Stop
                if (Test-Path $f) { throw "Impossible de mettre $f de cote (fichier verrouille ?)" }
            }
        }
        Start-Service mosquitto
        for ($i = 0; $i -lt 60 -and -not (Test-Path $pw); $i++) { Start-Sleep -Milliseconds 500 }
        if (-not (Test-Path $pw)) { throw "Le broker n'a pas genere $pw (voir le journal)" }
        & takeown /f $pw /a 2>&1 | Out-Null
        & icacls $pw /grant "*S-1-5-32-544:R" 2>&1 | Out-Null
        # une ligne par compte genere : « admin <mdp> », puis « democlient <mdp> » (supprime plus bas)
        $premiere = ([IO.File]::ReadAllLines($pw) | Where-Object { $_.Trim() } | Select-Object -First 1)
        $morceaux = $premiere.Trim() -split '\s+', 2
        if ($morceaux.Count -ne 2) { throw "Format inattendu de $pw" }
        $adminId = $morceaux[0]; $mdp["admin-ot"] = $morceaux[1]
        Remove-Item $pw -Force                          # secret : ne pas le laisser sur disque
        Ecrire "1. Fichier des comptes genere par le broker (administrateur : $adminId)"
    } else {
        $adminId = $ancien.admin.identifiant
        Ecrire "1. Fichier des comptes existant conserve (administrateur : $adminId)"
    }
    $ErrorActionPreference = "Stop"
    Start-Sleep -Seconds 1
    if ((Get-Service mosquitto).Status -ne "Running") { throw "Le service ne tourne pas : voir $base\mosquitto.log" }
    Ecrire "2. Service actif avec le module Dynamic Security (port 127.0.0.1:1883)"

    # --- 3. identifiants de la passerelle (fichier protege) ---
    $conf = [ordered]@{
        hote = "127.0.0.1"; port = 1883
        admin = [ordered]@{ identifiant = $adminId; mdp = $mdp["admin-ot"] }
        passerelle = [ordered]@{ identifiant = "passerelle"; mdp = $mdp["passerelle"] }
        public = [ordered]@{ hote = "oretracking.tail6a2cf7.ts.net"; port = 443; tls = $true }
        comptes_techniques = @($adminId, "passerelle", "controle", "DT-TEST")
    }
    $conf | ConvertTo-Json -Depth 4 | Set-Content -Path $fichierPasserelle -Encoding utf8
    $droits = @("*S-1-5-18:(OI)(CI)F", "*S-1-5-32-544:(OI)(CI)F")
    if ($Utilisateur) { $droits += "${Utilisateur}:(OI)(CI)R" }
    & icacls $dossierPasserelle /inheritance:r /grant:r @droits | Out-Null
    Ecrire ("4. Identifiants de la passerelle : $fichierPasserelle (lecture : SYSTEM, administrateurs" + $(if ($Utilisateur) { ", $Utilisateur" } else { "" }) + ")")

    # --- 2. roles et comptes techniques : en MQTT par dynsec.py (pas de ligne de
    #        commande : les mots de passe generes contiennent des caracteres speciaux)
    $env:OT_MQTT_PASSERELLE = $mdp["passerelle"]; $env:OT_MQTT_CONTROLE = $mdp["controle"]; $env:OT_MQTT_DT_TEST = $mdp["DT-TEST"]
    $env:OT_MQTT_CONF = $fichierPasserelle
    Ecrire "3. Roles et comptes techniques :"
    $ErrorActionPreference = "Continue"
    & $Python -W ignore "$kit\dynsec.py" mettre-en-place 2>&1 | ForEach-Object { Ecrire ("   " + $_) }
    $ErrorActionPreference = "Stop"

    # mots de passe de recette : sur le Bureau (inchanges s'ils y etaient deja)
    Add-Content -Path $bureau -Encoding utf8 -Value @(
        "Comptes du broker MQTT OreTracking - $(Get-Date -Format 'yyyy-MM-dd HH:mm') (migration Dynamic Security)",
        ("{0,-12} {1}" -f "controle", $mdp["controle"]),
        ("{0,-12} {1}" -f "DT-TEST", $mdp["DT-TEST"]), "")

    # --- 4. recette locale ---
    Ecrire "5. Recette locale :"
    & $Python -W ignore "$kit\verifier_broker.py" local 2>&1 | ForEach-Object { Ecrire ("   " + $_) }
    Remove-Item Env:OT_MQTT_CONTROLE, Env:OT_MQTT_DT_TEST, Env:OT_MQTT_PASSERELLE, Env:OT_MQTT_CONF -ErrorAction SilentlyContinue
} catch {
    Ecrire ("ERREUR : " + $_.Exception.Message)
}
Ecrire "FIN"
