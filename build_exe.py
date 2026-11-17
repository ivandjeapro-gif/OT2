#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Construit OreTracking.exe (lanceur du poste de site).

    python build_exe.py

Resultat : dist/OreTracking.exe

IMPORTANT : l'exe ne contient QUE le code. Les dossiers html/, js/, css/ et
data/ restent a cote de lui et sont lus depuis son propre dossier. C'est
voulu : la base et les plans doivent survivre a une mise a jour de l'exe,
et rester modifiables sans reconstruire.
"""
import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
NOM = "OreTracking"

# Le lanceur charge les deux serveurs depuis les .py places A COTE de l'exe,
# pas depuis le bundle. On n'embarque donc pas leur code, mais il FAUT
# embarquer les modules de la bibliotheque standard qu'ils importent :
# PyInstaller ne peut pas les deviner en analysant le seul lanceur.
MODULES_CACHES = [
    "sqlite3", "csv", "hashlib", "hmac", "json", "math", "secrets",
    "threading", "time", "uuid", "datetime", "base64", "re", "ssl",
    "ipaddress", "http.server", "socketserver",
    "urllib.request", "urllib.error", "urllib.parse",
    # `cryptography` est importe AU NIVEAU MODULE par server/server.py
    # (certificat TLS auto-signe) : sans lui l'exe echoue des le demarrage.
    "cryptography",
    "cryptography.x509",
    "cryptography.x509.oid",
    "cryptography.hazmat.primitives.hashes",
    "cryptography.hazmat.primitives.serialization",
    "cryptography.hazmat.primitives.asymmetric.rsa",
    "cryptography.hazmat.primitives.asymmetric.padding",
]

# Paquets a embarquer en entier (sous-modules et binaires compris).
PAQUETS_COMPLETS = ["cryptography"]

OPTIONNELS = ["serial", "paho.mqtt.client"]  # absents = fonctions desactivees


def disponible(module):
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def main():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller absent. Installez-le :  pip install pyinstaller")
        return 1

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--console",
        "--name", NOM,
        "--distpath", os.path.join(BASE, "dist"),
        "--workpath", os.path.join(BASE, "build"),
        "--specpath", os.path.join(BASE, "build"),
        "--noconfirm",
        # Le lanceur importe les serveurs par chemin : sans ces imports
        # caches, l'exe se construirait sans eux et echouerait au demarrage.
        "--paths", BASE,
        "--paths", os.path.join(BASE, "server"),
    ]
    for m in MODULES_CACHES:
        cmd += ["--hidden-import", m]
    for p in PAQUETS_COMPLETS:
        cmd += ["--collect-all", p]
    for m in OPTIONNELS:
        if disponible(m.split(".")[0]):
            cmd += ["--hidden-import", m]
        else:
            # Evite un avertissement bruyant et un echec de resolution.
            cmd += ["--exclude-module", m.split(".")[0]]

    icone = os.path.join(BASE, "assets", "excavator.png")
    if os.path.isfile(icone.replace(".png", ".ico")):
        cmd += ["--icon", icone.replace(".png", ".ico")]

    cmd.append(os.path.join(BASE, "lanceur.py"))

    print("Construction en cours...\n" + " ".join(cmd) + "\n")
    r = subprocess.run(cmd, cwd=BASE)
    if r.returncode != 0:
        print("\nEchec de la construction.")
        return r.returncode

    exe = os.path.join(BASE, "dist", NOM + ".exe")
    if not os.path.isfile(exe):
        print("\nExecutable introuvable apres construction : %s" % exe)
        return 1

    taille = os.path.getsize(exe) / (1024 * 1024)
    print("\n" + "=" * 58)
    print(" OK : %s  (%.1f Mo)" % (exe, taille))
    print("=" * 58)
    print(" Copiez l'exe A LA RACINE du projet, a cote de html/ et data/,")
    print(" puis double-cliquez dessus.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
