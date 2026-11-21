# diagnostic.ps1 — copie (EN ADMINISTRATEUR) les informations NON secretes du
# broker dans diagnostic.log, a cote de ce script : fin du journal Mosquitto,
# configuration chargee, comptes et roles dynsec SANS mots de passe ni sels.
$ErrorActionPreference = "Continue"
$sortie = Join-Path $PSScriptRoot "diagnostic.log"
$base = "C:\ProgramData\OreTracking\mosquitto"
$l = @("=== Diagnostic du $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') ===", "", "--- conf.d ---")
$l += Get-ChildItem "$base\conf.d" | ForEach-Object { $_.Name }
$l += "", "--- mosquitto.log (40 dernieres lignes) ---"
function Lire-Partage($chemin) {
    # le broker garde ses fichiers ouverts : lecture en partage
    try {
        $fs = [IO.File]::Open($chemin, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite -bor [IO.FileShare]::Delete)
        $sr = New-Object IO.StreamReader($fs); $t = $sr.ReadToEnd(); $sr.Close(); return $t
    } catch { return "(lecture impossible : " + $_.Exception.Message + ")" }
}
# le broker (SYSTEM) cree ses fichiers avec des droits restreints : les administrateurs en reprennent la lecture
foreach ($f in "$base\mosquitto.log", "$base\dynamic-security.json.pw") {
    if (Test-Path $f) { & takeown /f $f /a 2>&1 | Out-Null; & icacls $f /grant "*S-1-5-32-544:R" 2>&1 | Out-Null }
}
$txt = Lire-Partage "$base\mosquitto.log"
$lignes = $txt -split "`r?`n" | Where-Object { $_ -match "\S" }
$l += $lignes | Where-Object { $_ -notmatch "New connection|disconnected: not authorised" } | Select-Object -Last 40
$l += "", "--- tentatives des tablettes (lignes dt- / DT-, 30 dernieres) ---"
$l += $lignes | Where-Object { $_ -match "dt-|DT-" } | Select-Object -Last 30
$l += "", "--- connexions par le tunnel (hors passerelle/admin/recette), 15 dernieres ---"
$l += $lignes | Where-Object { $_ -match "New client connected|New connection" -and $_ -notmatch "passerelle|admin|recette|ot-dynsec|nettoyage" } | Select-Object -Last 15
$l += "", "--- 12 dernieres lignes brutes ---"
$l += $lignes | Select-Object -Last 12
$l += "", "--- .pw (longueur et forme, pas le contenu) ---"
if (Test-Path "$base\dynamic-security.json.pw") { $pw = Lire-Partage "$base\dynamic-security.json.pw"; $l += ("{0} caracteres, commence par : {1}" -f $pw.Length, ($pw -replace '[A-Za-z0-9+/=]','x').Substring(0, [Math]::Min(12,$pw.Length))) }
$l += "", "--- dynamic-security.json (sans secrets) ---"
if (Test-Path "$base\dynamic-security.json") {
    $j = $null  # lecture volontairement abandonnee : changer ses droits casserait le broker
    foreach ($c in $j.clients) {
        $l += ("client {0} roles=[{1}] desactive={2} champs={3}" -f $c.username, (($c.roles | ForEach-Object { $_.rolename }) -join ","), $c.disabled, (($c.PSObject.Properties.Name | Where-Object { $_ -notin "password", "salt" }) -join ","))
    }
    foreach ($r in $j.roles) { $l += ("role {0} : {1} ACL" -f $r.rolename, @($r.acls).Count) }
} else { $l += "(absent)" }
$l += "", "--- fichiers ---"
$l += Get-ChildItem $base | ForEach-Object { "{0}  {1} o  {2}" -f $_.Name, $_.Length, $_.LastWriteTime }
Set-Content -Path $sortie -Value $l -Encoding utf8
