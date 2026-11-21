#!/usr/bin/env bash
# =====================================================================
# ot-ajouter-comptes — cree (ou renouvelle) des comptes du broker.
#
#   sudo ot-ajouter-comptes DT-01 DT-02 DT-18      (un compte par DT)
#   sudo ot-ajouter-comptes passerelle              (compte de service)
#
# L'identifiant d'un DT est son code (celui des remontees : dt.id), car les
# droits (acl) limitent chaque compte a camions/<son identifiant>/...
# Un mot de passe aleatoire est genere et affiche UNE SEULE FOIS : le noter
# aussitot dans le gestionnaire de mots de passe de l'exploitation, puis le
# saisir dans les parametres MQTT de la tablette concernee.
# Relancer pour un compte existant = nouveau mot de passe (l'ancien ne
# marche plus).
# =====================================================================
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "A lancer avec sudo." >&2; exit 1; }
(( $# )) || { echo "Usage : sudo ot-ajouter-comptes DT-01 [DT-02 ...]" >&2; exit 1; }

FICHIER=/etc/mosquitto/passwd
[[ -f "$FICHIER" ]] || install -m 640 -o root -g mosquitto /dev/null "$FICHIER"

printf '\n%-14s %s\n' "COMPTE" "MOT DE PASSE"
for compte in "$@"; do
    if [[ ! "$compte" =~ ^[A-Za-z0-9_-]{2,40}$ ]]; then
        echo "Identifiant refuse (lettres, chiffres, - et _ seulement) : $compte" >&2; continue
    fi
    mdp="$(openssl rand -base64 32 | tr -d '/+=\n' | cut -c1-24)"
    mosquitto_passwd -b "$FICHIER" "$compte" "$mdp"
    printf '%-14s %s\n' "$compte" "$mdp"
done
echo
chown root:mosquitto "$FICHIER"; chmod 640 "$FICHIER"
# relit comptes et droits sans couper les connexions en cours
systemctl reload mosquitto 2>/dev/null || systemctl restart mosquitto 2>/dev/null || true
