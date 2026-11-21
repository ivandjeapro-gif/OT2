#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OreTracking V2 - lanceur du poste de site.

Demarre en un seul processus :
  - le SaaS            (pages + API locale)      http://localhost:8100
  - la passerelle      (Senor Geology, terrain)  http://localhost:8101

Les deux serveurs sont des ThreadingHTTPServer : on les lance chacun dans
un thread demon, ce qui evite deux fenetres et un double clic.

Construction de l'executable :
    python build_exe.py

L'exe doit rester A COTE des dossiers html/, js/, css/, data/ : c'est son
propre dossier qui sert de racine (voir BASE_DIR dans les deux serveurs).
"""

import importlib.util
import os
import socket
import sys
import threading
import time
import webbrowser

PORT_SAAS = 8100
PORT_PASSERELLE = 8101


def racine():
    """Dossier de travail : celui de l'exe une fois fige, du script sinon."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


BASE = racine()


def charger_module(nom, chemin_relatif):
    """Charge un serveur depuis son fichier .py, a cote du lanceur.

    Volontairement le MEME chemin de code en mode fige et en mode script :
    `server/` n'a pas d'__init__.py, donc `import server` serait ambigu dans
    l'exe (dossier vu comme namespace package vs module server.py). Charger
    par chemin supprime l'ambiguite.

    Consequence assumee : les deux .py restent a cote de l'exe, comme html/
    et data/. L'exe apporte l'interpreteur, pas le code metier - ce qui
    permet de corriger un serveur sans tout reconstruire.
    """
    chemin = os.path.join(BASE, chemin_relatif)
    if not os.path.isfile(chemin):
        raise FileNotFoundError(chemin)
    spec = importlib.util.spec_from_file_location(nom, chemin)
    module = importlib.util.module_from_spec(spec)
    sys.modules[nom] = module
    spec.loader.exec_module(module)
    return module


def port_occupe(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex(("127.0.0.1", port)) == 0


def attendre_port(port, limite=25.0):
    """Attend qu'un port reponde. Rend True des qu'il accepte une connexion."""
    debut = time.time()
    while time.time() - debut < limite:
        if port_occupe(port):
            return True
        time.sleep(0.3)
    return False


def demarrer(nom_affiche, module, erreurs):
    """Lance le main() d'un serveur ; une exception est remontee au lieu de
    mourir en silence dans son thread."""
    try:
        module.main()
    except Exception as e:  # noqa: BLE001 - on veut tout rapporter
        erreurs.append((nom_affiche, e))


def main():
    print("=" * 58)
    print(" OreTracking V2 - poste de site")
    print("=" * 58)
    print(" Dossier : %s" % BASE)
    print()

    # Un dossier mal place est l'erreur la plus probable a l'installation :
    # on la nomme tout de suite plutot que de laisser des 404 inexpliques.
    manquants = [d for d in ("html", "js", "css") if not os.path.isdir(os.path.join(BASE, d))]
    manquants += [f for f in ("Server_Senor_Geology.py", os.path.join("server", "server.py"))
                  if not os.path.isfile(os.path.join(BASE, f))]
    if manquants:
        print(" ERREUR : element(s) introuvable(s) : %s" % ", ".join(manquants))
        print(" Placez l'executable dans le dossier du projet, a cote de html/")
        print(" et des deux fichiers serveur.")
        input("\n Entree pour fermer...")
        return 1

    occupes = [p for p in (PORT_SAAS, PORT_PASSERELLE) if port_occupe(p)]
    if occupes:
        print(" ERREUR : port(s) deja utilise(s) : %s"
              % ", ".join(str(p) for p in occupes))
        print(" Un autre lanceur tourne deja, ou un serveur est reste ouvert.")
        input("\n Entree pour fermer...")
        return 1

    erreurs = []
    try:
        saas = charger_module("server", os.path.join("server", "server.py"))
        passerelle = charger_module("Server_Senor_Geology", "Server_Senor_Geology.py")
    except Exception as e:  # noqa: BLE001
        print(" ERREUR au chargement des serveurs : %s" % e)
        input("\n Entree pour fermer...")
        return 1

    for nom, module in (("SaaS", saas), ("Passerelle", passerelle)):
        threading.Thread(target=demarrer, args=(nom, module, erreurs),
                         daemon=True).start()

    ok_saas = attendre_port(PORT_SAAS)
    ok_pass = attendre_port(PORT_PASSERELLE)

    print(" SaaS        http://localhost:%-6d %s" % (PORT_SAAS, "OK" if ok_saas else "ECHEC"))
    print(" Passerelle  http://localhost:%-6d %s" % (PORT_PASSERELLE, "OK" if ok_pass else "ECHEC"))
    for nom, e in erreurs:
        print(" ERREUR %s : %s" % (nom, e))
    print()

    if not ok_saas:
        print(" Le SaaS n'a pas demarre : impossible d'ouvrir l'interface.")
        input("\n Entree pour fermer...")
        return 1

    # on ouvre la page de CONNEXION : session deja ouverte -> elle renvoie directement sur Home
    url = "http://localhost:%d/html/connexion.html" % PORT_SAAS
    print(" Ouverture de %s" % url)
    try:
        webbrowser.open(url)
    except Exception:
        print(" (ouvrez l'adresse manuellement)")

    if not ok_pass:
        print()
        print(" ATTENTION : la passerelle n'a pas demarre. Les pages")
        print(" s'afficheront, mais sans blasts ni positions d'engins.")

    print()
    print(" Serveurs en service. Fermez cette fenetre ou Ctrl+C pour arreter.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n Arret demande.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
