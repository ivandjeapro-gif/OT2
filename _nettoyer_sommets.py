# -*- coding: utf-8 -*-
"""Retire le point fictif (0,0) laisse par l'ancien parseur DXF.

Prudence : on ne supprime (0,0) QUE s'il est en tete ET que le reste du
polygone en est tres eloigne (coordonnees UTM). Un polygone reellement
situe pres de l'origine n'est pas touche.
"""
import json
import sqlite3

SEUIL_ELOIGNEMENT = 1000.0  # m : au-dela, (0,0) ne peut pas etre un vrai sommet

conn = sqlite3.connect("data/senor_geology.db")
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT o.id, o.nom, o.sommets, b.numero FROM oreblocs o "
    "LEFT JOIN blasts b ON b.id = o.blast_id WHERE o.actif=1").fetchall()

corriges = ignores = 0
for r in rows:
    try:
        s = json.loads(r["sommets"] or "[]")
    except Exception:
        continue
    if not s or not (s[0][0] == 0 and s[0][1] == 0):
        continue
    reste = s[1:]
    if not reste:
        continue
    # le reste du polygone est-il loin de l'origine ?
    dmin = min((p[0] ** 2 + p[1] ** 2) ** 0.5 for p in reste)
    if dmin < SEUIL_ELOIGNEMENT:
        print("  IGNORE %-36s : polygone proche de l'origine, (0,0) peut etre reel"
              % r["nom"][:36])
        ignores += 1
        continue
    conn.execute("UPDATE oreblocs SET sommets=? WHERE id=?",
                 (json.dumps(reste), r["id"]))
    print("  corrige %-36s %d -> %d sommets  (blast %s)"
          % (r["nom"][:36], len(s), len(reste), r["numero"]))
    corriges += 1

conn.commit()
print()
print("%d orebloc(s) corrige(s), %d ignore(s)" % (corriges, ignores))

# controle : plus aucun (0,0) parasite
restants = 0
for r in conn.execute("SELECT nom, sommets FROM oreblocs WHERE actif=1"):
    try:
        s = json.loads(r["sommets"] or "[]")
    except Exception:
        continue
    if s and s[0][0] == 0 and s[0][1] == 0:
        restants += 1
print("points (0,0) en tete restants :", restants)
conn.close()
