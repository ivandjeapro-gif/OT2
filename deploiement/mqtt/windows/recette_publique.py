"""Recette du broker PAR INTERNET (chemin d'une tablette en 4G).

    python recette_publique.py oretracking.<tailnet>.ts.net

Sur le PC du broker, MagicDNS resout le nom vers l'adresse Tailscale
privee (100.x) : on passerait par le reseau prive, pas par Internet. On
resout donc le nom avec un DNS public (1.1.1.1 / 8.8.8.8) et on force
cette adresse publique, en gardant le nom pour TLS (SNI + certificat).
Les mots de passe de recette sont lus dans OreTracking-MQTT-comptes.txt
(Bureau) et passes par l'environnement : jamais affiches.
"""
import os, re, socket, subprocess, sys, runpy

hote = sys.argv[1]
ici = os.path.dirname(os.path.abspath(__file__))

# 1. adresse publique du tunnel : DNS public interroge en HTTPS (DoH), que
#    le resolveur local / MagicDNS ne peut pas intercepter
import json, urllib.request
# adresse publique donnee en 2e argument (ex. lue avec MagicDNS coupe) : DoH saute
ip = sys.argv[2] if len(sys.argv) > 2 else None
for url in (() if ip else ("https://cloudflare-dns.com/dns-query?name=%s&type=A", "https://dns.google/resolve?name=%s&type=A")):
    try:
        req = urllib.request.Request(url % hote, headers={"accept": "application/dns-json"})
        rep = json.load(urllib.request.urlopen(req, timeout=15))
    except Exception:
        continue
    adresses = [r["data"] for r in rep.get("Answer", []) if r.get("type") == 1]
    publiques = [a for a in adresses if not a.startswith("100.")]
    print("DNS public %s : %s" % (url.split("/")[2], ", ".join(adresses) or "aucune adresse"), flush=True)
    if publiques:
        ip = publiques[0]
        break
if not ip:
    print("ECHEC  pas encore d'adresse publique pour ce nom (propagation du Funnel : jusqu'a 10 min)")
    sys.exit(1)
print("Adresse publique du tunnel : %s" % ip, flush=True)

_vrai = socket.getaddrinfo
def _force(host, *a, **k):
    return _vrai(ip if host == hote else host, *a, **k)
socket.getaddrinfo = _force

# 2. mots de passe de recette (fichier du Bureau), sans affichage
fichier = os.path.join(os.path.expanduser("~"), "Desktop", "OreTracking-MQTT-comptes.txt")
comptes = {}
try:
    for ligne in open(fichier, encoding="utf-8-sig"):
        m = re.match(r"^(controle|DT-TEST)\s+(\S+)\s*$", ligne.strip())
        if m:
            comptes[m.group(1)] = m.group(2)          # la derniere generation gagne
except OSError:
    print("ECHEC  fichier des comptes introuvable sur le Bureau")
    sys.exit(1)
os.environ["OT_MQTT_CONTROLE"] = comptes.get("controle", "")
os.environ["OT_MQTT_DT_TEST"] = comptes.get("DT-TEST", "")

# 3. meme recette que en local, par le tunnel public (TLS 443)
sys.argv = ["verifier_broker.py", "public", hote]
runpy.run_path(os.path.join(ici, "verifier_broker.py"), run_name="__main__")
