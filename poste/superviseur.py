#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OreTracking V2 - superviseur des serveurs du poste (28/11).

Lance lanceur.py (dashboard :8100 + passerelle :8101) et le RELANCE s'il s'arrete, quelle qu'en soit la raison
(plantage, Ctrl+C recu par la console, fermeture de fenetre, poste\\relancer.cmd). Le 28/11, les serveurs se sont
arretes sur un Ctrl+C de console et la tache planifiee ne les a pas relances : ce superviseur comble ce trou.

  - lanceur.py tourne SANS console (CREATE_NO_WINDOW) : aucun Ctrl+C ne peut plus l'atteindre ;
  - le superviseur ignore lui-meme Ctrl+C ;
  - une seule instance (verrou sur 127.0.0.1:8199) ;
  - journal : data\\lanceur.log (au-dela de 20 Mo -> data\\lanceur.old.log) ;
  - arret VOLONTAIRE : creer le fichier data\\arret_serveurs (poste\\arreter.cmd) ; il est efface au demarrage suivant.

Usage (tache planifiee "OreTracking - serveurs", via poste\\demarrer.cmd) :  python poste/superviseur.py
"""

import datetime
import os
import signal
import socket
import subprocess
import sys
import time

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(RACINE, "data")
JOURNAL = os.path.join(DATA, "lanceur.log")
DRAPEAU_ARRET = os.path.join(DATA, "arret_serveurs")
PORT_VERROU = 8199
TAILLE_MAX = 20_000_000
PAUSE_S = 10            # attente avant relance
PAUSE_LONGUE_S = 60     # si le lanceur s'arrete tout de suite plusieurs fois (ports pris...)


def ecrire(msg):
    ligne = "===== %s %s =====\n" % (datetime.datetime.now().strftime("%d/%m/%Y %H:%M:%S"), msg)
    try:
        with open(JOURNAL, "a", encoding="utf-8") as f:
            f.write(ligne)
    except OSError:
        pass


def tourner_journal():
    try:
        if os.path.getsize(JOURNAL) > TAILLE_MAX:
            os.replace(JOURNAL, os.path.join(DATA, "lanceur.old.log"))
    except OSError:
        pass


def main():
    for sig in ("SIGINT", "SIGBREAK"):
        if hasattr(signal, sig):
            signal.signal(getattr(signal, sig), signal.SIG_IGN)
    verrou = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        verrou.bind(("127.0.0.1", PORT_VERROU))
    except OSError:
        print("Superviseur deja actif : rien a faire.")
        return 0
    os.makedirs(DATA, exist_ok=True)
    try:
        os.remove(DRAPEAU_ARRET)            # un arret volontaire precedent ne bloque pas ce demarrage
    except OSError:
        pass
    ecrire("superviseur demarre (pid %d)" % os.getpid())
    rapides = 0
    drapeaux = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    while True:
        if os.path.exists(DRAPEAU_ARRET):
            ecrire("arret volontaire demande : superviseur arrete")
            return 0
        tourner_journal()
        ecrire("demarrage des serveurs (lanceur.py)")
        debut = time.time()
        with open(JOURNAL, "a", encoding="utf-8", errors="replace") as sortie:
            p = subprocess.Popen([sys.executable, "-u", os.path.join(RACINE, "lanceur.py")], cwd=RACINE,
                                 stdin=subprocess.DEVNULL, stdout=sortie, stderr=subprocess.STDOUT,
                                 creationflags=drapeaux)
            code = p.wait()
        duree = time.time() - debut
        if os.path.exists(DRAPEAU_ARRET):
            ecrire("serveurs arretes volontairement (code %s)" % code)
            return 0
        rapides = rapides + 1 if duree < 30 else 0
        pause = PAUSE_LONGUE_S if rapides >= 3 else PAUSE_S
        ecrire("serveurs arretes (code %s apres %d s) : relance dans %d s" % (code, int(duree), pause))
        time.sleep(pause)


if __name__ == "__main__":
    sys.exit(main())
