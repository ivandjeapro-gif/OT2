#!/usr/bin/env bash
# =====================================================================
# verifier_broker.sh — recette du broker OreTracking (CDC § 7 : R4).
#
#   bash verifier_broker.sh <nom-de-domaine>
#
# Demande les mots de passe du compte « controle » et d'un compte DT de test
# (creez-le avant : sudo ot-ajouter-comptes DT-TEST). Peut etre lance sur la
# machine du broker ou sur n'importe quel poste ayant mosquitto-clients.
# Verifie :
#   1. connexion TLS 8883 refusee sans compte ;
#   2. un DT publie sa position, le compte controle la recoit ;
#   3. un DT ne peut PAS publier sur le sujet d'un autre DT ;
#   4. la derniere position est conservee (retain).
# =====================================================================
set -uo pipefail
DOMAINE="${1:-}"
[[ -n "$DOMAINE" ]] || { echo "Usage : bash verifier_broker.sh <nom-de-domaine>" >&2; exit 1; }
command -v mosquitto_pub >/dev/null || { echo "Installer mosquitto-clients." >&2; exit 1; }

read -r -p  "Compte DT de test [DT-TEST] : " DT;  DT="${DT:-DT-TEST}"
read -r -s -p "Mot de passe de $DT : " MDP_DT; echo
read -r -s -p "Mot de passe du compte controle : " MDP_CTRL; echo

TLS=(-h "$DOMAINE" -p 8883 --capath /etc/ssl/certs)
echecs=0
ok()  { echo "OK     $*"; }
ko()  { echo "ECHEC  $*"; echecs=$((echecs + 1)); }
temp="$(mktemp -d)"; trap 'rm -rf "$temp"' EXIT

# 1. sans compte
if mosquitto_pub "${TLS[@]}" -t camions/X/position -m test -q 1 2>/dev/null; then
    ko "connexion anonyme acceptee (elle doit etre refusee)"
else
    ok "connexion anonyme refusee"
fi

# 2 et 3 : le compte controle ecoute tout pendant 6 s
mosquitto_sub "${TLS[@]}" -u controle -P "$MDP_CTRL" -t 'camions/#' -v -W 6 > "$temp/recu" 2>"$temp/err" &
ecoute=$!
sleep 1.5
jeton="essai-$(date +%s)"
mosquitto_pub "${TLS[@]}" -u "$DT" -P "$MDP_DT" -t "camions/$DT/position" -q 1 -r \
    -m "{\"v\":1,\"dt\":\"$DT\",\"at\":\"$(date -u +%FT%TZ)\",\"x\":319800,\"y\":913700,\"z\":312,\"seq\":1,\"essai\":\"$jeton\"}" \
    || ko "le DT n'a pas pu publier sa position (compte ou mot de passe ?)"
mosquitto_pub "${TLS[@]}" -u "$DT" -P "$MDP_DT" -t "camions/DT-AUTRE/position" -q 1 \
    -m "{\"usurpation\":\"$jeton\"}" -V mqttv5 2>/dev/null
wait $ecoute 2>/dev/null
grep -q "camions/$DT/position.*$jeton" "$temp/recu" && ok "position du DT recue par le compte controle" \
    || ko "position du DT non recue ($(cat "$temp/err"))"
grep -q "usurpation" "$temp/recu" && ko "un DT a pu publier sur le sujet d'un autre DT" \
    || ok "publication sur le sujet d'un autre DT refusee"

# 4. retain : un nouvel abonne recoit tout de suite la derniere position
mosquitto_sub "${TLS[@]}" -u controle -P "$MDP_CTRL" -t "camions/$DT/position" -C 1 -W 4 > "$temp/retain" 2>/dev/null
grep -q "$jeton" "$temp/retain" && ok "derniere position conservee (retain)" || ko "derniere position non conservee"
# on efface la position de test conservee
mosquitto_pub "${TLS[@]}" -u "$DT" -P "$MDP_DT" -t "camions/$DT/position" -r -n 2>/dev/null

echo
(( echecs == 0 )) && echo "BROKER CONFORME" || echo "$echecs ECHEC(S)"
exit $(( echecs > 0 ))
