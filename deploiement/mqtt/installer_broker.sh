#!/usr/bin/env bash
# =====================================================================
# installer_broker.sh — installe le broker MQTT OreTracking (Mosquitto)
# sur une machine Ubuntu 22.04 / 24.04 (Oracle Cloud ou autre).
#
# Usage (sur la machine, dans le dossier du kit) :
#     sudo bash installer_broker.sh <nom-de-domaine> <email-pour-lets-encrypt>
#   ex. sudo bash installer_broker.sh ot-lafigue.duckdns.org exploitation@exemple.com
#
# Le nom de domaine doit DEJA pointer vers l'IP publique de la machine.
# L'email sert uniquement a Let's Encrypt (avis d'expiration du certificat).
# En lancant ce script, vous acceptez les conditions d'utilisation de
# Let's Encrypt (https://letsencrypt.org/repository/).
#
# Ce que fait le script (on peut le relancer sans risque) :
#   1. installe mosquitto, mosquitto-clients, certbot ;
#   2. ouvre les ports 80 (certificat), 443 et 8883 dans le pare-feu LOCAL
#      (les images Ubuntu d'Oracle bloquent tout sauf le 22) ;
#   3. obtient le certificat TLS et prevoit son renouvellement automatique ;
#   4. installe la configuration et les droits (oretracking.conf, acl) ;
#   5. cree les comptes « passerelle » et « controle » s'ils n'existent pas.
# Les ports doivent AUSSI etre ouverts cote fournisseur (Oracle : « Security
# List » du reseau virtuel) : voir README.md, etape 3.
# =====================================================================
set -euo pipefail

DOMAINE="${1:-}"
EMAIL="${2:-}"
ICI="$(cd "$(dirname "$0")" && pwd)"

if [[ $EUID -ne 0 ]]; then echo "A lancer avec sudo." >&2; exit 1; fi
if [[ -z "$DOMAINE" || -z "$EMAIL" ]]; then
    echo "Usage : sudo bash installer_broker.sh <nom-de-domaine> <email>" >&2; exit 1
fi
for f in oretracking.conf acl ajouter_comptes.sh; do
    [[ -f "$ICI/$f" ]] || { echo "Fichier manquant dans le kit : $f" >&2; exit 1; }
done

echo "== 1/5 Paquets"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq mosquitto mosquitto-clients certbot openssl >/dev/null

echo "== 2/5 Pare-feu local (80, 443, 8883)"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
    for p in 80 443 8883; do ufw allow "$p/tcp" >/dev/null; done
fi
# Images Oracle : regles iptables qui rejettent tout sauf SSH
if iptables -S INPUT 2>/dev/null | grep -q -- "-j REJECT"; then
    for p in 80 443 8883; do
        if ! iptables -C INPUT -p tcp --dport "$p" -m state --state NEW -j ACCEPT 2>/dev/null; then
            # juste avant la premiere regle REJECT (sinon elle ne servirait a rien)
            n="$(iptables -L INPUT --line-numbers -n | awk '$2 == "REJECT" {print $1; exit}')"
            iptables -I INPUT "${n:-1}" -p tcp --dport "$p" -m state --state NEW -j ACCEPT
        fi
    done
    if command -v netfilter-persistent >/dev/null; then
        netfilter-persistent save >/dev/null
    else
        apt-get install -y -qq iptables-persistent >/dev/null && netfilter-persistent save >/dev/null
    fi
fi

echo "== 3/5 Certificat TLS pour $DOMAINE"
if [[ ! -d "/etc/letsencrypt/live/$DOMAINE" ]]; then
    certbot certonly --standalone --non-interactive --agree-tos -m "$EMAIL" -d "$DOMAINE"
fi
# A chaque renouvellement : copie lisible par mosquitto, puis redemarrage
install -d -m 755 /etc/letsencrypt/renewal-hooks/deploy
cat > /etc/letsencrypt/renewal-hooks/deploy/mosquitto.sh <<EOF
#!/bin/sh
install -d -m 750 -o root -g mosquitto /etc/mosquitto/certs
install -m 640 -o root -g mosquitto /etc/letsencrypt/live/$DOMAINE/fullchain.pem /etc/mosquitto/certs/fullchain.pem
install -m 640 -o root -g mosquitto /etc/letsencrypt/live/$DOMAINE/privkey.pem   /etc/mosquitto/certs/privkey.pem
systemctl restart mosquitto 2>/dev/null || true
EOF
chmod 755 /etc/letsencrypt/renewal-hooks/deploy/mosquitto.sh
/etc/letsencrypt/renewal-hooks/deploy/mosquitto.sh

echo "== 4/5 Configuration et droits"
if [[ -f /etc/mosquitto/mosquitto.conf && ! -f /etc/mosquitto/mosquitto.conf.origine ]]; then
    cp /etc/mosquitto/mosquitto.conf /etc/mosquitto/mosquitto.conf.origine
fi
# configuration principale reduite au minimum : tout est dans conf.d/oretracking.conf
cat > /etc/mosquitto/mosquitto.conf <<'EOF'
# Configuration OreTracking : voir /etc/mosquitto/conf.d/oretracking.conf
pid_file /run/mosquitto/mosquitto.pid
include_dir /etc/mosquitto/conf.d
EOF
install -m 644 "$ICI/oretracking.conf" /etc/mosquitto/conf.d/oretracking.conf
install -m 640 -o root -g mosquitto "$ICI/acl" /etc/mosquitto/acl
if [[ ! -f /etc/mosquitto/passwd ]]; then
    install -m 640 -o root -g mosquitto /dev/null /etc/mosquitto/passwd
fi
install -m 755 "$ICI/ajouter_comptes.sh" /usr/local/sbin/ot-ajouter-comptes

echo "== 5/5 Comptes de service et demarrage"
nouveaux=()
for compte in passerelle controle; do
    grep -q "^$compte:" /etc/mosquitto/passwd || nouveaux+=("$compte")
done
systemctl enable mosquitto >/dev/null 2>&1
if (( ${#nouveaux[@]} )); then
    /usr/local/sbin/ot-ajouter-comptes "${nouveaux[@]}"
fi
systemctl restart mosquitto
sleep 1
systemctl is-active --quiet mosquitto || { journalctl -u mosquitto -n 30 --no-pager; exit 1; }

echo
echo "Broker OreTracking pret :"
echo "  MQTT chiffre      : $DOMAINE:8883"
echo "  WebSocket chiffre : wss://$DOMAINE:443"
echo "Comptes des DT : sudo ot-ajouter-comptes DT-01 DT-02 ...   (mots de passe affiches une seule fois)"
echo "Verification     : bash verifier_broker.sh $DOMAINE"
