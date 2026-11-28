#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OreTracking V2 - sauvegarde quotidienne du poste de site (28/11).

Copie dans une archive datee :
  - les bases SQLite (passerelle + dashboard), copiees PROPREMENT pendant que les serveurs tournent
    (API de sauvegarde de SQLite : jamais une copie de fichier a moitie ecrite) ;
  - les reglages et petits dossiers de data/ (plans, tracking, tuiles, config, certificat local) ;
  - la configuration MQTT de la passerelle (C:\\ProgramData\\OreTracking).
Les videos du tracking (data/enregistrements) ne sont PAS sauvegardees : trop lourdes, et leur duree de
conservation se regle deja dans Parametres.

Destination : D:\\Sauvegardes\\OreTracking par defaut (autre disque que le projet), ou la variable
d'environnement OT_SAUVEGARDE_DIR, ou le 1er argument. On garde les OT_SAUVEGARDE_JOURS (14) dernieres archives.

Usage :  python poste/sauvegarde.py [dossier_destination]
"""

import datetime
import os
import shutil
import sqlite3
import sys
import tempfile
import zipfile

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(RACINE, "data")
BASES = ("senor_geology.db", "oretracking.db")
FICHIERS = ("server_config.json", "fond_video.json", "Mining Areas.csv", "Trails.geojson.geojson")
DOSSIERS = ("plans", "tracking", "tuiles", "tls", "roads")
PROGRAMDATA = os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "OreTracking")
PREFIXE = "oretracking-"


def destination():
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    return os.environ.get("OT_SAUVEGARDE_DIR", r"D:\Sauvegardes\OreTracking")


def jours_gardes():
    try:
        return max(1, int(os.environ.get("OT_SAUVEGARDE_JOURS", "14")))
    except ValueError:
        return 14


def copier_base(source, cible):
    """Copie coherente d'une base SQLite en service (mode WAL compris)."""
    src = sqlite3.connect("file:%s?mode=ro" % source.replace("\\", "/"), uri=True, timeout=30)
    dst = sqlite3.connect(cible)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


IGNORES = []      # fichiers illisibles (droits reserves au systeme, fichier verrouille) : notes, jamais bloquants


def ajouter_dossier(z, dossier, nom_archive):
    for base, _, fichiers in os.walk(dossier):
        for f in fichiers:
            chemin = os.path.join(base, f)
            try:
                z.write(chemin, os.path.join(nom_archive, os.path.relpath(chemin, dossier)))
            except OSError:
                # 28/11 : ex. mosquitto\dynamic-security.json, lisible par le seul service Mosquitto (SYSTEM)
                IGNORES.append(os.path.relpath(chemin, dossier))


def main():
    dest = destination()
    os.makedirs(dest, exist_ok=True)
    horodatage = datetime.datetime.now().strftime("%Y%m%d-%H%M")
    archive = os.path.join(dest, PREFIXE + horodatage + ".zip")
    tmp = tempfile.mkdtemp(prefix="ot-sauv-")
    contenu = []
    try:
        with zipfile.ZipFile(archive + ".partiel", "w", zipfile.ZIP_DEFLATED) as z:
            for b in BASES:
                p = os.path.join(DATA, b)
                if os.path.isfile(p):
                    copie = os.path.join(tmp, b)
                    copier_base(p, copie)
                    z.write(copie, os.path.join("data", b))
                    contenu.append(b)
            for f in FICHIERS:
                p = os.path.join(DATA, f)
                if os.path.isfile(p):
                    z.write(p, os.path.join("data", f))
                    contenu.append(f)
            for d in DOSSIERS:
                p = os.path.join(DATA, d)
                if os.path.isdir(p):
                    ajouter_dossier(z, p, os.path.join("data", d))
                    contenu.append(d + "/")
            if os.path.isdir(PROGRAMDATA):
                ajouter_dossier(z, PROGRAMDATA, "ProgramData-OreTracking")
                contenu.append("ProgramData/OreTracking")
        os.replace(archive + ".partiel", archive)      # l'archive n'apparait que complete
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if os.path.exists(archive + ".partiel"):
            os.remove(archive + ".partiel")

    # rotation : on garde les N plus recentes
    archives = sorted(f for f in os.listdir(dest) if f.startswith(PREFIXE) and f.endswith(".zip"))
    for vieille in archives[:-jours_gardes()]:
        try:
            os.remove(os.path.join(dest, vieille))
        except OSError:
            pass
    taille = os.path.getsize(archive) / 1e6
    print("%s SAUVEGARDE OK : %s (%.1f Mo) - %s%s" % (
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), archive, taille, ", ".join(contenu),
        (" - non lisibles, ignores : " + ", ".join(IGNORES)) if IGNORES else ""))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001
        print("%s SAUVEGARDE ECHEC : %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), e))
        sys.exit(1)
