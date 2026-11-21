"""Surveillance du suivi MQTT des tablettes DT, depuis le PC de la passerelle.

    python surveiller_tablettes.py verifier   # chaque DT de referentiels peut-il se connecter ?
    python surveiller_tablettes.py ecouter    # en direct : connexions, positions, delais

Aucun mot de passe n'est affiche. Identifiants lus dans :
  - C:\\ProgramData\\OreTracking\\passerelle\\mqtt.json (compte admin + passerelle) ;
  - la base de la passerelle (OFFICE_URL / OFFICE_TOKEN) pour lire referentiels.
Option --base <chemin de senor_geology.db> (defaut : data/ de la copie du lanceur).
"""
import json, os, sqlite3, ssl, sys, threading, time, urllib.parse, urllib.request
from datetime import datetime, timezone
import paho.mqtt.client as mqtt

ICI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ICI)
import dynsec  # noqa: E402

BASE_DEFAUT = r"E:\PROJETS\VISION MINING\DB\OT V2\OT 2 - DT Travel - Top - REFONTE\data\senor_geology.db"


def arg(nom, defaut):
    return sys.argv[sys.argv.index(nom) + 1] if nom in sys.argv else defaut


def referentiels():
    base = arg("--base", BASE_DEFAUT)
    c = sqlite3.connect("file:%s?mode=ro" % urllib.request.pathname2url(base), uri=True)
    p = dict(c.execute("SELECT cle, valeur FROM params WHERE cle IN ('OFFICE_URL','OFFICE_TOKEN')").fetchall())
    c.close()
    url = p["OFFICE_URL"].rstrip("/") + "?action=referentiels&token=" + urllib.parse.quote(p["OFFICE_TOKEN"])
    return json.load(urllib.request.urlopen(url, timeout=60))


def connexion(code, mdp, hote="127.0.0.1", port=1883, tls=False):
    r = {}
    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="verif-%s-%d" % (code, os.getpid()),
                     protocol=mqtt.MQTTv311)
    cl.username_pw_set(code, mdp)
    if tls:
        cl.tls_set(cert_reqs=ssl.CERT_REQUIRED)
    cl.on_connect = lambda a, b, f, rc, p=None: r.update(rc=rc)
    try:
        cl.connect(hote, port, 30); cl.loop_start()
        for _ in range(60):
            if "rc" in r:
                break
            time.sleep(0.1)
    except Exception as e:
        return "erreur reseau : %s" % e
    finally:
        cl.loop_stop(); cl.disconnect()
    return str(r.get("rc", "pas de reponse"))


def verifier():
    d = referentiels()
    print("referentiels : ok=%s | bloc mqtt=%s" % (d.get("ok"), d.get("mqtt")))
    dts = [x for x in d.get("dts") or [] if x.get("role", "DT") == "DT" or str(x.get("code", "")).startswith("DT-")]
    print("%-8s %-6s %-6s %-9s %s" % ("CODE", "ACTIF", "MDP", "BROKER", "DETAIL"))
    for x in dts:
        code, mdp = x.get("code", "?"), x.get("mqtt_mdp")
        if not mdp:
            res = "pas de mot de passe (DT inactif ou compte absent)"
            etat = "-"
        else:
            res = connexion(code, mdp)
            etat = "OK" if res == "Success" else "REFUS"
        print("%-8s %-6s %-6s %-9s %s" % (code, x.get("actif"), "oui" if mdp else "non", etat, "" if etat == "OK" else res))


def ecouter():
    conf = dynsec.lire_conf()
    print("Ecoute de camions/# (compte passerelle) — Ctrl+C pour arreter.\n")
    vus = {}

    def message(cl, u, m):
        maintenant = datetime.now(timezone.utc)
        h = datetime.now().strftime("%H:%M:%S")
        if not m.payload:
            print("%s  %-28s (message conserve efface)" % (h, m.topic)); return
        try:
            d = json.loads(m.payload.decode("utf-8"))
        except ValueError:
            print("%s  %-28s JSON ILLISIBLE : %r" % (h, m.topic, m.payload[:80])); return
        nature = m.topic.split("/")[-1]
        if nature == "position":
            delai = ""
            try:
                at = datetime.fromisoformat(str(d.get("at")).replace("Z", "+00:00"))
                delai = "delai %.1f s" % (maintenant - at).total_seconds()
            except (TypeError, ValueError):
                delai = "CHAMP at ILLISIBLE (%r)" % d.get("at")
            manque = [k for k in ("v", "dt", "at", "seq", "x", "y") if k not in d]
            print("%s  %-28s x=%s y=%s v=%s km/h  %s%s%s" % (
                h, m.topic, d.get("x"), d.get("y"), d.get("vit_kmh", "?"), delai,
                ("  CHAMPS MANQUANTS : %s" % manque) if manque else "", "  (conserve)" if m.retain else ""))
        else:
            print("%s  %-28s %s%s" % (h, m.topic, json.dumps(d, ensure_ascii=False)[:120], "  (conserve)" if m.retain else ""))

    cl = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="surveillance-%d" % os.getpid(), protocol=mqtt.MQTTv311)
    cl.username_pw_set(conf["passerelle"]["identifiant"], conf["passerelle"]["mdp"])
    cl.on_connect = lambda a, b, f, rc, p=None: (print("Connecte au broker :", rc), a.subscribe("camions/#", 1))
    cl.on_message = message
    cl.connect(conf.get("hote", "127.0.0.1"), int(conf.get("port", 1883)), 30)
    cl.loop_start()
    duree = float(arg("--duree", "0") or 0)
    t0 = time.time()
    try:
        while not duree or time.time() - t0 < duree:
            adm = dynsec.Dynsec(conf)
            try:
                connectes = sorted(c["username"] for c in adm.lister()
                                   if c.get("connections") and c["username"] not in conf.get("comptes_techniques", []))
            finally:
                adm.fermer()
            cle = ",".join(connectes)
            if vus.get("c") != cle:
                print("%s  ** tablettes connectees au broker : %s" % (datetime.now().strftime("%H:%M:%S"), cle or "AUCUNE"))
                vus["c"] = cle
            time.sleep(5)
    except KeyboardInterrupt:
        pass
    finally:
        cl.loop_stop(); cl.disconnect()


if __name__ == "__main__":
    {"verifier": verifier, "ecouter": ecouter}.get(sys.argv[1] if len(sys.argv) > 1 else "verifier", verifier)()
