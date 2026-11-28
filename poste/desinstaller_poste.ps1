# OreTracking V2 - retour en arriere de installer_poste.ps1 (a lancer EN ADMINISTRATEUR).
# Supprime les deux taches planifiees et remet la veille sur secteur a 2 h (reglage d'avant le 28/11).
# Ne touche ni aux donnees, ni aux sauvegardes, ni au tunnel Cloudflare.
$ErrorActionPreference = 'Continue'
schtasks /end /tn "OreTracking - serveurs" 2>$null | Out-Null
Unregister-ScheduledTask -TaskName 'OreTracking - serveurs' -Confirm:$false -ErrorAction SilentlyContinue
Unregister-ScheduledTask -TaskName 'OreTracking - sauvegarde' -Confirm:$false -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'lanceur\.py|superviseur\.py' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
powercfg /change standby-timeout-ac 120
powercfg /change hibernate-timeout-ac 120
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 1
powercfg /setactive SCHEME_CURRENT
Write-Host "Taches supprimees, veille sur secteur remise a 2 h. Relancez les serveurs a la main : python lanceur.py"
