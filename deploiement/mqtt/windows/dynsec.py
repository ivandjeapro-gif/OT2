"""Administration du module « Dynamic Security » du broker OreTracking, en
MQTT (sujet $CONTROL/dynamic-security/v1) : aucun appel en ligne de commande,
donc aucun probleme de guillemets dans les mots de passe.

Utilise par la passerelle (comptes des DT d'apres la flotte) et par la mise
en place du broker :

    python dynsec.py mettre-en-place     # roles + comptes techniques (relancable)
    python dynsec.py lister              # comptes (sans mots de passe)

Identifiants lus dans C:\\ProgramData\\OreTracking\\passerelle\\mqtt.json
(lisible par l'utilisateur de la passerelle). Rien de secret n'est affiche.
"""
import json, os, sys, threading, time, uuid
import paho.mqtt.client as mqtt

FICHIER = os.environ.get("OT_MQTT_CONF", r"C:\ProgramData\OreTracking\passerelle\mqtt.json")
SUJET = "$CONTROL/dynamic-security/v1"

ROLES = {
    # chaque DT : ses propres sujets (%u = identifiant = code du DT)
    "dt": [("publishClientSend", "camions/%u/position"), ("publishClientSend", "camions/%u/evenement"),
           ("publishClientSend", "camions/%u/connexion"), ("subscribePattern", "camions/%u/mission"),
           ("subscribePattern", "camions/%u/alerte")],
    "passerelle": [("subscribePattern", "camions/#"), ("publishClientSend", "camions/+/mission"),
                   ("publishClientSend", "camions/+/alerte")],
    "controle": [("subscribePattern", "camions/#")],
}


def lire_conf(fichier=FICHIER):
    with open(fichier, encoding="utf-8-sig") as f:
        return json.load(f)


class Dynsec:
    """Client d'administration : envoie des commandes, attend leurs reponses."""

    def __init__(self, conf=None, timeout=8):
        self.conf = conf or lire_conf()
        self.timeout = timeout
        self._rep = {}
        self._ev = threading.Event()
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ot-dynsec-" + uuid.uuid4().hex[:6],
                        protocol=mqtt.MQTTv311)
        c.username_pw_set(self.conf["admin"]["identifiant"], self.conf["admin"]["mdp"])
        self._rc = None
        c.on_connect = lambda cl, u, f, rc, p=None: self._connecte(rc)
        c.on_message = self._message
        self.c = c
        c.connect(self.conf.get("hote", "127.0.0.1"), int(self.conf.get("port", 1883)), keepalive=30)
        c.loop_start()
        t0 = time.time()
        while self._rc is None and time.time() - t0 < timeout:
            time.sleep(0.05)
        if self._rc is None or self._rc.is_failure:
            self.fermer()
            raise RuntimeError("administration du broker refusee : %s" % self._rc)

    def _connecte(self, rc):
        self._rc = rc
        if not rc.is_failure:
            self.c.subscribe(SUJET + "/response", qos=1)

    def _message(self, cl, u, m):
        try:
            for r in json.loads(m.payload.decode("utf-8")).get("responses", []):
                self._rep[r.get("correlationData")] = r
        except ValueError:
            pass
        self._ev.set()

    def commande(self, nom, **champs):
        """Une commande dynsec. Rend (ok, reponse). Erreurs « existe deja » = ok."""
        cid = uuid.uuid4().hex
        corps = dict(command=nom, correlationData=cid, **champs)
        time.sleep(0.05)
        self.c.publish(SUJET, json.dumps({"commands": [corps]}), qos=1)
        t0 = time.time()
        while cid not in self._rep and time.time() - t0 < self.timeout:
            self._ev.wait(0.2); self._ev.clear()
        r = self._rep.pop(cid, None)
        if r is None:
            return False, {"error": "pas de reponse du broker"}
        err = r.get("error")
        return (not err or "already exists" in str(err)), r

    def fermer(self):
        try:
            self.c.loop_stop(); self.c.disconnect()
        except Exception:
            pass

    # ---- operations de haut niveau ----
    def mettre_en_place(self, comptes):
        """Droits par defaut, roles, comptes techniques {identifiant: (mdp, role)}."""
        journal = []
        for acltype, allow in (("publishClientSend", False), ("publishClientReceive", True),
                               ("subscribe", False), ("unsubscribe", True)):
            ok, r = self.commande("setDefaultACLAccess", acls=[{"acltype": acltype, "allow": allow}])
            journal.append(("defaut %s" % acltype, ok, r.get("error", "")))
        for role, acls in ROLES.items():
            ok, r = self.commande("createRole", rolename=role)
            journal.append(("role " + role, ok, r.get("error", "")))
            for acltype, topic in acls:
                ok, r = self.commande("addRoleACL", rolename=role, acltype=acltype, topic=topic, allow=True)
                journal.append(("  acl %s %s" % (acltype, topic), ok, r.get("error", "")))
        for ident, (mdp, role) in comptes.items():
            journal.append(("compte " + ident,) + self.compte(ident, mdp, True, role)[:2])
        # compte de demonstration cree par le broker avec sa configuration initiale
        ok, r = self.commande("deleteClient", username="democlient")
        journal.append(("suppression du compte de demonstration democlient", True, ""))
        return journal

    def compte(self, ident, mdp, actif=True, role="dt"):
        """Cree ou met a jour un compte (mot de passe, role, actif/inactif), a chaud."""
        ok, r = self.commande("createClient", username=ident, password=mdp, roles=[{"rolename": role}])
        if ok and "already exists" in str(r.get("error", "")):
            self.commande("setClientPassword", username=ident, password=mdp)
            self.commande("addClientRole", username=ident, rolename=role)
            etat = "modifie"
        else:
            etat = "cree" if ok else "erreur"
        self.commande("enableClient" if actif else "disableClient", username=ident)
        return ok, etat, r.get("error", "")

    def supprimer(self, ident):
        return self.commande("deleteClient", username=ident)[0]

    def lister(self):
        ok, r = self.commande("listClients", verbose=True)
        return ((r.get("data") or {}).get("clients") or []) if ok else []


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else "lister"
    conf = lire_conf()
    d = Dynsec(conf)
    try:
        if action == "mettre-en-place":
            mdps = {}
            for ident in ("passerelle", "controle", "DT-TEST"):
                cle = "OT_MQTT_" + ident.upper().replace("-", "_")
                mdps[ident] = os.environ.get(cle) or (conf.get(ident, {}) or {}).get("mdp", "")
            comptes = {"passerelle": (mdps["passerelle"] or conf["passerelle"]["mdp"], "passerelle")}
            if mdps["controle"]:
                comptes["controle"] = (mdps["controle"], "controle")
            if mdps["DT-TEST"]:
                comptes["DT-TEST"] = (mdps["DT-TEST"], "dt")
            for nom, ok, err in d.mettre_en_place(comptes):
                print(("OK     " if ok else "ECHEC  ") + nom + (("  -> " + err) if err and not ok else ""))
        for c in d.lister():
            roles = ",".join(r.get("rolename", "") for r in c.get("roles", []))
            print("compte %-12s roles=%-12s %s" % (c.get("username"), roles, "DESACTIVE" if c.get("disabled") else "actif"))
    finally:
        d.fermer()
