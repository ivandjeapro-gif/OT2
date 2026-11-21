"""Recette du broker OreTracking (CDC MQTT § 7, R4) — paho-mqtt 2.x.

    python verifier_broker.py local          # ports 1883 / 1884 de ce PC
    python verifier_broker.py public <hote>  # par le tunnel : <hote>:443 en TLS

Mots de passe lus dans l'environnement (jamais en argument ni a l'ecran) :
    OT_MQTT_DT_TEST    compte DT-TEST
    OT_MQTT_CONTROLE   compte controle
N'affiche aucun mot de passe.
"""
import json, os, socket, ssl, sys, threading, time, uuid
import paho.mqtt.client as mqtt

MODE = sys.argv[1] if len(sys.argv) > 1 else "local"
HOTE = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
MDP_DT = os.environ.get("OT_MQTT_DT_TEST", "")
MDP_CTRL = os.environ.get("OT_MQTT_CONTROLE", "")
echecs = 0


def verif(nom, ok, detail=""):
    global echecs
    print(("OK     " if ok else "ECHEC  ") + nom + (("  -> " + detail) if detail else ""), flush=True)
    echecs += 0 if ok else 1


def client(user=None, mdp=None, port=1883, tls=False, hote="127.0.0.1"):
    c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="recette-" + uuid.uuid4().hex[:8],
                    protocol=mqtt.MQTTv5)
    if user:
        c.username_pw_set(user, mdp)
    if tls:
        c.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
    etat = {"rc": None, "msgs": []}
    c.on_connect = lambda cl, u, f, rc, p=None: etat.update(rc=rc)
    c.on_message = lambda cl, u, m: etat["msgs"].append((m.topic, m.payload.decode("utf-8", "replace"), m.retain))
    c._etat = etat
    try:
        c.connect(hote, port, keepalive=30)
    except (OSError, socket.error) as e:
        etat["rc"] = "erreur reseau : %s" % e
        return c
    c.loop_start()
    for _ in range(100):
        if etat["rc"] is not None:
            break
        time.sleep(0.1)
    return c


def connecte(c):
    rc = c._etat["rc"]
    return rc is not None and not isinstance(rc, str) and not rc.is_failure


def fin(*cs):
    for c in cs:
        try:
            c.loop_stop(); c.disconnect()
        except Exception:
            pass


if MODE == "public":
    port, tls, hote = 443, True, HOTE
else:
    port, tls, hote = 1883, False, "127.0.0.1"
jeton = "essai-%d" % time.time()
pos = lambda dt: json.dumps({"v": 1, "dt": dt, "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                             "seq": 1, "x": 319800.0, "y": 913700.0, "z": 312.0, "essai": jeton})

# 1. sans compte : refuse
a = client(port=port, tls=tls, hote=hote)
rc = a._etat["rc"]
# refus = reponse du broker ; une erreur reseau ne prouve rien
verif("connexion sans compte refusee (%s:%d)" % (hote, port),
      rc is not None and not isinstance(rc, str) and rc.is_failure, str(rc))
fin(a)

# 2. controle ecoute, DT-TEST publie
ctrl = client("controle", MDP_CTRL, port, tls, hote)
verif("compte controle connecte", connecte(ctrl), str(ctrl._etat["rc"]))
ctrl.subscribe("camions/#", qos=1)
dt = client("DT-TEST", MDP_DT, port, tls, hote)
verif("compte DT-TEST connecte", connecte(dt), str(dt._etat["rc"]))
if not (connecte(dt) and connecte(ctrl)):
    print("Arret : connexion impossible, la suite n'aurait pas de sens.")
    sys.exit(1)
time.sleep(1.0)
t0 = time.time()
dt.publish("camions/DT-TEST/position", pos("DT-TEST"), qos=1, retain=True).wait_for_publish(5)
dt.publish("camions/DT-AUTRE/position", json.dumps({"usurpation": jeton}), qos=1).wait_for_publish(5)
time.sleep(2.5)
recus = ctrl._etat["msgs"]
propre = [m for m in recus if m[0] == "camions/DT-TEST/position" and jeton in m[1]]
verif("position du DT recue par controle", bool(propre))
verif("publication pour un autre DT refusee", not any("usurpation" in m[1] for m in recus))
fin(ctrl)

# 3. retain : un nouvel abonne recoit la derniere position
nouv = client("controle", MDP_CTRL, port, tls, hote)
nouv.subscribe("camions/DT-TEST/position", qos=1)
time.sleep(2.0)
verif("derniere position conservee (retain)", any(jeton in m[1] and m[2] for m in nouv._etat["msgs"]))
fin(nouv)

# 4. latence aller-retour (publication -> reception)
ctrl2 = client("controle", MDP_CTRL, port, tls, hote)
ctrl2.subscribe("camions/DT-TEST/position", qos=0)
time.sleep(1.0)
delais = []
for i in range(5):
    marque = "lat-%d-%d" % (i, time.time() * 1000)
    ctrl2._etat["msgs"].clear()
    t = time.time()
    dt.publish("camions/DT-TEST/position", json.dumps({"v": 1, "dt": "DT-TEST", "essai": marque}), qos=0)
    while time.time() - t < 3 and not any(marque in m[1] for m in ctrl2._etat["msgs"]):
        time.sleep(0.005)
    if any(marque in m[1] for m in ctrl2._etat["msgs"]):
        delais.append((time.time() - t) * 1000)
verif("latence publication -> reception", len(delais) == 5,
      "%d/5 recus, mediane %.0f ms" % (len(delais), sorted(delais)[len(delais) // 2]) if delais else "aucun")
# efface la position de test conservee
dt.publish("camions/DT-TEST/position", b"", qos=1, retain=True).wait_for_publish(5)
fin(ctrl2, dt)

# 5. (local) port 1884 de la passerelle : lecture sans compte, ecriture de position refusee
MDP_GW = os.environ.get("OT_MQTT_PASSERELLE", "")
if MODE == "local" and MDP_GW:
    gw = client("passerelle", MDP_GW, 1883)
    verif("compte passerelle connecte", connecte(gw), str(gw._etat["rc"]))
    gw.subscribe("camions/#", qos=1)
    time.sleep(0.8)
    dt2 = client("DT-TEST", MDP_DT, 1883)
    dt2.publish("camions/DT-TEST/position", pos("DT-TEST"), qos=1).wait_for_publish(5)
    gw.publish("camions/DT-TEST/position", json.dumps({"faux": jeton}), qos=1).wait_for_publish(5)
    time.sleep(2.0)
    verif("passerelle recoit la position du DT", any(jeton in m[1] and "essai" in m[1] for m in gw._etat["msgs"]))
    verif("passerelle ne peut pas publier une position", not any("faux" in m[1] for m in gw._etat["msgs"]))
    dt2.publish("camions/DT-TEST/position", b"", qos=1, retain=True).wait_for_publish(5)
    fin(gw, dt2)

print("\n" + ("BROKER CONFORME" if not echecs else "%d ECHEC(S)" % echecs), flush=True)
sys.exit(1 if echecs else 0)
