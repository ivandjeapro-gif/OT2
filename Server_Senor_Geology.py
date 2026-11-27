#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Server_Senor_Geology - Serveur de gestion des missions de voyages DT (OreTracking V2)
---------------------------------------------------------------------------
Dossier : E:\PROJETS\VISION MINING\DB\OT V2\OT 2 - DT Travel - Top
Port par defaut : 8101 (8100 est occupe par le serveur SAAS principal)

Fonctions :
  - Gestion des oreblocs (polygones x,y depuis CSV/DXF)
  - Position temps reel des engins (excavatrices OMEX/WEX, camions DT)
  - Affectation automatique des missions (destination + Dumping Area)
  - Chemin a suivre (travels roads)
  - Surveillance de deviation et alertes (DT + Serveur Office)
  - Notification fin de mission
  - Stockage SQLite + API REST + page HTML dediee

Usage:
    python Server_Senor_Geology.py [port]
"""

import base64
import csv
import hashlib
import hmac
import io
import json
import math
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, quote
import urllib.request
import urllib.error

try:
    import serial  # pyserial ; optionnel, voir ecouteur_xbee()
except ImportError:
    serial = None

try:
    import paho.mqtt.client as mqtt  # paho-mqtt ; optionnel, voir mqtt_publier()
except ImportError:
    mqtt = None

# ------------------------------------------------------------- configuration --
# En executable PyInstaller, __file__ pointe dans le dossier TEMPORAIRE
# d'extraction : data/, html/ et les bases seraient cherches au mauvais
# endroit (et perdus a chaque arret). On vise le dossier de l'executable.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "senor_geology.db")


def _port_demande(defaut):
    """Port depuis la ligne de commande, en ignorant ce qui n'est pas un
    entier : sous le lanceur, sys.argv appartient au lanceur, pas au serveur."""
    if len(sys.argv) > 1:
        try:
            return int(sys.argv[1])
        except (TypeError, ValueError):
            pass
    return defaut


PORT = _port_demande(8101)
HOST = "0.0.0.0"

# Liaison radio XBee (montee des positions DT/Rover) : desactivee tant que
# XBEE_PORT n'est pas renseigne (pas de radio branchee en dev). A activer sur
# site en fixant la variable d'environnement, ex. XBEE_PORT=COM5 (Windows) ou
# XBEE_PORT=/dev/ttyUSB0 (Linux).
# 21/11 : ces deux reglages se font depuis la console (Settings > GNSS > Radio de la passerelle) ; la variable
# d'environnement n'est qu'un repli tant que la console n'a rien choisi.
XBEE_BAUDS = (9600, 19200, 38400, 57600, 115200, 230400)

# Descente des ordres par MQTT (4G) : chaque DT ecoute son propre canal
# camions/<dt_code>/mission (+ camions/<dt_code>/alerte pour les alertes et
# la voix off). Desactivee tant que MQTT_HOST n'est pas renseigne.
MQTT_HOST = os.environ.get("MQTT_HOST", "")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")

# Liaison reelle vers le serveur Office (server_OT, Google Apps Script).
# OFFICE_URL = URL de deploiement Web App (".../exec"). OFFICE_TOKEN = jeton
# partage (CONFIG.TOKEN cote server_OT). Les deux sont vides par defaut
# (liaison desactivee) : a fixer via variables d'environnement sur le site,
# jamais en dur dans le code (le jeton est un secret).
# Ces quatre reglages vivaient uniquement dans l'environnement : invisibles
# pour les collegues, perdus a chaque poste, et impossibles a changer sans
# relancer le service. Ils sont desormais persistes dans la table `params`,
# l'environnement restant prioritaire pour surcharger un poste ponctuellement.
_PARAMS_RELAIS = ("OFFICE_URL", "OFFICE_TOKEN", "SG_URL", "SG_TOKEN",
                  "SG_INTERVALLE_S", "MISSIONS_SOURCE")
_params_cache = {}
_params_verrou = threading.Lock()


def param_lire(cle, defaut=""):
    """Valeur d'un reglage : environnement d'abord, puis base, puis defaut."""
    env = os.environ.get(cle, "")
    if env:
        return env
    return param_lire_base(cle, defaut)


def param_lire_base(cle, defaut=""):
    """Valeur d'un reglage dans la base (console), sans regarder l'environnement."""
    with _params_verrou:
        if cle in _params_cache:
            return _params_cache[cle] or defaut
    valeur = ""
    try:
        c = sqlite3.connect(DB_PATH)
        ligne = c.execute("SELECT valeur FROM params WHERE cle=?", (cle,)).fetchone()
        c.close()
        valeur = (ligne[0] if ligne else "") or ""
    except Exception:
        valeur = ""          # base pas encore creee : on vit sans
    with _params_verrou:
        _params_cache[cle] = valeur
    return valeur or defaut


def param_ecrire(conn, cle, valeur):
    conn.execute(
        "INSERT INTO params (cle, valeur, maj_le) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur, maj_le=excluded.maj_le",
        (cle, str(valeur or "")))
    conn.commit()
    with _params_verrou:
        _params_cache[cle] = str(valeur or "")


def office_conf():
    """URL + jeton de Serveur_OT. Vide = canal desactive."""
    return param_lire("OFFICE_URL", "").rstrip("/"), param_lire("OFFICE_TOKEN", "")


def sg_conf():
    """URL + jeton + intervalle du relais vers Serveur_SG."""
    try:
        intervalle = int(param_lire("SG_INTERVALLE_S", "60") or 60)
    except ValueError:
        intervalle = 60
    return (param_lire("SG_URL", "").rstrip("/"), param_lire("SG_TOKEN", ""),
            max(5, intervalle))

def empreinte_jeton(jeton):
    """6 caracteres derives du jeton (SHA-256) : permet de comparer deux
    jetons a l'ecran sans jamais afficher leur valeur."""
    j = str(jeton or "").strip()
    return hashlib.sha256(j.encode("utf-8")).hexdigest()[:6] if j else ""


def sg_sync_ref_fond(motif=""):
    """26/11 : synchro du referentiel de SG en arriere-plan apres un changement de blast (Dig Plan). Laisse 20 s a
    Serveur_OT pour finir d'ecrire le blast, puis demande a SG de le recopier. Ne leve jamais."""
    try:
        time.sleep(20)
        ok, d, err = sg_sync_ref()
        print("SG: referentiel resynchronise (%s) : %s" % (motif, ("%s oreblocs" % d.get("oreblocs")) if ok else err))
    except Exception as e:
        print("SG: resynchronisation du referentiel impossible (%s) : %s" % (motif, e))


def sg_sync_ref():
    """Lance ?action=sync_ref sur Serveur_SG : il recopie depuis Serveur_OT
    les oreblocs, zones de deversement, teneurs et pistes (onglets ref_*).
    Appele par la page Parametres juste apres « Pousser le referentiel » :
    sans cela SG gardait l'ancien referentiel jusqu'a une synchro manuelle.
    Le jeton SG reste sur ce poste. Rend (ok, reponse de SG ou {}, erreur)."""
    sg_url, sg_token, _ = sg_conf()
    if not sg_url or not sg_token:
        return False, {}, "Serveur_SG non configure (Parametres -> Liaisons)"
    url = "%s?action=sync_ref&token=%s" % (sg_url, quote(sg_token))
    try:
        with urllib.request.urlopen(url, timeout=240) as rep_:
            d = json.loads(rep_.read().decode("utf-8", "replace"))
    except Exception as e:
        return False, {}, "%s: %s" % (type(e).__name__, str(e)[:160])
    if d.get("ok") is False:
        return False, d, str(d.get("erreur") or "refus de Serveur_SG")
    # 26/11 : referentiel neuf -> les chargements recents sont reclasses avec lui (en arriere-plan)
    threading.Thread(target=sg_recalculer_chargements, daemon=True).start()
    return True, d, ""


CHARGEMENTS_RECALCUL_H = 12


def sg_recalculer_chargements(heures=CHARGEMENTS_RECALCUL_H):
    """26/11 (essai DT-15) : ?action=chargements_recalculer chez Serveur_SG. Les cycles commences dans les `heures`
    dernieres heures sont reclasses avec le referentiel actuel (un blast importe juste avant leur calcul les laissait
    « hors de tout orebloc », comptes en waste). Puis relecture des chargements du jour : les voyages deja
    enregistres prennent le bon bloc. Rend (ok, reponse ou {}, erreur) ; ne leve jamais."""
    sg_url, sg_token, _ = sg_conf()
    if not sg_url or not sg_token:
        return False, {}, "Serveur_SG non configure"
    url = "%s?action=chargements_recalculer&heures=%d&token=%s" % (sg_url, int(heures), quote(sg_token))
    try:
        with urllib.request.urlopen(url, timeout=240) as rep_:
            d = json.loads(rep_.read().decode("utf-8", "replace"))
    except Exception as e:
        print("SG: recalcul des chargements impossible (%s)" % e)
        return False, {}, "%s: %s" % (type(e).__name__, str(e)[:160])
    if d.get("ok") is False:
        # Serveur_SG pas encore redeploye : « action inconnue »
        print("SG: recalcul des chargements refuse (%s)" % (d.get("erreur") or "?"))
        return False, d, str(d.get("erreur") or "refus de Serveur_SG")
    print("SG: chargements recalcules (%s h) : %s cycle(s), %s ligne(s) mise(s) a jour"
          % (d.get("heures"), d.get("chargements"), d.get("mis_a_jour")))
    try:
        chargements_un_passage()          # KPI et voyages relus tout de suite
    except Exception as e:
        print("SG: relecture des chargements impossible (%s)" % e)
    return True, d, ""


def relais_tester():
    """Teste les deux liaisons sortantes AVEC les reglages enregistres :
    ?action=sante sur Serveur_OT et sur Serveur_SG. Les jetons restent sur
    ce poste : seul le verdict (ok / erreur / version) remonte a la page."""
    def sante(url, jeton):
        if not url:
            return {"configure": False, "ok": False, "erreur": "URL non renseignee"}
        if not jeton:
            return {"configure": False, "ok": False, "erreur": "jeton non renseigne"}
        adresse = "%s?action=sante&token=%s" % (url, quote(jeton))
        debut = time.time()
        try:
            with urllib.request.urlopen(adresse, timeout=30) as rep:
                d = json.loads(rep.read().decode("utf-8", "replace"))
        except Exception as e:
            return {"configure": True, "ok": False,
                    "erreur": "%s: %s" % (type(e).__name__, str(e)[:160])}
        res = {"configure": True, "ok": d.get("ok") is not False,
               "duree_ms": int((time.time() - debut) * 1000),
               "version": d.get("version") or ""}
        if d.get("ok") is False:
            res["erreur"] = str(d.get("erreur") or "refus du serveur")
        elif not d.get("version"):
            res["avertissement"] = ("aucune version renvoyee : ce deploiement execute "
                                    "un ancien script")
        return res

    office_url, office_token = office_conf()
    sg_url, sg_token, _ = sg_conf()
    return {"office": sante(office_url, office_token), "sg": sante(sg_url, sg_token)}


# Relais des positions vers Serveur_SG (Apps Script, cerveau d'affectation).
# Vide = desactive, comme OFFICE_URL : le poste de dev n'envoie rien.
# Les positions sont groupees puis envoyees par lot toutes les
# SG_INTERVALLE_S secondes : un appel par engin saturerait le quota Apps
# Script (90 min de runtime/jour sur le palier gratuit).
# (voir sg_conf() plus haut : lecture dynamique, base + environnement)

# Reprojection GPS -> repere local (metres), provisoire en attendant la
# calibration Phase 2 sur des points de repere connus dans les deux reperes.
# Fiable pour les DISTANCES RELATIVES (donc l'immobilisation), pas encore
# pour superposer une position GPS aux oreblocs CSV/DXF (geofencing).
GPS_ORIGINE_LAT = float(os.environ.get("GPS_ORIGINE_LAT", "0"))
GPS_ORIGINE_LON = float(os.environ.get("GPS_ORIGINE_LON", "0"))
METRES_PAR_DEGRE_LAT = 111320.0

# Seuil par defaut de teneur de coupure (pour qualifier un chargement)
DEFAULT_CUTOFF_GRADE = 10.0

# Charge utile d'un voyage DT pour un truck_factor de 1 (tonnes).
# VALEUR PAR DEFAUT SEULEMENT : le reglage reel vit dans Parametres
# (charge_moyenne_t, calibrable par l'exploitant), lu par
# chargement_moyen_t() a chaque cloture de mission.
TRUCK_BARITE_T = 30.0


def chargement_moyen_t(conn):
    """Charge moyenne par voyage (tonnes), reglable dans Parametres >
    Connection (SaaS :8100, cle params.charge_moyenne_t). La passerelle
    lit le reglage a distance (cache 60 s) : un changement prend effet
    au prochain voyage sans redemarrage. Fallback : 30 t historique."""
    return _chargement_moyen_cache(conn)


_chargement_cache = {"valeur": None, "lue_le": 0.0}


def _chargement_moyen_cache(conn):
    import json as _json
    import urllib.request as _urllib
    now = time.time()
    if _chargement_cache["valeur"] and now - _chargement_cache["lue_le"] < 60:
        return _chargement_cache["valeur"]
    # lecture du param chez le SaaS (source des Parametres)
    try:
        req = _urllib.Request("http://localhost:8100/api/config", headers={"Accept": "application/json"})
        with _urllib.urlopen(req, timeout=3) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        v = float((data.get("params") or {}).get("charge_moyenne_t") or 0)
        if v > 0:
            _chargement_cache["valeur"] = v
            _chargement_cache["lue_le"] = now
            return v
    except Exception:
        pass
    # fallback : param local passerelle, sinon constante
    try:
        v = float(param_lire("charge_moyenne_t", str(TRUCK_BARITE_T)))
    except (ValueError, TypeError):
        return TRUCK_BARITE_T
    return v if v > 0 else TRUCK_BARITE_T

# Seuils metier (metres)
DT_PROCHE_EXCA_ODT = 5.0        # DT proche exca OMEX -> candidat ODT
DT_LOIN_EXCA_SBDT = 25.0        # DT loin exca OMEX -> candidat SBDT
IMMOBILISATION_ODT_MIN = 3      # 3 min pour valider ODT
IMMOBILISATION_SBDT_MIN = 3     # 3 min pour valider SBDT
SEUIL_IMMOBILISATION_M = 2.0    # deplacement max tolere (bruit GPS) pour considerer un engin immobile
MARGE_OREBLOC_M = 5.0           # tolerance "proche des limites d'un orebloc" (cahier des charges, statut ODT)
MARGE_AFFECTATION_DEFAUT_M = 0.0   # tolerance de l'AFFECTATION (reglable dans la console, 0 = polygone strict)
HMAC_TOLERANCE_S = 120          # tolerance d'horloge / anti-rejeu sur les trames XBee signees (secondes)

# --------------------------------------------------------------- schema -------
SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS oreblocs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT NOT NULL,
    grade_cutoff REAL DEFAULT 10.0,
    sommets TEXT NOT NULL,          -- JSON liste de [x, y]
    source_fichier TEXT DEFAULT '',
    blast_id INTEGER DEFAULT 0,     -- orebloc issu d'un blast (numero)
    volume REAL DEFAULT 0,          -- volume du polygone (m3)
    tonnes REAL DEFAULT 0,          -- tonnage du bloc
    au_teneur REAL DEFAULT 0,       -- teneur Au mesuree (Au_ok), distincte du grade_cutoff seuil
    density REAL DEFAULT 0,         -- densite (t/m3)
    destination TEXT DEFAULT '',    -- teneur/dumping assignee (ex. W, F5W)
    litho TEXT DEFAULT '',          -- lithologie (ex. IMAF)
    actif INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
,
    -- 1 = bloc de minerai (present dans le Geosum), 0 = sterile.
    -- Seuls les blocs de minerai sont projetes vers les tablettes.
    minerai INTEGER DEFAULT 1);

-- Plan de dig (page "Dig Plan") : un blast = une planche/layer, avec ses
-- metadonnees de plan (bench/heading/flitch/echelle) en plus du numero.
CREATE TABLE IF NOT EXISTS blasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    numero TEXT NOT NULL,
    nom TEXT DEFAULT '',              -- nom libre du blast (Dig Plan: "Name")
    name_base TEXT DEFAULT '',        -- Dig Plan: "Name_base" (CSV "Base de donnees")
    name_dxf TEXT DEFAULT '',         -- Dig Plan: "Name_dxf" (reference du fichier DXF)
    name_geosum TEXT DEFAULT '',      -- Dig Plan: "Name_Geosum"
    name_survey TEXT DEFAULT '',      -- Dig Plan: "Name_servey"
    name_map TEXT DEFAULT '',         -- Dig Plan: "Name_Map" (nom du fichier)
    map_url TEXT DEFAULT '',          -- URL de la carte (fichier depose dans data/plans/)
    ot_sync INTEGER DEFAULT 0,        -- 1 = recopie dans le classeur Serveur_OT
    ot_erreur TEXT DEFAULT '',        -- motif du dernier echec de recopie
    ot_maj_le TEXT DEFAULT '',        -- horodatage de la derniere recopie reussie
    map_image TEXT DEFAULT '',        -- LEGACY : ancien stockage base64, remplace par map_url
                                      -- (incompatible Sheets : plafond 50 000 car. par cellule)
    source_nom TEXT DEFAULT '',       -- source miniere liee (ex. MAIN PIT)
    destination TEXT DEFAULT '',      -- teneur selectionnee (F5M/F2M/F4M...)
    bench TEXT DEFAULT '',
    heading TEXT DEFAULT '',
    flitch TEXT DEFAULT '',
    plan_date TEXT DEFAULT '',
    scale TEXT DEFAULT '',
    page_label TEXT DEFAULT '',
    source_fichier TEXT DEFAULT '',   -- csv / dxf tire par la page
    nb_blocs INTEGER DEFAULT 0,
    actif INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dumping_areas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT NOT NULL,
    x REAL DEFAULT 0,               -- centroide du polygone (recalcule a chaque save)
    y REAL DEFAULT 0,
    rayon_m REAL DEFAULT 50,        -- ancien rayon (legacy), le perimetre reel est sommets
    sommets TEXT DEFAULT '[]',      -- JSON liste de [x, y] : contour de la zone (obligatoire)
    couleur TEXT DEFAULT 'blue',    -- bleu/vert/jaune/orange (comme zones live)
    actif INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Checkpoints (17/11) : une LIGNE de passage (deux extremites), en UTM.
-- Import CSV "Y, X, id, Nom" : deux lignes par checkpoint.
CREATE TABLE IF NOT EXISTS checkpoints (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT NOT NULL,
    x1 REAL NOT NULL, y1 REAL NOT NULL,     -- premiere extremite (Est, Nord)
    x2 REAL NOT NULL, y2 REAL NOT NULL,     -- seconde extremite
    actif INTEGER DEFAULT 1,
    interdit_waste INTEGER DEFAULT 1,       -- 23/11 : 1 = un DT charge de WASTE ne doit jamais le franchir
    interdit_ore INTEGER DEFAULT 0,         -- 23/11 : 1 = un DT charge de ORE ne doit jamais le franchir
    created_at TEXT DEFAULT (datetime('now'))
);

-- 23/11 : VISITES DE ZONES pendant une mission (geofencing ore / waste). Une ligne par entree dans une dumping area :
-- role 'prevue' (zone de la mission), 'interdite' (RomPad pour du waste, Waste dump pour du ore) ou 'autre'.
-- arret_max_s = plus longue immobilisation dans la zone (rayon ARRET_RAYON_M) : >= ARRET_DUMP_S = dumping.
CREATE TABLE IF NOT EXISTS zone_visites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id INTEGER NOT NULL,
    dt_code TEXT NOT NULL,
    matiere TEXT DEFAULT '',
    zone_id INTEGER NOT NULL,
    zone_nom TEXT DEFAULT '',
    zone_type TEXT DEFAULT '',          -- rompad | waste | ''
    role TEXT DEFAULT 'autre',
    entree TEXT NOT NULL,
    sortie TEXT DEFAULT '',
    arret_max_s REAL DEFAULT 0,
    arret_debut TEXT DEFAULT '',
    arret_x REAL DEFAULT 0,
    arret_y REAL DEFAULT 0,
    alerte_dump INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_zone_visites_mission ON zone_visites(mission_id);

-- 21/11 : TRAJECTOIRE = une source (pit) vers une dumping area ; porte la liste ORDONNEE des checkpoints
-- que doit franchir un DT dont la mission relie ces deux points. Les checkpoints (lignes de passage)
-- restent definis dans la table `checkpoints`.
CREATE TABLE IF NOT EXISTS trajectoires (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    dumping_area_id INTEGER NOT NULL,
    checkpoints TEXT DEFAULT '[]',      -- JSON : ids de checkpoints, dans l'ordre de passage
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE (source_id, dumping_area_id)
);

-- 21/11 : TABLETTES (console, Settings > Tablettes). Une installation = une famille de camion = un id DT.
CREATE TABLE IF NOT EXISTS tablettes (
    serial TEXT PRIMARY KEY,           -- numero de serie adb (stable, USB comme Wi-Fi)
    nom TEXT DEFAULT '',               -- modele lu sur l'appareil
    dt_code TEXT DEFAULT '',           -- (ancien) remplace par profil + ident
    famille TEXT DEFAULT '',           -- famille du camion / de l'engin : 1500, 785, ADT...
    profil TEXT DEFAULT 'dt',          -- dt | exc | spotter | link : l'application installee sur la tablette
    ident TEXT DEFAULT '',             -- DT-04 (dt), id de l'engin (exc), nom du spotter (spotter)
    equipe TEXT DEFAULT '',            -- equipe du spotter
    apk TEXT DEFAULT '',               -- dernier APK installe
    installee_le TEXT DEFAULT '',
    derniere_vue TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_tablettes_dt ON tablettes(dt_code) WHERE dt_code <> '';

-- 21/11 : RECEPTEURS GNSS (console, Settings > GNSS). Identifie par la puce (UBX-SEC-UNIQID), recoit un ID
-- GNSS-NN, est configure selon le profil GNSS du site, puis affecte a UNE tablette (donc a un engin).
CREATE TABLE IF NOT EXISTS gnss (
    chip TEXT PRIMARY KEY,             -- identifiant unique de la puce (hex)
    id TEXT NOT NULL UNIQUE,           -- GNSS-01 : l'etiquette physique du recepteur
    modele TEXT DEFAULT '',
    configure_le TEXT DEFAULT '',      -- derniere configuration verifiee sur le recepteur
    config_sig TEXT DEFAULT '',        -- profil applique (vitesse|frequence|trames)
    tablette TEXT DEFAULT '',          -- numero de serie de la tablette qui le porte
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_gnss_tablette ON gnss(tablette) WHERE tablette <> '';

-- 21/11 : JETONS PAR TABLETTE. Un jeton = code.generation.signature (HMAC du code et de la generation avec un secret
-- que seuls la console et les serveurs connaissent). Renouveler = generation + 1 (l'ancienne reste valable quelques jours) ;
-- revoquer = generation + 1 sans delai.
CREATE TABLE IF NOT EXISTS jetons_appareils (
    code TEXT PRIMARY KEY,             -- DT-04, EX-2000...
    gen INTEGER NOT NULL DEFAULT 1,    -- generation courante
    prev INTEGER NOT NULL DEFAULT 0,   -- generation precedente encore acceptee (0 = aucune)
    prev_jusqua REAL NOT NULL DEFAULT 0,   -- epoch (s) de fin de tolerance
    maj_le TEXT DEFAULT '',
    action TEXT DEFAULT ''             -- inscrit | renouvele | revoque
);

CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT NOT NULL,              -- source miniere : Main Pit, Second Pit...
    codes TEXT DEFAULT '',          -- codes de source admis dans le nom d'un blast
                                    -- (3e segment : 275_OM_LMP2_... -> LMP2), separes
                                    -- par des virgules. Vide = verification desactivee.
    actif INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS teneurs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,     -- la source possede ses teneurs
    nom TEXT NOT NULL,              -- famille de teneur : F5M, F2M, F4M...
    valeur REAL DEFAULT 0,          -- ancien champ unique (conserve, plus ecrit)
    valeur_min REAL DEFAULT 0,      -- seuil minimum de l'intervalle de teneur (%)
    valeur_max REAL DEFAULT 0,      -- seuil maximum de l'intervalle de teneur (%)
    dumping_area_id INTEGER DEFAULT 0, -- Dumping Instruction : DA obligatoire pour chaque teneur
    -- Codes porteurs de teneur lus dans le NOM d'un bloc DXF
    -- (230_LWP2_106_HG_FR_008_OP -> HG). Plusieurs codes possibles pour une
    -- meme teneur, separes par des virgules. C'est ce qui permet de remplir
    -- `destination` automatiquement a l'import d'un blast.
    codes TEXT DEFAULT '',
    couleur TEXT DEFAULT '',        -- 21/11 : couleur propre a la famille (#rrggbb) ; vide = celle de son grade
    actif INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS travels_roads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT NOT NULL,
    geojson TEXT DEFAULT '{}',
    type TEXT DEFAULT 'geojson',    -- geojson | geotiff
    fichier TEXT DEFAULT '',        -- chemin du fichier (.tif) sur le serveur
    actif INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS engins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    type TEXT NOT NULL,             -- 'EXCA' ou 'DT'
    statut TEXT DEFAULT 'WEX',      -- WEX/OMEX pour EXCA ; WDT/SBDT/ODT pour DT
    x REAL DEFAULT 0,
    y REAL DEFAULT 0,
    immobilise_depuis TEXT DEFAULT '',
    actif INTEGER DEFAULT 1,
    derniere_maj TEXT DEFAULT (datetime('now')),
    offroad INTEGER DEFAULT 0,              -- 1 = DT hors piste (confirme)
    offroad_depuis TEXT DEFAULT '',         -- debut de la sortie de route
    offroad_candidat_depuis TEXT DEFAULT '',-- hors piste, pas encore confirme
    offroad_distance REAL DEFAULT 0,        -- distance a la piste la plus proche (m)
    canal TEXT DEFAULT '',                  -- canal de la derniere position retenue (mqtt, http, radio...)
    auth TEXT DEFAULT ''                    -- authentification de son auteur (broker, jeton, hmac, aucune)
);

-- 21/11 : CANAUX des positions. Derniere fois ou chaque engin a ete entendu par chaque canal, avec la facon dont l'auteur
-- a ete authentifie (broker = mot de passe MQTT, jeton = jeton de tablette, hmac = signature radio, aucune = relayee).
CREATE TABLE IF NOT EXISTS positions_canaux (
    code TEXT NOT NULL,
    canal TEXT NOT NULL,
    auth TEXT DEFAULT '',
    vu_epoch REAL DEFAULT 0,
    vu_le TEXT DEFAULT '',
    nb INTEGER DEFAULT 0,
    PRIMARY KEY (code, canal)
);

CREATE TABLE IF NOT EXISTS missions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dt_code TEXT NOT NULL,
    orebloc_id INTEGER DEFAULT 0,
    destination_x REAL DEFAULT 0,
    destination_y REAL DEFAULT 0,
    dumping_area_id INTEGER DEFAULT 0,
    chemin TEXT DEFAULT '[]',       -- JSON liste de points du chemin a suivre
    statut TEXT DEFAULT 'en_cours', -- en_cours, termine (devoie = legacy)
    arrivee INTEGER DEFAULT 0,      -- 1 = DT entre dans la DA (geofencing)
    devie INTEGER DEFAULT 0,        -- 1 = DT ecarte du trajet (confirme)
    devie_depuis TEXT DEFAULT '',
    devie_candidat_depuis TEXT DEFAULT '',
    ecart_m REAL DEFAULT 0,         -- distance au trajet (m)
    distance_parcourue REAL DEFAULT 0,
    debut TEXT DEFAULT (datetime('now')),
    fin TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS alertes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,             -- 'deviation', 'fin_mission', 'non_conforme'
    dt_code TEXT NOT NULL,
    message TEXT DEFAULT '',
    voix_off INTEGER DEFAULT 1,     -- 1 = jouer voix off cote tablette
    dest_office INTEGER DEFAULT 0,  -- 1 = envoye au serveur office
    acquittee INTEGER DEFAULT 0,    -- 1 = vue/traitee par un operateur
    acquittee_par TEXT DEFAULT '',
    acquittee_le TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now'))
);

-- Minage declare : qui mine quel bloc selon le spotter (canal A).
-- Le clignotement du Progress combine cette declaration (fraiche < 30 min)
-- et la preuve d'activite (canal D : dernier voyage du bloc < 15 min).
CREATE TABLE IF NOT EXISTS minage_actuel (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    exca_code TEXT NOT NULL,
    orebloc_id INTEGER NOT NULL,
    declared_at TEXT DEFAULT (datetime('now')),
    origine TEXT DEFAULT 'bureau',   -- 21/11 : 'spotter' (son app, reseau OK) ou 'bureau' (formulaire de secours)
    UNIQUE (exca_code)
);

CREATE TABLE IF NOT EXISTS office_sync (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload TEXT NOT NULL,
    statut TEXT DEFAULT 'en_attente', -- en_attente, envoye, echec
    created_at TEXT DEFAULT (datetime('now')),
    envoie_le TEXT DEFAULT '',
    tentatives INTEGER DEFAULT 0,       -- essais d'envoi (rejeu des echecs)
    derniere_tentative TEXT DEFAULT '',
    event_id TEXT DEFAULT ''            -- UUID de l'evenement envoye a OT
);

CREATE TABLE IF NOT EXISTS voyages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    mission_id INTEGER DEFAULT 0,
    dt_code TEXT NOT NULL,
    orebloc_id INTEGER DEFAULT 0,
    dumping_area_id INTEGER DEFAULT 0,
    teneur REAL DEFAULT 10.0,       -- teneur de coupure du bloc charge
    volume_t REAL DEFAULT 0,        -- tonnage cumule du voyage (truck factor x barite)
    truck_factor INTEGER DEFAULT 1, -- capacite du DT (nb de coups / coefficient)
    conformite TEXT DEFAULT '',     -- conforme / compromis / non_conforme (dumping instruction)
    conformite_detail TEXT DEFAULT '', -- JSON : de quoi expliquer le verdict (regle, zones, position, raison)
    created_at TEXT DEFAULT (datetime('now'))
);

-- Points de calibration GPS (lat,lon) <-> repere local mine (x,y), releves
-- sur le terrain (meme point physique mesure au RTK et connu sur le plan
-- CSV/DXF). Sert a calculer la transformation affine utilisee par
-- gps_vers_local().
CREATE TABLE IF NOT EXISTS calibration_points (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT DEFAULT '',
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    actif INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Derniere transformation affine calculee : x = a*lon + b*lat + c ;
-- y = d*lon + e*lat + f. rmse_m/emax_m = erreur quadratique moyenne / max
-- (en metres) sur les points de calibration ayant servi au calcul.
CREATE TABLE IF NOT EXISTS calibration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    a REAL NOT NULL, b REAL NOT NULL, c REAL NOT NULL,
    d REAL NOT NULL, e REAL NOT NULL, f REAL NOT NULL,
    nb_points INTEGER DEFAULT 0,
    rmse_m REAL DEFAULT 0,
    emax_m REAL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

-- Cle secrete HMAC-SHA256 par DT, partagee avec la tablette embarquee
-- (provisionnement manuel). Authentifie les trames XBee (montee des
-- positions) pour empecher l'usurpation d'identite sur le canal radio.
CREATE TABLE IF NOT EXISTS cles_dt (
    dt_code TEXT PRIMARY KEY,
    cle_hex TEXT NOT NULL,
    actif INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    cle_prec TEXT DEFAULT '',              -- cle precedente, encore acceptee jusqu'a prec_jusqua (renouvellement)
    prec_jusqua REAL DEFAULT 0,
    maj_le TEXT DEFAULT ''
);

-- Systeme de coordonnees geographiques retenu pour la mine (ligne unique,
-- id=1). WGS84 (latitude/longitude) ou UTM (fuseau + hemisphere + datum).
CREATE TABLE IF NOT EXISTS geographic_system (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    systeme TEXT NOT NULL DEFAULT 'WGS84',   -- 'WGS84' | 'UTM'
    zone INTEGER,                            -- fuseau UTM (1..60), NULL si WGS84
    hemisphere TEXT DEFAULT 'N',             -- 'N' | 'S' (UTM)
    datum TEXT DEFAULT 'WGS 84',
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Historique des positions des DT (19/11) : vitesse et cap du suivi en
-- direct, trajets par shift sur les cartes (Live, dashboard). Un point est
-- garde s'il a bouge de plus de 1 m ou 30 s apres le precedent ; purge
-- au-dela de POSITIONS_DT_JOURS jours.
CREATE TABLE IF NOT EXISTS positions_dt (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    x REAL NOT NULL,
    y REAL NOT NULL,
    horodatage TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_positions_dt ON positions_dt (code, horodatage);

-- Comptes MQTT des DT (19/11) : un par DT de la flotte du SaaS (lue dans
-- data/oretracking.db, table sync_flotte, en LECTURE seule). Le mot de passe
-- est genere ici, applique a chaud au broker (Dynamic Security) et envoye a
-- Serveur_OT (referentiels) pour que la tablette se connecte seule.
CREATE TABLE IF NOT EXISTS mqtt_comptes (
    code TEXT PRIMARY KEY,
    famille TEXT DEFAULT '',
    numero INTEGER DEFAULT 0,
    mdp TEXT NOT NULL,
    actif INTEGER DEFAULT 1,
    mdp_applique TEXT DEFAULT '',   -- empreinte du mot de passe connu du broker
    cree_le TEXT DEFAULT '',
    maj_le TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS params (
    cle TEXT PRIMARY KEY,
    valeur TEXT DEFAULT '',
    maj_le TEXT DEFAULT (datetime('now'))
);
"""

# ------------------------------------------------- projection UTM (WGS84) --
# Les pistes arrivent en degres (GeoJSON CRS84) alors que les oreblocs et les
# dumping areas sont deja en UTM. Tant que les deux reperes coexistent, aucun
# itineraire n'est calculable : Dijkstra ne trouve jamais de noeud proche du
# point de depart et renvoie None.
#
# Serie de Snyder (USGS Professional Paper 1395). Precision centimetrique a
# quelques degres du meridien central, tres au-dela de ce qu'exige une fosse.
# Ecrite a la main : pyproj est absent, et la passerelle doit rester
# embarquable dans un .exe sans dependance native.

_UTM_A = 6378137.0                    # demi-grand axe WGS84
_UTM_F = 1.0 / 298.257223563          # aplatissement
_UTM_K0 = 0.9996                      # facteur d'echelle UTM
_UTM_E2 = _UTM_F * (2.0 - _UTM_F)     # premiere excentricite au carre
_UTM_EP2 = _UTM_E2 / (1.0 - _UTM_E2)  # deuxieme excentricite au carre


def utm_zone_pour(lon):
    """Fuseau UTM (1..60) contenant cette longitude."""
    return int((float(lon) + 180.0) // 6.0) % 60 + 1


def utm_meridien_central(zone):
    return (zone - 1) * 6.0 - 180.0 + 3.0


def wgs84_vers_utm(lat, lon, zone=None, hemisphere=None):
    """(lat, lon) en degres -> (easting, northing) en metres."""
    lat = float(lat)
    lon = float(lon)
    if zone is None:
        zone = utm_zone_pour(lon)
    if hemisphere is None:
        hemisphere = "N" if lat >= 0 else "S"

    phi = math.radians(lat)
    lam = math.radians(lon)
    lam0 = math.radians(utm_meridien_central(zone))

    sin_phi = math.sin(phi)
    cos_phi = math.cos(phi)
    tan_phi = math.tan(phi)

    N = _UTM_A / math.sqrt(1.0 - _UTM_E2 * sin_phi * sin_phi)
    T = tan_phi * tan_phi
    C = _UTM_EP2 * cos_phi * cos_phi
    A = (lam - lam0) * cos_phi

    e2, e4, e6 = _UTM_E2, _UTM_E2 ** 2, _UTM_E2 ** 3
    M = _UTM_A * (
        (1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * phi
        - (3 * e2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * phi)
        + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * phi)
        - (35 * e6 / 3072) * math.sin(6 * phi))

    est = _UTM_K0 * N * (
        A + (1 - T + C) * A ** 3 / 6
        + (5 - 18 * T + T * T + 72 * C - 58 * _UTM_EP2) * A ** 5 / 120) + 500000.0
    nord = _UTM_K0 * (M + N * tan_phi * (
        A * A / 2 + (5 - T + 9 * C + 4 * C * C) * A ** 4 / 24
        + (61 - 58 * T + T * T + 600 * C - 330 * _UTM_EP2) * A ** 6 / 720))
    if str(hemisphere).upper().startswith("S"):
        nord += 10000000.0
    return est, nord


def utm_vers_wgs84(est, nord, zone, hemisphere="N"):
    """(easting, northing) -> (lat, lon) en degres. Sert surtout a verifier
    la projection directe par aller-retour."""
    est = float(est) - 500000.0
    nord = float(nord)
    if str(hemisphere).upper().startswith("S"):
        nord -= 10000000.0

    e1 = (1 - math.sqrt(1 - _UTM_E2)) / (1 + math.sqrt(1 - _UTM_E2))
    M = nord / _UTM_K0
    mu = M / (_UTM_A * (1 - _UTM_E2 / 4 - 3 * _UTM_E2 ** 2 / 64
                        - 5 * _UTM_E2 ** 3 / 256))
    phi1 = (mu
            + (3 * e1 / 2 - 27 * e1 ** 3 / 32) * math.sin(2 * mu)
            + (21 * e1 ** 2 / 16 - 55 * e1 ** 4 / 32) * math.sin(4 * mu)
            + (151 * e1 ** 3 / 96) * math.sin(6 * mu)
            + (1097 * e1 ** 4 / 512) * math.sin(8 * mu))

    sin1, cos1, tan1 = math.sin(phi1), math.cos(phi1), math.tan(phi1)
    C1 = _UTM_EP2 * cos1 * cos1
    T1 = tan1 * tan1
    N1 = _UTM_A / math.sqrt(1 - _UTM_E2 * sin1 * sin1)
    R1 = _UTM_A * (1 - _UTM_E2) / (1 - _UTM_E2 * sin1 * sin1) ** 1.5
    D = est / (N1 * _UTM_K0)

    lat = phi1 - (N1 * tan1 / R1) * (
        D * D / 2
        - (5 + 3 * T1 + 10 * C1 - 4 * C1 * C1 - 9 * _UTM_EP2) * D ** 4 / 24
        + (61 + 90 * T1 + 298 * C1 + 45 * T1 * T1 - 252 * _UTM_EP2
           - 3 * C1 * C1) * D ** 6 / 720)
    lon = (D - (1 + 2 * T1 + C1) * D ** 3 / 6
           + (5 - 2 * C1 + 28 * T1 - 3 * C1 * C1 + 8 * _UTM_EP2
              + 24 * T1 * T1) * D ** 5 / 120) / cos1
    return math.degrees(lat), math.degrees(utm_meridien_central(zone) * math.pi / 180 + lon)


def semble_degres(points):
    """True si la suite de points ressemble a du lon/lat plutot qu'a de l'UTM.

    Un easting UTM depasse toujours largement 180 et un northing 90 (hors
    equateur strict), alors qu'une longitude tient dans [-180, 180]. Le test
    est donc sans ambiguite en pratique.
    """
    vus = 0
    for x, y in points:
        if abs(x) > 180.0 or abs(y) > 90.0:
            return False
        vus += 1
        if vus >= 50:
            break
    return vus > 0


def geojson_points(geo):
    """Enumere les couples [x, y] d'un GeoJSON, quelle que soit sa structure."""
    out = []

    def descendre(c):
        if not isinstance(c, list) or not c:
            return
        if isinstance(c[0], (int, float)) and len(c) >= 2:
            out.append((c[0], c[1]))
            return
        for sous in c:
            descendre(sous)

    if geo.get("type") == "FeatureCollection":
        for f in geo.get("features") or []:
            descendre(((f.get("geometry") or {}).get("coordinates")) or [])
    elif geo.get("type") == "Feature":
        descendre(((geo.get("geometry") or {}).get("coordinates")) or [])
    else:
        descendre(geo.get("coordinates") or [])
    return out


# ------------------------------------------- itineraire sur les travels roads --
# Jusqu'ici le chemin d'une mission etait code en dur comme un segment droit
# entre le camion et sa destination -- une ligne qui traverse allegrement les
# talus et les fosses. Les pistes etaient stockees mais jamais lues.
#
# On construit ici un graphe a partir des LineString : chaque sommet est un
# noeud, chaque couple consecutif une arete ponderee par sa longueur. Les
# sommets distants de moins de `tolerance_m` sont fusionnes, ce qui soude les
# intersections entre pistes importees separement -- sans cette fusion le
# graphe serait une collection de brins disjoints et aucun trajet ne
# traverserait d'une piste a l'autre.

ROUTE_TOLERANCE_M = 2.0      # fusion des sommets proches (intersections)
ROUTE_ACCROCHE_MAX_M = 200.0  # au-dela, le point n'est pas desservi


def _cle_noeud(x, y, tolerance):
    return (int(round(x / tolerance)), int(round(y / tolerance)))


def construire_graphe_pistes(conn, tolerance=ROUTE_TOLERANCE_M):
    """Graphe non oriente des pistes actives. (noeuds, adjacence).

    `noeuds` : {cle: (x, y)}   `adjacence` : {cle: [(cle_voisine, cout)]}
    """
    noeuds = {}
    adjacence = {}

    def ajouter_noeud(x, y):
        cle = _cle_noeud(x, y, tolerance)
        if cle not in noeuds:
            noeuds[cle] = (x, y)
            adjacence[cle] = []
        return cle

    def relier(a, b):
        if a == b:
            return
        cout = distance(noeuds[a][0], noeuds[a][1], noeuds[b][0], noeuds[b][1])
        for voisin, _ in adjacence[a]:
            if voisin == b:
                return          # arete deja posee (pistes superposees)
        adjacence[a].append((b, cout))
        adjacence[b].append((a, cout))

    for r in conn.execute("SELECT geojson FROM travels_roads "
                          "WHERE actif=1 AND type='geojson'"):
        try:
            geo = json.loads(r["geojson"] or "{}")
        except Exception:
            continue
        lignes = []

        def collecter(c):
            if not isinstance(c, list) or not c:
                return
            if isinstance(c[0], (int, float)):
                return
            if isinstance(c[0], list) and c[0] and isinstance(c[0][0], (int, float)):
                lignes.append(c)
                return
            for sous in c:
                collecter(sous)

        if geo.get("type") == "FeatureCollection":
            for f in geo.get("features") or []:
                collecter(((f.get("geometry") or {}).get("coordinates")) or [])
        elif geo.get("type") == "Feature":
            collecter(((geo.get("geometry") or {}).get("coordinates")) or [])
        else:
            collecter(geo.get("coordinates") or [])

        for ligne in lignes:
            precedent = None
            for p in ligne:
                if not isinstance(p, list) or len(p) < 2:
                    continue
                cle = ajouter_noeud(float(p[0]), float(p[1]))
                if precedent is not None:
                    relier(precedent, cle)
                precedent = cle
    return noeuds, adjacence


def aretes_pistes(conn):
    """Reseau de pistes sous forme d'aretes [x1, y1, x2, y2].

    C'est le format que Serveur_OT stocke (une arete par ligne) et que
    Serveur_SG reconstruit : une cellule de classeur plafonne a 50 000
    caracteres, un GeoJSON entier n'y tiendrait pas longtemps.
    """
    noeuds, adjacence = construire_graphe_pistes(conn)
    vues = set()
    out = []
    for cle, voisins in adjacence.items():
        for voisin, _ in voisins:
            paire = (cle, voisin) if cle < voisin else (voisin, cle)
            if paire in vues:
                continue
            vues.add(paire)
            a, b = noeuds[cle], noeuds[voisin]
            out.append([round(a[0], 2), round(a[1], 2),
                        round(b[0], 2), round(b[1], 2)])
    return out


def _noeud_le_plus_proche(noeuds, x, y):
    meilleur, meilleure_dist = None, float("inf")
    for cle, (nx, ny) in noeuds.items():
        d = distance(x, y, nx, ny)
        if d < meilleure_dist:
            meilleure_dist, meilleur = d, cle
    return meilleur, meilleure_dist


def itineraire(conn, x1, y1, x2, y2, accroche_max=ROUTE_ACCROCHE_MAX_M):
    """Trajet par les pistes entre deux points. Retourne (chemin, info).

    `chemin` part toujours du point de depart reel et finit au point
    d'arrivee reel, les noeuds de piste s'intercalant entre les deux. Si
    aucune piste n'est accrochable, on rend le segment droit : mieux vaut un
    trajet approximatif qu'une mission sans chemin.
    """
    droit = [[x1, y1], [x2, y2]]
    noeuds, adjacence = construire_graphe_pistes(conn)
    if not noeuds:
        return droit, {"mode": "segment droit", "motif": "aucune piste enregistree"}

    depart, d_depart = _noeud_le_plus_proche(noeuds, x1, y1)
    arrivee, d_arrivee = _noeud_le_plus_proche(noeuds, x2, y2)
    if d_depart > accroche_max or d_arrivee > accroche_max:
        return droit, {
            "mode": "segment droit",
            "motif": "piste trop eloignee (depart %.0f m, arrivee %.0f m, "
                     "maximum %.0f m)" % (d_depart, d_arrivee, accroche_max)}

    # Dijkstra
    import heapq
    cout = {depart: 0.0}
    venant_de = {}
    vus = set()
    tas = [(0.0, depart)]
    while tas:
        c, noeud = heapq.heappop(tas)
        if noeud in vus:
            continue
        vus.add(noeud)
        if noeud == arrivee:
            break
        for voisin, poids in adjacence[noeud]:
            if voisin in vus:
                continue
            neuf = c + poids
            if neuf < cout.get(voisin, float("inf")):
                cout[voisin] = neuf
                venant_de[voisin] = noeud
                heapq.heappush(tas, (neuf, voisin))

    if arrivee not in cout:
        return droit, {"mode": "segment droit",
                       "motif": "aucun trajet reliant ces deux pistes"}

    suite = [arrivee]
    while suite[-1] != depart:
        suite.append(venant_de[suite[-1]])
    suite.reverse()

    chemin = [[x1, y1]] + [[round(noeuds[c][0], 2), round(noeuds[c][1], 2)]
                           for c in suite] + [[x2, y2]]
    return chemin, {
        "mode": "pistes",
        "noeuds_graphe": len(noeuds),
        "noeuds_trajet": len(suite),
        "longueur_m": round(cout[arrivee] + d_depart + d_arrivee, 1),
        "vol_oiseau_m": round(distance(x1, y1, x2, y2), 1),
        "accroche_depart_m": round(d_depart, 1),
        "accroche_arrivee_m": round(d_arrivee, 1),
    }


def points_en_utm(conn, points):
    """[[x, y], ...] -> (points en UTM, converti ?). Des points en degres
    (X = longitude, Y = latitude, comme un CSV exporte d'un SIG) sont
    projetes dans le fuseau declare dans Geographic System (a defaut, celui
    de la longitude). Tout le systeme raisonne en UTM 30N : des degres
    stockes tels quels ne tombent sur aucune piste ni aucune position.
    Meme regle pour les dumping areas et les checkpoints (17/11)."""
    if not points or not semble_degres(points):
        return points, False
    zone, hemi = repere_site(conn)
    if not zone:
        zone = utm_zone_pour(points[0][0])
        hemi = "N" if points[0][1] >= 0 else "S"
    sortie = []
    for x, y in points:
        est, nord = wgs84_vers_utm(y, x, zone, hemi)
        sortie.append([round(est, 3), round(nord, 3)])
    return sortie, True


def extraire_checkpoints_csv(texte):
    """CSV de checkpoints -> (checkpoints [(nom, [[x, y], [x, y]])], rejets).

    En-tete obligatoire, colonnes lues par NOM (ordre libre) : Y / X (ou
    nord / est, lat / lon...) et Nom ; les autres (id...) sont ignorees.
    Un checkpoint = les lignes de meme Nom : EXACTEMENT deux (les deux
    extremites de la ligne de passage), sinon il est rejete avec le motif."""
    lignes = lire_csv_lignes(texte)
    cols, groupes, ordre = None, {}, []
    for brutes in lignes:
        if cols is None:
            cols = _csv_colonnes(brutes)
            if cols is None or cols[0] is None:
                return [], [("(en-tete)", "colonnes Y, X et Nom attendues en premiere ligne")]
            continue
        inom, ix, iy = cols
        if len(brutes) <= max(inom, ix, iy):
            continue
        nom = brutes[inom].strip()
        x, y = _num(brutes[ix]), _num(brutes[iy])
        if not nom or x is None or y is None:
            continue
        if nom not in groupes:
            groupes[nom] = []
            ordre.append(nom)
        groupes[nom].append([x, y])
    ok, rejets = [], []
    for nom in ordre:
        if len(groupes[nom]) == 2:
            ok.append((nom, groupes[nom]))
        else:
            rejets.append((nom, "%d point(s) au lieu de 2" % len(groupes[nom])))
    return ok, rejets


def repere_site(conn):
    """Fuseau UTM declare dans Geographic System. (zone, hemisphere)."""
    r = conn.execute(
        "SELECT systeme, zone, hemisphere FROM geographic_system WHERE id=1").fetchone()
    if not r or str(r["systeme"]).upper() != "UTM" or not r["zone"]:
        return None, None
    return int(r["zone"]), (r["hemisphere"] or "N")


def geojson_reprojeter(geo, zone, hemisphere="N"):
    """Projette en place toutes les coordonnees d'un GeoJSON en UTM.
    Retourne le nombre de points convertis."""
    compteur = [0]

    def descendre(c):
        if not isinstance(c, list) or not c:
            return
        if isinstance(c[0], (int, float)) and len(c) >= 2:
            est, nord = wgs84_vers_utm(c[1], c[0], zone, hemisphere)
            c[0], c[1] = round(est, 3), round(nord, 3)
            compteur[0] += 1
            return
        for sous in c:
            descendre(sous)

    if geo.get("type") == "FeatureCollection":
        for f in geo.get("features") or []:
            descendre(((f.get("geometry") or {}).get("coordinates")) or [])
    elif geo.get("type") == "Feature":
        descendre(((geo.get("geometry") or {}).get("coordinates")) or [])
    else:
        descendre(geo.get("coordinates") or [])
    # Le CRS nomme ne doit plus annoncer du degre apres projection.
    geo.pop("crs", None)
    geo["crs_local"] = {"projection": "UTM", "zone": zone,
                        "hemisphere": hemisphere, "datum": "WGS 84"}
    return compteur[0]


# ----------------------------------------------------------- helpers geodesie --
# Codes qui designent du STERILE : pas de teneur, donc pas de consigne de
# dumping area. Le camion ira a la verse la plus proche, ce qui est le
# comportement voulu pour du sterile.
# 21/11 : liste UNIQUE, reglable dans Settings (param CODES_STERILES) ; ceci n'est que la valeur par defaut.
# Lue via codes_steriles() par : l'affectation des dumping areas, l'import Geosum et la classification des voyages.
CODES_STERILES_DEFAUT = ("WST", "WASTE", "ST", "STERILE")

# LG et MG se subdivisent chacun en deux familles, inferieure et superieure
# (F1/F2 pour LG, F3/F4 pour MG). Le nom d'un bloc ne porte que le code nu,
# `_LG_`, sans dire laquelle. La teneur MESUREE trancherait exactement, les
# intervalles etant contigus et sans recouvrement -- mais elle n'existe pas
# encore dans les fichiers. En attendant, un code nu retombe sur la famille
# designee ici. Un seul mot bascule tout le comportement.
FAMILLE_PAR_DEFAUT = "inferieure"       # "inferieure" | "superieure"


def code_dans_nom_bloc(nom):
    """Code de teneur porte par le nom d'un bloc DXF.

    230_LWP2_106_HG_FR_008_OP -> "HG". Le code est le segment qui precede
    immediatement "_FR_" ; a defaut on accepte n'importe quel segment en
    majuscules de 2 a 4 lettres, hors prefixes connus.
    """
    texte = str(nom or "").upper()
    m = re.search(r"_([A-Z]{2,4})_FR_", texte)
    if m:
        return m.group(1)
    for seg in texte.split("_"):
        if 2 <= len(seg) <= 4 and seg.isalpha():
            return seg
    return ""


def teneur_pour_bloc(conn, source_nom, nom_bloc, grade=None):
    """(teneur ou None, motif). Le code du nom du bloc fait foi.

    Quand plusieurs familles partagent le code -- LG et MG se subdivisent en
    inferieure et superieure -- le depart se fait dans cet ordre :

      1. la teneur MESUREE du bloc, si elle existe : les intervalles sont
         contigus et sans recouvrement, donc une valeur designe exactement
         une famille et le code devient superflu ;
      2. a defaut, FAMILLE_PAR_DEFAUT (la plus basse ou la plus haute).

    Le motif rendu dit toujours lequel des deux chemins a ete pris : une
    affectation par defaut ne doit jamais passer pour une mesure.
    """
    code = code_dans_nom_bloc(nom_bloc)
    if not code:
        return None, "aucun code dans le nom"
    if code in codes_steriles():
        return None, "sterile (%s)" % code

    src = (source_nom or "").strip()
    if not src:
        return None, "blast sans source"
    ligne = conn.execute("SELECT id FROM sources WHERE nom=? AND actif=1",
                         (src,)).fetchone()
    if not ligne:
        return None, "source inconnue : %s" % src

    candidats = []
    for t in conn.execute(
            "SELECT * FROM teneurs WHERE source_id=? AND actif=1", (ligne["id"],)):
        codes = [c.strip() for c in str(t["codes"] or "").split(",") if c.strip()]
        if code in codes:
            candidats.append(dict(t))
    if not candidats:
        return None, "code %s non declare dans %s" % (code, src)
    if len(candidats) == 1:
        return candidats[0], "code %s" % code

    # 1. la teneur mesuree tranche exactement
    valeur = _num(grade)
    if valeur:
        dans = [t for t in candidats
                if (t.get("valeur_min") or 0) <= valeur <= (t.get("valeur_max") or 0)]
        if len(dans) == 1:
            return dans[0], "teneur mesuree %.3f %% dans %s-%s %%" % (
                valeur, dans[0]["valeur_min"], dans[0]["valeur_max"])

    # 2. repli : famille inferieure ou superieure, selon la constante
    classees = sorted(candidats, key=lambda t: (t.get("valeur_min") or 0,
                                                t.get("valeur_max") or 0))
    choisi = classees[0] if FAMILLE_PAR_DEFAUT == "inferieure" else classees[-1]
    rang = "inferieure" if FAMILLE_PAR_DEFAUT == "inferieure" else "superieure"
    return choisi, "code %s, famille %s par defaut (%s-%s %%, teneur mesuree absente)" % (
        code, rang, choisi["valeur_min"], choisi["valeur_max"])


def appliquer_teneurs_blast(conn, blast_id, ecrire=True):
    """Remplit `destination` des blocs d'un blast. Retourne un rapport."""
    b = conn.execute("SELECT numero, source_nom FROM blasts WHERE id=?",
                     (blast_id,)).fetchone()
    if not b:
        return {"erreur": "blast introuvable"}
    rapport = {"blast": b["numero"], "source": b["source_nom"],
               "remplis": [], "sterile": [], "non_resolus": []}
    for o in conn.execute(
            "SELECT id, nom, destination, au_teneur FROM oreblocs "
            "WHERE blast_id=? AND actif=1", (blast_id,)):
        t, motif = teneur_pour_bloc(conn, b["source_nom"], o["nom"],
                                    grade=o["au_teneur"])
        if t:
            if ecrire and str(o["destination"] or "").strip() != t["nom"]:
                conn.execute("UPDATE oreblocs SET destination=? WHERE id=?",
                             (t["nom"], o["id"]))
            rapport["remplis"].append({"bloc": o["nom"], "teneur": t["nom"],
                                       "motif": motif})
        elif motif.startswith("sterile"):
            rapport["sterile"].append({"bloc": o["nom"], "motif": motif})
        else:
            rapport["non_resolus"].append({"bloc": o["nom"], "motif": motif})
    if ecrire:
        conn.commit()
    return rapport


# ------------------------------------------------------------- Geosum ------
# Le Geosum est le fichier de reserve produit par la geologie. Il ne liste
# QUE les blocs de minerai : un polygone du DXF absent de ce fichier est du
# sterile, et n'a donc rien a faire sur la tablette d'un chargeur.
#
# Il porte aussi la teneur MESUREE et la famille de destination, ce qui rend
# caduque toute deduction depuis le code du nom (_LG_, _MG_...) : ce code ne
# distingue pas les familles inferieure et superieure, le Geosum si.
#
# En-tete observe, identique sur les 13 fichiers du site :
#   elevation, label, polygon, hole, id, x, y, z, volume, tonnes, Au_ok,
#   Density, Destination, Pre Fragmentation, PolygonID, Weath, Litho,
#   PolygonMatch

# colonne du fichier (en minuscules) -> colonne de la table oreblocs
GEOSUM_COLONNES = {
    "volume": "volume",
    "tonnes": "tonnes",
    "au_ok": "au_teneur",
    "density": "density",
    "destination": "destination",
    "litho": "litho",
}

# Le Geosum nomme certaines familles autrement que le referentiel du SaaS.
GEOSUM_ALIAS_DESTINATION = {"FM": "MO"}

# `Destination` qui designe du STERILE. Le fichier complet (un sommet par ligne) liste le sterile avec son
# volume et son tonnage, marque `W` (fixe : c'est la convention du fichier) ; le fichier de reserve allege,
# lui, ne liste que le minerai et le sterile s'y reconnait a son absence. Les deux formes sont lues.
# Les autres marqueurs (WST, WASTE...) sont ceux du reglage CODES_STERILES.
GEOSUM_DESTINATION_STERILE = "W"


# Le Geosum existe sous deux formes dans les dossiers du site, cote a cote :
# un CSV et un classeur .xlsx produits par la geologie. Les deux portent la
# meme information sous des intitules differents, et rien n'indique a
# l'operateur lequel choisir -- donc on lit les deux.
#
#   CSV  : label | volume | tonnes | Au_ok     | Density | Destination   | Litho
#   xlsx : Sources |  --   | reserve tonnes | ReserveAu | density | Material type | Litho
#
# Le .xlsx est lu sans dependance : c'est une archive ZIP de XML, et la
# bibliotheque standard suffit. Ajouter openpyxl alourdirait l'executable
# distribue sur le poste de site pour un seul format de fichier.

GEOSUM_ENTETES = {
    # intitule (minuscules, sans espaces) -> colonne de la table oreblocs
    "label": "_nom", "sources": "_nom", "polygonid": "_nom",
    "volume": "volume",
    "tonnes": "tonnes", "reservetonnes": "tonnes",
    "au_ok": "au_teneur", "auok": "au_teneur", "reserveau": "au_teneur",
    "density": "density",
    "destination": "destination", "materialtype": "destination",
    "litho": "litho",
}


def _xlsx_lignes(octets):
    """Premiere feuille d'un .xlsx -> liste de listes de chaines.

    Lecture directe de l'archive : `sharedStrings.xml` porte les textes,
    `sheet1.xml` les cellules. Suffisant pour un tableau plat comme le
    Geosum ; ne gere ni formules ni mise en forme, dont on n'a pas besoin.
    """
    import io as _io
    import zipfile
    import xml.etree.ElementTree as ET
    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    try:
        z = zipfile.ZipFile(_io.BytesIO(octets))
    except zipfile.BadZipFile:
        return []          # pas une archive : ce n'est pas un .xlsx
    partages = []
    if "xl/sharedStrings.xml" in z.namelist():
        racine = ET.fromstring(z.read("xl/sharedStrings.xml"))
        for si in racine.findall(NS + "si"):
            partages.append("".join(t.text or "" for t in si.iter(NS + "t")))
    feuilles = [n for n in z.namelist()
                if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
    if not feuilles:
        return []
    racine = ET.fromstring(z.read(sorted(feuilles)[0]))
    def colonne_de(ref):
        """"D12" -> 3. Les cellules VIDES sont omises du XML : sans cette
        remise en place, une colonne vide decale toutes les suivantes vers
        la gauche et on lit la litho a la place de la destination."""
        lettres = "".join(c for c in str(ref or "") if c.isalpha()).upper()
        if not lettres:
            return None
        n = 0
        for c in lettres:
            n = n * 26 + (ord(c) - 64)
        return n - 1

    lignes = []
    for tr in racine.iter(NS + "row"):
        cellules = []
        for i, td in enumerate(tr.findall(NS + "c")):
            v = td.find(NS + "v")
            texte = "" if v is None else (v.text or "")
            if td.get("t") == "s":
                try:
                    texte = partages[int(texte)]
                except (ValueError, IndexError):
                    texte = ""
            elif td.get("t") == "inlineStr":
                texte = "".join(t.text or "" for t in td.iter(NS + "t"))
            pos = colonne_de(td.get("r"))
            if pos is None:
                pos = i
            while len(cellules) <= pos:
                cellules.append("")
            cellules[pos] = texte
        lignes.append(cellules)
    return lignes


def _geosum_octets(contenu):
    """Contenu recu -> octets bruts. Accepte data URL, base64 nu, ou texte."""
    if isinstance(contenu, bytes):
        return contenu
    texte = str(contenu or "")
    m = re.match(r"^data:[^;,]*;base64,(.*)$", texte, re.S)
    if m:
        try:
            return base64.b64decode(m.group(1))
        except Exception:
            return b""
    return texte.encode("utf-8", "replace")


def extraire_geosum(contenu):
    """Geosum (CSV ou xlsx) -> {cle_bloc: {colonnes}}. Vide si illisible."""
    octets = _geosum_octets(contenu)
    if octets[:2] == b"PK":
        lignes = _xlsx_lignes(octets)
    else:
        texte = octets.decode("utf-8-sig", "replace")
        texte = texte.replace("\r\n", "\n").replace("\r", "\n")
        brutes = [l for l in texte.splitlines() if l.strip()]
        if len(brutes) < 2:
            return {}
        # Separateur deduit de l'en-tete : le fichier de reserve est en
        # virgules, le fichier complet en points-virgules.
        sep = ";" if brutes[0].count(";") > brutes[0].count(",") else ","
        lignes = list(csv.reader(brutes, delimiter=sep))
    if len(lignes) < 2:
        return {}

    entete = [re.sub(r"[\s_]+", "", str(c).strip().lower()) for c in lignes[0]]
    # `au_ok` perd son souligne par la normalisation : on le rattrape.
    cibles = [GEOSUM_ENTETES.get(c, "") for c in entete]
    if "_nom" not in cibles:
        return {}
    iNom = cibles.index("_nom")

    out = {}
    for cellules in lignes[1:]:
        if len(cellules) <= iNom:
            continue
        nom = str(cellules[iNom]).strip()
        if not nom:
            continue
        cle = cle_bloc(nom)
        # Le fichier complet repete les attributs du bloc sur chacun de ses
        # sommets : la premiere occurrence suffit, les suivantes sont
        # identiques.
        if cle in out:
            continue
        bloc = {}
        for i, cible in enumerate(cibles):
            if not cible or cible == "_nom" or i >= len(cellules):
                continue
            brut = str(cellules[i]).strip()
            if brut == "":
                continue
            if cible == "destination":
                val = brut.upper()
                bloc[cible] = GEOSUM_ALIAS_DESTINATION.get(val, val)
            elif cible == "litho":
                bloc[cible] = brut
            else:
                v = _num(brut)
                if v is not None:
                    bloc[cible] = v
        # `_minerai` : le fichier complet le DIT (Destination W = sterile),
        # le fichier allege le laisse deduire de l'absence du bloc.
        dest = str(bloc.get("destination") or "").strip().upper()
        bloc["_minerai"] = 0 if (dest == GEOSUM_DESTINATION_STERILE or dest in codes_steriles()) else 1
        # La destination du sterile (`W`) est CONSERVEE : la table doit dire
        # ce que le fichier dit. Aucun risque de la confondre avec une
        # consigne de dumping area -- une famille inconnue du referentiel
        # retombe sur la zone la plus proche, ce qui est justement le
        # comportement voulu pour du sterile. Et si un jour une teneur `W`
        # est creee avec sa verse, l'acheminement devient explicite.
        out[cle] = bloc
    return out


# --- Pit d'une teneur et d'une source (18/11, regle utilisateur) ---------
# La teneur du Geosum porte le pit : F5M = Main Pit, F5W = West Pit. Un blast
# enregistre sous une source ne doit contenir AUCUN bloc de minerai de l'autre
# pit (ex. bloc F5W dans un blast LMP2 de Main Pit) : creation refusee.
TENEUR_PIT_RE = re.compile(r"^[A-Z]*\d+([MW])$")


def pit_de_source(codes, source_nom=""):
    """'M' (Main Pit) ou 'W' (West Pit) d'apres les codes de la source (LMP* /
    LWP*), a defaut d'apres son nom ; '' si indeterminable (pas de controle)."""
    cs = [str(c).strip().upper() for c in (codes or [])]
    main = any(c.startswith("LMP") for c in cs)
    west = any(c.startswith("LWP") for c in cs)
    if main != west:
        return "M" if main else "W"
    n = str(source_nom or "").upper()
    if "MAIN" in n and "WEST" not in n:
        return "M"
    if "WEST" in n and "MAIN" not in n:
        return "W"
    return ""


def blocs_autre_pit(contenus, pit):
    """Blocs de MINERAI dont la teneur (Destination du Geosum / de la base)
    appartient a l'autre pit que `pit`. Rend [(bloc, teneur)], trie."""
    if pit not in ("M", "W"):
        return []
    intrus = set()
    for contenu in contenus:
        if not contenu:
            continue
        for cle, bloc in extraire_geosum(contenu).items():
            if not bloc.get("_minerai"):
                continue
            teneur = str(bloc.get("destination") or "").strip().upper()
            m = TENEUR_PIT_RE.match(teneur)
            if m and m.group(1) != pit:
                intrus.add((cle, teneur))
    return sorted(intrus)


def cle_bloc(nom):
    """Cle d'appariement d'un nom de bloc entre DXF et Geosum.

    Un nom de calque DXF n'accepte pas le point decimal : le flitch 242.5
    y devient `242_5`, alors que le Geosum ecrit `242.5`. Comparer les noms
    bruts ferait echouer tous les blasts a flitch demi-entier -- et
    silencieusement, en les classant tous en sterile.
    """
    return str(nom or "").strip().upper().replace(".", "_")


def appliquer_geosum(conn, blast_id, geosum):
    """Remplit les blocs d'un blast depuis un Geosum deja parse.

    Rend un compte rendu : ce qui a ete enrichi, ce qui est du sterile, et
    ce que le Geosum annonce sans que le DXF le contienne -- ce dernier cas
    signale une incoherence entre les deux fichiers, pas une erreur de code.
    """
    rapport = {"minerai": [], "sterile": [], "absents_du_dxf": [], "blocs": 0, "apparies": 0, "sans_donnees": []}
    vus = set()
    for o in conn.execute(
            "SELECT id, nom FROM oreblocs WHERE blast_id=? AND actif=1", (blast_id,)):
        rapport["blocs"] += 1
        donnees = geosum.get(cle_bloc(o["nom"]))
        if donnees is None:
            # Absent du fichier : sterile, et sans donnees de reserve.
            conn.execute("UPDATE oreblocs SET minerai=0 WHERE id=?", (o["id"],))
            rapport["sterile"].append({"bloc": o["nom"], "tonnes": None})
            rapport["sans_donnees"].append(o["nom"])        # bloc du DXF dont le nom n'est pas dans le fichier
            continue
        rapport["apparies"] += 1
        vus.add(cle_bloc(o["nom"]))
        est_minerai = donnees.get("_minerai", 1)
        colonnes = ["minerai=%d" % (1 if est_minerai else 0)]
        valeurs = []
        for cible, val in donnees.items():
            if cible.startswith("_"):
                continue
            colonnes.append("%s=?" % cible)
            valeurs.append(val)
        valeurs.append(o["id"])
        conn.execute("UPDATE oreblocs SET %s WHERE id=?" % ", ".join(colonnes),
                     tuple(valeurs))
        if est_minerai:
            rapport["minerai"].append({"bloc": o["nom"],
                                       "teneur": donnees.get("destination", ""),
                                       "au": donnees.get("au_teneur")})
        else:
            # Sterile DOCUMENTE : il ne part pas sur les tablettes, mais son
            # tonnage compte pour le blast et le ratio sterile/minerai.
            rapport["sterile"].append({"bloc": o["nom"],
                                       "tonnes": donnees.get("tonnes")})
    rapport["absents_du_dxf"] = sorted(set(geosum) - vus)
    conn.commit()
    return rapport


def appliquer_base_attributs(conn, blast_id, base_contenu):
    """Attributs des blocs (volume, tonnes, Au, densite, destination, litho)
    depuis la BASE DE DONNEES du blast. Rend le nombre de blocs lus, 0 si la
    base est illisible ou ne porte pas ces colonnes.

    La base couvre TOUS les polygones, waste compris, quand le Geosum ne
    liste que le minerai : sans elle, les WST restaient a 0 dans le SaaS.
    Son format est celui du « Geosum complet » (`Destination` W = sterile) :
    meme lecteur, meme application. A appeler AVANT le Geosum.
    """
    attributs = extraire_geosum(base_contenu)
    if not attributs:
        return {"lus": 0, "illisible": True, "blocs": 0, "apparies": 0, "sans_donnees": [], "absents_du_dxf": []}
    rapport = appliquer_geosum(conn, blast_id, attributs)
    rapport["lus"] = len(attributs)
    return rapport


def normaliser_codes(valeur):
    """"HG, mo ; lg" -> "HG,MO,LG". Majuscules, sans doublon, ordre conserve.

    Les codes viennent du nom des blocs DXF et sont saisis a la main : on ne
    peut compter ni sur la casse ni sur un separateur constant.
    """
    brut = str(valeur or "").replace(";", ",").replace(" ", ",")
    vus, out = set(), []
    for c in brut.split(","):
        c = c.strip().upper()
        if c and c not in vus:
            vus.add(c)
            out.append(c)
    return ",".join(out)


def _num(chaine):
    """Convertit '1,5' / '1.5' -> float, sinon None."""
    try:
        return float(str(chaine).strip().replace(",", "."))
    except Exception:
        return None


# En-tetes de colonnes reconnus. Un export de leve arrive souvent en
# "id,X,Y,Z" : sans lire l'en-tete, le numero de sommet etait pris pour une
# abscisse et la vraie ordonnee etait ignoree -- polygone a 900 km de la fosse.
_CSV_X = ("x", "easting", "est", "e", "lon", "long", "longitude")
_CSV_Y = ("y", "northing", "nord", "n", "lat", "latitude")
_CSV_NOM = ("nom", "name", "zone", "bloc", "blast", "libelle", "label",
            "designation", "polygone", "polygon")


def _csv_colonnes(cellules):
    """En-tete CSV -> (i_nom, i_x, i_y), ou None si x et y introuvables."""
    inom = ix = iy = None
    for i, c in enumerate(cellules):
        cle = re.sub(r"[\s_.-]+", "", str(c).strip().lower())
        if ix is None and cle in _CSV_X:
            ix = i
        elif iy is None and cle in _CSV_Y:
            iy = i
        elif inom is None and cle in _CSV_NOM:
            inom = i
    return None if (ix is None or iy is None) else (inom, ix, iy)


def lire_csv_lignes(texte):
    """Texte CSV -> lignes de cellules (sans les lignes vides).

    Separateur deduit de la premiere ligne : `;` s'il y domine, sinon `,`.
    Un CSV d'Excel en francais (`;` et decimales a virgule) reste donc
    intact, et les champs entre guillemets perdent leurs guillemets. Avant,
    `;` devenait `,` puis tout etait coupe a la virgule : `6083,93` donnait
    deux cellules.
    """
    brutes = [l for l in str(texte or "").replace("\r\n", "\n").replace("\r", "\n").splitlines()
              if l.strip()]
    if not brutes:
        return []
    sep = ";" if brutes[0].count(";") > brutes[0].count(",") else ","
    return [[c.strip() for c in ligne] for ligne in csv.reader(brutes, delimiter=sep)]


def nom_bloc_pris(conn, nom):
    """Vrai si un bloc ACTIF d'un blast ACTIF porte deja ce nom.

    Un bloc reste actif d'un blast supprime (orphelin) ne doit plus bloquer
    le nom : c'est lui qui faisait nommer les polygones `... (2)`."""
    return conn.execute(
        "SELECT o.id FROM oreblocs o JOIN blasts b ON b.id = o.blast_id "
        "WHERE o.nom=? AND o.actif=1 AND b.actif=1", (nom,)).fetchone() is not None


def extraire_oreblocs_csv(texte, prefixe="Orebloc", nom_defaut=None):
    """CSV de polygones -> liste de (nom, sommets).

    Trois formes acceptees, separateur , ou ; :
      - en-tete explicite : "id,X,Y,Z", "nom,x,y", "Easting,Northing"...
        les colonnes sont alors lues par position, les autres ignorees ;
      - sans en-tete, "nom,x,y" : un nom repete regroupe un polygone ;
      - sans en-tete, "x,y" : une seule suite de coordonnees.

    `nom_defaut` nomme les polygones depourvus de colonne nom (typiquement
    le nom du fichier). Sans lui on retombe sur l'ancien "<prefixe> 1", qui
    collisionne des le deuxieme fichier importe.
    """
    lignes = lire_csv_lignes(texte)
    groupes = {}
    ordre = []
    cols = None                 # mapping issu d'un en-tete, si trouve
    nom_courant = None          # None = pas de polygone anonyme ouvert
    anonymes = 0

    def nom_anonyme():
        if nom_defaut:
            return nom_defaut if anonymes == 1 else "%s %d" % (nom_defaut, anonymes)
        return "%s %d" % (prefixe, anonymes)

    def ajouter(nom, x, y):
        groupes.setdefault(nom, [])
        if nom not in ordre:
            ordre.append(nom)
        groupes[nom].append([x, y])

    for brutes in lignes:
        # deux decoupages : positionnel (cellules vides conservees, sinon les
        # indices de l'en-tete glissent) et compact (heuristique historique).
        cellules = [c for c in brutes if c != ""]
        if not cellules:
            continue

        if cols is not None:
            inom, ix, iy = cols
            if len(brutes) <= max(ix, iy):
                continue
            x, y = _num(brutes[ix]), _num(brutes[iy])
            if x is None or y is None:
                continue                      # ligne d'en-tete repetee, etc.
            if inom is not None and inom < len(brutes) and brutes[inom]:
                nom_courant = brutes[inom]
            elif nom_courant is None:
                anonymes += 1
                nom_courant = nom_anonyme()
            ajouter(nom_courant, x, y)
            continue

        n0 = _num(cellules[0])
        if n0 is not None:
            if len(cellules) < 2:
                continue
            y = _num(cellules[1])
            if y is None:
                continue
            if nom_courant is None:
                anonymes += 1
                nom_courant = nom_anonyme()
            ajouter(nom_courant, n0, y)
        else:
            nom = cellules[0]
            x = _num(cellules[1]) if len(cellules) > 1 else None
            y = _num(cellules[2]) if len(cellules) > 2 else None
            if x is None or y is None:
                # en-tete : on en deduit les colonnes et on ferme le polygone
                # anonyme en cours.
                cols = _csv_colonnes(brutes)
                nom_courant = None
                continue
            nom_courant = nom
            ajouter(nom_courant, x, y)

    return [(nom, groupes[nom]) for nom in ordre if len(groupes[nom]) >= 3]


# ------------------------------------------------------------ dig_bloc -------
# La "base de donnees" d'un blast (CSV : option label | polygon | hole |
# id | x | y) doit arriver INTEGRALEMENT dans l'onglet Sheet `dig_bloc`,
# une ligne par sommet — c'est la que la tablette lit les polygones.
# Format cible = celui de la capture du 08/11 : colonnes
#   B:option label, C:polygon, D:hole, E:id, F:x, G:y
# Chaque polygone garde son numero d'origine (colonne polygon), chaque
# sommet son numero de point (colonne id). La supression du blast purge
# integralement ses lignes des deux onglets.

BLAST_BLOC_ONGLETS = {"blocs": "dig_bloc", "ore": "ore_dig_bloc"}


def lignes_dig_bloc(base_contenu, blast_id=None):
    """CSV de la base de donnees -> lignes `dig_bloc` VERBATIM.

    REGLE DU CLIENT (08/11) : le contenu de l'onglet doit etre IDENTIQUE
    au CSV fourni a la creation du blast. Aucune colonne inventee, aucun
    renommage : l'entete du CSV devient l'entete de l'onglet, chaque ligne
    du CSV devient une ligne de l'onglet, cellules conservees telles quelles.
    Retourne [entete, ...lignes] (tableaux de cellules).
    """
    return lire_csv_lignes(base_contenu)


def lignes_ore_dig_bloc(texte_geosum):
    """CSV du Geosum -> lignes `ore_dig_bloc` VERBATIM.

    REGLE DU CLIENT (08/11) : le contenu de l'onglet est IDENTIQUE au CSV
    Geosum fourni a la creation du blast. Aucune colonne ajoutee, aucun
    renommage : entete = entete, lignes = lignes. La tablette lit cet
    onglet a l'identique ce qui est dedans.
    Retourne [entete, ...lignes] (tableaux de cellules).

    Le Geosum arrive en DATA URL (CSV ou .xlsx, lu par readAsDataURL) : on le
    decode avec les memes lecteurs que `extraire_geosum`. Decoupe tel quel, le
    texte encode aurait donne une seule cellule de base64.
    """
    octets = _geosum_octets(texte_geosum)
    if not octets:
        return []
    if octets[:2] == b"PK":
        lignes = _xlsx_lignes(octets)
    else:
        lignes = lire_csv_lignes(octets.decode("utf-8-sig", "replace"))
    return [[str(c).strip() for c in l] for l in lignes if any(str(c).strip() for c in l)]


# 24/11 : FLITCH DEMI-ENTIER ECRIT AVEC UN UNDERSCORE. Un calque DXF n'accepte pas le point : le flitch 272.5 y
# devient « 272_5_LMP2_119_HG_FR_035_OP », alors que le Geosum et les numeros de blast ecrivent « 272.5 ». Lu tel
# quel, « 5 » passait pour le numero du blast : lot « 272_5 » au lieu de « 272.5_LMP2_119 », et la purge des
# onglets dig_bloc / ore_dig_bloc de Serveur_OT visait TOUTES les lignes « 272_5_* » (autres pits, autres blasts).
# Regle (aucun pas de flitch suppose) : un entier, puis 1 ou 2 chiffres, puis un segment qui commence par une
# lettre (le pit) = flitch decimal. 272_5_LMP2 -> 272.5_LMP2 ; 280_LMP2 et 277.5_LMP2 inchanges.
_FLITCH_SOULIGNE_RE = re.compile(r"^(\d+)[_,](\d{1,2})_(?=[A-Za-z])")


def nom_flitch_canonique(nom):
    """Nom de bloc / label avec le flitch ecrit avec un point (272_5_LMP2_... -> 272.5_LMP2_...)."""
    return _FLITCH_SOULIGNE_RE.sub(r"\1.\2_", str(nom or "").strip(), count=1)


_NOM_BLAST_RE = re.compile(r"^(\d+(?:[._]\d+)?)_[A-Za-z]+_([A-Za-z0-9]+)_\d+_\d+_(\d+)[A-Za-z0-9]*$")


def prefixe_blast(numero):
    """26/11 : prefixe des noms de blocs d'un blast (flitch_source_numero), tire de son NOM INTEGRAL
    (255_OM_LMP2_260_250_106 -> 255_LMP2_106 ; 275_OM_LMP2_280_270_119v3 -> 275_LMP2_119). '' si le nom ne suit pas
    la nomenclature. Les labels de blocs ne portent pas la profondeur : deux blasts actifs de meme prefixe (bancs
    superposes, ou deux versions) auraient des blocs indiscernables."""
    m = _NOM_BLAST_RE.match(str(numero or "").strip())
    if not m:
        return ""
    return "%s_%s_%s" % (m.group(1).replace("_", "."), m.group(2).upper(), m.group(3))


def proprietaire_nom_bloc(conn, nom):
    """Nom integral du blast ACTIF qui porte deja un bloc de ce nom, '' sinon."""
    r = conn.execute("SELECT b.numero FROM oreblocs o JOIN blasts b ON b.id = o.blast_id "
                     "WHERE o.nom=? AND o.actif=1 AND b.actif=1 LIMIT 1", (nom,)).fetchone()
    return r["numero"] if r else ""


def prefixe_lot_label(label):
    """Prefixe `flitch_site_blast` d'un label, '' si introuvable.

    270_LMP2_119_WST_FR_044_OP -> '270_LMP2_119' : tout jusqu'au premier
    segment entier pur (1 a 4 chiffres) apres le flitch, regle du numero de
    blast de Serveur_OT (`numeroBlastDepuisLabel`).
    24/11 : 272_5_LMP2_119_... -> '272.5_LMP2_119' (flitch normalise, voir nom_flitch_canonique)."""
    seg = nom_flitch_canonique(label).split("_")
    for i in range(1, len(seg)):
        if seg[i].isdigit() and 1 <= len(seg[i]) <= 4:
            return "_".join(seg[:i + 1])
    return ""


def numero_lot_onglets(conn, blast_id, numero, lignes=None):
    """Identifiant du lot envoye a Serveur_OT pour dig_bloc / ore_dig_bloc.

    Critere PAR FLITCH (12/11) : le serveur purge les lignes dont le label
    COMMENCE par ce prefixe. Le nom complet du blast (270_OM_LMP2_280_270_119)
    ne commence aucun label : rien n'etait remplace (doublons) ni purge.
    `119` seul viserait tous les flitchs du blast. Sources : le CSV envoye,
    les noms d'oreblocs du blast, puis le nom complet (aucune purge)."""
    if lignes and len(lignes) > 1:
        entete = [str(c).strip().strip('"').lower() for c in lignes[0]]
        for nom_col in ("label", "option label"):
            if nom_col in entete:
                i = entete.index(nom_col)
                for ligne in lignes[1:]:
                    pre = prefixe_lot_label(ligne[i] if i < len(ligne) else "")
                    if pre:
                        return pre
                break
    if blast_id:
        for (nom,) in conn.execute(
                "SELECT nom FROM oreblocs WHERE blast_id=? ORDER BY id", (blast_id,)):
            pre = prefixe_lot_label(nom)
            if pre:
                return pre
    return str(numero or "").strip()


def ot_pousser_onglets_blocs(conn, blast_id, numero, base_contenu, geosum_contenu=""):
    """Pousse les deux onglets du blast vers Serveur_OT.

    REGLE DU CLIENT (08/11) : le contenu des onglets est IDENTIQUE aux deux
    CSV fournis a la creation du blast — aucun remappage, aucune colonne
    inventee. lignes[0] = l'entete du CSV, les suivantes = les lignes du CSV,
    cellules conservees telles quelles.
    Contrat Apps Script : {numero, cible:'blocs'|'ore', lignes:[[...]]}
    Retourne (ok, erreur).
    """
    dig = lignes_dig_bloc(base_contenu) if base_contenu else []
    ore = lignes_ore_dig_bloc(geosum_contenu) if geosum_contenu else []
    # Lot = prefixe du flitch (270_LMP2_119) : le serveur remplace exactement
    # les lignes de ce flitch, et rien d'autre.
    numero = numero_lot_onglets(conn, blast_id, numero, dig or ore)
    erreurs = []
    if dig:
        ok, _, err = ot_appel("onglet_upload", payload={
            "numero": numero, "cible": "blocs", "lignes": dig,
        })
        if not ok:
            erreurs.append("dig_bloc: %s" % err)
    if ore:
        ok, _, err = ot_appel("onglet_upload", payload={
            "numero": numero, "cible": "ore", "lignes": ore,
        })
        if not ok:
            erreurs.append("ore_dig_bloc: %s" % err)
    if erreurs:
        return False, " ; ".join(erreurs)
    return True, ""


def ot_purger_onglets_blocs(numero):
    """Suppression INTEGRALE des lignes du blast dans les deux onglets.
    Appelle Serveur_OT avec action=onglet_purge."""
    # Harmonisation (10/11) : Serveur_OT ignore le parametre `onglet` et
    # purge TOUJOURS les deux onglets (dig_bloc + ore_dig_bloc) en un seul
    # appel onglet_purge. Un seul appel suffit ; on n'envoie plus `onglet`.
    ok1, _, err1 = ot_appel("onglet_purge", params={"numero": numero})
    if not ok1:
        return False, err1 or "echec purge"
    return True, ""


def extraire_oreblocs_dxf(texte, prefixe="Orebloc"):
    """Parseur DXF minimal (pas de lib externe) : entites LWPOLYLINE et
    POLYLINE/VERTEX (closes ou non) -> un orebloc par polyline (nom = calque,
    code 8). Retourne liste de (nom, sommets).

    Piege de la specification DXF : une entite POLYLINE porte dans son propre
    en-tete un point FICTIF (codes 10/20 toujours a 0,0) ; les vrais sommets
    n'arrivent qu'ensuite dans les entites VERTEX. Sans distinction, ce (0,0)
    entre dans le polygone et etire la boite englobante jusqu'a l'origine
    (constate sur un vrai fichier : tout le dessin ecrase sur une diagonale).
    On n'accepte donc les coordonnees d'une POLYLINE qu'apres un VERTEX ;
    pour une LWPOLYLINE, les 10/20 sont bien les sommets.
    """
    lignes = texte.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    L = len(lignes)
    blocs = []
    etat = {"auto_n": 0, "courant": None, "nom": "", "pending_x": None,
            "type": "", "dans_vertex": False}

    def cloturer():
        som = etat["courant"]
        if som and len(som) >= 3:
            etat["auto_n"] += 1
            nom = etat["nom"] or "%s %d" % (prefixe, etat["auto_n"])
            blocs.append((nom, som))
        etat["courant"] = None
        etat["nom"] = ""
        etat["pending_x"] = None
        etat["type"] = ""
        etat["dans_vertex"] = False

    k = 0
    while k < L - 1:
        try:
            code = int(lignes[k].strip())
        except Exception:
            k += 2
            continue
        val = lignes[k + 1].strip()
        k += 2
        if code == 0:
            mot = val.upper()
            if mot == "LWPOLYLINE":
                cloturer()                      # cloture la polyline precedente
                etat["courant"] = []
                etat["type"] = "LW"
            elif mot == "POLYLINE":
                cloturer()
                etat["courant"] = []
                etat["type"] = "POLY"
            elif mot == "VERTEX":
                etat["dans_vertex"] = True      # les vrais sommets commencent
                etat["pending_x"] = None
            elif mot in ("SEQEND", "ENDSEC", "EOF"):
                cloturer()
            else:
                cloturer()                      # autre entite : on ne la suit pas
        elif etat["courant"] is not None:
            if code == 8 and not etat["nom"]:
                etat["nom"] = val
            elif etat["type"] == "POLY" and not etat["dans_vertex"]:
                pass                            # point fictif de l'en-tete POLYLINE
            elif code == 10:
                etat["pending_x"] = _num(val)
            elif code == 20 and etat["pending_x"] is not None:
                y = _num(val)
                if y is not None:
                    etat["courant"].append([etat["pending_x"], y])
                etat["pending_x"] = None
    cloturer()
    return blocs
PLANS_DIR = os.path.join(DATA_DIR, "plans")


def apercu_carte(nom_fichier):
    """URL locale de la carte d'un blast, ou "" si le fichier n'est pas la.

    La page Dig Plan ne peut PAS afficher l'URL Drive : Drive renvoie
    `Cross-Origin-Resource-Policy: same-site`, et le navigateur refuse donc
    d'integrer l'image dans une page servie depuis un autre hote. Une
    tablette, elle, telecharge le fichier au lieu de l'integrer : elle n'est
    pas concernee, et `map_url` reste le lien Drive pour elle.

    URL RELATIVE a dessein : elle vaut depuis localhost comme depuis
    l'adresse reseau du poste, sans rien reconstruire.
    """
    base = os.path.basename(str(nom_fichier or "")).strip()
    if not base:
        return ""
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    if not os.path.isfile(os.path.join(PLANS_DIR, base)):
        return ""
    return "/data/plans/%s" % quote(base)


def deposer_carte(nom_fichier, data_url, hote):
    """Ecrit une carte (data URL base64) dans data/plans/ et rend son URL.

    Pendant local de deposerFichierDrive() cote Apps Script : le contenu
    binaire ne doit JAMAIS finir en base64 dans une colonne (incompatible
    avec le plafond de 50 000 caracteres d'une cellule Sheets).
    """
    m = re.match(r"^data:([^;,]+);base64,(.*)$", str(data_url or ""), re.S)
    if not m:
        return ""
    os.makedirs(PLANS_DIR, exist_ok=True)
    # nom assaini : pas de traversee de repertoire depuis un nom client
    base = os.path.basename(str(nom_fichier or "carte.jpg")).strip() or "carte.jpg"
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base)
    chemin = os.path.join(PLANS_DIR, base)
    with open(chemin, "wb") as f:
        f.write(base64.b64decode(m.group(2)))
    return "http://%s/data/plans/%s" % (hote, quote(base))


def point_in_polygon(px, py, polygon):
    """Ray casting algorithm. polygon = liste de [x, y]. Retourne True si dedans."""
    inside = False
    n = len(polygon)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > py) != (yj > py)):
            x_intersect = (xj - xi) * (py - yi) / (yj - yi) + xi
            if px < x_intersect:
                inside = not inside
        j = i
    return inside


def distance(ax, ay, bx, by):
    return math.hypot(ax - bx, ay - by)


def distance_point_segment(px, py, ax, ay, bx, by):
    """Distance d'un point au segment [A, B] (projection clampee)."""
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return distance(px, py, ax, ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return distance(px, py, ax + t * dx, ay + t * dy)


def distance_polygone(px, py, polygon):
    """Distance d'un point au contour d'un polygone (plus proche arete)."""
    n = len(polygon)
    if n < 2:
        return float("inf")
    dmin = float("inf")
    for i in range(n):
        ax, ay = polygon[i]
        bx, by = polygon[(i + 1) % n]
        d = distance_point_segment(px, py, ax, ay, bx, by)
        if d < dmin:
            dmin = d
    return dmin


def dans_ou_pres_polygone(px, py, polygon, marge):
    """Vrai si le point est a l'interieur du polygone OU a moins de
    `marge` metres de son contour (tolerance GPS / cahier des charges :
    'a l'interieur ou proche des limites d'un orebloc')."""
    if point_in_polygon(px, py, polygon):
        return True
    return distance_polygone(px, py, polygon) <= marge


def aire_signee(polygon):
    """Aire signee par la formule du chevauchement (shoelace)."""
    n = len(polygon)
    s = 0.0
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def aire_polygone(polygon):
    """Aire absolue du polygone (unites carrees = m2 pour le plan mine)."""
    if len(polygon) < 3:
        return 0.0
    return abs(aire_signee(polygon))


def centroide_polygone(polygon):
    """Centroide geometrique du polygone (moyenne des sommets si aire nulle)."""
    n = len(polygon)
    if not n:
        return 0.0, 0.0
    a = aire_signee(polygon)
    if a == 0:
        return (sum(p[0] for p in polygon) / n, sum(p[1] for p in polygon) / n)
    cx = 0.0
    cy = 0.0
    for i in range(n):
        x1, y1 = polygon[i]
        x2, y2 = polygon[(i + 1) % n]
        croix = x1 * y2 - x2 * y1
        cx += (x1 + x2) * croix
        cy += (y1 + y2) * croix
    return (cx / (6.0 * a), cy / (6.0 * a))


def polygone_depuis_point(x, y, rayon_m):
    """Polygone legacy pour une dumping area saisie comme un point + un rayon
    (ancien format / importer sans sommets) : carre de cote 2*rayon."""
    r = float(rayon_m or 50)
    return [[x - r, y - r], [x + r, y - r], [x + r, y + r], [x - r, y + r]]


def sommets_depuis_body(body):
    """Valide le polygone fourni par le client. Accepte une liste de [x, y]
    ou une chaine "x y; x y; ...". Retourne (polygone, erreur)."""
    raw = body.get("sommets")
    if isinstance(raw, str):
        pts = []
        for ligne in raw.replace(';', '\n').replace('|', '\n').splitlines():
            morceaux = ligne.replace(',', ' ').split()
            if len(morceaux) >= 2:
                try:
                    pts.append([float(morceaux[0]), float(morceaux[1])])
                except ValueError:
                    pass
        raw = pts
    poly = []
    for p in (raw or []):
        if isinstance(p, (list, tuple)) and len(p) >= 2:
            try:
                poly.append([float(p[0]), float(p[1])])
            except (TypeError, ValueError):
                pass
    if len(poly) < 3:
        return None, "sommets invalides : au moins 3 points [x, y] attendus"
    return poly, None


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------- base de donnees --
def get_conn():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


class VerrouTrace:
    """Verrou global qui signale qui le garde trop longtemps (12/11).

    La passerelle s'est figee : une route gardait LOCK pendant une serie
    d'appels a Google et toutes les autres attendaient, sans que rien dise
    laquelle. Au-dela de SEUIL_S secondes, la route est ecrite au journal.
    """
    SEUIL_S = 5.0

    def __init__(self):
        self._verrou = threading.Lock()
        self._local = threading.local()

    def route(self, libelle):
        self._local.libelle = libelle

    def __enter__(self):
        self._verrou.acquire()
        self._local.debut = time.time()
        return self

    def __exit__(self, *exc):
        duree = time.time() - getattr(self._local, "debut", time.time())
        libelle = getattr(self._local, "libelle", "(hors route)")
        self._verrou.release()
        if duree >= self.SEUIL_S:
            print("VERROU LENT : %s garde %.1f s" % (libelle, duree), flush=True)
        return False


LOCK = VerrouTrace()


# ------------------------------------------------------- calibration GPS --
def _resoudre_3x3(M, v):
    """Resout M*sol = v (systeme 3x3) par elimination de Gauss avec pivot
    partiel. Retourne None si le systeme est degenere (points colineaires)."""
    A = [list(M[i]) + [v[i]] for i in range(3)]
    for i in range(3):
        piv = max(range(i, 3), key=lambda r: abs(A[r][i]))
        if abs(A[piv][i]) < 1e-9:
            return None
        A[i], A[piv] = A[piv], A[i]
        for r in range(3):
            if r != i:
                facteur = A[r][i] / A[i][i]
                for c in range(4):
                    A[r][c] -= facteur * A[i][c]
    return [A[i][3] / A[i][i] for i in range(3)]


def ajuster_affine(points):
    """Calibration GPS -> repere local par moindres carres (sans dependance
    externe). points = liste de (lon, lat, x, y), au moins 3 points non
    colineaires. Resout x = a*lon + b*lat + c ; y = d*lon + e*lat + f.
    Retourne (a,b,c,d,e,f) ou None si points insuffisants/degeneres."""
    if len(points) < 3:
        return None
    ATA = [[0.0] * 3 for _ in range(3)]
    ATx = [0.0] * 3
    ATy = [0.0] * 3
    for lon, lat, x, y in points:
        ligne = (lon, lat, 1.0)
        for i in range(3):
            for j in range(3):
                ATA[i][j] += ligne[i] * ligne[j]
            ATx[i] += ligne[i] * x
            ATy[i] += ligne[i] * y
    sol_x = _resoudre_3x3(ATA, ATx)
    sol_y = _resoudre_3x3(ATA, ATy)
    if sol_x is None or sol_y is None:
        return None
    return tuple(sol_x) + tuple(sol_y)


def calculer_calibration(conn):
    """Recalcule la transformation affine GPS -> repere local a partir des
    points de calibration actifs, et l'enregistre (avec l'erreur residuelle
    en metres). Retourne le resultat, ou None si pas assez de points."""
    pts = [dict(r) for r in conn.execute(
        "SELECT * FROM calibration_points WHERE actif=1 ORDER BY id")]
    data = [(p["lon"], p["lat"], p["x"], p["y"]) for p in pts]
    sol = ajuster_affine(data)
    if sol is None:
        return None
    a, b, c, d, e, f = sol
    erreurs = []
    for lon, lat, x, y in data:
        xp = a * lon + b * lat + c
        yp = d * lon + e * lat + f
        erreurs.append(distance(xp, yp, x, y))
    rmse = math.sqrt(sum(err * err for err in erreurs) / len(erreurs))
    emax = max(erreurs)
    conn.execute(
        "INSERT INTO calibration (a, b, c, d, e, f, nb_points, rmse_m, emax_m) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (a, b, c, d, e, f, len(data), rmse, emax))
    conn.commit()
    return {"a": a, "b": b, "c": c, "d": d, "e": e, "f": f,
            "nb_points": len(data), "rmse_m": rmse, "emax_m": emax}


# -------------------------------------------------------- position des engins --
def gps_vers_local(conn, lat, lon):
    """Convertit lat/lon -> (x, y) repere local mine via la derniere
    calibration affine enregistree (calculer_calibration). Si aucune
    calibration n'existe encore (pas assez de points de reference releves),
    repli sur une projection equirectangulaire approximative : fiable pour
    les distances relatives (immobilisation), PAS pour le geofencing contre
    les oreblocs tant que la calibration n'est pas faite."""
    cal = conn.execute("SELECT * FROM calibration ORDER BY id DESC LIMIT 1").fetchone()
    if cal:
        x = cal["a"] * lon + cal["b"] * lat + cal["c"]
        y = cal["d"] * lon + cal["e"] * lat + cal["f"]
        return x, y
    if not GPS_ORIGINE_LAT and not GPS_ORIGINE_LON:
        # Sans calibration ni origine explicite : projection UTM, le repere
        # des pistes, oreblocs et dumping areas. L'ancien repli
        # equirectangulaire centre sur (0, 0) placait un DT reel a des
        # centaines de km des pistes (sortie de route permanente).
        zone, hemisphere = repere_site(conn)
        return wgs84_vers_utm(lat, lon, zone, hemisphere)
    y = (lat - GPS_ORIGINE_LAT) * METRES_PAR_DEGRE_LAT
    x = (lon - GPS_ORIGINE_LON) * METRES_PAR_DEGRE_LAT * math.cos(math.radians(lat))
    return x, y


POSITIONS_DT_JOURS = 7
_positions_dt_compteur = [0]


def historiser_position_dt(conn, code, typ, x, y, instant, existe):
    """Garde la position d'un DT dans positions_dt (suivi, trajets). Pas de
    commit ici : celui de appliquer_position_engin suit."""
    if str(typ).upper() != "DT":
        return
    if existe is not None:
        bouge = distance(x, y, existe["x"], existe["y"]) > 1.0
        if not bouge and abs(_secondes_depuis(existe["derniere_maj"], instant)) < 30:
            return
    conn.execute("INSERT INTO positions_dt (code, x, y, horodatage) VALUES (?, ?, ?, ?)",
                 (code, x, y, instant))
    _positions_dt_compteur[0] += 1
    if _positions_dt_compteur[0] % 500 == 1:          # purge de temps en temps
        conn.execute("DELETE FROM positions_dt WHERE horodatage < datetime('now', 'localtime', ?)",
                     ("-%d days" % POSITIONS_DT_JOURS,))


# ---- canaux et auteurs des positions (21/11) ----
# Une meme position peut arriver par plusieurs canaux (MQTT en direct, remontees de Serveur_SG ~30 s plus tard, radio de secours).
# On garde pour chaque position QUI l'a envoyee (auth) et PAR OU elle est arrivee (canal), pour ne traiter qu'une fois
# la meme observation et pour ne pas laisser un canal moins fiable prendre la place d'un canal authentifie.
RANG_AUTH = {"console": 4, "hmac": 3, "broker": 3, "jeton": 3, "aucune": 1, "": 0}
LIBELLE_CANAL = {"mqtt": "MQTT (internet)", "http": "Remontées (Serveur_SG)", "radio": "Radio XBee (secours)", "console": "Console",
                 "mqtt-relais": "Relayée par un DT (MQTT)", "http-relais": "Relayée par un DT (remontées)"}
LIBELLE_AUTH = {"console": "console", "hmac": "signature radio", "broker": "compte MQTT", "jeton": "jeton de tablette", "aucune": "non authentifiée"}
DOUBLON_FENETRE_S = 2.0        # meme observation : a moins de 2 s ...
DOUBLON_DISTANCE_M = 2.0       # ... et a moins de 2 m, vue par deux canaux differents
PRIORITE_FENETRE_S = 5.0       # un canal moins fiable ne remplace pas un canal plus fiable dans cette fenetre


def _epoch_local(texte):
    try:
        return time.mktime(datetime.strptime(str(texte)[:19], "%Y-%m-%d %H:%M:%S").timetuple())
    except (TypeError, ValueError):
        return None


def noter_canal(conn, code, canal, auth):
    """Memorise que `code` vient d'etre entendu par `canal` (et comment son auteur a ete authentifie)."""
    conn.execute(
        "INSERT INTO positions_canaux (code, canal, auth, vu_epoch, vu_le, nb) VALUES (?, ?, ?, ?, ?, 1) "
        "ON CONFLICT(code, canal) DO UPDATE SET auth=excluded.auth, vu_epoch=excluded.vu_epoch, vu_le=excluded.vu_le, nb=nb+1",
        (code, canal, auth, time.time(), now_iso()))


def canaux_de(conn, code, fenetre_s=300):
    """Canaux qui ont entendu `code` dans la derniere fenetre, du plus recent au plus ancien."""
    maintenant = time.time()
    return [{"canal": r["canal"], "libelle": LIBELLE_CANAL.get(r["canal"], r["canal"]), "auth": r["auth"],
             "auth_libelle": LIBELLE_AUTH.get(r["auth"], r["auth"]), "age_s": round(maintenant - r["vu_epoch"])}
            for r in conn.execute("SELECT * FROM positions_canaux WHERE code=? AND vu_epoch>? ORDER BY vu_epoch DESC",
                                  (code, maintenant - fenetre_s))]


def doublon_de_position(existe, canal, auth, x, y, instant):
    """True si cette position n'apporte rien : meme observation deja recue par un autre canal, ou canal moins fiable
    pour une observation quasi simultanee d'un canal plus fiable."""
    if not canal or existe is None or not existe["canal"] or existe["canal"] == canal:
        return False
    t_nouveau, t_ancien = _epoch_local(instant), _epoch_local(existe["derniere_maj"])
    if t_nouveau is None or t_ancien is None:
        return False
    ecart = abs(t_nouveau - t_ancien)
    if ecart <= DOUBLON_FENETRE_S and distance(x, y, existe["x"], existe["y"]) <= DOUBLON_DISTANCE_M:
        return True
    return ecart <= PRIORITE_FENETRE_S and RANG_AUTH.get(auth, 0) < RANG_AUTH.get(existe["auth"], 0)


def appliquer_position_engin(conn, code, typ, x, y, horodatage=None, relayer=True, canal="", auth=""):
    """Met a jour (ou cree) un engin a la position (x, y) et gere le chrono
    d'immobilisation (seuil SEUIL_IMMOBILISATION_M). Utilise par la route
    HTTP /api/engins, l'ecouteur radio XBee et la lecture des remontees.

    `horodatage` : heure de l'observation (remontees) ; par defaut maintenant.
    `relayer=False` : ne pas renvoyer la position a Serveur_SG (elle en vient).
    `canal` / `auth` : par ou elle est arrivee et comment son auteur est authentifie (voir RANG_AUTH)."""
    instant = horodatage or now_iso()
    existe = conn.execute("SELECT * FROM engins WHERE code=?", (code,)).fetchone()
    if canal:
        noter_canal(conn, code, canal, auth)
        if doublon_de_position(existe, canal, auth, x, y, instant):
            conn.commit()
            return
    # 19/11 : deux sources de positions (MQTT en direct, remontees de SG avec
    # ~30 s de retard). Une position plus ANCIENNE que la derniere connue va
    # seulement dans l'historique : elle ne doit pas ramener le DT en arriere.
    if horodatage and existe is not None and str(typ or existe["type"]).upper() == "DT":
        derniere = conn.execute("SELECT MAX(horodatage) FROM positions_dt WHERE code=?", (code,)).fetchone()[0]
        if derniere and horodatage < derniere:
            conn.execute("INSERT INTO positions_dt (code, x, y, horodatage) VALUES (?, ?, ?, ?)",
                         (code, x, y, horodatage))
            conn.commit()
            tracking_enregistrer(conn, existe, code, typ or existe["type"], x, y, horodatage, retard=True)
            return
    tracking_enregistrer(conn, existe, code, (existe["type"] if existe else typ) or "DT", x, y, instant)
    historiser_position_dt(conn, code, (existe["type"] if existe else typ) or "DT",
                           x, y, instant, existe)
    if existe:
        deplacement = distance(x, y, existe["x"], existe["y"])
        if deplacement <= SEUIL_IMMOBILISATION_M:
            immobilise_depuis = existe["immobilise_depuis"] or instant
        else:
            immobilise_depuis = ""
        conn.execute(
            "UPDATE engins SET x=?, y=?, immobilise_depuis=?, derniere_maj=?, canal=?, auth=? WHERE code=?",
            (x, y, immobilise_depuis, instant, canal or existe["canal"] or "", auth or existe["auth"] or "", code))
    else:
        conn.execute(
            "INSERT INTO engins (code, type, x, y, derniere_maj, canal, auth) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (code, typ, x, y, instant, canal, auth))
    conn.commit()
    # Relais vers Serveur_SG : point d'entree UNIQUE des positions (HTTP et
    # radio XBee passent tous deux ici), donc rien ne peut etre oublie.
    if relayer:
        sg_relayer_position(code, typ or "DT", x, y)
        sg_vider_file()
    # geofencing dumping area : le DT est-il entre dans la zone de sa mission ?
    if (typ or "DT") == "DT":
        verifier_arrivee_da(conn, code)
        # sortie de route : hors de toute piste (hors zones autorisees)
        try:
            verifier_offroad(conn, code, instant)
        except Exception as e:      # ne jamais perdre une position pour ca
            print("OFFROAD: verification impossible pour %s: %s" % (code, e))
        try:
            verifier_checkpoints(conn, code, instant)
        except Exception as e:
            print("CHECKPOINT: verification impossible pour %s: %s" % (code, e))
        # apres l'offroad : un DT hors piste n'est pas aussi signale devie
        try:
            verifier_deviation(conn, code, instant)
        except Exception as e:
            print("DEVIATION: verification impossible pour %s: %s" % (code, e))


# --------------------------------------------- authentification des trames --
def extraire_corps_signe(ligne):
    """Isole le texte JSON signe (tous les champs sauf 'sig'), en coupant la
    ligne brute juste avant le suffixe ,"sig":"...."} plutot qu'en
    re-serialisant l'objet parse. Evite toute divergence de formattage des
    nombres flottants entre JS (tablette) et Python (serveur) : on verifie
    la signature sur les OCTETS EXACTS qui ont ete signes cote tablette,
    pas sur une reconstruction. Retourne None si le suffixe est absent."""
    idx = ligne.rfind(',"sig":"')
    if idx == -1:
        return None
    return ligne[:idx] + "}"


def verifier_signature_pelle(conn, code, ligne):
    """Trame d'EXCAVATRICE (CRC « ck » + signature « sg » placee avant le CRC) : 'ok', 'non_signee' ou 'invalide'.
    Meme calcul que l'app (radio-protocol.ts : signerLigne) : HMAC-SHA256 du texte sans « ck » ni « sg », 32 premiers hexa."""
    t1 = re.sub(r',"ck":"[0-9a-f]{4}"\}$', "}", str(ligne).strip())
    m = re.search(r',"sg":"([0-9a-f]{32})"\}$', t1)
    if not m:
        return "non_signee"
    row = conn.execute("SELECT cle_hex, cle_prec, prec_jusqua FROM cles_dt WHERE dt_code=? AND actif=1", (code,)).fetchone()
    if not row:
        return "invalide"                                 # pelle inconnue de la console
    corps = t1[:m.start()] + "}"
    cles = [row["cle_hex"]] + ([row["cle_prec"]] if row["cle_prec"] and (row["prec_jusqua"] or 0) > time.time() else [])
    for cle_hex in cles:
        try:
            attendu = hmac.new(bytes.fromhex(cle_hex), corps.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
        except ValueError:
            continue
        if hmac.compare_digest(attendu, m.group(1)):
            return "ok"
    return "invalide"


def verifier_signature_trame(conn, dt_code, corps_signe, ts, sig):
    """Verifie la signature HMAC-SHA256 d'une trame XBee pour dt_code.
    Rejette si : aucune cle active enregistree pour ce DT, horodatage
    absent/hors tolerance (rejeu ou derive d'horloge), ou signature
    invalide. Retourne True/False."""
    row = conn.execute(
        "SELECT cle_hex, cle_prec, prec_jusqua FROM cles_dt WHERE dt_code=? AND actif=1", (dt_code,)).fetchone()
    if not row:
        return False
    if corps_signe is None or ts is None or not sig:
        return False
    try:
        ts_f = float(ts)
    except Exception:
        return False
    if abs(time.time() - ts_f) > HMAC_TOLERANCE_S:
        return False
    candidates = [row["cle_hex"]]
    if row["cle_prec"] and (row["prec_jusqua"] or 0) > time.time():
        candidates.append(row["cle_prec"])           # renouvellement en cours : l'ancienne cle reste valable quelques jours
    for cle_hex in candidates:
        try:
            cle = bytes.fromhex(cle_hex)
        except Exception:
            continue
        attendu = hmac.new(cle, corps_signe.encode("utf-8"), hashlib.sha256).hexdigest()
        if hmac.compare_digest(attendu, str(sig)):
            return True
    return False


def traiter_trame_xbee(conn, ligne):
    """Parse une trame JSON montee par radio XBee, ex. {"id":"DT_04",
    "lat":6.12,"lon":-1.52,"ts":...,"sig":"..."} (ou {"x":...,"y":...} en
    test banc sans GPS), verifie sa signature HMAC puis met a jour la
    position de l'engin. Retourne (code, x, y) si traite, None si la trame
    est ignoree (vide, invalide, sans position, ou non authentifiee)."""
    ligne = (ligne or "").strip()
    if not ligne:
        return None
    try:
        trame = json.loads(ligne)
    except Exception:
        print("XBEE: trame JSON invalide ignoree:", ligne[:200])
        return None
    code = trame.get("id") or trame.get("code")
    if not code:
        print("XBEE: trame sans id ignoree:", ligne[:200])
        return None
    # Trame d'EXCAVATRICE entendue sur la radio (elles portent « ck », pas « sig ») : on constate seulement QUI parle
    # (signature valide ou non) ; elle n'est pas une position de DT et n'est pas rejetee comme telle.
    if "sig" not in trame and "ck" in trame:
        resultat = verifier_signature_pelle(conn, str(code), ligne)
        _xbee_etat["pelles_" + resultat] = _xbee_etat.get("pelles_" + resultat, 0) + 1
        _xbee_etat["derniere_pelle"] = {"id": str(code), "resultat": resultat, "le": now_iso()}
        return "pelle"
    corps_signe = extraire_corps_signe(ligne)
    if not verifier_signature_trame(conn, code, corps_signe, trame.get("ts"), trame.get("sig")):
        print("XBEE: trame non authentifiee rejetee pour id=%s" % code)
        return None
    if "lat" in trame and "lon" in trame:
        lat = _num(trame.get("lat"))
        lon = _num(trame.get("lon"))
        if lat is None or lon is None:
            print("XBEE: lat/lon invalides ignores:", ligne[:200])
            return None
        x, y = gps_vers_local(conn, lat, lon)
    else:
        x = _num(trame.get("x"))
        y = _num(trame.get("y"))
        if x is None or y is None:
            print("XBEE: trame sans position exploitable ignoree:", ligne[:200])
            return None
    typ = trame.get("type", "DT")
    # heure de la MESURE (portee par la trame signee) : une trame de secours en retard ne ramene pas le DT en arriere
    try:
        instant = datetime.fromtimestamp(float(trame.get("ts"))).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        instant = None
    instant = instant or now_iso()        # meme instant pour la position et sa vitesse
    if trame.get("vk") is not None:
        noter_vitesse_gnss(code, trame.get("vk"), instant)
    appliquer_position_engin(conn, code, typ, x, y, horodatage=instant, canal="radio", auth="hmac")
    statuts_engins(conn)
    verifier_affectations_auto(conn)
    notifier_office(conn)
    return (code, x, y)


_xbee_serie = {"ser": None}            # port ouvert par l'ecouteur (pour la simulation d'une pelle)
_xbee_ecriture = threading.Lock()
_xbee_etat = {"pyserial": serial is not None, "connecte": False, "port": "", "baud": 0, "erreur": "",
              "valides": 0, "rejetees": 0, "derniere_valide": "", "derniere_recue": "",
              "pelles_ok": 0, "pelles_non_signee": 0, "pelles_invalide": 0, "derniere_pelle": None}


def xbee_conf():
    """(port, vitesse) de la radio de la passerelle : reglage de la console, sinon variable d'environnement."""
    # la console prime ; la variable d'environnement ne sert que tant qu'aucun port n'est choisi dans la console
    port = (param_lire_base("XBEE_PORT", "") or os.environ.get("XBEE_PORT", "")).strip()
    try:
        baud = int(param_lire_base("XBEE_BAUD", "") or os.environ.get("XBEE_BAUD", "") or "9600")
    except ValueError:
        baud = 9600
    return port, (baud if baud in XBEE_BAUDS else 9600)


def xbee_ports():
    """Ports serie du poste (pour choisir la radio dans la console)."""
    if serial is None:
        return []
    try:
        from serial.tools import list_ports
        return [{"device": p.device, "description": (p.description or "")[:80]} for p in sorted(list_ports.comports(), key=lambda x: x.device)]
    except Exception:
        return []


def xbee_ecrire_conf(conn, port, baud):
    port = str(port or "").strip()
    if port and not re.match(r"^(COM\d{1,3}|/dev/[A-Za-z0-9_./-]{2,40})$", port):
        return 400, {"error": "port invalide (COM5, /dev/ttyUSB0)"}
    try:
        baud = int(baud)
    except (TypeError, ValueError):
        return 400, {"error": "vitesse invalide"}
    if baud not in XBEE_BAUDS:
        return 400, {"error": "vitesse non prise en charge : %s" % baud}
    param_ecrire(conn, "XBEE_PORT", port)
    param_ecrire(conn, "XBEE_BAUD", baud)
    return 200, {"ok": True, "port": port, "baud": baud}


def _crc16_ccitt(texte):
    """CRC-16/CCITT-FALSE sur les codes de caracteres (comme radio-protocol.ts : crc16)."""
    crc = 0xFFFF
    for c in texte:
        crc ^= (ord(c) & 0xFFFF) << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return "%04x" % crc


MODES_PELLE_TEST = ("signee", "non_signee", "mauvaise_cle", "inconnue")
TYPES_TRAME_PELLE_TEST = ("prop", "load", "end")


def trame_pelle_test(conn, pelle, mode, dt_code, type_trame="prop", pos=None):
    """Proposition de chargement (k=prop) d'une pelle SIMULEE, adressee a `dt_code`, a la position de ce DT, pour tester le filtre
    de signature des DT avec une seule radio libre. Memes octets que l'app Excavatrice (serializeFrame + signerLigne)."""
    if mode not in MODES_PELLE_TEST:
        raise ValueError("mode inconnu : %s" % mode)
    if type_trame not in TYPES_TRAME_PELLE_TEST:
        raise ValueError("type de trame inconnu : %s" % type_trame)
    eng = conn.execute("SELECT x, y FROM engins WHERE code=?", (dt_code,)).fetchone()
    if not eng:
        raise ValueError("DT inconnu : %s" % dt_code)
    zone, hemi = repere_site(conn)
    px, py = (float(pos[0]), float(pos[1])) if pos else (eng["x"], eng["y"])    # position de la pelle : imposee, sinon celle du DT
    ll = _vers_latlng(px, py, zone, hemi or "N") if zone else None
    ident = pelle if mode != "inconnue" else "EX-9999"
    trame = {"v": 3, "k": type_trame, "id": ident, "nom": "EX", "dt": dt_code,
             "heure": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    if ll:
        trame["Long"], trame["Lat"] = round(ll[1], 7), round(ll[0], 7)
    trame.update({"Ele": 359.0, "E": round(px, 1), "N": round(py, 1),
                  "Zone": "%s%s" % (zone or 30, hemi or "N"), "Statut": "NOM" if type_trame == "end" else "OL", "points": 0 if type_trame == "prop" else 5, "p": 30, "ld": 600})
    corps = json.dumps(trame, separators=(",", ":"), ensure_ascii=False)
    if mode != "non_signee":
        cle = cle_radio_courante(conn, pelle) if mode == "signee" else secrets.token_hex(32)
        if not cle:
            raise ValueError("pelle sans cle radio : %s" % pelle)
        sg = hmac.new(bytes.fromhex(cle), corps.encode("utf-8"), hashlib.sha256).hexdigest()[:32]
        corps = corps[:-1] + ',"sg":"%s"}' % sg
    return corps[:-1] + ',"ck":"%s"}' % _crc16_ccitt(corps)


def xbee_emettre(ligne):
    """Ecrit une ligne sur la radio de la passerelle ; (ok, motif)."""
    ser = _xbee_serie["ser"]
    if ser is None:
        return False, "radio non connectee"
    try:
        with _xbee_ecriture:
            ser.write((ligne + "\r\n").encode("utf-8"))
            ser.flush()
        return True, ""
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, str(e)[:100])


def xbee_etat_public():
    port, baud = xbee_conf()
    return dict(_xbee_etat, configure_port=port, configure_baud=baud, bauds=list(XBEE_BAUDS), ports=xbee_ports(),
                environnement=bool(os.environ.get("XBEE_PORT")), exiger_signature=radio_exiger_signature())


def ecouteur_xbee():
    """Thread d'ecoute radio XBee (montee des positions DT via la Base RTK +
    relais DigiMesh). Se desactive proprement si pyserial n'est pas
    installe ou si XBEE_PORT n'est pas configure (poste de dev sans
    radio branchee) ; le reste du serveur continue de fonctionner."""
    if serial is None:
        _xbee_etat["erreur"] = "pyserial non installe (pip install pyserial)"
        print("XBEE: pyserial non installe, ecoute radio desactivee (pip install pyserial)")
        return
    while True:
        port, baud = xbee_conf()
        if not port:
            _xbee_etat.update(connecte=False, port="", baud=0, erreur="")
            time.sleep(5)                    # aucun port choisi : on attend qu'il le soit (console)
            continue
        try:
            with serial.Serial(port, baud, timeout=5) as ser:
                print("XBEE: connecte sur %s @ %d bauds" % (port, baud))
                _xbee_etat.update(connecte=True, port=port, baud=baud, erreur="")
                _xbee_serie["ser"] = ser
                while True:
                    if xbee_conf() != (port, baud):
                        break                # reglage change dans la console : on rouvre avec le nouveau
                    brut = ser.readline()
                    if not brut:
                        continue
                    _xbee_etat["derniere_recue"] = now_iso()
                    conn = get_conn()
                    try:
                        with LOCK:
                            res = traiter_trame_xbee(conn, brut.decode("utf-8", errors="replace"))
                    finally:
                        conn.close()
                    if res == "pelle":
                        pass                     # trame d'excavatrice : comptee a part (pelles_*)
                    elif res:
                        _xbee_etat["valides"] += 1
                        _xbee_etat["derniere_valide"] = now_iso()
                    else:
                        _xbee_etat["rejetees"] += 1
        except Exception as e:
            _xbee_serie["ser"] = None
            _xbee_etat.update(connecte=False, erreur="%s: %s" % (type(e).__name__, str(e)[:120]))
            print("XBEE: erreur liaison serie (%s), nouvelle tentative dans 5s" % e)
            time.sleep(5)


# ----------------------------------------------------------- logique metier -----
# 20/11 : une position n'est « en direct » que si elle a moins de POSITION_FRAICHE_S secondes.
# Les excavatrices d'un ancien essai restaient OMEX avec leur derniere position (ex. « ivan »,
# « 88 », EXCA-02/03/04 du 18/11) : un DT arrete pres de cet emplacement passait ODT, et des DT
# figes en ODT depuis des jours restaient « charges ». Une pelle muette n'ouvre plus de chargement ;
# un DT muet n'est ni ODT ni SBDT.
POSITION_FRAICHE_S = 120


def age_position_s(derniere_maj):
    """Age (s) de la derniere POSITION ; infini si inconnu / illisible."""
    try:
        t = datetime.strptime(str(derniere_maj)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return float("inf")
    return (datetime.now() - t).total_seconds()


def position_fraiche(engin):
    return age_position_s(engin["derniere_maj"]) <= POSITION_FRAICHE_S


def statuts_engins(conn):
    """Recalcule les statuts WEX/OMEX (exca) et WDT/SBDT/ODT (DT) selon les positions.
    Seules les positions FRAICHES comptent ; `derniere_maj` (heure de la derniere position)
    n'est jamais modifiee ici."""
    engins = [dict(r) for r in conn.execute("SELECT * FROM engins WHERE actif=1")]
    excas = [e for e in engins if e["type"] == "EXCA"]
    dts = [e for e in engins if e["type"] == "DT"]
    oreblocs = [dict(r) for r in conn.execute("SELECT * FROM oreblocs WHERE actif=1")]

    # 1) Statut excavatrice : OMEX si dans un orebloc ET en direct, sinon WEX
    for e in excas:
        dedans = False
        if position_fraiche(e):
            for ob in oreblocs:
                poly = json.loads(ob["sommets"] or "[]")
                if point_in_polygon(e["x"], e["y"], poly):
                    dedans = True
                    break
        nouveau = "OMEX" if dedans else "WEX"
        if nouveau != e["statut"]:
            conn.execute("UPDATE engins SET statut=? WHERE id=?", (nouveau, e["id"]))
            e["statut"] = nouveau

    # 2) Statut DT
    excas_omex = [e for e in excas if e["statut"] == "OMEX"]
    for d in dts:
        x, y = d["x"], d["y"]
        immo_min = 0
        if d["immobilise_depuis"]:
            try:
                immo_min = (datetime.now() - datetime.strptime(d["immobilise_depuis"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 60.0
            except Exception:
                immo_min = 0

        # cherche exca OMEX la plus proche
        exca_proche = None
        dist_min = float("inf")
        for ex in excas_omex:
            dist = distance(x, y, ex["x"], ex["y"])
            if dist < dist_min:
                dist_min = dist
                exca_proche = ex

        # est-il dans ou proche (MARGE_OREBLOC_M) d'un orebloc ?
        dans_orebloc = False
        for ob in oreblocs:
            poly = json.loads(ob["sommets"] or "[]")
            if dans_ou_pres_polygone(x, y, poly, MARGE_OREBLOC_M):
                dans_orebloc = True
                break

        if not position_fraiche(d):
            nouveau = "WDT"          # DT muet : on ne le declare pas charge d'apres une position ancienne
        elif exca_proche and dist_min < DT_PROCHE_EXCA_ODT and immo_min >= IMMOBILISATION_ODT_MIN and dans_orebloc:
            nouveau = "ODT"
        elif exca_proche and dist_min > DT_LOIN_EXCA_SBDT and exca_proche["statut"] == "OMEX" and immo_min >= IMMOBILISATION_SBDT_MIN:
            nouveau = "SBDT"
        else:
            nouveau = "WDT"

        if nouveau != d["statut"]:
            conn.execute("UPDATE engins SET statut=? WHERE id=?", (nouveau, d["id"]))
            d["statut"] = nouveau

    conn.commit()
    return [dict(r) for r in conn.execute("SELECT * FROM engins WHERE actif=1")]


def boucle_statuts():
    """Reevalue les statuts toutes les 30 s : sans cela, un statut ODT / OMEX ne retombait que
    si une page rappelait /api/engins (une pelle qui s'arrete d'emettre restait « en minage »)."""
    while True:
        try:
            conn = get_conn()
            try:
                statuts_engins(conn)
            finally:
                conn.close()
        except Exception as e:
            print("STATUTS: recalcul impossible (%s)" % e)
        time.sleep(30)


def generer_affectation(conn, dt_code):
    """Cree une mission pour un DT en statut ODT : destination + Dumping Area + chemin."""
    dt = conn.execute("SELECT * FROM engins WHERE code=? AND type='DT'", (dt_code,)).fetchone()
    if not dt or dt["statut"] != "ODT":
        return None
    # idempotence : un DT n'a qu'une mission en cours (comme genererAffectation
    # cote Serveur_SG) ; l'appel manuel /api/affecter passe aussi par ici.
    if conn.execute("SELECT 1 FROM missions WHERE dt_code=? AND statut='en_cours'",
                    (dt_code,)).fetchone():
        return None

    # 24/11 : blasts clos exclus, flitch le plus haut d'abord (oreblocs_par_flitch) ; un bloc qui CONTIENT le DT
    # passe avant un bloc seulement proche (marge d'affectation, meme marge que le passage ODT).
    oreblocs = oreblocs_par_flitch(conn)
    orebloc_id = 0
    marge = marge_affectation_m()
    for avec_marge in (False, True):
        for ob in oreblocs:
            try:
                poly = json.loads(ob["sommets"] or "[]")
            except (TypeError, ValueError):
                continue
            if len(poly) < 3:
                continue
            if (dans_ou_pres_polygone(dt["x"], dt["y"], poly, marge) if avec_marge
                    else point_in_polygon(dt["x"], dt["y"], poly)):
                orebloc_id = ob["id"]
                break
        if orebloc_id:
            break

    # destination = centre de l'orebloc (ou dernier point connu)
    dest_x, dest_y = dt["x"], dt["y"]
    if orebloc_id:
        ob = conn.execute("SELECT * FROM oreblocs WHERE id=?", (orebloc_id,)).fetchone()
        poly = json.loads(ob["sommets"] or "[]")
        if poly:
            sx = sum(p[0] for p in poly) / len(poly)
            sy = sum(p[1] for p in poly) / len(poly)
            dest_x, dest_y = sx, sy

    # Dumping Area la plus proche
    das = [dict(r) for r in conn.execute("SELECT * FROM dumping_areas WHERE actif=1")]
    da_id = 0
    da_dist = float("inf")
    for da in das:
        dist = distance(dest_x, dest_y, da["x"], da["y"])
        if dist < da_dist:
            da_dist = dist
            da_id = da["id"]

    # Le camion est charge (ODT) : sa destination est la dumping area. Le
    # chemin allait auparavant vers le centre de l'orebloc, ou le DT se
    # trouve deja : un trajet de quelques metres, inutilisable par la tablette
    # et qui rendait toute detection de deviation absurde.
    if da_id:
        da = next(d for d in das if d["id"] == da_id)
        poly = json.loads(da.get("sommets") or "[]")
        if len(poly) >= 3:
            dest_x = sum(p[0] for p in poly) / len(poly)
            dest_y = sum(p[1] for p in poly) / len(poly)
        else:
            dest_x, dest_y = da["x"], da["y"]

    # Chemin par les pistes. Repli sur le segment droit si aucune piste n'est
    # accrochable : une mission sans chemin serait inexploitable cote tablette.
    trajet, info_trajet = itineraire(conn, dt["x"], dt["y"], dest_x, dest_y)
    chemin = json.dumps(trajet)

    cur = conn.execute(
        "INSERT INTO missions (dt_code, orebloc_id, destination_x, destination_y, dumping_area_id, chemin, statut) "
        "VALUES (?, ?, ?, ?, ?, ?, 'en_cours')",
        (dt_code, orebloc_id, dest_x, dest_y, da_id, chemin))
    conn.commit()
    mission_id = cur.lastrowid

    # alerte notification (voix off + office)
    creer_alerte(conn, "mission", dt_code,
                 "Nouvelle mission assignee : destination Dumping Area #%d" % da_id)

    # ordre de mission structure, publie sur le canal dedie du camion
    # (camions/<dt_code>/mission) pour affichage en cabine
    mqtt_publier("camions/%s/mission" % dt_code, {
        "dt_code": dt_code,
        "mission_id": mission_id,
        "orebloc_id": orebloc_id,
        "destination_x": dest_x,
        "destination_y": dest_y,
        "dumping_area_id": da_id,
        "chemin": json.loads(chemin),
    })

    return {
        "id": mission_id,
        "dt_code": dt_code,
        "orebloc_id": orebloc_id,
        "destination_x": dest_x,
        "destination_y": dest_y,
        "dumping_area_id": da_id,
        "chemin": chemin,
        "statut": "en_cours"
    }


def verifier_affectations_auto(conn):
    """Affectation automatique des missions (cf. mission #1 du cahier des
    charges) : pour chaque DT en statut ODT sans mission en cours, declenche
    generer_affectation() sans intervention manuelle. A appeler juste apres
    statuts_engins() a chaque reception de position."""
    # 20/11 : pas d'affectation sur la seule position (statut ODT). Une mission ne nait qu'APRES
    # un chargement (cycle envoye par la tablette du DT, traite par Serveur_SG) ; un camion qui
    # roule ou passe pres d'une pelle sans etre charge ne recoit rien. Reactivable : AFFECTATION_ODT=1.
    if str(param_lire("AFFECTATION_ODT", "0")).strip().lower() not in ("1", "true", "oui", "on"):
        return
    if missions_source() == "cloud":
        # Serveur_SG decide. Ses missions arrivent par la reponse du relais
        # (voir ingerer_missions_sg) ; generer ici en parallele creerait deux
        # missions concurrentes pour le meme camion.
        return
    odts = [dict(r) for r in conn.execute(
        "SELECT * FROM engins WHERE type='DT' AND statut='ODT' AND actif=1")]
    for dt in odts:
        deja = conn.execute(
            "SELECT id FROM missions WHERE dt_code=? AND statut='en_cours'",
            (dt["code"],)).fetchone()
        if deja:
            continue
        mission = generer_affectation(conn, dt["code"])
        if mission:
            enregistrer_pour_office(conn, {"mission": mission, "action": "affectation_auto"})


def mission_a_surveiller(conn, dt_code):
    """Mission en cours du DT QUI A UNE DESTINATION (dumping area ou coordonnees d'arrivee), ou None.
    Sans affectation il n'y a ni destination ni dumping area : rien a surveiller, donc aucune alerte
    (sortie de route, deviation, checkpoint, arrivee). Une mission sans destination n'est pas suivie."""
    return conn.execute(
        "SELECT * FROM missions WHERE dt_code=? AND statut='en_cours' "
        "AND (COALESCE(dumping_area_id, 0) > 0 OR (COALESCE(destination_x, 0) <> 0 AND COALESCE(destination_y, 0) <> 0)) "
        "ORDER BY id DESC LIMIT 1", (dt_code,)).fetchone()


# ---------------------------------------------------------------- minages en cours (21/11) ---
# Qui mine quel bloc, par ORDRE DE FIABILITE :
#   1. le DT (automatique) : une pelle OMEX (en minage, position fraiche venue du DT qui la charge) dont la
#      position est DANS un orebloc ;
#   2. le spotter (secours) : il declare depuis son app, seulement quand il est dans le reseau ;
#   3. le bureau (formulaire de Senor) : dernier recours.
# Une ligne par pelle : la source la plus fiable l'emporte. Declarations manuelles valables 30 min.
MINAGE_DECLARATION_MIN = 30
ORIGINES_MINAGE = ("dt", "spotter", "bureau")


# 24/11 : FLITCHES. Un meme blast est exploite en plusieurs flitches (ex. 280, 277.5, 272.5, 270 pour le banc
# 280 -> 270 ; le pas n'est jamais suppose), chacun importe comme un blast distinct, avec des polygones qui se
# recouvrent. Regle (la meme que sur la tablette, dt-geo-core.ts) : les blocs des blasts CLOS ou inactifs ne
# comptent plus ; parmi les polygones qui contiennent le point, le FLITCH LE PLUS HAUT l'emporte (on mine de haut
# en bas), et a flitch egal le minerai passe avant le sterile. Avant : premier bloc par id, flitches clos compris.
def flitch_blast(numero, flitch_colonne=None):
    """Flitch d'un blast : nombre en tete du numero (280_OM_..., 272.5_OM_..., 272,5_...), sinon la colonne
    blasts.flitch, sinon None (classe apres tous les flitches connus)."""
    f = _geo_flitch(numero)
    if f is None and flitch_colonne not in (None, ""):
        f = _geo_flitch(flitch_colonne)
    return f


def oreblocs_par_flitch(conn, colonnes="o.*"):
    """Oreblocs actifs des blasts non clos, tries : flitch decroissant, minerai avant sterile, id.
    Lignes sqlite3.Row avec en plus blast_numero et blast_flitch (None si inconnu)."""
    lignes = conn.execute(
        "SELECT %s, b.numero AS blast_numero, b.flitch AS blast_flitch_col FROM oreblocs o "
        "LEFT JOIN blasts b ON b.id = o.blast_id "
        "WHERE o.actif = 1 AND (b.id IS NULL OR (COALESCE(b.actif, 1) = 1 AND COALESCE(b.clos_le, '') = ''))"
        % colonnes).fetchall()
    res = []
    for o in lignes:
        d = dict(o)
        d["blast_flitch"] = flitch_blast(d.get("blast_numero"), d.get("blast_flitch_col"))
        res.append(d)
    res.sort(key=lambda d: (-(d["blast_flitch"]) if d["blast_flitch"] is not None else float("inf"),
                            0 if (d.get("minerai") is None or bool(d.get("minerai"))) else 1,
                            d["id"]))
    return res


def polygones_blocs(conn):
    """Blocs actifs (blasts non clos) avec leur contour en UTM : [(id, nom, matiere, points)], dans l'ordre de
    priorite des flitches (oreblocs_par_flitch : flitch le plus haut, puis minerai avant sterile). Un contour en
    degres est PROJETE en UTM (fuseau de Geographic System) avant tout test : comparer des degres a des metres
    ne trouverait jamais rien."""
    res = []
    for o in oreblocs_par_flitch(conn, "o.id, o.nom, o.sommets, o.minerai"):
        try:
            pts = json.loads(o["sommets"] or "[]")
        except (TypeError, ValueError):
            continue
        if not isinstance(pts, list) or len(pts) < 3:
            continue
        pts, _converti = points_en_utm(conn, pts)
        est_ore = o["minerai"] is None or bool(o["minerai"])
        res.append((o["id"], o["nom"], "ore" if est_ore else "waste", pts))
    return res


def bloc_au_point(conn, x, y, blocs=None):
    """Bloc qui CONTIENT le point (x, y en UTM) : {id, nom, matiere} ou None. Le premier polygone contenant dans
    l'ordre de polygones_blocs : flitch actif le plus haut, puis minerai avant sterile (24/11 ; avant : tout le
    minerai avant tout le sterile, flitches confondus). Reponse CONFIRMEE par un test de projection exact (point
    dans le polygone), jamais par proximite."""
    for oid, nom, matiere, pts in (blocs if blocs is not None else polygones_blocs(conn)):
        if point_in_polygon(x, y, pts):
            return {"id": oid, "nom": nom, "matiere": matiere}
    return None


def minages_deduits_dt(conn):
    """Pelles en minage deduites des positions remontees par les DT :
    [{exca_code, orebloc_id, bloc, matiere}] ; matiere = 'ore' ou 'waste' (bloc sterile)."""
    blocs = polygones_blocs(conn)
    res = []
    for e in conn.execute("SELECT code, x, y, derniere_maj FROM engins WHERE type='EXCA' AND actif=1 AND statut='OMEX'"):
        if age_position_s(e["derniere_maj"]) > POSITION_FRAICHE_S:
            continue                     # pelle muette : on ne devine pas
        b = bloc_au_point(conn, e["x"], e["y"], blocs)
        if b:
            res.append({"exca_code": e["code"], "orebloc_id": b["id"], "bloc": b["nom"], "matiere": b["matiere"]})
    return res


def minages_en_cours(conn):
    """Tous les minages en cours, une ligne par pelle : [{exca_code, orebloc_id, bloc, origine, depuis}]."""
    res = {}
    for r in conn.execute(
            "SELECT m.exca_code, m.orebloc_id, o.nom AS bloc, COALESCE(m.origine, 'bureau') AS origine, "
            "CASE WHEN o.minerai = 0 THEN 'waste' ELSE 'ore' END AS matiere, "
            "datetime(m.declared_at, 'localtime') AS depuis FROM minage_actuel m "
            "LEFT JOIN oreblocs o ON o.id = m.orebloc_id "
            "WHERE m.declared_at >= datetime('now', ?)", ("-%d minutes" % MINAGE_DECLARATION_MIN,)):
        d = dict(r)
        d["origine"] = d["origine"] if d["origine"] in ORIGINES_MINAGE else "bureau"
        res[d["exca_code"].upper()] = d
    maintenant = now_iso()
    for d in minages_deduits_dt(conn):          # le DT prime sur toute declaration manuelle
        d.update(origine="dt", depuis=maintenant)
        res[d["exca_code"].upper()] = d
    return sorted(res.values(), key=lambda d: d["exca_code"])


def verifier_arrivee_da(conn, dt_code):
    """Geofencing dumping area : des que le DT entre dans le polygone de la
    dumping area de sa mission en cours, la mission est marquee "arrivee"
    (une seule fois) et une alerte "vous etes arrive" part au camion."""
    mission = mission_a_surveiller(conn, dt_code)
    if not mission or mission["arrivee"]:
        return None
    # 24/11 : mission de waste (« WasteDump », dumping_area_id = 0, ou un waste dump precis) : toute zone
    # waste vaut arrivee (decision utilisateur n° 4). Minerai : seulement la zone de la mission.
    waste = mission_waste(conn, mission)
    if not mission["dumping_area_id"] and not waste:
        return None
    dt = conn.execute("SELECT * FROM engins WHERE code=?", (dt_code,)).fetchone()
    if not dt:
        return None
    da = None
    for z in conn.execute("SELECT * FROM dumping_areas WHERE actif=1 ORDER BY (id = ?) DESC, id",
                          (mission["dumping_area_id"] or 0,)):
        if not zone_prevue_mission(conn, mission, z["id"], type_zone(z["nom"]), waste):
            continue
        try:
            poly = json.loads(z["sommets"] or "[]")
        except ValueError:
            continue
        if len(poly) >= 3 and point_in_polygon(dt["x"], dt["y"], poly):
            da = z
            break
    if not da:
        return None
    conn.execute("UPDATE missions SET arrivee=1 WHERE id=?", (mission["id"],))
    conn.commit()
    # 21/11 : fin de mission dans SA dumping area (pas une autre) -> les alertes encore ouvertes du DT sont closes
    closes = sum(clore_alertes_ouvertes(conn, dt_code, typ, cause="arrivee_da")
                 for typ in ("offroad", "deviation", "checkpoint", "checkpoint_interdit", "zone_approche", "zone_interdite"))
    if closes and serveur_controle_direction():     # 24/11 : la tablette leve elle-meme ses alertes
        # l'app DT leve ses bandeaux/voix en silence (type « cloture », sans retour_* parle)
        msg_cloture = "Mission terminee en dumping area : alertes closes."
        mqtt_publier("camions/%s/alerte" % dt_code, {
            "type": "cloture", "dt_code": dt_code, "message": msg_cloture,
            "voix_off": False, "horodatage": now_iso()})
        if alertes_chauffeur_actives():
            enregistrer_pour_office(conn, {"chauffeur": {
                "profil": dt_code, "message": "[cloture] " + msg_cloture, "type": "cloture"}})
    message = "Vous etes arrive : dumping area %s." % da["nom"]
    creer_alerte(conn, "arrivee", dt_code, message)
    enregistrer_pour_office(conn, {"mission_id": mission["id"], "dt_code": dt_code,
                                   "dumping_area": da["nom"], "action": "arrivee"})
    return message


# ----------------------------------------------------------- deviation ----
# "Deviation" = le DT roule (souvent sur une piste) mais s'eloigne du TRAJET
# de SA mission. A ne pas confondre avec la sortie de route (hors de toute
# piste), qui a sa propre alerte : un DT deja offroad n'est pas signale une
# deuxieme fois en deviation.
#
# La mission reste "en_cours" quand le DT devie : l'ancien passage en statut
# 'devoie' la sortait du suivi (plus de detection d'arrivee, fin de mission
# refusee, nouvelle mission concurrente possible). L'etat vit dans
# missions.devie, avec la meme logique que l'offroad : seuil d'entree,
# confirmation dans la duree, seuil de retour plus bas (hysteresis), une
# alerte au changement d'etat seulement.
DEVIATION_DEFAUTS = {
    "DEVIATION_ACTIF": "1",
    "DEVIATION_SEUIL_M": "30",
    "DEVIATION_RETOUR_M": "20",
    "DEVIATION_DUREE_S": "15",
    "DEVIATION_RETOUR_DUREE_S": "10",   # 21/11 : retour sur le trajet confirme apres N s d'affilee (anti-clignotement)
}


def deviation_reglages():
    def num(cle):
        try:
            return float(param_lire(cle, DEVIATION_DEFAUTS[cle]))
        except (TypeError, ValueError):
            return float(DEVIATION_DEFAUTS[cle])
    actif = str(param_lire("DEVIATION_ACTIF", "1")).strip().lower()
    seuil = num("DEVIATION_SEUIL_M")
    return {
        "actif": actif not in ("0", "false", "non", "off"),
        "seuil_m": seuil,
        "retour_m": min(num("DEVIATION_RETOUR_M"), seuil),
        "duree_s": max(0.0, num("DEVIATION_DUREE_S")),
        "retour_duree_s": max(0.0, num("DEVIATION_RETOUR_DUREE_S")),
    }


# 21/11 - Cycle de vie d'une alerte : l'alerte « offroad » / « deviation » est OUVERTE a sa creation et
# se CLOT cote DT au retour (colonnes retour_le + retour_duree_s) ; elle n'est « cloturee » que lorsque
# l'Office l'a aussi marquee vue (acquittee). L'acquittement ne coupe PAS la voix du DT.
_retour_candidat = {}      # code DT -> instant du premier releve revenu sur le trajet (confirmation)


def fin_surveillance_dt(conn, dt_code, cause="fin_mission"):
    """26/11 (regle utilisateur) : un DT n'est suivi que CHARGE, de la fin du chargement au dumping valide. Quand sa
    mission se ferme (dumping valide, annulee, abandonnee), la surveillance s'arrete : etat hors piste efface et
    alertes encore ouvertes (sortie de route, deviation) closes avec la cause `fin_mission`. Avant : une sortie de
    route ouverte au moment de la fin restait « en cours » indefiniment (DT-15, 26/11, alerte 142).
    Ne commit pas : l'appelant commit avec sa propre mise a jour."""
    if not dt_code:
        return
    if conn.execute("SELECT 1 FROM missions WHERE dt_code=? AND statut='en_cours' LIMIT 1", (dt_code,)).fetchone():
        return                                   # une autre mission (nouveau chargement) est deja suivie
    conn.execute("UPDATE engins SET offroad=0, offroad_depuis='', offroad_candidat_depuis='' WHERE code=?", (dt_code,))
    for typ in ("offroad", "deviation"):
        conn.execute(
            "UPDATE alertes SET retour_le=datetime('now'), retour_cause=?, "
            "retour_duree_s=MAX(0, CAST((julianday('now') - julianday(created_at)) * 86400 AS INTEGER)) "
            "WHERE dt_code=? AND type=? AND retour_le=''", (cause, dt_code, typ))


def clore_alertes_ouvertes(conn, dt_code, type_alerte, instant=None, cause="retour"):
    """Note le retour sur les alertes `type_alerte` encore ouvertes du DT : heure, duree et cause
    ('retour' = revenu sur son trajet / sa piste, 'arrivee_da' = mission terminee dans SA dumping area).
    Meme convention d'heure que created_at (UTC, SQLite)."""
    conn.execute(
        "UPDATE alertes SET retour_le=datetime('now'), retour_cause=?, "
        "retour_duree_s=MAX(0, CAST((julianday('now') - julianday(created_at)) * 86400 AS INTEGER)) "
        "WHERE dt_code=? AND type=? AND retour_le=''", (cause, dt_code, type_alerte))
    conn.commit()
    return conn.execute("SELECT changes()").fetchone()[0]


def distance_au_chemin(chemin, x, y):
    """Distance du point au trajet (polyligne [[x, y], ...])."""
    dmin = float("inf")
    for i in range(len(chemin) - 1):
        x1, y1 = chemin[i][0], chemin[i][1]
        x2, y2 = chemin[i + 1][0], chemin[i + 1][1]
        d = distance_point_segment(x, y, float(x1), float(y1), float(x2), float(y2))
        if d < dmin:
            dmin = d
    return dmin


def verifier_deviation(conn, dt_code, instant=None):
    """Met a jour l'etat de deviation de la mission en cours du DT apres une
    nouvelle position. Retourne l'evenement {"type": "deviation" |
    "retour_trajet", ...} lors d'un changement d'etat, sinon None."""
    reglages = deviation_reglages()
    if not reglages["actif"]:
        return None
    mission = mission_a_surveiller(conn, dt_code)
    if not mission or mission["arrivee"]:
        return None     # pas de mission (ou sans destination), ou deja arrive dans la dumping area
    dt = conn.execute("SELECT * FROM engins WHERE code=?", (dt_code,)).fetchone()
    if not dt:
        return None
    try:
        chemin = [p for p in json.loads(mission["chemin"] or "[]")
                  if isinstance(p, (list, tuple)) and len(p) >= 2]
    except Exception:
        chemin = []
    # Un trajet de 2 points est le repli "segment droit" (aucune piste
    # accrochable) : il traverse talus et fosses, personne ne peut le suivre.
    if len(chemin) < 3:
        return None

    x, y = dt["x"], dt["y"]
    ecart = distance_au_chemin(chemin, x, y)
    # manoeuvre au chargement / au dechargement : jamais une deviation
    zone = zone_hors_piste_autorisee(conn, x, y, offroad_reglages())
    deja_offroad = bool(dt["offroad"]) if "offroad" in dt.keys() else False
    maintenant = instant or now_iso()
    evenement = None

    if mission["devie"]:
        revenu = bool(zone) or ecart <= reglages["retour_m"]
        if revenu and not zone:
            # confirmation : revenu sur le trajet pendant retour_duree_s secondes d'affilee
            debut_retour = _retour_candidat.setdefault(dt_code, maintenant)
            revenu = _secondes_depuis(debut_retour, maintenant) >= reglages["retour_duree_s"]
        elif not revenu:
            _retour_candidat.pop(dt_code, None)
        if revenu:
            _retour_candidat.pop(dt_code, None)
            message = "%s de retour sur le trajet de sa mission." % dt_code
            conn.execute(
                "UPDATE missions SET devie=0, devie_depuis='', devie_candidat_depuis='', "
                "ecart_m=? WHERE id=?", (round(ecart, 1), mission["id"]))
            conn.commit()
            clore_alertes_ouvertes(conn, dt_code, "deviation", maintenant)
            creer_alerte(conn, "retour_trajet", dt_code, message)
            evenement = {"type": "retour_trajet", "dt_code": dt_code, "x": x, "y": y,
                         "mission_id": mission["id"], "distance": ecart, "message": message}
        else:
            conn.execute("UPDATE missions SET ecart_m=? WHERE id=?",
                         (round(ecart, 1), mission["id"]))
            conn.commit()
    elif not zone and not deja_offroad and ecart > reglages["seuil_m"]:
        candidat = mission["devie_candidat_depuis"] or maintenant
        if _secondes_depuis(candidat, maintenant) >= reglages["duree_s"]:
            message = "DEVIATION : %s a %d m du trajet de sa mission." % (dt_code, int(ecart))
            conn.execute(
                "UPDATE missions SET devie=1, devie_depuis=?, devie_candidat_depuis='', "
                "ecart_m=? WHERE id=?", (candidat, round(ecart, 1), mission["id"]))
            conn.commit()
            creer_alerte(conn, "deviation", dt_code, message)
            evenement = {"type": "deviation", "dt_code": dt_code, "x": x, "y": y,
                         "mission_id": mission["id"], "distance": ecart, "message": message}
        else:
            conn.execute(
                "UPDATE missions SET devie_candidat_depuis=?, ecart_m=? WHERE id=?",
                (candidat, round(ecart, 1), mission["id"]))
            conn.commit()
    else:
        conn.execute(
            "UPDATE missions SET devie_candidat_depuis='', ecart_m=? WHERE id=?",
            (round(ecart, 1), mission["id"]))
        conn.commit()

    if evenement:
        enregistrer_alerte_office(conn, evenement)
    return evenement


# ------------------------------------------------ checkpoints sautes (19/11) ---
# Serveur_SG decide de la mission et de son itineraire (liste ORDONNEE de
# checkpoints, lignes de passage a 2 extremites). La passerelle, qui recoit les
# positions en temps reel, controle le passage dans l'ordre PENDANT la mission
# seulement :
#   - un checkpoint est franchi si la trace du DT (position precedente ->
#     position courante) coupe la ligne, ou passe a moins de CHECKPOINT_RAYON_M ;
#   - si le DT franchit un checkpoint alors qu'un precedent n'a pas ete franchi,
#     ou arrive dans la dumping area avec des checkpoints restants :
#     alerte « checkpoint » (une fois par checkpoint saute), voix + bandeau chez le DT.
# Sans mission, ou sans checkpoints connus pour l'itineraire : rien n'est verifie.
CHECKPOINT_RAYON_M = 15.0
_cp_derniere_pos = {}      # code DT -> (x, y) precedent


def checkpoints_actif():
    return str(param_lire("CHECKPOINT_ACTIF", "1")).strip().lower() not in ("0", "false", "non", "off")


def _orientation(ax, ay, bx, by, cx, cy):
    v = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)


def segments_se_coupent(a, b, c, d):
    """Les segments [a,b] et [c,d] (points (x, y)) se croisent-ils ?"""
    o1 = _orientation(*a, *b, *c)
    o2 = _orientation(*a, *b, *d)
    o3 = _orientation(*c, *d, *a)
    o4 = _orientation(*c, *d, *b)
    return o1 != o2 and o3 != o4


def checkpoint_franchi(cp, prec, pos):
    ax, ay, bx, by = cp["x1"], cp["y1"], cp["x2"], cp["y2"]
    if distance_point_segment(pos[0], pos[1], ax, ay, bx, by) <= CHECKPOINT_RAYON_M:
        return True
    return bool(prec) and segments_se_coupent(prec, pos, (ax, ay), (bx, by))


def sg_lire_itineraires():
    """GET ?action=missions&statut=en_cours chez Serveur_SG ->
    {dt_code: [segments ordonnes]} (champ `itineraire.checkpoints`)."""
    sg_url, sg_token, _ = sg_conf()
    if not sg_url or not sg_token:
        return {}
    url = "%s?action=missions&statut=en_cours&token=%s" % (sg_url, quote(sg_token))
    with urllib.request.urlopen(url, timeout=30) as rep_:
        d = json.loads(rep_.read().decode("utf-8", "replace"))
    res = {}
    for m in d.get("missions") or []:
        cps = ((m.get("itineraire") or {}).get("checkpoints")) or []
        propres = []
        for c in cps:
            try:
                propres.append({"nom": str(c["nom"]), "x1": float(c["x1"]), "y1": float(c["y1"]),
                                "x2": float(c["x2"]), "y2": float(c["y2"])})
            except (KeyError, TypeError, ValueError):
                continue
        if propres:
            res[str(m.get("dt_code") or "").strip()] = propres
    return res


def source_pour_nom(conn, nom):
    """Id de la source (pit) dont un code admis figure dans le nom du bloc/blast
    (280_LMP2_119_WST... -> LMP2 -> Main Pit), sinon 0. Meme regle que Home."""
    segs = [x for x in re.split(r"[_\s.\-]+", str(nom or "").upper()) if x]
    for src in conn.execute("SELECT id, codes FROM sources WHERE actif=1 ORDER BY id"):
        codes = [c.strip().upper() for c in re.split(r"[,\s;]+", src["codes"] or "") if c.strip()]
        if any(c in segs for c in codes):
            return src["id"]
    return 0


def checkpoints_trajectoire(conn, source_id, da_id):
    """Liste ordonnee [{nom, x1, y1, x2, y2}] de la trajectoire source -> dumping area ([] si aucune)."""
    if not source_id or not da_id:
        return []
    t = conn.execute("SELECT checkpoints FROM trajectoires WHERE source_id=? AND dumping_area_id=?",
                     (source_id, da_id)).fetchone()
    if not t:
        return []
    try:
        ids = [int(i) for i in json.loads(t["checkpoints"] or "[]")]
    except (TypeError, ValueError):
        return []
    res = []
    for i in ids:
        c = conn.execute("SELECT nom, x1, y1, x2, y2 FROM checkpoints WHERE id=? AND actif=1", (i,)).fetchone()
        if c:
            res.append({"nom": c["nom"], "x1": c["x1"], "y1": c["y1"], "x2": c["x2"], "y2": c["y2"]})
    return res


def charger_checkpoints_missions():
    """Une passe : les missions en cours sans checkpoints les recoivent. PRIORITE a la trajectoire definie
    dans Senor (source du bloc -> dumping area de la mission) ; Serveur_SG ne sert que de repli."""
    conn = get_conn()
    try:
        sans = [dict(r) for r in conn.execute(
            "SELECT id, dt_code, orebloc_id, dumping_area_id FROM missions WHERE statut='en_cours' "
            "AND (cp_json IS NULL OR cp_json IN ('', '[]'))")]
        if not sans:
            return 0
        n = 0
        restantes = []
        for m in sans:
            ob = conn.execute("SELECT nom FROM oreblocs WHERE id=?", (m["orebloc_id"],)).fetchone() if m["orebloc_id"] else None
            cps = checkpoints_trajectoire(conn, source_pour_nom(conn, ob["nom"] if ob else ""), m["dumping_area_id"])
            if cps:
                conn.execute("UPDATE missions SET cp_json=? WHERE id=?", (json.dumps(cps), m["id"]))
                n += 1
            else:
                restantes.append(m)
        conn.commit()
        if not restantes:
            return n
        sans = restantes
        par_dt = sg_lire_itineraires()
        for m in sans:
            cps = par_dt.get(m["dt_code"])
            if cps:
                conn.execute("UPDATE missions SET cp_json=? WHERE id=?",
                             (json.dumps(cps), m["id"]))
                n += 1
        conn.commit()
        return n
    finally:
        conn.close()


def boucle_checkpoints():
    while True:
        try:
            if checkpoints_actif():
                charger_checkpoints_missions()
        except Exception as e:
            print("CHECKPOINT: lecture des itineraires impossible (%s)" % e)
        time.sleep(20)


# ------------------------------------------ checkpoints INTERDITS au waste (23/11) ---
# Un DT charge de WASTE (chargement hors Geosum, bloc sterile, ou dumping area « waste ») ne doit jamais
# franchir un checkpoint marque `interdit_waste` (section « Trajectoire Waste » de Senor ; tous le sont par
# defaut). Exception : un checkpoint que la mission demande elle-meme (sa trajectoire) n'est pas interdit.
_cp_interdits_vus = {}      # id de mission -> noms deja signales (une alerte par checkpoint et par mission)


# 24/11 : MEMOIRE DES ALERTES DEJA SIGNALEES, GARDEE EN BASE. Avant, « deja signale pour cette mission »
# (checkpoints interdits, zones interdites approchees) n'existait qu'en memoire : a chaque relance de la
# passerelle, les memes alertes repartaient vers le chauffeur et le bureau (DT-12 : 08:40 puis 09:58 le 24/11).
# Colonne missions.alertes_vues (JSON {"cp_interdits": [...], "zones_approche": [...]}), A PART de cp_etat
# (cp_etat est reecrit a partir de la ligne lue en debut de traitement, il aurait ecrase ces cles).
# La memoire vive reste le cache ; elle est rechargee depuis la base la premiere fois qu'une mission est vue.
def memoire_alertes(conn, cache, mission_id, cle):
    """Ensemble des elements deja signales (`cle`) pour la mission ; charge depuis la base si absent du cache."""
    if mission_id in cache:
        return cache[mission_id]
    vus = set()
    try:
        r = conn.execute("SELECT alertes_vues FROM missions WHERE id=?", (mission_id,)).fetchone()
        d = json.loads((r["alertes_vues"] if r else "") or "{}")
        vus = set(d.get(cle) or [])
    except (sqlite3.Error, ValueError, TypeError, IndexError, KeyError):
        pass
    cache[mission_id] = vus
    return vus


def memoire_alertes_ajouter(conn, cache, mission_id, cle, valeur):
    """Ajoute `valeur` aux elements signales de la mission (cache + base). La base est relue juste avant
    l'ecriture : les autres cles de alertes_vues ne sont jamais ecrasees."""
    memoire_alertes(conn, cache, mission_id, cle).add(valeur)
    try:
        r = conn.execute("SELECT alertes_vues FROM missions WHERE id=?", (mission_id,)).fetchone()
        d = json.loads((r["alertes_vues"] if r else "") or "{}")
        if not isinstance(d, dict):
            d = {}
    except (sqlite3.Error, ValueError, TypeError, IndexError, KeyError):
        d = {}
    liste = list(d.get(cle) or [])
    if valeur not in liste:
        liste.append(valeur)
    d[cle] = liste
    try:
        conn.execute("UPDATE missions SET alertes_vues=? WHERE id=?", (json.dumps(d), mission_id))
        conn.commit()
    except sqlite3.Error as e:
        print("ALERTES: memoire de la mission %s non enregistree (%s)" % (mission_id, e))


def mission_waste(conn, mission):
    """La mission transporte-t-elle du waste ? Hors Geosum (aucun bloc), bloc sterile, ou DA nommee « waste »."""
    if not mission["orebloc_id"]:
        return True
    b = conn.execute("SELECT minerai FROM oreblocs WHERE id=?", (mission["orebloc_id"],)).fetchone()
    if b is not None and b["minerai"] == 0:
        return True
    if mission["dumping_area_id"]:
        da = conn.execute("SELECT nom FROM dumping_areas WHERE id=?", (mission["dumping_area_id"],)).fetchone()
        if da and re.search(r"waste", da["nom"] or "", re.I):
            return True
    return False


# 24/11 : ALERTE UNIQUE « MAUVAISE DIRECTION » emise par la passerelle quand la tablette du DT ne controle pas
# elle-meme (pas de paquet geo recent : geo_tablette_active faux). Memes faits verifies que la tablette :
# checkpoint interdit franchi, entree dans une zone qui n'est pas la sienne. Levee (« retour_direction ») quand le
# demi-tour est verifie : re-franchissement du meme checkpoint, sortie de la mauvaise zone ; la fin de mission dans
# sa zone leve tout (« cloture »). La tablette repete la voix tant que l'alerte est en cours.
# Etat garde en base (missions.alertes_vues, cle « alarme ») : survit a une relance.
def _alarme_lire(conn, mission_id):
    try:
        r = conn.execute("SELECT alertes_vues FROM missions WHERE id=?", (mission_id,)).fetchone()
        d = json.loads((r["alertes_vues"] if r else "") or "{}")
        return d.get("alarme") if isinstance(d, dict) else None
    except (sqlite3.Error, ValueError, TypeError, IndexError, KeyError):
        return None


def _alarme_ecrire(conn, mission_id, alarme):
    try:
        r = conn.execute("SELECT alertes_vues FROM missions WHERE id=?", (mission_id,)).fetchone()
        d = json.loads((r["alertes_vues"] if r else "") or "{}")
        if not isinstance(d, dict):
            d = {}
    except (sqlite3.Error, ValueError, TypeError, IndexError, KeyError):
        d = {}
    d["alarme"] = alarme
    conn.execute("UPDATE missions SET alertes_vues=? WHERE id=?", (json.dumps(d), mission_id))
    conn.commit()


def _cote_ligne(p, c):
    """Cote d'un point par rapport a la ligne d'un checkpoint : 1, -1, 0."""
    v = (c["x2"] - c["x1"]) * (p[1] - c["y1"]) - (c["y2"] - c["y1"]) * (p[0] - c["x1"])
    return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)


def alerte_direction(conn, dt_code, mission, pos, cause, raison, checkpoint="", zone="", prec=None, cp=None):
    """Leve « mauvaise_direction » (chauffeur + bureau) et memorise le fait pour reconnaitre le demi-tour.
    24/11 (essai terrain) : le BON COTE de la ligne (d'ou venait le camion) est garde : le retour = re-franchir
    vers ce cote."""
    destination = nom_zone_prevue(conn, mission) or "?"
    message = 'MAUVAISE DIRECTION : %s - %s. Allez a "%s".' % (dt_code, raison, destination)
    creer_alerte(conn, "mauvaise_direction", dt_code, message)
    cote = 0
    if cp is not None:
        cote = _cote_ligne(prec, cp) if prec else 0
        if not cote:
            cote = -_cote_ligne(pos, cp)
    _alarme_ecrire(conn, mission["id"], {"cause": cause, "checkpoint": checkpoint, "zone": zone,
                                         "point": [pos[0], pos[1]], "cote": cote, "le": now_iso()})
    evenement = {"type": "mauvaise_direction", "cause": cause, "dt_code": dt_code, "x": pos[0], "y": pos[1],
                 "mission_id": mission["id"], "checkpoints": [checkpoint] if checkpoint else [], "zone": zone,
                 "message": message, "origine": "passerelle"}
    enregistrer_alerte_office(conn, evenement)
    return evenement


def retour_direction(conn, dt_code, mission, pos, motif):
    """Demi-tour verifie : leve l'alerte (« retour_direction »), chauffeur + bureau."""
    destination = nom_zone_prevue(conn, mission) or "?"
    message = 'BONNE DIRECTION : %s - continuez vers "%s".' % (dt_code, destination)
    creer_alerte(conn, "retour_direction", dt_code, message)
    _alarme_ecrire(conn, mission["id"], None)
    evenement = {"type": "retour_direction", "dt_code": dt_code, "x": pos[0], "y": pos[1], "mission_id": mission["id"],
                 "motif": motif, "message": message, "origine": "passerelle"}
    enregistrer_alerte_office(conn, evenement)
    return evenement


def verifier_retour_direction(conn, dt_code, mission, prec, pos):
    """Alarme de la passerelle en cours (cause checkpoint) : re-franchissement STRICT du meme checkpoint, VERS LE BON
    COTE (sens inverse du fait) = demi-tour verifie. Autant de fois que necessaire (24/11, essai terrain)."""
    a = _alarme_lire(conn, mission["id"])
    if not a or a.get("cause") == "mauvaise_zone" or not a.get("checkpoint") or not prec:
        return None
    c = conn.execute("SELECT nom, x1, y1, x2, y2 FROM checkpoints WHERE nom=? AND actif=1", (a["checkpoint"],)).fetchone()
    if c and segments_se_coupent(prec, pos, (c["x1"], c["y1"]), (c["x2"], c["y2"])) \
            and (not a.get("cote") or _cote_ligne(pos, c) == a["cote"]):
        return retour_direction(conn, dt_code, mission, pos, "checkpoint %s re-franchi" % a["checkpoint"])
    return None


def verifier_checkpoints_interdits(conn, dt_code, mission, prec, pos):
    """Franchissement d'un checkpoint interdit a la matiere de la mission (waste : interdit_waste, ore : interdit_ore) :
    alerte `checkpoint_interdit` (une fois par checkpoint et par mission). Retourne l'evenement, sinon None."""
    matiere = "waste" if mission_waste(conn, mission) else "ore"
    colonne = "interdit_waste" if matiere == "waste" else "interdit_ore"
    defaut = 1 if matiere == "waste" else 0
    try:
        propres = {c["nom"] for c in json.loads(mission["cp_json"] or "[]")}
    except (ValueError, TypeError, KeyError):
        propres = set()
    vus = memoire_alertes(conn, _cp_interdits_vus, mission["id"], "cp_interdits")
    alarme = _alarme_lire(conn, mission["id"]) or {}
    for c in conn.execute("SELECT nom, x1, y1, x2, y2 FROM checkpoints WHERE actif=1 AND COALESCE(%s, %d)=1" % (colonne, defaut)):
        if c["nom"] in propres or c["nom"] == alarme.get("checkpoint"):
            continue                      # checkpoint de l'alarme en cours : c'est le retour (verifier_retour_direction)
        # 24/11 (essai terrain) : AUTANT DE FOIS. Deja signale : nouvelle alerte a chaque franchissement STRICT.
        if c["nom"] in vus:
            franchi = bool(prec) and segments_se_coupent(prec, pos, (c["x1"], c["y1"]), (c["x2"], c["y2"]))
        else:
            franchi = checkpoint_franchi(dict(c), prec, pos)
        if franchi:
            if c["nom"] not in vus:
                memoire_alertes_ajouter(conn, _cp_interdits_vus, mission["id"], "cp_interdits", c["nom"])
            message = "CHECKPOINT INTERDIT : %s (%s) a franchi %s." % (dt_code, matiere, c["nom"])
            creer_alerte(conn, "checkpoint_interdit", dt_code, message)
            evenement = {"type": "checkpoint_interdit", "dt_code": dt_code, "x": pos[0], "y": pos[1],
                         "mission_id": mission["id"], "checkpoints": [c["nom"]], "message": message}
            enregistrer_alerte_office(conn, evenement)
            # 24/11 : fait verifie -> alerte unique au chauffeur (cette fonction ne tourne que sans tablette active)
            return alerte_direction(conn, dt_code, mission, pos, "checkpoint_interdit",
                                    "checkpoint %s interdit au %s" % (c["nom"], "waste" if matiere == "waste" else "minerai"),
                                    checkpoint=c["nom"], prec=prec, cp=dict(c))
    return None


# 23/11 : MODE GEOFENCING. Pendant le chargement, le DT est gare a cote de la pelle, parfois a quelques metres
# d'un checkpoint : rien n'est controle. Le controle des checkpoints (valides / interdits / sautes) demarre quand
# le DT a fini d'etre charge et S'ELOIGNE : a plus de GEOFENCE_DEPART_M de sa position au debut de la mission
# (= lieu de chargement). L'etat est garde dans missions.cp_etat : "depart" [x, y], "geofencing" true.
GEOFENCE_DEPART_M = 30.0


def point_de_chargement(conn, dt_code, mission, pos):
    """Position du DT au debut de la mission (derniere position connue a cet instant), sinon la position actuelle."""
    debut = str(mission["debut"] or "")
    if debut:
        r = conn.execute("SELECT x, y FROM positions_dt WHERE code=? AND horodatage<=? ORDER BY horodatage DESC LIMIT 1",
                         (dt_code, debut)).fetchone()
        if r:
            return [r["x"], r["y"]]
    return [pos[0], pos[1]]


def geofencing_actif(conn, dt_code, mission, pos):
    """True des que le DT s'est eloigne de plus de GEOFENCE_DEPART_M de son point de chargement (definitif pour la mission)."""
    try:
        etat = json.loads(mission["cp_etat"] or "{}")
    except (ValueError, TypeError):
        etat = {}
    if etat.get("geofencing"):
        return True
    change = False
    if not etat.get("depart"):
        etat["depart"] = point_de_chargement(conn, dt_code, mission, pos)
        change = True
    dep = etat["depart"]
    # 23/11 : distance de depart reglable dans Senor (essais : checkpoints poses pres de la pelle)
    if math.hypot(pos[0] - dep[0], pos[1] - dep[1]) > reglages_zones()["depart_m"]:
        etat["geofencing"] = True
        etat["geofencing_le"] = now_iso()
        change = True
        print("CHECKPOINT: %s s'eloigne du chargement : mode geofencing (mission %s)" % (dt_code, mission["id"]))
    if change:
        conn.execute("UPDATE missions SET cp_etat=? WHERE id=?", (json.dumps(etat), mission["id"]))
        conn.commit()
    return bool(etat.get("geofencing"))


# ------------------------------------------ zones interdites + dumping a l'arret (23/11) ---
# But : un DT charge de WASTE ne doit jamais decharger dans un RomPad, un DT charge de ORE jamais dans un Waste dump.
# Le camion ne peut pas faire demi-tour sur la piste : il a le DROIT d'entrer dans une zone pour manoeuvrer.
# Ce qui compte, c'est l'ARRET : immobile (positions dans un rayon ARRET_RAYON_M) pendant ARRET_DUMP_S = dumping.
#   - approche d'une zone interdite (< ZONE_APPROCHE_M du polygone) : pre-alerte « zone_approche » ;
#   - entree dans une zone interdite : « zone_interdite » (information : demi-tour autorise, ne pas decharger) ;
#   - arret >= ARRET_DUMP_S dans une zone interdite : « dumping_hors_zone » (alerte rouge) ;
#   - verdict de la mission (rapport) confirme a la fin du cycle (mission close ou chargement suivant).
# Type de zone d'apres son nom : « waste » -> waste dump ; « rompad » / « rom pad » -> RomPad.
ZONES_DEFAUTS = {"ZONE_APPROCHE_M": "50", "ARRET_DUMP_S": "45", "ARRET_RAYON_M": "10", "ARRET_VITESSE_KMH": "1",
                 "GEOFENCE_DEPART_M": "30"}
_zones_approches = {}      # id de mission -> zones deja signalees a l'approche

# 23/11 : VITESSE GNSS. Le recepteur mesure sa vitesse par effet Doppler (independante de l'erreur de position) :
# a l'arret elle vaut ~0 meme quand la position flotte de plusieurs metres. Elle arrive avec la position (MQTT
# « vit_kmh » + « vit_src »: "gnss", radio « vk »). Gardee ici par DT avec l'instant de SA mesure : elle ne sert
# que pour la position de ce meme instant. Sans vitesse GNSS (ancienne app, remontees via SG) : repli sur le rayon.
_vitesse_gnss = {}         # code DT -> {"kmh", "instant", "t"}


def noter_vitesse_gnss(code, kmh, instant):
    try:
        v = float(kmh)
    except (TypeError, ValueError):
        return
    if v >= 0 and math.isfinite(v):
        _vitesse_gnss[str(code)] = {"kmh": v, "instant": instant, "t": time.time()}


def vitesse_gnss(code, instant=None, age_max_s=None):
    """Vitesse GNSS (km/h) du DT pour la position de `instant`, ou la plus recente de moins de `age_max_s` s ; sinon None."""
    v = _vitesse_gnss.get(str(code))
    if not v:
        return None
    if instant is not None and v["instant"] == instant:
        return v["kmh"]
    if age_max_s is not None and time.time() - v["t"] <= age_max_s:
        return v["kmh"]
    return None


def reglages_zones():
    def num(cle):
        try:
            return float(param_lire(cle, ZONES_DEFAUTS[cle]) or ZONES_DEFAUTS[cle])
        except ValueError:
            return float(ZONES_DEFAUTS[cle])
    return {"actif": str(param_lire("ZONES_ACTIF", "1")).strip().lower() not in ("0", "false", "non", "off"),
            "approche_m": num("ZONE_APPROCHE_M"), "arret_s": num("ARRET_DUMP_S"), "rayon_m": num("ARRET_RAYON_M"),
            "vitesse_kmh": num("ARRET_VITESSE_KMH"), "depart_m": num("GEOFENCE_DEPART_M")}


def type_zone(nom):
    n = str(nom or "")
    if re.search(r"waste", n, re.I):
        return "waste"
    if re.search(r"rom\s*a?\s*pad", n, re.I):
        return "rompad"
    return ""


def type_zone_id(conn, da_id):
    """Type (waste | rompad | '') de la dumping area `da_id`, '' si inconnue."""
    if not da_id:
        return ""
    r = conn.execute("SELECT nom FROM dumping_areas WHERE id=?", (da_id,)).fetchone()
    return type_zone(r["nom"]) if r else ""


def zone_prevue_mission(conn, mission, zone_id, zone_type=None, waste=None):
    """La zone `zone_id` est-elle une destination correcte pour la mission ?

    24/11 (decision utilisateur n° 4) : la dumping area du waste est « WasteDump », sans preciser laquelle :
    pour une mission de WASTE, toute zone dont le nom contient « waste » convient. SG envoie alors
    dumping_area = « WasteDump » (aucune zone de ce nom : dumping_area_id = 0) ou le nom d'un waste dump
    precis. Pour le minerai : seulement la zone de la mission."""
    if zone_id and zone_id == mission["dumping_area_id"]:
        return True
    if waste is None:
        waste = mission_waste(conn, mission)
    if not waste:
        return False
    if zone_type is None:
        zone_type = type_zone_id(conn, zone_id)
    return zone_type == "waste"


def nom_zone_prevue(conn, mission, waste=None):
    """Nom de la zone prevue pour l'affichage : celle de la mission, sinon « WasteDump » pour du waste."""
    if mission["dumping_area_id"]:
        r = conn.execute("SELECT nom FROM dumping_areas WHERE id=?", (mission["dumping_area_id"],)).fetchone()
        if r:
            return r["nom"]
    if waste is None:
        waste = mission_waste(conn, mission)
    return "WasteDump" if waste else ""


def zones_dumping(conn):
    """Dumping areas actives avec polygone : [{id, nom, type, poly}]."""
    res = []
    for d in conn.execute("SELECT id, nom, sommets FROM dumping_areas WHERE actif=1"):
        try:
            poly = json.loads(d["sommets"] or "[]")
        except ValueError:
            continue
        if len(poly) >= 3:
            res.append({"id": d["id"], "nom": d["nom"], "type": type_zone(d["nom"]), "poly": poly})
    return res


def distance_polygone(x, y, poly):
    """0 dans le polygone, sinon distance au bord le plus proche (m)."""
    if point_in_polygon(x, y, poly):
        return 0.0
    n = len(poly)
    return min(distance_point_segment(x, y, poly[i][0], poly[i][1], poly[(i + 1) % n][0], poly[(i + 1) % n][1])
               for i in range(n))


# 24/11 (decision utilisateur, apres l'essai terrain) : TOUT SE DECIDE SUR LA TABLETTE. Les checkpoints, les zones
# et l'alerte « mauvaise direction » sont controles par la tablette du DT avec le paquet geo ; la passerelle ne
# controle plus rien et n'envoie AUCUNE alerte au chauffeur. Elle enregistre seulement, pour le bureau, ce que la
# tablette publie (camions/<code>/evenement) et garde les visites de zones (rapport de dumping).
# Le repli d'avant (la passerelle reprenait la main quand la tablette ne s'annoncait pas en MQTT) a fait sonner des
# alertes calculees sur des positions HTTP figees, que la tablette ignorait : interdit par defaut.
# SERVEUR_CONTROLE_DIRECTION = 1 le rallume (depannage seulement).
def serveur_controle_direction():
    return str(param_lire("SERVEUR_CONTROLE_DIRECTION", "0")).strip().lower() in ("1", "oui", "true", "on")


def _alerte_zone(conn, typ, dt_code, mission, pos, zone, message):
    creer_alerte(conn, typ, dt_code, message)
    evenement = {"type": typ, "dt_code": dt_code, "x": pos[0], "y": pos[1], "mission_id": mission["id"],
                 "zone": zone["nom"], "message": message}
    enregistrer_alerte_office(conn, evenement)
    return evenement


def verifier_zones(conn, dt_code, mission, pos, instant=None):
    """Visites de zones, arrets et alertes ore / waste pour une position (mode geofencing). Rend le dernier evenement."""
    r = reglages_zones()
    if not r["actif"]:
        return None
    t = instant or now_iso()
    waste = mission_waste(conn, mission)
    matiere = "waste" if waste else "ore"
    type_interdit = "rompad" if waste else "waste"
    zones = zones_dumping(conn)
    for z in zones:
        z["prevue"] = zone_prevue_mission(conn, mission, z["id"], z["type"], waste)
    # la zone de la mission d'abord (zones qui se chevauchent)
    zones.sort(key=lambda z: 0 if z["id"] == mission["dumping_area_id"] else 1 if z["prevue"] else 2)
    dedans = next((z for z in zones if point_in_polygon(pos[0], pos[1], z["poly"])), None)
    evt = None
    ouverte = conn.execute("SELECT * FROM zone_visites WHERE mission_id=? AND sortie='' ORDER BY id DESC LIMIT 1",
                           (mission["id"],)).fetchone()
    tablette = geo_tablette_active(dt_code)     # 24/11 : la tablette leve elle-meme l'alerte unique (zones comprises)
    alertes_serveur = serveur_controle_direction()   # 24/11 : par defaut, AUCUNE alerte de zone par la passerelle
    if ouverte and (not dedans or dedans["id"] != ouverte["zone_id"]):
        conn.execute("UPDATE zone_visites SET sortie=? WHERE id=?", (t, ouverte["id"]))
        # 24/11 : sortie de la zone qui avait declenche l'alarme de la passerelle = demi-tour verifie
        a = _alarme_lire(conn, mission["id"])
        if a and a.get("cause") == "mauvaise_zone" and a.get("zone") == ouverte["zone_nom"] and not tablette and alertes_serveur:
            evt = retour_direction(conn, dt_code, mission, pos, 'sortie de "%s"' % ouverte["zone_nom"])
        ouverte = None
    if dedans and not ouverte:
        role = ("prevue" if dedans["prevue"]
                else "interdite" if dedans["type"] == type_interdit else "autre")
        conn.execute("INSERT INTO zone_visites (mission_id, dt_code, matiere, zone_id, zone_nom, zone_type, role, entree, "
                     "arret_debut, arret_x, arret_y) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (mission["id"], dt_code, matiere, dedans["id"], dedans["nom"], dedans["type"], role, t, t, pos[0], pos[1]))
        if role == "interdite" and alertes_serveur:
            # plus de pre-alerte en ressortant (memoire gardee en base : survit a une relance)
            memoire_alertes_ajouter(conn, _zones_approches, mission["id"], "zones_approche", dedans["id"])
            evt = _alerte_zone(conn, "zone_interdite", dt_code, mission, pos, dedans,
                               'ZONE INTERDITE : %s (%s) entre dans "%s". Demi-tour autorise, ne pas decharger.'
                               % (dt_code, matiere, dedans["nom"]))
        # 24/11 : toute zone qui n'est pas la sienne (interdite OU autre RomPad) = fait verifie -> alerte unique
        if role != "prevue" and not tablette and alertes_serveur:
            evt = alerte_direction(conn, dt_code, mission, pos, "mauvaise_zone", 'entree dans "%s"' % dedans["nom"],
                                   zone=dedans["nom"])
    elif ouverte:
        kmh = vitesse_gnss(dt_code, instant=t)
        immobile = (kmh <= r["vitesse_kmh"]) if kmh is not None else \
            math.hypot(pos[0] - ouverte["arret_x"], pos[1] - ouverte["arret_y"]) <= r["rayon_m"]
        if immobile:
            duree = _secondes_depuis(ouverte["arret_debut"], t)
            conn.execute("UPDATE zone_visites SET arret_max_s=MAX(arret_max_s, ?) WHERE id=?", (duree, ouverte["id"]))
            if ouverte["role"] == "interdite" and duree >= r["arret_s"] and not ouverte["alerte_dump"] and alertes_serveur:
                conn.execute("UPDATE zone_visites SET alerte_dump=1 WHERE id=?", (ouverte["id"],))
                evt = _alerte_zone(conn, "dumping_hors_zone", dt_code, mission, pos, {"nom": ouverte["zone_nom"]},
                                   'DUMPING HORS ZONE PROBABLE : %s (%s) arrete %d s dans "%s".'
                                   % (dt_code, matiere, int(duree), ouverte["zone_nom"]))
        else:
            conn.execute("UPDATE zone_visites SET arret_debut=?, arret_x=?, arret_y=? WHERE id=?",
                         (t, pos[0], pos[1], ouverte["id"]))
    # pre-alerte : approche d'une zone interdite (une fois par zone et par mission)
    if alertes_serveur and (not dedans or dedans["type"] != type_interdit or dedans["id"] == mission["dumping_area_id"]):
        vus = memoire_alertes(conn, _zones_approches, mission["id"], "zones_approche")
        for z in zones:
            if z["type"] != type_interdit or z["id"] == mission["dumping_area_id"] or z["id"] in vus:
                continue
            d = distance_polygone(pos[0], pos[1], z["poly"])
            if 0 < d <= r["approche_m"]:
                memoire_alertes_ajouter(conn, _zones_approches, mission["id"], "zones_approche", z["id"])
                evt = _alerte_zone(conn, "zone_approche", dt_code, mission, pos, z,
                                   'ZONE INTERDITE PROCHE : %s (%s) a %d m de "%s".' % (dt_code, matiere, int(d), z["nom"]))
    conn.commit()
    return evt


def verdict_mission(visites, cycle_clos, arret_s):
    """Verdict du dumping d'une mission d'apres ses visites de zones : (verdict, zone).
    correct | mauvais_area (confirme : cycle clos) | hors_zone_probable (cycle en cours) | a_verifier | en_cours."""
    ok = [v for v in visites if v["role"] == "prevue" and v["arret_max_s"] >= arret_s]
    faux = [v for v in visites if v["role"] != "prevue" and v["arret_max_s"] >= arret_s]
    if ok:
        return "correct", ok[0]["zone_nom"]
    if faux:
        return ("mauvais_area" if cycle_clos else "hors_zone_probable"), faux[-1]["zone_nom"]
    return ("a_verifier" if cycle_clos else "en_cours"), ""


def rapport_dumping(conn, depuis, jusqua, dt=""):
    """Une ligne par mission commencee dans [depuis, jusqua[ (heure locale AAAA-MM-JJ HH:MM:SS)."""
    r = reglages_zones()
    sql = ("SELECT m.id, m.id_sg, m.dt_code, m.statut, m.debut, m.fin, m.orebloc_id, m.dumping_area_id, "
           "COALESCE(d.nom, '') AS zone_prevue FROM missions m LEFT JOIN dumping_areas d ON d.id = m.dumping_area_id "
           "WHERE m.debut >= ? AND m.debut < ?")
    args = [depuis, jusqua]
    if dt:
        sql += " AND m.dt_code = ?"
        args.append(dt)
    lignes = []
    for m in conn.execute(sql + " ORDER BY m.debut DESC", args).fetchall():
        visites = [dict(v) for v in conn.execute(
            "SELECT zone_nom, zone_type, role, entree, sortie, ROUND(arret_max_s) AS arret_max_s, matiere "
            "FROM zone_visites WHERE mission_id=? ORDER BY id", (m["id"],))]
        suivante = conn.execute("SELECT 1 FROM missions WHERE dt_code=? AND id>? LIMIT 1", (m["dt_code"], m["id"])).fetchone()
        clos = m["statut"] != "en_cours" or bool(suivante)
        verdict, zone = verdict_mission(visites, clos, r["arret_s"])
        matiere = visites[0]["matiere"] if visites else ("waste" if mission_waste(conn, m) else "ore")
        zone_prevue = m["zone_prevue"] or ("WasteDump" if matiere == "waste" else "")
        lignes.append({
            "mission": m["id"], "mission_sg": m["id_sg"], "dt_code": m["dt_code"], "debut": m["debut"], "fin": m["fin"],
            "statut": m["statut"], "matiere": matiere, "zone_prevue": zone_prevue, "verdict": verdict,
            "zone_dumping": zone,
            "demi_tours": [v["zone_nom"] for v in visites if v["role"] == "interdite" and v["arret_max_s"] < r["arret_s"]],
            "visites": visites})
    return {"reglages": r, "missions": lignes}


def rapport_offroad(conn, depuis_utc, jusqua_utc, dt=""):
    """Sorties de route (alertes offroad) dans [depuis, jusqua[ (UTC, comme created_at)."""
    sql = ("SELECT id, dt_code, message, created_at, retour_le, retour_duree_s, retour_cause, acquittee "
           "FROM alertes WHERE type='offroad' AND created_at >= ? AND created_at < ?")
    args = [depuis_utc, jusqua_utc]
    if dt:
        sql += " AND dt_code = ?"
        args.append(dt)
    lignes = []
    for a in conn.execute(sql + " ORDER BY created_at DESC", args):
        d = dict(a)
        mm = re.search(r"(\d+(?:[.,]\d+)?)\s*m\b", d["message"] or "")
        d["distance_m"] = float(mm.group(1).replace(",", ".")) if mm else None
        lignes.append(d)
    return {"offroad": lignes}


def _csv(lignes, colonnes):
    tampon = io.StringIO()
    w = csv.writer(tampon, delimiter=";")
    w.writerow(colonnes)
    for l in lignes:
        w.writerow([" | ".join(map(str, l.get(c))) if isinstance(l.get(c), list)
                    else ("" if l.get(c) is None else l.get(c)) for c in colonnes])
    return "\ufeff" + tampon.getvalue()


def verifier_checkpoints(conn, dt_code, instant=None):
    """A chaque position, en MODE GEOFENCING seulement (DT charge qui s'eloigne de la pelle) :
    checkpoints interdits (waste), puis franchissements dans l'ordre + checkpoint saute.
    Retourne le dernier evenement cree, sinon None.
    24/11 : desactive par defaut (serveur_controle_direction) : la tablette controle tout. Seules les visites de
    zones restent enregistrees (rapport de dumping), sans aucune alerte."""
    prec = _cp_derniere_pos.get(dt_code)
    if not serveur_controle_direction():
        mission = mission_a_surveiller(conn, dt_code)
        dt = conn.execute("SELECT x, y FROM engins WHERE code=?", (dt_code,)).fetchone()
        if mission and dt:
            pos = (dt["x"], dt["y"])
            _cp_derniere_pos[dt_code] = pos
            try:
                verifier_zones(conn, dt_code, mission, pos, instant)     # visites seulement (alertes coupees)
            except Exception as e:
                print("ZONES: verification impossible pour %s: %s" % (dt_code, e))
        return None
    if not checkpoints_actif():
        return _verifier_checkpoints_sautes(conn, dt_code, instant)
    mission = mission_a_surveiller(conn, dt_code)
    dt = conn.execute("SELECT x, y FROM engins WHERE code=?", (dt_code,)).fetchone()
    if not mission or not dt:
        return _verifier_checkpoints_sautes(conn, dt_code, instant)
    pos = (dt["x"], dt["y"])
    if not geofencing_actif(conn, dt_code, mission, pos):
        _cp_derniere_pos[dt_code] = pos     # encore au chargement : la trace repart d'ici, rien n'est controle
        return None
    if geo_tablette_active(dt_code):
        # 24/11 (phase 3) : la tablette controle elle-meme les checkpoints (interdits, sautes) et les publie sur
        # camions/<code>/evenement. Les refaire ici doublerait les alertes. Les zones restent controlees ici.
        _cp_derniere_pos[dt_code] = pos
        saute = interdit = None
    else:
        saute = _verifier_checkpoints_sautes(conn, dt_code, instant)
        interdit = verifier_retour_direction(conn, dt_code, mission, prec, pos) or \
            verifier_checkpoints_interdits(conn, dt_code, mission, prec, pos)
    zone = None
    try:
        zone = verifier_zones(conn, dt_code, mission, pos, instant)
    except Exception as e:
        print("ZONES: verification impossible pour %s: %s" % (dt_code, e))
    return zone or interdit or saute


def _verifier_checkpoints_sautes(conn, dt_code, instant=None):
    """A chaque position : franchissements + checkpoint saute. Retourne
    l'evenement `checkpoint` s'il y en a un nouveau, sinon None."""
    dt = conn.execute("SELECT x, y FROM engins WHERE code=?", (dt_code,)).fetchone()
    if not dt:
        return None
    pos = (dt["x"], dt["y"])
    prec = _cp_derniere_pos.get(dt_code)
    _cp_derniere_pos[dt_code] = pos
    if not checkpoints_actif():
        return None
    mission = mission_a_surveiller(conn, dt_code)
    if not mission:
        return None
    try:
        cps = json.loads(mission["cp_json"] or "[]")
        etat = json.loads(mission["cp_etat"] or "{}")
    except ValueError:
        return None
    if not cps:
        return None                      # itineraire inconnu : on ne devine pas
    franchis = set(etat.get("franchis") or [])
    sautes = set(etat.get("sautes") or [])
    noms = [c["nom"] for c in cps]
    manque = []

    if mission["arrivee"]:
        # arrive dans la DA : ce qui n'a pas ete franchi est saute (une seule fois)
        if etat.get("clos"):
            return None
        manque = [n for n in noms if n not in franchis and n not in sautes]
        etat["clos"] = True
    else:
        for i, c in enumerate(cps):
            if c["nom"] in franchis or c["nom"] in sautes:
                continue
            if checkpoint_franchi(c, prec, pos):
                # tout ce qui precede et n'est pas franchi est saute
                manque = [n for n in noms[:i] if n not in franchis and n not in sautes]
                franchis.add(c["nom"])
                break

    sautes.update(manque)
    etat["franchis"], etat["sautes"] = sorted(franchis), sorted(sautes)
    conn.execute("UPDATE missions SET cp_etat=? WHERE id=?", (json.dumps(etat), mission["id"]))
    conn.commit()
    if not manque:
        return None
    message = "CHECKPOINT SAUTE : %s n'a pas franchi %s%s." % (
        dt_code, manque[0], " (+%d autre(s))" % (len(manque) - 1) if len(manque) > 1 else "")
    creer_alerte(conn, "checkpoint", dt_code, message)
    evenement = {"type": "checkpoint", "dt_code": dt_code, "x": pos[0], "y": pos[1],
                 "mission_id": mission["id"], "checkpoints": manque, "message": message}
    enregistrer_alerte_office(conn, evenement)
    return evenement


# ---------------------------------------------------- sortie de route ----
# "Offroad" = le DT roule hors de TOUTE piste, pendant une mission seulement (19/11).
# A ne pas confondre avec la deviation (sur une piste, mais pas la sienne).
#
# Decision, a chaque position recue d'un DT :
#   - distance a l'arete de piste la plus proche (reseau travels_roads) ;
#   - hors piste si distance > OFFROAD_SEUIL_M, confirme seulement apres
#     OFFROAD_DUREE_S secondes continues (bruit GPS, passage de croisement) ;
#   - retour sur piste si distance <= OFFROAD_RETOUR_M (hysteresis : pas de
#     clignotement d'alerte quand le camion longe la limite) ;
#   - jamais hors piste dans une zone ou rouler hors piste est normal :
#     dumping areas / ROM pads, oreblocs (front de taille), ou a moins de
#     OFFROAD_RAYON_EXCA_M d'une excavatrice (chargement dans le pit) ;
#   - une alerte au CHANGEMENT d'etat seulement (entree / retour).
OFFROAD_DEFAUTS = {
    "OFFROAD_ACTIF": "1",
    "OFFROAD_SEUIL_M": "15",
    "OFFROAD_RETOUR_M": "10",
    "OFFROAD_DUREE_S": "10",
    "OFFROAD_MARGE_ZONE_M": "10",
    "OFFROAD_RAYON_EXCA_M": "50",
}

_offroad_aretes = {"signature": None, "aretes": []}
_offroad_verrou = threading.Lock()


def offroad_reglages():
    def num(cle):
        try:
            return float(param_lire(cle, OFFROAD_DEFAUTS[cle]))
        except (TypeError, ValueError):
            return float(OFFROAD_DEFAUTS[cle])
    actif = str(param_lire("OFFROAD_ACTIF", "1")).strip().lower()
    seuil = num("OFFROAD_SEUIL_M")
    return {
        "actif": actif not in ("0", "false", "non", "off"),
        "seuil_m": seuil,
        # le seuil de retour ne peut pas depasser le seuil d'entree
        "retour_m": min(num("OFFROAD_RETOUR_M"), seuil),
        "duree_s": max(0.0, num("OFFROAD_DUREE_S")),
        "marge_zone_m": max(0.0, num("OFFROAD_MARGE_ZONE_M")),
        "rayon_exca_m": max(0.0, num("OFFROAD_RAYON_EXCA_M")),
    }


def offroad_aretes(conn):
    """Aretes du reseau de pistes, mises en cache. Le cache est invalide des
    qu'une piste est ajoutee, retiree ou reprojetee (signature de la table),
    sans quoi on reconstruirait le graphe a chaque position GPS."""
    sig = tuple(conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(LENGTH(geojson)), 0) "
        "FROM travels_roads WHERE actif=1 AND type='geojson'").fetchone())
    with _offroad_verrou:
        if _offroad_aretes["signature"] == sig:
            return _offroad_aretes["aretes"]
    aretes = aretes_pistes(conn)
    with _offroad_verrou:
        _offroad_aretes["signature"] = sig
        _offroad_aretes["aretes"] = aretes
    return aretes


def pistes_en_degres(aretes):
    """Vrai si les pistes sont encore en lon/lat (non reprojetees) : comparer
    a des positions en metres donnerait des DT tous hors piste."""
    return bool(aretes) and all(
        abs(a[0]) <= 180 and abs(a[1]) <= 90 and abs(a[2]) <= 180 and abs(a[3]) <= 90
        for a in aretes[:50])


def distance_piste_la_plus_proche(aretes, x, y):
    dmin = float("inf")
    for x1, y1, x2, y2 in aretes:
        # rejet rapide : la boite englobante du segment est deja plus loin
        if (x < min(x1, x2) - dmin or x > max(x1, x2) + dmin or
                y < min(y1, y2) - dmin or y > max(y1, y2) + dmin):
            continue
        d = distance_point_segment(x, y, x1, y1, x2, y2)
        if d < dmin:
            dmin = d
    return dmin


def zone_hors_piste_autorisee(conn, x, y, reglages):
    """Nom de la zone ou rouler hors piste est normal, ou None."""
    marge = reglages["marge_zone_m"]
    for r in conn.execute("SELECT nom, sommets FROM dumping_areas WHERE actif=1"):
        poly = json.loads(r["sommets"] or "[]")
        if len(poly) >= 3 and dans_ou_pres_polygone(x, y, poly, marge):
            return "zone %s" % r["nom"]
    for r in conn.execute("SELECT nom, sommets FROM oreblocs WHERE actif=1"):
        poly = json.loads(r["sommets"] or "[]")
        if len(poly) >= 3 and dans_ou_pres_polygone(x, y, poly, marge):
            return "orebloc %s" % r["nom"]
    rayon = reglages["rayon_exca_m"]
    if rayon > 0:
        for r in conn.execute(
                "SELECT code, x, y FROM engins WHERE type='EXCA' AND actif=1"):
            if distance(x, y, r["x"], r["y"]) <= rayon:
                return "chargement %s" % r["code"]
    return None


def _secondes_depuis(horodatage, reference=None):
    """Secondes ecoulees entre `horodatage` et `reference` (par defaut :
    maintenant). `reference` = l'heure de l'OBSERVATION quand les positions
    arrivent par lots (remontees de Serveur_SG) : le delai de confirmation
    se mesure sur le terrain, pas a l'heure de lecture du lot."""
    try:
        t = datetime.strptime(horodatage, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return 0.0
    ref = datetime.now()
    if reference:
        try:
            ref = datetime.strptime(reference, "%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError):
            pass
    return (ref - t).total_seconds()


def verifier_offroad(conn, dt_code, instant=None):
    """Met a jour l'etat offroad du DT apres une nouvelle position.
    Retourne l'evenement {"type": "offroad"|"retour_piste", ...} lors d'un
    changement d'etat, sinon None."""
    reglages = offroad_reglages()
    if not reglages["actif"]:
        return None
    dt = conn.execute(
        "SELECT * FROM engins WHERE code=? AND type='DT' AND actif=1",
        (dt_code,)).fetchone()
    if not dt:
        return None
    # 19/11 : on ne surveille un DT que pendant une MISSION (mission creee par
    # Serveur_SG depuis un chargement). Sans mission : les positions remontent,
    # rien n'est verifie, rien n'est notifie. Un etat hors piste laisse par une
    # mission terminee est efface sans alerte.
    mission = mission_a_surveiller(conn, dt_code)
    if not mission:
        if dt["offroad"] or dt["offroad_candidat_depuis"]:
            conn.execute("UPDATE engins SET offroad=0, offroad_depuis='', "
                         "offroad_candidat_depuis='' WHERE code=?", (dt_code,))
            conn.commit()
        return None
    aretes = offroad_aretes(conn)
    if not aretes or pistes_en_degres(aretes):
        return None         # pas de reseau exploitable : on ne devine pas

    x, y = dt["x"], dt["y"]
    dist = distance_piste_la_plus_proche(aretes, x, y)
    zone = zone_hors_piste_autorisee(conn, x, y, reglages)
    maintenant = instant or now_iso()
    evenement = None

    if dt["offroad"]:
        if zone or dist <= reglages["retour_m"]:
            duree = int(_secondes_depuis(dt["offroad_depuis"], maintenant))
            message = "%s de retour sur piste (%s apres %d s hors piste)." % (
                dt_code, zone or "%d m de la piste" % int(dist), duree)
            conn.execute(
                "UPDATE engins SET offroad=0, offroad_depuis='', "
                "offroad_candidat_depuis='', offroad_distance=? WHERE code=?",
                (round(dist, 1), dt_code))
            conn.commit()
            clore_alertes_ouvertes(conn, dt_code, "offroad", maintenant)
            creer_alerte(conn, "retour_piste", dt_code, message)
            evenement = {"type": "retour_piste", "dt_code": dt_code, "x": x, "y": y,
                         "distance": dist, "duree_s": duree, "message": message}
        else:
            conn.execute("UPDATE engins SET offroad_distance=? WHERE code=?",
                         (round(dist, 1), dt_code))
            conn.commit()
    elif not zone and dist > reglages["seuil_m"]:
        candidat = dt["offroad_candidat_depuis"] or maintenant
        if _secondes_depuis(candidat, maintenant) >= reglages["duree_s"]:
            message = "SORTIE DE ROUTE : %s a %d m de la piste la plus proche." % (
                dt_code, int(dist))
            conn.execute(
                "UPDATE engins SET offroad=1, offroad_depuis=?, "
                "offroad_candidat_depuis='', offroad_distance=? WHERE code=?",
                (candidat, round(dist, 1), dt_code))
            conn.commit()
            creer_alerte(conn, "offroad", dt_code, message)
            evenement = {"type": "offroad", "dt_code": dt_code, "distance": dist,
                         "x": x, "y": y, "depuis": candidat, "message": message}
        else:
            conn.execute(
                "UPDATE engins SET offroad_candidat_depuis=?, offroad_distance=? "
                "WHERE code=?", (candidat, round(dist, 1), dt_code))
            conn.commit()
    else:
        conn.execute(
            "UPDATE engins SET offroad_candidat_depuis='', offroad_distance=? "
            "WHERE code=?", (round(dist, 1), dt_code))
        conn.commit()

    if evenement:
        enregistrer_alerte_office(conn, evenement)
    return evenement


def alertes_liste(conn, depuis="", jusqua=""):
    """Alertes (200 dernieres) ; depuis / jusqua (heure locale AAAA-MM-JJ HH:MM:SS) bornent UNE journee
    ou UN shift ; sans borne, tout l'historique (page Alertes). created_at est en UTC."""
    if depuis:
        lignes = [dict(r) for r in conn.execute(
            "SELECT * FROM alertes WHERE datetime(created_at, 'localtime') >= ? "
            "AND (? = '' OR datetime(created_at, 'localtime') < ?) ORDER BY id DESC LIMIT 200",
            (depuis, jusqua, jusqua))]
    else:
        lignes = [dict(r) for r in conn.execute("SELECT * FROM alertes ORDER BY id DESC LIMIT 200")]
    for l in lignes:
        l["statut"] = statut_alerte(l.get("type"))      # 24/11 : Off-route / Wrong Dumping (page Alertes)
        # 25/11 (calibrage) : UNE seule regle « info / a traiter » pour toutes les pages (dashboard, Home, barre
        # de navigation, Alertes) ; avant, chaque page avait sa propre liste et comptait autre chose.
        l["info"] = 1 if l.get("type") in ALERTES_INFO else 0
    return lignes


# Alertes qui ne demandent aucune action au bureau : fins et arrivees, retours a la normale (piste, trajet,
# bonne direction, demi-tour fait), checkpoint franchi dans le bon sens, approche d'une zone (pre-alerte de la
# tablette ; l'alerte a traiter est zone_interdite), rejet d'import Dig Plan (erreur de donnees, pas de terrain).
ALERTES_INFO = frozenset(("fin_mission", "arrivee", "mission", "retour_piste", "retour_trajet", "retour_direction",
                          "demi_tour", "checkpoint_valide", "zone_approche", "blast_rejete"))


def etat_offroad(conn):
    """Synthese pour le dashboard : DT actuellement hors piste."""
    reglages = offroad_reglages()
    aretes = offroad_aretes(conn)
    motif = ""
    if not reglages["actif"]:
        motif = "detection desactivee (OFFROAD_ACTIF)"
    elif not aretes:
        motif = "aucune piste active"
    elif pistes_en_degres(aretes):
        motif = "pistes en degres : reprojeter (/api/travels_roads_reprojeter)"
    dts = [dict(r) for r in conn.execute(
        "SELECT code, x, y, offroad_distance AS distance_m, offroad_depuis AS depuis, "
        "derniere_maj FROM engins WHERE type='DT' AND actif=1 AND offroad=1 "
        # un DT muet depuis plus de SUIVI_SIGNAL_PERDU_S n'est plus « hors piste en
        # ce moment » : sa position date, la carte de Live le montre « signal perdu »
        "AND (julianday('now', 'localtime') - julianday(derniere_maj)) * 86400 <= ? "
        "ORDER BY offroad_depuis", (SUIVI_SIGNAL_PERDU_S,))]
    return {"actif": not motif, "motif": motif, "count": len(dts), "dts": dts,
            "reglages": reglages, "aretes": len(aretes)}


# ------------------------------------------------------------- KPI reels ---
# KPI du dashboard calcules sur les VOYAGES REELS (16/11). Avant : constantes
# simulees dans dashboard.js, et un /api/stats SaaS lisant une table vide.
#   - minerais_t : tonnage des voyages de blocs minerai
#   - waste_t    : tonnage des voyages de blocs steriles (code WST / dest. W)
#   - onces      : or contenu = tonnes x Au (g/t, colonne Au_ok du Geosum)
#                  / 31,1035 (g par once troy), minerai seulement
#   - grades     : tonnage par code de teneur du NOM de bloc (4e segment,
#                  cf. nomenclature) : HG (+ SHG), MG, LG, MO
GRAMMES_PAR_ONCE = 31.1035
# 21/11 : table PAR DEFAUT ; reglable dans Settings (params CODES_TENEUR = JSON {code: grade} et
# CODES_STERILES = codes separes par des virgules). Les lectures passent par codes_teneur() / codes_steriles().
KPI_GRADES = {"HG": "high", "SHG": "high", "MG": "medium", "MG1": "medium",
              "MG2": "medium", "LG": "low", "LG1": "low", "LG2": "low", "MO": "marginal"}
GRADES_VALIDES = ("high", "medium", "low", "marginal")
_codes_cache = {"brut": None, "table": KPI_GRADES}


def codes_teneur():
    """Table {code de teneur: grade} en vigueur (reglage Settings, sinon KPI_GRADES)."""
    brut = param_lire("CODES_TENEUR", "")
    if brut != _codes_cache["brut"]:
        table = KPI_GRADES
        if brut:
            try:
                lue = json.loads(brut)
                propre = {str(k).strip().upper(): str(v).strip().lower() for k, v in lue.items()
                          if str(v).strip().lower() in GRADES_VALIDES and str(k).strip()}
                if propre:
                    table = propre
            except (ValueError, AttributeError):
                table = KPI_GRADES
        _codes_cache["brut"], _codes_cache["table"] = brut, table
    return _codes_cache["table"]


def codes_steriles():
    brut = param_lire("CODES_STERILES", "")
    codes = tuple(c.strip().upper() for c in brut.split(",") if c.strip())
    return codes or CODES_STERILES_DEFAUT


# 21/11 - COULEURS : source unique pour TOUTES les pages (Dig Plan, Senor, Inbox, Dashboard...).
# Une couleur par GRADE (reglable dans Settings, param COULEURS_GRADES = JSON {grade: "#rrggbb"}) ; chaque
# famille de teneur herite de la couleur du grade de son premier code de bloc, sauf si elle a la sienne
# (colonne teneurs.couleur, vide = automatique).
COULEURS_GRADES_DEFAUT = {"high": "#e8641c", "medium": "#1d6fd1", "low": "#e0b413",
                          "marginal": "#8a5cc2", "waste": "#8d887a"}
COULEUR_INCONNUE = "#b8b3a2"
_RE_COULEUR = re.compile(r"#[0-9a-fA-F]{6}")


def couleur_valide(v):
    v = str(v or "").strip()
    return v.lower() if _RE_COULEUR.fullmatch(v) else ""


def couleurs_grades():
    table = dict(COULEURS_GRADES_DEFAUT)
    try:
        lue = json.loads(param_lire("COULEURS_GRADES", "") or "{}")
        for g, c in (lue or {}).items():
            if g in table and couleur_valide(c):
                table[g] = couleur_valide(c)
    except (ValueError, AttributeError):
        pass
    return table


def couleurs_teneur(conn):
    """{grades, familles, codes, inconnue} : les couleurs EFFECTIVES, lues par toutes les pages."""
    grades, table = couleurs_grades(), codes_teneur()
    codes = {c: grades.get(g, COULEUR_INCONNUE) for c, g in table.items()}
    for c in codes_steriles():
        codes.setdefault(c, grades["waste"])
    familles = {}
    for r in conn.execute("SELECT nom, codes, couleur FROM teneurs WHERE actif=1 ORDER BY id"):
        propre = couleur_valide(r["couleur"])
        premier = next((c.strip().upper() for c in str(r["codes"] or "").split(",") if c.strip()), "")
        familles[r["nom"]] = propre or codes.get(premier, COULEUR_INCONNUE)
    return {"grades": grades, "familles": familles, "codes": codes, "inconnue": COULEUR_INCONNUE}


def classer_voyage(nom, destination, minerai):
    """(sterile, grade) d'un voyage : SOURCE UNIQUE de la classification (KPI du jour, KPI cumules,
    page Inbox). grade = 'high' / 'medium' / 'low' / 'marginal' d'apres le code de teneur du nom du
    bloc, '' si inconnu ; sterile = bloc WST, destination W ou bloc non minerai (sauf code de teneur connu)."""
    code = code_teneur_bloc(nom)
    dest = str(destination or "").strip().upper()
    steriles, grades = codes_steriles(), codes_teneur()
    minerai = (code not in steriles and dest != "W") if minerai is None else bool(minerai)
    sterile = (code in steriles or dest == "W" or not minerai) and code not in grades
    return sterile, grades.get(code, "")


def code_teneur_bloc(nom):
    """Code du 4e segment du nom (260_LMP2_113_MG_FR_021_OP -> MG), '' sinon.
    24/11 : flitch normalise d'abord (272_5_LMP2_119_HG_... -> HG, et non 119)."""
    parties = nom_flitch_canonique(nom).split("_")
    return parties[3].upper() if len(parties) >= 4 else ""


def kpi_voyages(conn, jour=None):
    """KPI des voyages du jour (UTC, comme created_at) ou de `jour` (AAAA-MM-JJ)."""
    lignes = conn.execute(
        "SELECT v.volume_t, o.nom, o.au_teneur, o.minerai, o.destination "
        "FROM voyages v LEFT JOIN oreblocs o ON o.id = v.orebloc_id "
        "WHERE date(v.created_at) = COALESCE(?, date('now'))", (jour,)).fetchall()
    res = {"jour": jour or conn.execute("SELECT date('now')").fetchone()[0],
           "voyages": len(lignes), "minerais_t": 0.0, "waste_t": 0.0, "onces": 0.0,
           "grades": {g: {"tonnes": 0.0, "onces": 0.0} for g in ("high", "medium", "low", "marginal")},
           "sans_bloc": 0}
    for r in lignes:
        t = float(r["volume_t"] or 0)
        if r["nom"] is None:
            res["sans_bloc"] += 1          # voyage sans orebloc connu : tonnage non classe
            continue
        sterile, g = classer_voyage(r["nom"], r["destination"], bool(r["minerai"]))
        if sterile:
            res["waste_t"] += t
            continue
        onces = t * float(r["au_teneur"] or 0) / GRAMMES_PAR_ONCE
        res["minerais_t"] += t
        res["onces"] += onces
        if g:
            res["grades"][g]["tonnes"] += t
            res["grades"][g]["onces"] += onces
    arrondir = lambda v: round(v, 1)
    res["minerais_t"], res["waste_t"], res["onces"] = map(arrondir, (res["minerais_t"], res["waste_t"], res["onces"]))
    for g in res["grades"].values():
        g["tonnes"], g["onces"] = arrondir(g["tonnes"]), arrondir(g["onces"])
    return res


# ------------------------------------------------------- descente MQTT --
_MQTT_CLIENT = None
_MQTT_LOCK = threading.Lock()


_MQTT_ECHEC_T = 0.0
_MQTT_REESSAI_S = 30
_mqtt_desc_etat = {"compte": "", "hote": "", "connecte_le": "", "erreur": "", "publies": 0, "echecs": 0,
                   "dernier": ""}


def mqtt_identifiants_publication():
    """(hote, port, identifiant, mdp, origine) du compte qui publie vers les DT.

    24/11 (E1) : avant, seule la variable d'environnement MQTT_HOST (jamais definie sur le poste)
    ouvrait la descente : aucune alerte ni mission ne partait par MQTT. Le compte « passerelle »
    du broker (mqtt.json, celui de l'abonnement aux positions) a les droits de publication sur
    camions/+/alerte et camions/+/mission : c'est lui qu'on utilise. MQTT_HOST reste un repli."""
    conf = mqtt_conf() or {}
    p = conf.get("passerelle") or {}
    if p.get("identifiant") and p.get("mdp"):
        return (conf.get("hote", "127.0.0.1"), int(conf.get("port", 1883)), p["identifiant"], p["mdp"], "mqtt.json")
    if MQTT_HOST:
        return (MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS, "env")
    return None


def _mqtt_client():
    """Cree et connecte (une seule fois, paresseusement) le client MQTT de
    publication. Retourne None si paho-mqtt n'est pas installe, si aucun
    compte n'est configure, ou si la connexion echoue (nouvel essai au plus
    toutes les 30 s) : le reste du serveur continue sans la descente MQTT."""
    global _MQTT_CLIENT, _MQTT_ECHEC_T
    if mqtt is None:
        return None
    if _MQTT_CLIENT is not None:
        return _MQTT_CLIENT
    with _MQTT_LOCK:
        if _MQTT_CLIENT is not None:
            return _MQTT_CLIENT
        if time.time() - _MQTT_ECHEC_T < _MQTT_REESSAI_S:
            return None
        ident = mqtt_identifiants_publication()
        if not ident:
            _mqtt_desc_etat["erreur"] = "aucun compte MQTT (mqtt.json absent, MQTT_HOST vide)"
            _MQTT_ECHEC_T = time.time()
            return None
        hote, port, user, mdp, origine = ident
        try:
            if hasattr(mqtt, "CallbackAPIVersion"):          # paho-mqtt 2.x
                client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                     client_id="passerelle-pub-%d" % os.getpid(), protocol=mqtt.MQTTv311)
            else:
                client = mqtt.Client(client_id="passerelle-pub-%d" % os.getpid())
            if user:
                client.username_pw_set(user, mdp)
            client.reconnect_delay_set(min_delay=1, max_delay=30)
            client.connect(hote, port, keepalive=30)
            client.loop_start()
            _MQTT_CLIENT = client
            _mqtt_desc_etat.update(compte=user or "", hote="%s:%d" % (hote, port), connecte_le=now_iso(),
                                   erreur="")
            print("MQTT: publication vers les DT par %s:%d (compte %s, %s)" % (hote, port, user or "-", origine))
        except Exception as e:
            _MQTT_ECHEC_T = time.time()
            _mqtt_desc_etat["erreur"] = str(e)
            print("MQTT: connexion de publication impossible (%s), nouvel essai dans %d s" % (e, _MQTT_REESSAI_S))
            return None
    return _MQTT_CLIENT


def mqtt_publier(topic, payload_dict):
    """Publie un message JSON sur un canal MQTT dedie (ex. camions/DT_04/
    mission). Ne leve jamais d'exception : retourne True/False. Se
    desactive proprement si paho-mqtt n'est pas installe ou si MQTT_HOST
    n'est pas configure."""
    client = _mqtt_client()
    if client is None:
        return False
    try:
        corps = json.dumps(payload_dict, ensure_ascii=True)
        info = client.publish(topic, corps, qos=1)
        ok = info.rc == mqtt.MQTT_ERR_SUCCESS
    except Exception as e:
        print("MQTT: erreur publication sur %s: %s" % (topic, e))
        ok = False
    _mqtt_desc_etat["publies" if ok else "echecs"] += 1
    _mqtt_desc_etat["dernier"] = "%s %s %s" % (now_iso(), topic, "ok" if ok else "echec")
    return ok


def creer_alerte(conn, type_alerte, dt_code, message, voix_off=1, dest_office=1):
    """Enregistre une alerte en base ET, si c'est une alerte pour le chauffeur,
    la publie en temps reel sur le canal MQTT du camion (camions/<dt_code>/alerte).

    24/11 (E1) : meme filtre que la notification par Serveur_OT (message_chauffeur :
    TYPES_ALERTE_CHAUFFEUR + interrupteur ALERTES_CHAUFFEUR) et meme texte « [type] texte
    ascii » : la tablette reconnait le doublon quand les deux voies arrivent. Les alertes du
    bureau (non_conforme, mission, arrivee, blast_rejete...) ne partent plus au chauffeur."""
    conn.execute(
        "INSERT INTO alertes (type, dt_code, message, voix_off, dest_office) "
        "VALUES (?, ?, ?, ?, ?)",
        (type_alerte, dt_code, message, voix_off, dest_office))
    conn.commit()
    if message_chauffeur({"type": type_alerte, "dt_code": dt_code, "message": message}) \
            and alertes_chauffeur_actives():
        mqtt_publier("camions/%s/alerte" % dt_code, {
            "type": type_alerte, "dt_code": dt_code, "message": texte_ascii(message),
            "voix_off": bool(voix_off), "horodatage": now_iso()})


# --------------------------------------------- relais vers Serveur_SG ------
# File des dernieres positions connues, videe par lot. On ne garde qu'une
# entree PAR ENGIN : si un camion emet dix fois entre deux envois, seule sa
# position la plus recente a de la valeur, et c'est autant de quota econome.
_sg_file = {}
_sg_verrou = threading.Lock()
_sg_dernier_envoi = 0.0


def missions_source():
    """Qui decide des missions : "cloud" (Serveur_SG) ou "local".

    Les deux serveurs savent affecter. Les laisser faire tous les deux
    produirait deux missions concurrentes pour le meme camion, avec des
    destinations potentiellement differentes. Un seul decide.
    """
    v = param_lire("MISSIONS_SOURCE", "cloud").strip().lower()
    return "local" if v == "local" else "cloud"


def alerte_rouge_minutes():
    """Seuil (minutes) au-dela duquel une alerte non acquittee devient rouge
    sur Home. Reglable dans Parametres, 10 min par defaut."""
    try:
        v = int(float(param_lire("ALERTE_ROUGE_MIN", "10")))
    except ValueError:
        return 10
    return v if v > 0 else 10


def debut_sg_local(valeur):
    """Heure de debut renvoyee par SG -> heure locale « AAAA-MM-JJ HH:MM:SS ».
    SG renvoie soit « 2026-11-18T17:56:36.000Z » (UTC), soit une chaine locale ;
    illisible : maintenant."""
    v = str(valeur or "").strip()
    try:
        if "T" in v:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", v):
            return v
    except ValueError:
        pass
    return now_iso()


def ingerer_missions_sg(conn, missions):
    """Recopie en local les missions decidees par Serveur_SG.

    Le cloud raisonne en NOMS (orebloc, dumping area) parce qu'un id SQLite
    n'a de sens que sur ce poste. On les retraduit ici en identifiants
    locaux, en tolerant qu'un nom soit inconnu : mieux vaut une mission avec
    un rattachement incomplet qu'aucune mission du tout.
    """
    enregistrees = []
    for m in missions or []:
        code = str(m.get("dt_code") or "").strip()
        if not code:
            continue
        try:
            id_sg = int(m.get("id"))
        except (TypeError, ValueError):
            id_sg = None
        if id_sg is not None and conn.execute(
                "SELECT 1 FROM missions WHERE id_sg=?", (id_sg,)).fetchone():
            continue                              # deja recopiee
        deja = conn.execute(
            "SELECT id FROM missions WHERE dt_code=? AND statut='en_cours'",
            (code,)).fetchone()
        if deja:
            continue

        orebloc_id = 0
        nom_bloc = str(m.get("orebloc") or "").strip()
        if nom_bloc:
            r = conn.execute("SELECT id FROM oreblocs WHERE nom=? AND actif=1",
                             (nom_bloc,)).fetchone()
            orebloc_id = r["id"] if r else 0

        da_id = 0
        nom_zone = str(m.get("dumping_area") or "").strip()
        if nom_zone:
            r = conn.execute(
                "SELECT id FROM dumping_areas WHERE nom=? AND actif=1",
                (nom_zone,)).fetchone()
            da_id = r["id"] if r else 0

        chemin = m.get("chemin")
        if not isinstance(chemin, list):
            try:
                chemin = json.loads(chemin or "[]")
            except Exception:
                chemin = []

        cur = conn.execute(
            "INSERT INTO missions (dt_code, orebloc_id, destination_x, "
            "destination_y, dumping_area_id, chemin, statut, id_sg, cle_sg, debut) "
            "VALUES (?, ?, ?, ?, ?, ?, 'en_cours', ?, ?, ?)",
            (code, orebloc_id, float(m.get("destination_x") or 0),
             float(m.get("destination_y") or 0), da_id, json.dumps(chemin),
             id_sg, str(m.get("cle") or "").strip(),
             # heure de debut de SG quand elle est lisible (sinon : maintenant)
             debut_sg_local(m.get("debut"))))
        conn.commit()

        # La publication MQTT reste la responsabilite de la passerelle : le
        # cloud n'a pas de connexion persistante vers les tablettes.
        mqtt_publier("camions/%s/mission" % code, {
            "mission_id": cur.lastrowid, "dt_code": code,
            "orebloc": nom_bloc, "dumping_area": nom_zone,
            "destination": [m.get("destination_x"), m.get("destination_y")],
            "chemin": chemin, "origine": "Serveur_SG",
            "horodatage": now_iso()})
        enregistrees.append({"id": cur.lastrowid, "dt_code": code,
                             "orebloc": nom_bloc, "dumping_area": nom_zone,
                             "points_chemin": len(chemin)})
    if enregistrees:
        print("SG: %d mission(s) recue(s) du cloud" % len(enregistrees))
    return enregistrees


def position_dt_exploitable(conn, dt_code, age_max_s=POSITION_FRAICHE_S):
    """(x, y) du DT si sa derniere position est RECENTE (heure locale, comme `derniere_maj`) et non nulle."""
    r = conn.execute("SELECT x, y, derniere_maj FROM engins WHERE code=?", (dt_code,)).fetchone()
    if not r or not (r["x"] or r["y"]) or age_position_s(r["derniere_maj"]) > age_max_s:
        return None
    return (r["x"], r["y"])


def verifier_conformite_dumping(conn, mission, position=None, strict=False, a_controler=False, detail=None):
    """Dumping Instruction : la teneur du bloc impose une dumping area.
    Retourne "conforme", "non_conforme" ou "" (pas de verdict) et cree
    l'alerte `non_conforme` quand il y a ecart.

    Dumping area ATTENDUE : celle que la table `teneurs` impose pour la teneur
    du bloc, quand cette table est renseignee ; sinon celle affectee a la
    mission (Serveur_SG l'a choisie d'apres sa consigne teneur -> zone). Le
    controle de cloture compare donc ou le DT a REELLEMENT decharge (le
    polygone qui contient sa `position`) a ce qui etait attendu.
    `position` absente : on compare l'attendu a la zone affectee (ancien
    comportement de /api/mission_fin, sans verdict si aucune regle teneurs).
    `strict` : sans position dans une dumping area, pas de verdict (cloture
    vue chez SG : le DT a pu abandonner sa mission en route).
    `a_controler` (21/11, enregistrement du voyage) : on ne se fie JAMAIS a la zone affectee pour dire
    « conforme ». Sans position exploitable, ou si le DT n'est dans aucune dumping area, le verdict est
    "compromis" (« A controler ») des qu'une zone est attendue ; il n'y a pas d'alerte dans ce cas.
    `detail` (dict, optionnel) : rempli avec de quoi EXPLIQUER le verdict a un lecteur (regle, zone attendue,
    zone reelle, etat de la position, raison) ; stocke avec le voyage et affiche dans l'Inbox."""
    detail = detail if detail is not None else {}

    def nom_da(i):
        r = conn.execute("SELECT nom FROM dumping_areas WHERE id=?", (i,)).fetchone() if i else None
        return r["nom"] if r else ("DA #%d" % i if i else "")

    attendue, teneur = 0, None
    # 25/11 (calibrage) : tous les blocs importes ont la teneur de coupure par defaut (10.0) ; la table `teneurs`
    # donnait donc la meme zone pour tout bloc (« devait decharger en RomPad 3 ») alors que la mission, et le rapport
    # de dumping, disaient RomPad4. La zone affectee a la mission par Serveur_SG fait foi ; la regle teneur n'est
    # plus qu'un repli quand la mission n'a pas de zone.
    if mission["orebloc_id"] and not mission["dumping_area_id"]:
        ob = conn.execute("SELECT * FROM oreblocs WHERE id=?", (mission["orebloc_id"],)).fetchone()
        if ob:
            teneur = float(ob["grade_cutoff"]) if ob["grade_cutoff"] is not None else DEFAULT_CUTOFF_GRADE
            instr = conn.execute(
                "SELECT dumping_area_id FROM teneurs WHERE valeur_min <= ? AND ? <= valeur_max "
                "AND dumping_area_id > 0 LIMIT 1", (teneur, teneur)).fetchone()
            if instr:
                attendue = instr["dumping_area_id"]
                detail["teneur"] = round(teneur, 1)
    regle = "teneur %.1f" % teneur if attendue else "consigne de Serveur_SG"
    detail["regle"] = "teneur" if attendue else "affectation"
    if not attendue:
        if position is None:
            detail["position"] = "absente"
            if a_controler and mission["dumping_area_id"]:
                detail.update(attendue=nom_da(mission["dumping_area_id"]), raison="position_absente")
                return "compromis"
            detail["raison"] = "aucune_zone_attendue"
            return ""
        attendue = mission["dumping_area_id"]
    reelle = mission["dumping_area_id"]
    detail["attendue"] = nom_da(attendue)
    if a_controler and position is None:
        detail["position"] = "absente"
        detail["raison"] = "position_absente" if attendue else "aucune_zone_attendue"
        return "compromis" if attendue else ""      # on ne sait pas ou le DT a decharge
    if position is not None:
        trouvee = 0
        for da in conn.execute("SELECT id, sommets FROM dumping_areas WHERE actif=1"):
            try:
                if point_in_polygon(position[0], position[1], json.loads(da["sommets"] or "[]")):
                    trouvee = da["id"]
                    break
            except (TypeError, ValueError):
                continue
        detail["position"] = "dans_da" if trouvee else "hors_da"
        if trouvee:
            reelle = trouvee
        elif a_controler:
            detail["raison"] = "hors_da" if attendue else "aucune_zone_attendue"
            return "compromis" if attendue else ""   # hors de toute dumping area : a verifier, pas « conforme »
        elif strict:
            detail["raison"] = "hors_da"
            return ""
    # 24/11 : waste -> toute zone « waste » est conforme (decision n° 4), y compris sans zone affectee (« WasteDump »)
    waste = mission_waste(conn, mission)
    if waste and reelle and (position is not None or attendue):
        if type_zone_id(conn, reelle) == "waste":
            detail.update(attendue=detail.get("attendue") or "WasteDump", reelle=nom_da(reelle), raison="zone_waste")
            return "conforme"
        if not attendue:
            detail["attendue"] = "WasteDump"
    if not attendue and not (waste and reelle and position is not None):
        detail["raison"] = "aucune_zone_attendue"
        return ""
    if not reelle:
        detail["raison"] = "aucune_zone_attendue"
        return ""
    detail["reelle"] = nom_da(reelle)
    if attendue and attendue == reelle:
        detail["raison"] = "zone_attendue"
        return "conforme"
    detail["raison"] = "autre_zone"
    def nom(i):
        if not i and waste:
            return "WasteDump"
        r = conn.execute("SELECT nom FROM dumping_areas WHERE id=?", (i,)).fetchone()
        return r["nom"] if r else "DA #%d" % i
    creer_alerte(
        conn, "non_conforme", mission["dt_code"],
        "Dumping non conforme : %s devait decharger en %s (%s), a decharge en %s" % (
            mission["dt_code"], nom(attendue), regle, nom(reelle)))
    return "non_conforme"


# ------------------------------------------------ alignement missions / SG ---
# 20/11 : la copie locale des missions ne se refermait jamais. Serveur_SG
# termine / abandonne les missions (la tablette lui envoie mission_fin) ; la
# passerelle n'en savait rien. Resultat : des missions « en_cours » vieilles
# de plusieurs semaines, et la regle « un DT = une mission en cours » refusait
# d'enregistrer les vraies missions du jour. On compare donc regulierement la
# liste « en_cours » de SG a la base locale :
#   - mission locale liee a SG (id_sg) absente de SG   -> 'termine'
#   - mission locale sans lien (ancienne, ou creee ici)  -> 'abandonnee',
#     sauf mode « local » et DT inconnu de SG
#   - mission SG inconnue localement                      -> importee
# Un SG injoignable ne ferme RIEN (liste None).
def sg_lire_missions_en_cours():
    """Missions « en_cours » chez Serveur_SG ; None si SG injoignable/refuse."""
    sg_url, sg_token, _ = sg_conf()
    if not sg_url or not sg_token:
        return None
    url = "%s?action=missions&statut=en_cours&token=%s" % (sg_url, quote(sg_token))
    try:
        with urllib.request.urlopen(url, timeout=30) as rep:
            d = json.loads(rep.read().decode("utf-8", "replace"))
    except Exception as e:
        print("MISSIONS: lecture de Serveur_SG impossible (%s)" % e)
        return None
    if not isinstance(d, dict) or d.get("ok") is False or not isinstance(d.get("missions"), list):
        return None
    return d["missions"]


def sg_ids_missions_statut(statut):
    """26/11 : ids SG des missions d'un statut donne (?action=missions&statut=...) ; None si SG illisible."""
    sg_url, sg_token, _ = sg_conf()
    if not sg_url or not sg_token:
        return None
    url = "%s?action=missions&statut=%s&token=%s" % (sg_url, quote(statut), quote(sg_token))
    try:
        with urllib.request.urlopen(url, timeout=30) as rep_:
            d = json.loads(rep_.read().decode("utf-8", "replace"))
    except Exception:
        return None
    if d.get("ok") is False or not isinstance(d.get("missions"), list):
        return None
    ids = set()
    for m in d["missions"]:
        try:
            ids.add(int(m.get("id")))
        except (TypeError, ValueError):
            pass
    return ids


def reconcilier_missions(conn, missions_sg):
    """Aligne les missions locales sur la liste « en_cours » de SG.
    Retourne {"fermees": n, "abandonnees": n, "importees": n}."""
    res = {"fermees": 0, "abandonnees": 0, "importees": 0}
    if missions_sg is None:
        return res
    par_dt, ids_sg = {}, set()
    for m in missions_sg:
        try:
            i = int(m.get("id"))
        except (TypeError, ValueError):
            continue
        code = str(m.get("dt_code") or "").strip()
        if not code:
            continue
        ids_sg.add(i)
        if code not in par_dt or i > int(par_dt[code]["id"]):
            par_dt[code] = m                       # la plus recente gagne
    local = missions_source() == "local"
    maintenant = now_iso()
    ouvertes = [dict(r) for r in conn.execute(
        "SELECT id, dt_code, id_sg FROM missions WHERE statut='en_cours'")]
    # 26/11 : une mission close chez SG n'est pas forcement « terminee » : SG la marque « annulee » (tablette remise a
    # zero, cycle perime) ou « abandonnee » (nouveau chargement). On garde CE statut ici (avant : tout devenait
    # « termine », controle de dumping compris). SG illisible : on attend le passage suivant pour fermer.
    a_fermer = [l for l in ouvertes if l["id_sg"] and l["id_sg"] not in ids_sg]
    statut_sg = {}
    if a_fermer:
        for st_sg, st_local in (("annulee", "annulee"), ("abandonnee", "abandonnee")):
            ids = sg_ids_missions_statut(st_sg)
            if ids is None:
                a_fermer = None
                break
            for i in ids:
                statut_sg[i] = st_local
    for l in ouvertes:
        if l["id_sg"]:
            if l["id_sg"] in ids_sg:
                continue
            if a_fermer is None:
                continue                           # statut reel inconnu (SG illisible) : pas de fermeture a l'aveugle
            if l["id_sg"] in statut_sg:
                conn.execute("UPDATE missions SET statut=?, fin=? WHERE id=?", (statut_sg[l["id_sg"]], maintenant, l["id"]))
                fin_surveillance_dt(conn, l["dt_code"])
                res["abandonnees"] += 1
                continue
            # le DT a decharge : la dumping area ATTEINTE (position) est-elle celle que
            # la teneur impose ? (avant : ce controle n'existait que dans /api/mission_fin,
            # que personne n'appelle)
            mission = conn.execute("SELECT * FROM missions WHERE id=?", (l["id"],)).fetchone()
            dt = conn.execute("SELECT x, y FROM engins WHERE code=?", (l["dt_code"],)).fetchone()
            try:
                detail_conf = {}
                verdict = verifier_conformite_dumping(conn, mission, (dt["x"], dt["y"]) if dt else None, strict=True,
                                                      detail=detail_conf)
                if verdict:
                    # le voyage deja enregistre recoit aussi ce verdict (position reelle) s'il n'en a pas de ferme
                    conn.execute("UPDATE voyages SET conformite=?, conformite_detail=? "
                                 "WHERE mission_id=? AND conformite IN ('', 'compromis')",
                                 (verdict, json.dumps(detail_conf, ensure_ascii=True), l["id"]))
                    conn.commit()
            except Exception as e:
                print("MISSIONS: controle de conformite impossible (%s)" % e)
            conn.execute("UPDATE missions SET statut='termine', fin=? WHERE id=?", (maintenant, l["id"]))
            fin_surveillance_dt(conn, l["dt_code"])
            res["fermees"] += 1
        else:
            if local and l["dt_code"] not in par_dt:
                continue                           # decision d'ici, SG ne la connait pas encore
            conn.execute("UPDATE missions SET statut='abandonnee', fin=? WHERE id=?", (maintenant, l["id"]))
            fin_surveillance_dt(conn, l["dt_code"])
            res["abandonnees"] += 1
    conn.commit()
    a_importer = [m for m in par_dt.values()
                  if not conn.execute("SELECT 1 FROM missions WHERE id_sg=?", (int(m["id"]),)).fetchone()]
    if a_importer:
        res["importees"] = len(ingerer_missions_sg(conn, a_importer))
    if any(res.values()):
        print("MISSIONS: alignement sur Serveur_SG : %(fermees)d terminee(s), %(abandonnees)d abandonnee(s), "
              "%(importees)d importee(s)" % res)
    return res


def boucle_missions_sg():
    while True:
        try:
            if sg_conf()[0]:
                conn = get_conn()
                try:
                    reconcilier_missions(conn, sg_lire_missions_en_cours())
                finally:
                    conn.close()
        except Exception as e:
            print("MISSIONS: alignement impossible (%s)" % e)
        try:
            pause = max(1, int(float(param_lire("MISSIONS_SYNC_S", "6"))))
        except ValueError:
            pause = 6
        time.sleep(pause)


_sg_avert_local = False


def sg_relayer_position(code, typ, x, y):
    """Met la position en file pour Serveur_SG. Ne bloque jamais l'appelant."""
    if not sg_conf()[0]:
        return
    with _sg_verrou:
        _sg_file[code] = {"code": code, "type": typ, "x": x, "y": y}


def sg_vider_file(force=False):
    """Envoie la file a Serveur_SG si l'intervalle est ecoule.

    Tolerant a la panne : en cas d'echec reseau les positions sont remises
    en file (sans ecraser une position plus recente), et on reessaiera au
    prochain passage. Une coupure Internet ne doit pas perdre de donnees ni
    faire tomber la passerelle.
    """
    global _sg_dernier_envoi
    sg_url, sg_token, sg_intervalle = sg_conf()
    if not sg_url:
        return False
    global _sg_avert_local
    if missions_source() == "local" and not _sg_avert_local:
        _sg_avert_local = True
        print("ATTENTION : MISSIONS_SOURCE=local mais les positions partent encore vers Serveur_SG, "
              "qui cree ses propres missions (la tablette lit celles de Serveur_SG). "
              "Deux jeux de missions divergent : utiliser MISSIONS_SOURCE=cloud, ou vider SG_URL.")
    maintenant = time.time()
    if not force and (maintenant - _sg_dernier_envoi) < sg_intervalle:
        return False
    with _sg_verrou:
        if not _sg_file:
            return False
        lot = list(_sg_file.values())
        _sg_file.clear()
    _sg_dernier_envoi = maintenant

    url = "%s?action=positions&token=%s" % (sg_url, quote(sg_token))
    corps = json.dumps({"positions": lot}).encode("utf-8")
    requete = urllib.request.Request(
        url, data=corps, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(requete, timeout=20) as rep:
            reponse = json.loads(rep.read().decode("utf-8"))
        if not reponse.get("ok"):
            print("SG: refus applicatif : %s" % reponse.get("erreur"))
            return False
        # Serveur_SG rend les missions qu'il vient de decider : on les
        # recopie tout de suite. Pas de scrutation a prevoir -- la passerelle
        # est derriere un NAT, le cloud ne peut pas l'appeler, mais il repond
        # toujours a celui qui l'appelle.
        creees = reponse.get("missions_creees") or []
        if creees and missions_source() == "cloud":
            conn_sg = get_conn()
            try:
                ingerer_missions_sg(conn_sg, creees)
            except Exception as e:
                print("SG: missions recues mais non enregistrees (%s)" % e)
            finally:
                conn_sg.close()
        return True
    except Exception as e:
        print("SG: envoi de %d position(s) echoue (%s), remises en file" % (len(lot), e))
        with _sg_verrou:
            for p in lot:
                _sg_file.setdefault(p["code"], p)
        return False


# Champs descriptifs d'un blast, partages par les handlers et par le miroir
# vers Serveur_OT. Au niveau module : le miroir en a besoin hors handler.
BLAST_META_FIELDS = ("nom", "name_base", "name_dxf", "name_geosum", "name_survey",
                     "name_map", "map_url", "source_nom", "destination", "bench",
                     "heading", "flitch", "plan_date", "scale", "page_label")


# ------------------------------------------ miroir des blasts vers Serveur_OT --
# Jusqu'ici la page Dig Plan ecrivait dans UN SEUL backend : le selecteur
# `API()` de digplan.js renvoie ApiOT **ou** ApiSG. Un blast saisi en mode
# local n'atteignait donc jamais le classeur, et les deux bases divergeaient
# par construction. La passerelle devient le miroir : la base locale reste la
# source de verite (disponible hors ligne), et chaque ecriture est recopiee
# vers Serveur_OT. Un echec ne fait jamais echouer l'enregistrement local :
# le blast est simplement marque non synchronise et repoussable.

def ot_appel(action, payload=None, params=None):
    """Appel a Serveur_OT. Retourne (ok, donnees, erreur)."""
    office_url, office_token = office_conf()
    if not office_url:
        return False, {}, "liaison Serveur_OT non configuree (voir /api/relais)"
    url = "%s?action=%s&token=%s" % (office_url, action, quote(office_token))
    for cle, valeur in (params or {}).items():
        url += "&%s=%s" % (cle, quote(str(valeur)))
    donnees = None
    entetes = {}
    if payload is not None:
        donnees = json.dumps(payload, ensure_ascii=True).encode("utf-8")
        # text/plain volontaire : evite le preflight CORS qu'Apps Script ne
        # sait pas traiter.
        entetes["Content-Type"] = "text/plain;charset=utf-8"
    requete = urllib.request.Request(
        url, data=donnees, method="POST" if donnees else "GET", headers=entetes)
    try:
        with urllib.request.urlopen(requete, timeout=60) as rep:
            brut = rep.read().decode("utf-8", "replace")
        reponse = json.loads(brut)
    except Exception as e:
        return False, {}, "%s: %s" % (type(e).__name__, e)
    if reponse.get("ok") is False:
        return False, reponse, str(reponse.get("erreur") or "Serveur_OT a refuse")
    return True, reponse, ""


_MIMES_CARTE = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".gif": "image/gif", ".webp": "image/webp"}


def carte_en_data_url(map_url):
    """Carte deposee localement -> data URL base64, ou "" si introuvable.

    Serveur_OT sait deposer la carte sur Drive, mais seulement si on lui
    envoie `map_contenu`. Sans cela il recopie la `map_url` telle quelle,
    c'est-a-dire une adresse en localhost -- qui, pour une tablette, designe
    la tablette elle-meme. L'image existe donc bien, mais personne d'autre
    que ce poste ne peut la charger.
    """
    nom = os.path.basename(str(map_url or "").split("?")[0]).strip()
    if not nom:
        return ""
    nom = unquote(nom)
    chemin = os.path.join(PLANS_DIR, nom)
    if not os.path.isfile(chemin):
        return ""
    mime = _MIMES_CARTE.get(os.path.splitext(nom)[1].lower(), "image/jpeg")
    try:
        with open(chemin, "rb") as f:
            donnees = f.read()
    except OSError:
        return ""
    return "data:%s;base64,%s" % (mime, base64.b64encode(donnees).decode("ascii"))


def blast_corps_ot(conn, blast_id):
    """Blast local -> corps attendu par ?action=blast_upload."""
    ligne = conn.execute("SELECT * FROM blasts WHERE id=?", (blast_id,)).fetchone()
    if not ligne:
        return None
    b = dict(ligne)
    oreblocs = []
    for r in conn.execute(
            "SELECT * FROM oreblocs WHERE blast_id=? AND actif=1 ORDER BY id",
            (blast_id,)):
        o = dict(r)
        try:
            sommets = json.loads(o.get("sommets") or "[]")
        except Exception:
            sommets = []
        if len(sommets) < 3:
            continue
        oreblocs.append({
            "nom": o.get("nom") or "",
            "sommets": sommets,
            # 1 = minerai (present dans le Geosum). Serveur_OT ne projette
            # que ceux-la vers les onglets que lisent les tablettes.
            "minerai": 1 if (o.get("minerai") is None or o.get("minerai")) else 0,
            "volume": o.get("volume") or "",
            "tonnes": o.get("tonnes") or "",
            "au_teneur": o.get("au_teneur") or "",
            "density": o.get("density") or "",
            "destination": o.get("destination") or "",
            "litho": o.get("litho") or "",
        })
    corps = {"numero": (b.get("numero") or b.get("nom") or "").strip(),
             "oreblocs": oreblocs}
    for champ in BLAST_META_FIELDS:
        if b.get(champ):
            corps[champ] = b[champ]
    # La carte voyage avec le blast : Serveur_OT la depose sur Drive et rend
    # une URL que les tablettes peuvent reellement ouvrir. On ne joint le
    # binaire que si la map_url pointe encore sur ce poste.
    url = str(b.get("map_url") or "")
    if url and ("localhost" in url or "127.0.0.1" in url or url.startswith("/")):
        contenu = carte_en_data_url(url)
        if contenu:
            corps["map_contenu"] = contenu
            # NOM SUR LE DRIVE = NUMERO DU BLAST (18/11). Le code de carte
            # que lisent Serveur_OT et les tablettes est le nom du fichier
            # sans extension : `275_..._119v3_rogne.jpg` n'etait pas un code
            # de carte valide, et le RL partait sans carte. Le nom local
            # (name_map en base) n'est pas modifie.
            ext = os.path.splitext(unquote(url.split("?")[0]))[1].lower() or ".jpg"
            corps["name_map"] = corps["numero"] + ext
    return corps


def blast_a_une_carte(corps):
    """Vrai si le blast porte une carte joignable par le cloud : binaire
    joint (map_contenu) ou URL deja publiee (Drive, http distant)."""
    if corps.get("map_contenu"):
        return True
    url = str(corps.get("map_url") or "").strip()
    return bool(url) and not ("localhost" in url or "127.0.0.1" in url
                              or url.startswith("/"))


def ot_marquer_blast(conn, blast_id, ok, erreur=""):
    conn.execute(
        "UPDATE blasts SET ot_sync=?, ot_erreur=?, ot_maj_le=? WHERE id=?",
        (1 if ok else 0, (erreur or "")[:300], now_iso() if ok else "", blast_id))
    conn.commit()


def ot_pousser_blast(conn, blast_id):
    """Recopie un blast vers Serveur_OT. Retourne (ok, erreur)."""
    corps = blast_corps_ot(conn, blast_id)
    if corps is None:
        return False, "blast introuvable"
    if not corps["numero"]:
        ot_marquer_blast(conn, blast_id, False, "blast sans numero")
        return False, "blast sans numero"
    # Jamais de blast sans carte dans le classeur (18/11, regle client).
    if not blast_a_une_carte(corps):
        ot_marquer_blast(conn, blast_id, False, "carte absente : blast non envoye")
        return False, "carte absente : blast non envoye"
    ok, reponse, erreur = ot_appel("blast_upload", payload=corps)
    # Serveur_OT rend l'URL Drive de la carte qu'il vient de deposer : on la
    # reprend en local, sinon on renverrait le binaire a chaque poussee.
    if ok:
        neuve = str((reponse or {}).get("map_url") or "").strip()
        if neuve and neuve != str(corps.get("map_url") or ""):
            conn.execute("UPDATE blasts SET map_url=? WHERE id=?", (neuve, blast_id))
            conn.commit()
    ot_marquer_blast(conn, blast_id, ok, erreur)
    if ok:
        # Circuit tablettes : recopie le blast dans sync_blasts (base du
        # SaaS, data/oretracking.db) pour que la page Synch du SaaS voie
        # ce que Dig Plan a cree. Format attendu : numero/carte/
        # libelle_carte/blocs (canon_blasts cote server.py).
        alimenter_sync_blasts(conn, blast_id)
    else:
        print("OT: blast #%s non synchronise (%s)" % (blast_id, erreur))
    return ok, erreur


def alimenter_sync_blasts(conn, blast_id):
    """Recopie un blast Dig Plan dans sync_blasts (oretracking.db).
    Sans echec bloquant : le miroir tablette est secondaire, un souci de
    base SaaS ne doit pas marquer le blast en echec de synchro."""
    try:
        b = conn.execute("SELECT numero, source_nom FROM blasts WHERE id=?",
                         (blast_id,)).fetchone()
        if not b:
            return
        blocs = conn.execute(
            "SELECT nom FROM oreblocs WHERE blast_id=? AND actif=1 ORDER BY id",
            (blast_id,)).fetchall()
        carte = (b["source_nom"] or "").strip()
        saas = os.path.join(os.path.dirname(DATA_DIR), "data", "oretracking.db")
        if not os.path.isfile(saas):
            print("OT: sync_blasts non alimente (oretracking.db introuvable)")
            return
        c2 = sqlite3.connect(saas)
        try:
            c2.execute(
                "INSERT OR REPLACE INTO sync_blasts "
                "(numero, carte, libelle_carte, blocs) VALUES (?,?,?,?)",
                (str(b["numero"]), carte, carte,
                 json.dumps([x[0] for x in blocs])))
            c2.commit()
        finally:
            c2.close()
    except Exception as e:
        print("OT: sync_blasts non alimente (%s)" % e)


def ot_supprimer_blast_onglets(numero):
    """Supprime INTEGRALEMENT les lignes du blast des onglets dig_bloc et
    ore_dig_bloc (Serveur_OT, action=onglet_purge)."""
    erreurs = []
    for onglet in BLAST_BLOC_ONGLETS.values():
        ok, _, err = ot_appel("onglet_purge", params={"onglet": onglet, "numero": numero})
        if not ok:
            erreurs.append("%s: %s" % (onglet, err))
    return (not erreurs), " ; ".join(erreurs)


def ot_clore_blast(conn, blast_id):
    """Envoie l'etat CLOS d'un blast a Serveur_OT (action blast_clore).
    Retourne (ok, erreur). Marque clos_ot=1 au succes ; un echec est rejoue
    par rattraper_blasts_ot()."""
    ligne = conn.execute("SELECT numero, clos_le FROM blasts WHERE id=?",
                         (blast_id,)).fetchone()
    if not ligne or not (ligne["clos_le"] or "").strip():
        return False, "blast non clos"
    numero = (ligne["numero"] or "").strip()
    ok, _, err = ot_appel("blast_clore", payload={"numero": numero, "origine": "saas"})
    conn.execute("UPDATE blasts SET clos_ot=?, ot_erreur=? WHERE id=?",
                 (1 if ok else 0, "" if ok else ("cloture : %s" % err)[:300], blast_id))
    conn.commit()
    if not ok:
        print("OT: cloture du blast %s non transmise (%s)" % (numero, err))
    return ok, err


def clore_blast_local(conn, blast_id):
    """Pose l'etat CLOS (une seule fois : la date d'origine est gardee)."""
    conn.execute("UPDATE blasts SET clos_le=? WHERE id=? AND "
                 "(clos_le IS NULL OR clos_le='')", (now_iso(), blast_id))
    conn.commit()


def ot_supprimer_blast(numero):
    """Retire un blast du classeur. Retourne (ok, erreur)."""
    if not str(numero or "").strip():
        return False, "numero requis"
    return ot_appel("dig_plan_suppr", params={"numero": numero})[0::2]


# ------------------------------------- synchronisation descendante (OT -> local) --
# Le miroir ci-dessus ne va que dans un sens. Si quelqu'un modifie le classeur
# -- ou si un autre poste pousse ses blasts -- la base locale l'ignore.
#
# Politique retenue, volontairement prudente :
#   * ce qui MANQUE d'un cote est recopie automatiquement : additif, donc sans
#     perte possible ;
#   * ce qui DIVERGE des deux cotes n'est jamais tranche tout seul. On le
#     signale, et l'arbitrage se demande explicitement ("distant" ou "local").
#     Un merge silencieux detruirait du travail sans laisser de trace.

def ot_etat_distant():
    """Lit le classeur : blasts + referentiel minier. (ok, etat, erreur)."""
    ok, plan, err = ot_appel("dig_plan")
    if not ok:
        return False, {}, err
    ok, ref, err = ot_appel("dig_referentiel")
    if not ok:
        return False, {}, err
    return True, {
        "blasts": plan.get("blasts") or [],
        "sources": ref.get("sources") or [],
        "teneurs": ref.get("teneurs") or [],
        "dumping_areas": ref.get("dumping_areas") or [],
    }, ""


def _sommets_liste(v):
    if isinstance(v, list):
        return v
    try:
        return json.loads(v or "[]")
    except Exception:
        return []


def _norme_champ(champ, valeur):
    """Normalise un champ avant comparaison.

    Sheets convertit une date texte en cellule Date et la rend en ISO
    complet : "2026-10-11" ressort "2026-10-11T00:00:00.000Z". Meme jour,
    deux ecritures -- sans cette normalisation le diff signalerait une
    divergence a chaque passage, sur tous les blasts, indefiniment.
    """
    v = str(valeur or "").strip()
    if not v:
        return ""
    if champ in ("plan_date",):
        m = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[T ]|$)", v)
        if m:
            return m.group(1)
    return v


def _blocs_valides(blast_distant):
    return [o for o in (blast_distant.get("oreblocs") or [])
            if len(_sommets_liste(o.get("sommets"))) >= 3]


def ot_comparer(conn, distant=None):
    """Diff local <-> classeur. Ne modifie rien. Retourne (diff, erreur)."""
    if distant is None:
        ok, distant, err = ot_etat_distant()
        if not ok:
            return None, err

    local = {}
    for r in conn.execute("SELECT * FROM blasts WHERE actif=1"):
        b = dict(r)
        b["_blocs"] = [dict(x) for x in conn.execute(
            "SELECT nom, sommets, destination FROM oreblocs "
            "WHERE blast_id=? AND actif=1", (b["id"],))]
        local[(b.get("numero") or "").strip()] = b
    en_ligne = {(b.get("numero") or "").strip(): b for b in distant["blasts"]}

    blasts = {"seulement_local": sorted(set(local) - set(en_ligne)),
              "seulement_en_ligne": sorted(set(en_ligne) - set(local)),
              "divergents": []}
    for numero in sorted(set(local) & set(en_ligne)):
        a, b = local[numero], en_ligne[numero]
        ecarts = []
        if len(a["_blocs"]) != len(_blocs_valides(b)):
            ecarts.append("nombre de blocs")
        for champ in BLAST_META_FIELDS:
            if _norme_champ(champ, a.get(champ)) != _norme_champ(champ, b.get(champ)):
                ecarts.append(champ)
        if ecarts:
            blasts["divergents"].append({"numero": numero, "champs": ecarts})

    def noms(rows):
        return {str(r.get("nom") or "").strip() for r in rows
                if str(r.get("nom") or "").strip()}

    tables = {
        "dumping_areas": ({r["nom"].strip() for r in conn.execute(
            "SELECT nom FROM dumping_areas WHERE actif=1")},
            noms(distant["dumping_areas"])),
        "sources": ({r["nom"].strip() for r in conn.execute(
            "SELECT nom FROM sources WHERE actif=1")}, noms(distant["sources"])),
        "teneurs": ({r["nom"].strip() for r in conn.execute(
            "SELECT nom FROM teneurs WHERE actif=1")}, noms(distant["teneurs"])),
    }
    referentiel = {}
    for libelle, (ici, la_bas) in tables.items():
        referentiel[libelle] = {"seulement_local": sorted(ici - la_bas),
                                "seulement_en_ligne": sorted(la_bas - ici)}

    aligne = (not blasts["seulement_local"] and not blasts["seulement_en_ligne"]
              and not blasts["divergents"]
              and all(not v["seulement_local"] and not v["seulement_en_ligne"]
                      for v in referentiel.values()))
    return {"aligne": aligne, "blasts": blasts, "referentiel": referentiel}, ""


def ot_importer_blast(conn, b):
    """Cree en local un blast present seulement dans le classeur."""
    numero = (b.get("numero") or "").strip()
    if not numero:
        return False
    cols = [c for c in BLAST_META_FIELDS if b.get(c)]
    vals = [str(b[c]).strip() for c in cols]
    blocs = _blocs_valides(b)
    cur = conn.execute(
        "INSERT INTO blasts (numero, source_fichier, nb_blocs, ot_sync, ot_maj_le" +
        "".join(", " + c for c in cols) + ") VALUES (?, ?, ?, 1, ?" +
        ", ?" * len(cols) + ")",
        tuple([numero, "Serveur_OT", len(blocs), now_iso()] + vals))
    bid = cur.lastrowid
    for o in blocs:
        nom = str(o.get("nom") or "").strip() or ("Orebloc %s" % bid)
        if conn.execute("SELECT id FROM oreblocs WHERE nom=? AND actif=1",
                        (nom,)).fetchone():
            n = 2
            while conn.execute("SELECT id FROM oreblocs WHERE nom=? AND actif=1",
                               ("%s (%d)" % (nom, n),)).fetchone():
                n += 1
            nom = "%s (%d)" % (nom, n)
        conn.execute(
            "INSERT INTO oreblocs (nom, grade_cutoff, sommets, source_fichier, "
            "blast_id, destination) VALUES (?, ?, ?, ?, ?, ?)",
            (nom, DEFAULT_CUTOFF_GRADE,
             json.dumps(_sommets_liste(o.get("sommets"))),
             "Serveur_OT", bid, str(o.get("destination") or "").strip()))
    conn.commit()
    return True


def ot_reconcilier(conn, arbitrage="constat"):
    """Aligne les deux bases. Retourne (rapport, erreur).

    `arbitrage` : "constat" (defaut : ne tranche aucune divergence),
    "distant" (le classeur fait foi) ou "local" (la base locale fait foi).
    """
    ok, distant, err = ot_etat_distant()
    if not ok:
        return None, err
    diff, err = ot_comparer(conn, distant)
    if diff is None:
        return None, err

    actions = {"importes": [], "pousses": [], "arbitres": [], "en_attente": []}
    par_numero = {(b.get("numero") or "").strip(): b for b in distant["blasts"]}

    for numero in diff["blasts"]["seulement_en_ligne"]:
        if ot_importer_blast(conn, par_numero[numero]):
            actions["importes"].append(numero)

    for numero in diff["blasts"]["seulement_local"]:
        ligne = conn.execute("SELECT id FROM blasts WHERE numero=? AND actif=1",
                             (numero,)).fetchone()
        if ligne and ot_pousser_blast(conn, ligne["id"])[0]:
            actions["pousses"].append(numero)

    for d in diff["blasts"]["divergents"]:
        numero = d["numero"]
        if arbitrage == "local":
            ligne = conn.execute("SELECT id FROM blasts WHERE numero=? AND actif=1",
                                 (numero,)).fetchone()
            if ligne and ot_pousser_blast(conn, ligne["id"])[0]:
                actions["arbitres"].append({"numero": numero, "gagnant": "local"})
        elif arbitrage == "distant":
            conn.execute(
                "UPDATE oreblocs SET actif=0 WHERE blast_id IN "
                "(SELECT id FROM blasts WHERE numero=? AND actif=1)", (numero,))
            conn.execute("UPDATE blasts SET actif=0 WHERE numero=?", (numero,))
            conn.commit()
            if ot_importer_blast(conn, par_numero[numero]):
                actions["arbitres"].append({"numero": numero, "gagnant": "distant"})
        else:
            actions["en_attente"].append(d)

    apres, _ = ot_comparer(conn)
    return {"arbitrage": arbitrage, "actions": actions,
            "avant": diff, "apres": apres}, ""


def office_appel(action, payload_dict):
    """Appel HTTP reel vers le serveur Office (server_OT, doPost). Retourne
    True si Office a repondu ok:true, False sinon (reseau, HTTP, ok:false
    applicatif, ou OFFICE_URL non configure - desactivation propre)."""
    office_url, office_token = office_conf()
    if not office_url:
        return False
    url = "%s?action=%s&token=%s" % (office_url, action, quote(office_token))
    corps = json.dumps(payload_dict, ensure_ascii=True).encode("utf-8")
    requete = urllib.request.Request(
        url, data=corps, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(requete, timeout=10) as rep:
            brut = rep.read().decode("utf-8")
        try:
            reponse = json.loads(brut)
        except ValueError:
            # Apps Script rend une page HTML (connexion Google, deploiement supprime, quota) : on garde le debut
            raise ValueError("reponse non JSON : %s" % re.sub(r"\s+", " ", brut)[:160])
        if reponse.get("ok"):
            _office_derniere_erreur["ok_le"] = now_iso()
            return True
        raise ValueError("refus de OT : %s" % str(reponse.get("erreur") or reponse)[:200])
    except Exception as e:
        _office_derniere_erreur.update(erreur=str(e)[:300], le=now_iso(), action=action)
        print("OFFICE: appel '%s' echoue: %s" % (action, e))
        return False


def office_message(payload):
    """Construit un message humain (pour /action=notification) a partir
    d'un payload enfile dans office_sync."""
    if "alerte" in payload:
        a = payload["alerte"]
        return a.get("message") or ("Alerte %s sur %s" % (a.get("type", ""), a.get("dt_code", "")))
    if "mission" in payload:
        m = payload["mission"]
        return "Affectation : %s -> Dumping Area #%s" % (m.get("dt_code", ""), m.get("dumping_area_id", ""))
    if payload.get("action") == "fin_mission":
        return ("Fin de mission %s : %.0f t (bloc #%s, cumul %.0f t sur %d voyage(s))" % (
            payload.get("dt_code", ""), payload.get("volume_t", 0) or 0,
            payload.get("orebloc_id", ""), payload.get("cumul_bloc_t", 0) or 0,
            payload.get("cumul_bloc_voyages", 0) or 0))
    return json.dumps(payload, ensure_ascii=True)


# ------------------------------------------- evenements typees vers OT ---
# Depuis le 16/11, Serveur_OT accepte les evenements TERRAIN (MISSION,
# MISSION_FIN, ARRIVEE, OFFROAD, RETOUR_PISTE, DEVIATION, RETOUR_TRAJET).
# Ils partent en ?action=evenement, dans l'onglet `evenements` : exploitable
# par le bureau (KPI, historique, carte), au lieu d'une phrase en
# `notification`. Le reste (minage declare...) garde la notification texte.
# 24/11 : le 23/11, 11 envois ont ete perdus (10:02 -> 15:11) : OT a refuse tous les appels pendant des heures et
# les 10 essais d'une entree etaient epuises en 6 a 27 min (30 s entre deux essais). Maintenant : delai DOUBLE a
# chaque echec (30 s, 1 min, 2 min... plafonne a 30 min), 20 essais (environ 7 h de couverture), raison de l'echec
# gardee sur l'entree (colonne `erreur`) et dans l'etat de la liaison ; une alerte au CHAUFFEUR plus vieille que
# OFFICE_CHAUFFEUR_MAX_S n'est plus envoyee (statut 'perime') : la voix d'une sortie de route deux heures apres
# n'aurait aucun sens (l'evenement du bureau, lui, est toujours rejoue).
OFFICE_TENTATIVES_MAX = 20       # au-dela, l'entree reste en echec (visible)
OFFICE_REESSAI_S = 30            # delai apres le 1er echec ; double ensuite
OFFICE_REESSAI_MAX_S = 1800      # plafond du delai entre deux essais
OFFICE_CHAUFFEUR_MAX_S = 900     # au-dela, une notification au chauffeur est perimee
OFFICE_FENETRE_REESSAI_H = 24    # on ne rejoue pas d'evenements plus vieux
_office_derniere_erreur = {"erreur": "", "le": "", "action": "", "ok_le": ""}


def office_delai_reessai(tentatives):
    """Delai minimal (s) avant le prochain essai d'une entree deja tentee `tentatives` fois."""
    n = max(1, int(tentatives or 0))
    return min(OFFICE_REESSAI_MAX_S, OFFICE_REESSAI_S * (2 ** min(n - 1, 12)))
OFFICE_LOT_MAX = 20              # entrees traitees par passage

_TYPES_TERRAIN_OT = {
    "offroad": "OFFROAD", "retour_piste": "RETOUR_PISTE",
    "deviation": "DEVIATION", "retour_trajet": "RETOUR_TRAJET",
}


def office_evenement(payload):
    """Traduit un payload de la file en (event_type, payload OT), ou None si
    l'entree reste une notification texte. Tous portent dt_code."""
    action = payload.get("action")
    a = payload.get("alerte")
    if isinstance(a, dict) and a.get("type") in _TYPES_TERRAIN_OT:
        return _TYPES_TERRAIN_OT[a["type"]], dict(a)
    if action == "arrivee":
        return "ARRIVEE", {k: payload.get(k) for k in ("mission_id", "dt_code", "dumping_area")}
    if action == "fin_mission":
        corps = {k: v for k, v in payload.items() if k != "action"}
        corps["tonnes"] = payload.get("volume_t")
        return "MISSION_FIN", corps
    if isinstance(payload.get("mission"), dict) and action in ("affectation", "affectation_auto"):
        m = dict(payload["mission"])
        m["origine"] = action
        return "MISSION", m
    return None


def office_event_id(entree):
    """UUID stable pour une entree de la file : un renvoi (reseau coupe apres
    reception) est reconnu en DOUBLON par OT au lieu d'etre ecrit deux fois.
    created_at entre dans le nom : une base recreee ne reutilise pas d'UUID."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "senor-geology-passerelle:%s:%s" % (
        entree["id"], entree.get("created_at") or "")))


_office_reveil = threading.Event()
_office_thread_actif = {"oui": False}


def notifier_office(conn=None):
    """Demande l'envoi de la file vers Serveur_OT, SANS l'attendre (16/11).

    Les routes appellent cette fonction sous LOCK, le verrou global de la
    passerelle. Avant, les appels HTTP (jusqu'a 10 s chacun) se faisaient ici,
    sous ce verrou : un OT lent figeait les positions de toute la flotte. Le
    travail est desormais fait par le thread `boucle_office`, hors verrou.
    Sans thread demarre (tests, outil en ligne de commande), envoi direct."""
    if _office_thread_actif["oui"]:
        _office_reveil.set()
    elif conn is not None:
        vider_file_office(conn)


def boucle_office():
    """Thread d'envoi vers Serveur_OT : se reveille a chaque demande, et au
    plus tard toutes les 30 s pour rejouer les echecs. Connexion SQLite
    propre (WAL : lectures/ecritures concurrentes sures). Le verrou global
    n'est JAMAIS pris : seules de courtes ecritures SQLite sont faites."""
    _office_thread_actif["oui"] = True
    while True:
        _office_reveil.wait(timeout=OFFICE_REESSAI_S)
        _office_reveil.clear()
        try:
            conn = get_conn()
            try:
                vider_file_office(conn)
                rattraper_blasts_ot(conn)
                try:
                    pousser_geo_dt(conn)              # 24/11 : paquet geo des DT (si change)
                except Exception as e:
                    print("OFFICE: paquet geo DT : %s" % e)
            finally:
                conn.close()
        except Exception as e:
            print("OFFICE: boucle d'envoi en erreur: %s" % e)


# Rattrapage AUTOMATIQUE des blasts non recopies dans Serveur_OT (18/11).
# Constat : 4 des 5 RL du blast 119 etaient restes en `ot_sync=0` depuis un
# refus du serveur (« action POST inconnue : blast_upload », serveur deploye
# sans le pont) ; rien ne les renvoyait, sauf un POST manuel /api/ot_blasts.
# dig_plan restait donc incomplet et ces RL n'avaient pas de carte au Drive.
# Chaque envoi transporte l'image : cadence bornee a un passage / 5 min.
BLASTS_RATTRAPAGE_S = 300
_blasts_rattrapage = {"dernier": 0.0}


def rattraper_blasts_ot(conn):
    maintenant = time.time()
    if maintenant - _blasts_rattrapage["dernier"] < BLASTS_RATTRAPAGE_S:
        return
    _blasts_rattrapage["dernier"] = maintenant
    office_url, office_token = office_conf()
    if not office_url or not office_token:
        return
    # Un blast CLOS n'est plus repousse (18/11) : seule sa cloture l'est.
    cibles = conn.execute(
        "SELECT id, numero FROM blasts WHERE actif=1 "
        "AND (clos_le IS NULL OR clos_le='') "
        "AND (ot_sync IS NULL OR ot_sync=0) ORDER BY id").fetchall()
    for c in cibles:
        ok, err = ot_pousser_blast(conn, c["id"])
        print("OT: rattrapage blast %s -> %s" % (c["numero"], "ok" if ok else err))
    # Clotures non encore enregistrees par Serveur_OT (y compris celles des
    # blasts supprimes ensuite : actif=0).
    for c in conn.execute(
            "SELECT id, numero FROM blasts WHERE clos_le<>'' "
            "AND (clos_ot IS NULL OR clos_ot=0) ORDER BY id").fetchall():
        # 26/11 : un blast SUPPRIME dont un homonyme actif existe (reimporte) : sa cloture clorait le blast vivant
        if conn.execute("SELECT 1 FROM blasts WHERE numero=? AND actif=1 AND id<>? AND (clos_le IS NULL OR clos_le='')",
                        (c["numero"], c["id"])).fetchone():
            conn.execute("UPDATE blasts SET clos_ot=1, ot_erreur=? WHERE id=?",
                         ("cloture non envoyee : blast actif du meme nom (reimporte)", c["id"]))
            conn.commit()
            print("OT: cloture de %s non renvoyee (un blast actif porte ce nom)" % c["numero"])
            continue
        ok, err = ot_clore_blast(conn, c["id"])
        print("OT: rattrapage cloture %s -> %s" % (c["numero"], "ok" if ok else err))


# ------------------------------------------------------- paquet geo des DT (24/11) ---
# Decision utilisateur du 24/11 : l'AFFECTATION et le CONTROLE DES CHECKPOINTS se font sur la tablette du DT.
# La passerelle prepare ici le « paquet geo » (tout ce dont la tablette a besoin, rien d'autre) et le depose
# sur Serveur_OT (POST ?action=geo_dt_ecrire), ou la tablette le lit (GET ?action=geo_dt, jeton tablette) :
#   - blasts actifs NON CLOS : tous les polygones du DXF (minerai et sterile), drapeau minerai (Geosum),
#     destination Geosum, source (pit), altitude du flitch (flitches superposes : le plus haut l'emporte) ;
#   - consignes teneur -> dumping area (par source) ; dumping areas (polygone, type rompad / waste) ;
#   - checkpoints actifs + interdictions par matiere (interdit_waste / interdit_ore, Senor > Trails) ;
#   - trajectoires source -> dumping area (checkpoints OBLIGATOIRES dans l'ordre, minerai : phase 2) ;
#   - reglages : tolerance d'affectation (Parametres > Liaisons), distance de depart du controle (Senor).
# `version` = empreinte du contenu : la tablette ne retelecharge que si elle change.
# `anomalies` = controles a la construction (un blast ne doit jamais etre oublie) : visibles par le bureau.
GEO_DT_FORMAT = 1
GEO_DT_INTERVALLE_S = 60
_geo_dt_etat = {"version_ot": "", "envoye_le": "", "erreur": "", "dernier": 0.0}


def reglage_precision_geo():
    """24/11 (essai terrain) : precision exigee des positions de la tablette et distance de franchissement.
    Le GPS du telephone (+-5 a 15 m) et une detection au rayon faisaient partir les alertes dans tous les sens.
    - GEO_PRECISION_MODE : « gnss » (defaut : module GNSS USB, avec ou sans RTK : plus fiable que le GPS du telephone,
      meme sans base), « rtk » (RTK seulement), « differentiel », « tous » (GPS du telephone aussi, essais) ;
    - GEO_FRANCHISSEMENT_M (5) : distance perpendiculaire a la ligne d'un checkpoint (ou au bord d'une zone) pour
      confirmer le cote ; un franchissement = un changement de cote confirme, dans le sens du deplacement ;
    - GEO_VITESSE_MIN_KMH (3) : au-dessous, le cap GNSS n'est pas pris en compte."""
    mode = str(param_lire("GEO_PRECISION_MODE", "gnss") or "gnss").strip().lower()
    if mode not in ("gnss", "rtk", "differentiel", "tous"):
        mode = "gnss"

    def nombre(cle, defaut, bas, haut):
        try:
            v = float(str(param_lire(cle, "") or defaut).replace(",", "."))
        except ValueError:
            return defaut
        return v if bas <= v <= haut else defaut
    return {"mode": mode, "distance_m": nombre("GEO_FRANCHISSEMENT_M", 5.0, 0.5, 50),
            "vitesse_min_kmh": nombre("GEO_VITESSE_MIN_KMH", 3.0, 0, 30)}


def reglage_z_flitch():
    """24/11 : Z de la pelle dans le choix du flitch (tablette ; meme regle chez Serveur_SG).
    Le flitch le plus haut reste la regle ; le Z ecarte seulement un flitch AU-DESSUS de la pelle (deja mine,
    pas clos) : niveau = mediane robuste des Z du cycle - hauteur d'antenne ; flitch ecarte si niveau < cote - marge.
    Parametres (table params, Parametres > Liaisons) : Z_FLITCH_ACTIF (1), Z_HAUTEUR_ANTENNE_M (0 : prudent, le niveau
    est alors surestime et rien n'est ecarte a tort), Z_MARGE_M (1.5)."""
    def nombre(cle, defaut, bas, haut):
        try:
            v = float(str(param_lire(cle, "") or defaut).replace(",", "."))
        except ValueError:
            return defaut
        return v if bas <= v <= haut else defaut
    actif = str(param_lire("Z_FLITCH_ACTIF", "1")).strip().lower() not in ("0", "non", "false", "off")
    return {"actif": 1 if actif else 0, "hauteur_antenne_m": nombre("Z_HAUTEUR_ANTENNE_M", 0.0, 0, 20),
            "marge_m": nombre("Z_MARGE_M", 1.5, 0, 20)}


def _geo_flitch(numero):
    m = re.match(r"^\s*(\d+(?:[.,]\d+)?)", str(numero or ""))
    return float(m.group(1).replace(",", ".")) if m else None


def _geo_poly(brut):
    """JSON de sommets -> [[x, y], ...] propre (sans doublon consecutif ni fermeture), ou None si inexploitable."""
    try:
        pts = json.loads(brut or "[]")
    except (TypeError, ValueError):
        return None
    res = []
    for p in pts if isinstance(pts, list) else []:
        if not isinstance(p, (list, tuple)) or len(p) < 2:
            return None
        try:
            x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError):
            return None
        if not (math.isfinite(x) and math.isfinite(y)):
            return None
        q = [round(x, 3), round(y, 3)]
        if not res or res[-1] != q:
            res.append(q)
    if len(res) >= 2 and res[0] == res[-1]:
        res.pop()
    return res if len(res) >= 3 else None


def _geo_orient(a, b, c):
    v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)


def _geo_coupe(a, b, c, d):
    o = (_geo_orient(a, b, c), _geo_orient(a, b, d), _geo_orient(c, d, a), _geo_orient(c, d, b))
    return 0 not in o and o[0] != o[1] and o[2] != o[3]


def _geo_auto_intersecte(poly):
    n = len(poly)
    for i in range(n):
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            if _geo_coupe(poly[i], poly[(i + 1) % n], poly[j], poly[(j + 1) % n]):
                return True
    return False


def _geo_recouvre(p, q):
    """Deux polygones se recouvrent-ils (surface commune) ? Boites d'abord, puis sommet dedans ou aretes croisees."""
    if (max(a[0] for a in p) <= min(a[0] for a in q) or max(a[0] for a in q) <= min(a[0] for a in p) or
            max(a[1] for a in p) <= min(a[1] for a in q) or max(a[1] for a in q) <= min(a[1] for a in p)):
        return False
    if any(point_in_polygon(a[0], a[1], q) for a in p) or any(point_in_polygon(a[0], a[1], p) for a in q):
        return True
    return any(_geo_coupe(p[i], p[(i + 1) % len(p)], q[j], q[(j + 1) % len(q)])
               for i in range(len(p)) for j in range(len(q)))


def geo_dt_construire(conn):
    """Paquet geo des DT (dict) + anomalies de construction."""
    anomalies = []
    sources = {r["id"]: r["nom"] for r in conn.execute("SELECT id, nom FROM sources WHERE actif=1")}
    src_par_nom = {str(v).strip().lower(): v for v in sources.values()}
    blasts = []
    for b in conn.execute("SELECT * FROM blasts WHERE actif=1 ORDER BY numero, id"):
        if str(b["clos_le"] or "").strip():
            continue                                    # blast termine : plus aucun chargement possible
        brut_src = str(b["source_nom"] or "").strip()
        src = src_par_nom.get(brut_src.lower(), brut_src)
        if not src:
            anomalies.append({"code": "blast_sans_source", "blast": b["numero"]})
        blocs = []
        for o in conn.execute("SELECT nom, sommets, minerai, destination FROM oreblocs "
                              "WHERE blast_id=? AND actif=1 ORDER BY nom", (b["id"],)):
            poly = _geo_poly(o["sommets"])
            if not poly:
                anomalies.append({"code": "bloc_polygone_invalide", "blast": b["numero"], "bloc": o["nom"]})
                continue
            if _geo_auto_intersecte(poly):
                anomalies.append({"code": "bloc_auto_intersecte", "blast": b["numero"], "bloc": o["nom"]})
            minerai = 1 if o["minerai"] == 1 else 0
            dest = str(o["destination"] or "").strip()
            if minerai and not dest:
                anomalies.append({"code": "bloc_minerai_sans_destination", "blast": b["numero"], "bloc": o["nom"]})
            blocs.append({"nom": o["nom"], "minerai": minerai, "destination": dest, "poly": poly})
        if not blocs:
            anomalies.append({"code": "blast_sans_polygone", "blast": b["numero"]})
        elif not any(x["minerai"] for x in blocs):
            anomalies.append({"code": "blast_sans_minerai", "blast": b["numero"],
                              "detail": "Geosum absent ou non applique : tout le blast serait du waste"})
        blasts.append({"numero": b["numero"], "flitch": flitch_blast(b["numero"], b["flitch"] if "flitch" in b.keys() else None), "source": src, "blocs": blocs})
    # Blasts actifs qui se recouvrent (flitches superposes ou blast en double) : la tablette retient le flitch
    # le plus haut ; le bureau doit clore les flitches termines.
    for i in range(len(blasts)):
        for j in range(i + 1, len(blasts)):
            a, b = blasts[i], blasts[j]
            if any(_geo_recouvre(p["poly"], q["poly"]) for p in a["blocs"] for q in b["blocs"]):
                anomalies.append({"code": "blasts_superposes", "blast": a["numero"], "avec": b["numero"],
                                  "detail": "le flitch le plus haut est retenu ; clore le flitch termine"})
    zones = []
    for d in conn.execute("SELECT nom, sommets FROM dumping_areas WHERE actif=1 ORDER BY nom"):
        poly = _geo_poly(d["sommets"])
        if not poly:
            anomalies.append({"code": "zone_sans_polygone", "zone": d["nom"]})
        zones.append({"nom": d["nom"], "type": type_zone(d["nom"]), "poly": poly or []})
    if not any(z["type"] == "waste" for z in zones):
        anomalies.append({"code": "aucune_zone_waste"})
    consignes = []
    for t in conn.execute("SELECT t.nom AS teneur, t.source_id, d.nom AS zone FROM teneurs t "
                          "LEFT JOIN dumping_areas d ON d.id = t.dumping_area_id AND d.actif=1 "
                          "WHERE t.actif=1 ORDER BY t.source_id, t.nom"):
        src = sources.get(t["source_id"])
        if not src:
            continue
        if not t["zone"]:
            anomalies.append({"code": "teneur_sans_zone", "source": src, "teneur": t["teneur"]})
            continue
        consignes.append({"source": src, "teneur": str(t["teneur"]).strip(), "zone": t["zone"]})
    exactes = {(c["source"].lower(), c["teneur"].upper()) for c in consignes}
    par_teneur = {}
    for c in consignes:
        par_teneur.setdefault(c["teneur"].upper(), set()).add(c["zone"])
    for bl in blasts:
        for o in bl["blocs"]:
            t = o["destination"].upper()
            if not o["minerai"] or not t or (str(bl["source"]).lower(), t) in exactes:
                continue
            code = "consigne_autre_source" if len(par_teneur.get(t, ())) == 1 else "destination_sans_consigne"
            anomalies.append({"code": code, "blast": bl["numero"], "bloc": o["nom"], "destination": o["destination"]})
    checkpoints = []
    for c in conn.execute("SELECT nom, x1, y1, x2, y2, COALESCE(interdit_waste, 1) AS iw, "
                          "COALESCE(interdit_ore, 0) AS io FROM checkpoints WHERE actif=1 ORDER BY id"):
        try:
            a = [round(float(c["x1"]), 3), round(float(c["y1"]), 3)]
            b = [round(float(c["x2"]), 3), round(float(c["y2"]), 3)]
        except (TypeError, ValueError):
            anomalies.append({"code": "checkpoint_invalide", "checkpoint": c["nom"]})
            continue
        if a == b:
            anomalies.append({"code": "checkpoint_invalide", "checkpoint": c["nom"]})
            continue
        checkpoints.append({"nom": c["nom"], "a": a, "b": b,
                            "interdit_waste": 1 if c["iw"] else 0, "interdit_ore": 1 if c["io"] else 0})
    # Phase 2 : trajectoires (Senor > Trails > Trajectoires), checkpoints par NOM dans l'ordre.
    noms_cp = {c["nom"] for c in checkpoints}
    trajectoires = []
    for t in conn.execute("SELECT s.nom AS source, d.nom AS zone, t.checkpoints FROM trajectoires t "
                          "JOIN sources s ON s.id = t.source_id AND s.actif=1 "
                          "JOIN dumping_areas d ON d.id = t.dumping_area_id AND d.actif=1 ORDER BY s.nom, d.nom"):
        try:
            ids = [int(i) for i in json.loads(t["checkpoints"] or "[]")]
        except (TypeError, ValueError):
            ids = []
        noms = []
        for i in ids:
            c = conn.execute("SELECT nom FROM checkpoints WHERE id=? AND actif=1", (i,)).fetchone()
            if c and c["nom"] in noms_cp:
                noms.append(c["nom"])
        if not noms:
            anomalies.append({"code": "trajectoire_vide", "source": t["source"], "zone": t["zone"],
                              "detail": "aucun checkpoint : le passage n'est pas controle"})
            continue
        trajectoires.append({"source": t["source"], "zone": t["zone"], "checkpoints": noms})
    # Flux de minerai sans trajectoire : (source du blast, zone de la consigne) qu'aucune trajectoire ne couvre.
    couvertes = {(x["source"].lower(), x["zone"].lower()) for x in trajectoires}
    zone_de = {(c["source"].lower(), c["teneur"].upper()): c["zone"] for c in consignes}
    flux = set()
    for bl in blasts:
        for o in bl["blocs"]:
            z = zone_de.get((str(bl["source"]).lower(), o["destination"].upper())) if o["minerai"] else None
            if z and (str(bl["source"]).lower(), z.lower()) not in couvertes:
                flux.add((bl["source"], z))
    for s_, z in sorted(flux):
        anomalies.append({"code": "trajectoire_manquante", "source": s_, "zone": z,
                          "detail": "minerai de ce pit vers cette zone : checkpoints de passage non controles"})
    corps = {"v": GEO_DT_FORMAT, "tolerance_m": marge_affectation_m(), "depart_m": reglages_zones()["depart_m"],
             "z": reglage_z_flitch(),
             "precision": reglage_precision_geo(),
             "blasts": blasts, "zones": zones, "consignes": consignes, "checkpoints": checkpoints,
             "trajectoires": trajectoires}
    corps["version"] = hashlib.sha256(
        json.dumps(corps, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:12]
    corps["genere_le"] = now_iso()
    corps["anomalies"] = anomalies
    return corps


def pousser_geo_dt(conn, force=False):
    """Depose le paquet geo sur Serveur_OT quand il a change (au plus une fois par minute). Rend (ok, erreur)."""
    maintenant = time.time()
    if not force and maintenant - _geo_dt_etat["dernier"] < GEO_DT_INTERVALLE_S:
        return True, ""
    _geo_dt_etat["dernier"] = maintenant
    office_url, office_token = office_conf()
    if not office_url or not office_token:
        return False, "liaison Serveur_OT non configuree"
    paquet = geo_dt_construire(conn)
    deja = param_lire_base("GEO_DT_VERSION_OT", "")
    if not force and paquet["version"] == deja:
        _geo_dt_etat["version_ot"] = deja
        return True, ""
    ok, rep, err = ot_appel("geo_dt_ecrire", paquet)
    if ok:
        param_ecrire(conn, "GEO_DT_VERSION_OT", paquet["version"])
        _geo_dt_etat.update(version_ot=paquet["version"], envoye_le=now_iso(), erreur="")
        print("OT: paquet geo DT %s depose (%d blasts, %d checkpoints, %d anomalie(s))" % (
            paquet["version"], len(paquet["blasts"]), len(paquet["checkpoints"]), len(paquet["anomalies"])))
        return True, ""
    _geo_dt_etat["erreur"] = err or "refus de Serveur_OT (action geo_dt_ecrire deployee ?)"
    print("OT: paquet geo DT non depose : %s" % _geo_dt_etat["erreur"])
    return False, _geo_dt_etat["erreur"]


# ------------------------------------------- evenements decides par la tablette (24/11, phase 3) ---
# Une tablette qui a le paquet geo publie la version dans ses positions MQTT (champ `geo`) et ses decisions sur
# camions/<code>/evenement : checkpoint_interdit, checkpoint_saute, checkpoint_valide. Le chauffeur a deja eu le
# bandeau et la voix sur la tablette : ici, l'alerte est seulement enregistree pour le BUREAU (Live, Alerts,
# Serveur_OT), sans notification au chauffeur. Tant que la tablette est vue avec un paquet geo (GEO_TABLETTE_S),
# la passerelle ne refait pas son propre controle des checkpoints pour ce DT.
GEO_TABLETTE_S = 120
_geo_tablettes = {}            # code DT -> {"version", "t"}
_evt_tablette_vus = {}         # cle d'evenement -> epoch (QoS 1 : un message peut arriver deux fois)
_evt_tablette_etat = {"recus": 0, "dernier": "", "validations": []}


def geo_tablette_active(code):
    g = _geo_tablettes.get(str(code))
    return bool(g) and time.time() - g["t"] <= GEO_TABLETTE_S


def clore_mission_tablette(code, d):
    """25/11 : la tablette a efface son cycle (reinitialisation, cycle perime) : la mission en cours de ce cycle est
    close ici tout de suite (statut « abandonnee », pas un dumping fait). Serveur_SG recoit aussi la fin par la file
    de la tablette ; sa synchronisation n'aura plus rien a fermer."""
    cle = str(d.get("cle") or "").strip()
    conn = get_conn()
    try:
        LOCK.route("mqtt mission_close %s" % code)
        with LOCK:
            if cle:
                lignes = conn.execute("SELECT id FROM missions WHERE dt_code=? AND statut='en_cours' AND cle_sg=?",
                                      (code, cle)).fetchall()
            else:
                lignes = conn.execute("SELECT id FROM missions WHERE dt_code=? AND statut='en_cours'", (code,)).fetchall()
            for r in lignes:
                conn.execute("UPDATE missions SET statut='abandonnee', fin=? WHERE id=?", (now_iso(), r["id"]))
            fin_surveillance_dt(conn, code)
            conn.commit()
        print("MISSIONS: %s a efface son cycle (%s) : %d mission(s) close(s)" % (code, d.get("motif") or "?", len(lignes)))
    finally:
        conn.close()


def traiter_evenement_tablette(code, d):
    typ = str(d.get("type") or "").strip()
    if typ == "mission_close":                      # 25/11 : cycle efface sur la tablette
        clore_mission_tablette(code, d)
        return
    if typ not in ("checkpoint_interdit", "checkpoint_saute", "checkpoint_valide",
                   "mauvaise_direction", "demi_tour", "retour_direction"):     # 24/11 : alerte unique (bureau)
        return
    if d.get("geo_version"):
        _geo_tablettes[code] = {"version": str(d["geo_version"]), "t": time.time()}
    cle_evt = "%s|%s|%s|%s" % (code, d.get("cle") or "", typ, d.get("checkpoint") or "")
    if typ in ("mauvaise_direction", "demi_tour", "retour_direction"):
        cle_evt += "|%s" % (d.get("at") or "")      # l'alarme peut reprendre : chaque fait compte (QoS 1 : meme « at »)
    maintenant = time.time()
    for k in [k for k, t in _evt_tablette_vus.items() if maintenant - t > 6 * 3600]:
        del _evt_tablette_vus[k]
    if cle_evt in _evt_tablette_vus:
        return
    _evt_tablette_vus[cle_evt] = maintenant
    _evt_tablette_etat["recus"] += 1
    _evt_tablette_etat["dernier"] = "%s %s %s" % (code, typ, d.get("checkpoint") or "")
    if typ == "checkpoint_valide":
        _evt_tablette_etat["validations"] = (_evt_tablette_etat["validations"] + [{
            "dt_code": code, "checkpoint": d.get("checkpoint"), "rang": d.get("rang"), "total": d.get("total"),
            "le": now_iso()}])[-50:]
        return
    type_alerte = "checkpoint" if typ == "checkpoint_saute" else typ
    message = texte_ascii(str(d.get("message") or "")) or "%s : %s (%s)" % (type_alerte, d.get("checkpoint") or "?", code)
    conn = get_conn()
    try:
        LOCK.route("mqtt evenement %s" % code)
        with LOCK:
            conn.execute("INSERT INTO alertes (type, dt_code, message, voix_off, dest_office) VALUES (?, ?, ?, 0, 1)",
                         (type_alerte, code, message))
            evenement = {"type": type_alerte, "dt_code": code, "x": d.get("x"), "y": d.get("y"),
                         "checkpoints": [d.get("checkpoint")], "message": message, "origine": "tablette",
                         "cle": d.get("cle"), "geo_version": d.get("geo_version"),
                         "cause": d.get("cause"), "zone": d.get("zone")}
            enregistrer_pour_office(conn, {"alerte": evenement, "action": type_alerte})
            conn.commit()
    finally:
        conn.close()
    if _office_thread_actif["oui"]:
        _office_reveil.set()


# ------------------------------------------------ enregistrement du tracking ---
# 20/11 : le suivi est ENREGISTRE sur le poste de la passerelle, pour etre revu :
#   - CSV : un fichier par jour (data/tracking/AAAA-MM-JJ.csv), DT ET excavatrices,
#     une ligne par position retenue (le DT bouge de plus d'1 m, ou au moins toutes les 30 s) ;
#     conserve TRACKING_JOURS jours (90 par defaut). Export : /api/tracking/export.csv.
#   - VIDEO : un MP4 genere a partir du CSV (plan du site : oreblocs, dumping areas, pistes ;
#     les engins se deplacent, trace, heure), a la demande (page Live > Enregistrements) et
#     automatiquement a la fin de chaque shift (VIDEO_AUTO, 1 par defaut), dans
#     data/enregistrements/, conserve VIDEOS_JOURS jours (30). La video est generee dans un
#     PROCESSUS A PART, a priorite basse : elle ne ralentit jamais les alertes.
TRACKING_DIR = os.path.join(DATA_DIR, "tracking")
ENREGISTREMENTS_DIR = os.path.join(DATA_DIR, "enregistrements")
TRACKING_ENTETE = ["horodatage", "code", "type", "x_utm", "y_utm", "lat", "lng", "statut"]
_tracking_verrou = threading.Lock()
_tracking_repere = {"zone": None, "hemi": None, "at": 0.0}
_tracking_purge = {"jour": ""}
_jobs_video = {}                          # nom de fichier -> processus / thread


def videos_jours():
    """26/11 : duree de conservation des videos (Parametres > Passerelle), en jours ; 0 = jamais supprimees."""
    try:
        return max(0, min(int(float(param_lire("VIDEOS_JOURS", "30"))), 3650))
    except (TypeError, ValueError):
        return 30


def purger_videos():
    """Supprime les videos plus anciennes que videos_jours() (jamais l'enregistrement en cours). Rend leur nombre."""
    jours = videos_jours()
    if jours <= 0 or not os.path.isdir(ENREGISTREMENTS_DIR):
        return 0
    limite = datetime.now() - timedelta(days=jours)
    n = 0
    for f in os.listdir(ENREGISTREMENTS_DIR):
        chemin = os.path.join(ENREGISTREMENTS_DIR, f)
        if not f.endswith((".mp4", ".webm")) or os.path.abspath(chemin) == os.path.abspath(RECORD_PARTIE):
            continue
        try:
            if datetime.fromtimestamp(os.path.getmtime(chemin)) < limite:
                os.remove(chemin)
                n += 1
        except OSError:
            pass
    if n:
        print("VIDEO: %d video(s) de plus de %d jour(s) supprimee(s)" % (n, jours))
    return n


def _entier_param(cle, defaut, mini=1, maxi=3650):
    try:
        return max(mini, min(int(float(param_lire(cle, str(defaut)))), maxi))
    except (TypeError, ValueError):
        return defaut


def _repere_tracking(conn):
    if time.time() - _tracking_repere["at"] > 600:
        try:
            z, h = repere_site(conn)
        except Exception:
            z, h = None, None
        _tracking_repere.update(zone=z or 30, hemi=h or "N", at=time.time())
    return _tracking_repere["zone"], _tracking_repere["hemi"]


def tracking_purger(jour):
    """Une fois par jour : supprime les CSV plus vieux que TRACKING_JOURS jours."""
    if _tracking_purge["jour"] == jour:
        return
    _tracking_purge["jour"] = jour
    limite = (datetime.now() - timedelta(days=_entier_param("TRACKING_JOURS", 90))).strftime("%Y-%m-%d")
    try:
        for f in os.listdir(TRACKING_DIR):
            if re.match(r"^\d{4}-\d{2}-\d{2}\.csv$", f) and f[:10] < limite:
                os.remove(os.path.join(TRACKING_DIR, f))
    except OSError as e:
        print("TRACKING: purge impossible (%s)" % e)


def tracking_enregistrer(conn, existe, code, typ, x, y, instant, retard=False):
    """Ajoute une position au CSV du jour. Ne leve jamais : une position ne se perd pas pour ca.
    `existe` : ligne `engins` AVANT mise a jour (filtre anti-doublon) ; `retard` : point rejoue
    (historique), pas de filtre."""
    try:
        if existe is not None and not retard:
            if distance(x, y, existe["x"], existe["y"]) <= 1.0 and abs(_secondes_depuis(existe["derniere_maj"], instant)) < 30:
                return
        zone, hemi = _repere_tracking(conn)
        ll = _vers_latlng(x, y, zone, hemi) or ("", "")
        jour = str(instant)[:10]
        statut = (existe["statut"] if existe is not None else "") or ""
        with _tracking_verrou:
            os.makedirs(TRACKING_DIR, exist_ok=True)
            fichier = os.path.join(TRACKING_DIR, jour + ".csv")
            neuf = not os.path.exists(fichier)
            with open(fichier, "a", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                if neuf:
                    w.writerow(TRACKING_ENTETE)
                w.writerow([str(instant)[:19], code, str(typ or "DT").upper(), round(float(x), 2), round(float(y), 2),
                            ll[0], ll[1], statut])
            if neuf:
                tracking_purger(jour)
    except Exception as e:
        print("TRACKING: enregistrement impossible (%s)" % e)


def tracking_lire(depuis, jusqua, codes=None):
    """Lignes du CSV dans [depuis, jusqua] (AAAA-MM-JJ HH:MM:SS), triees par heure."""
    lignes = []
    d0, d1 = depuis[:10], (jusqua or "9999-12-31")[:10]
    try:
        fichiers = sorted(f for f in os.listdir(TRACKING_DIR) if re.match(r"^\d{4}-\d{2}-\d{2}\.csv$", f) and d0 <= f[:10] <= d1)
    except OSError:
        return lignes
    for f in fichiers:
        with open(os.path.join(TRACKING_DIR, f), encoding="utf-8", newline="") as fh:
            for r in csv.DictReader(fh):
                h = r.get("horodatage") or ""
                if h < depuis or (jusqua and h > jusqua):
                    continue
                if codes and r.get("code") not in codes:
                    continue
                lignes.append(r)
    lignes.sort(key=lambda r: r["horodatage"])
    return lignes


def tracking_csv(depuis, jusqua, codes=None):
    sortie = io.StringIO()
    w = csv.writer(sortie, lineterminator="\n")
    w.writerow(TRACKING_ENTETE)
    for r in tracking_lire(depuis, jusqua, codes):
        w.writerow([r.get(c, "") for c in TRACKING_ENTETE])
    return sortie.getvalue()


# ---------- video
def _fond_site(conn):
    """Oreblocs, dumping areas et pistes (UTM) pour le fond du plan."""
    blocs, das, pistes = [], [], []
    for r in conn.execute("SELECT sommets FROM oreblocs WHERE actif=1"):
        try:
            blocs.append(json.loads(r["sommets"] or "[]"))
        except ValueError:
            pass
    for r in conn.execute("SELECT nom, sommets FROM dumping_areas WHERE actif=1"):
        try:
            das.append((r["nom"], json.loads(r["sommets"] or "[]")))
        except ValueError:
            pass
    try:
        a = offroad_aretes(conn)
        pistes = [] if pistes_en_degres(a) else a
    except Exception:
        pistes = []
    return blocs, das, pistes


# ---- fond de la video : carte satellite + zones + pistes (comme la carte du dashboard) ----
FOND_VIDEO_FICHIER = os.path.join(DATA_DIR, "fond_video.json")
TUILES_DIR = os.path.join(DATA_DIR, "tuiles")
TUILES_SOURCES = {
    "esri-sat": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    "esri-topo": "https://server.arcgisonline.com/ArcGIS/rest/services/World_Topo_Map/MapServer/tile/{z}/{y}/{x}",
    "osm": "https://{s}.tile.openstreetmap.fr/hot/{z}/{x}/{y}.png",
}


def fond_video_lire():
    """Dernier decor transmis par la page Live (zones, pistes colorees, fond de carte) ; {} sinon."""
    try:
        with open(FOND_VIDEO_FICHIER, encoding="utf-8") as fh:
            return json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def fond_video_ecrire(brut):
    """Valide (tailles bornees, nombres finis) puis enregistre le decor de la carte pour la video."""
    def nombre(v):
        v = float(v)
        if not math.isfinite(v):
            raise ValueError("nombre invalide")
        return v
    zones, pistes = [], []
    for z in (brut.get("zones") or [])[:300]:
        try:
            zones.append({"name": str(z.get("name") or "")[:60], "lat": nombre(z["lat"]), "lng": nombre(z["lng"])})
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    for t in (brut.get("pistes") or [])[:400]:
        try:
            wp = [[nombre(p[0]), nombre(p[1])] for p in (t.get("waypoints") or [])[:3000]]
        except (TypeError, ValueError, IndexError, AttributeError):
            continue
        piste = {"waypoints": wp if len(wp) >= 2 else [], "from": str(t.get("from") or "")[:60],
                 "to": str(t.get("to") or "")[:60], "useColor": t.get("useColor") is not False}
        c = str(t.get("color") or "")
        piste["color"] = c if re.match(r"^#[0-9A-Fa-f]{6}$", c) else "#EE7A2A"
        pistes.append(piste)
    fond = str(brut.get("basemap") or "esri-sat")
    d = {"zones": zones, "pistes": pistes, "basemap": fond if fond in TUILES_SOURCES else "esri-sat"}
    if not zones and not pistes:
        return False
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(FOND_VIDEO_FICHIER, "w", encoding="utf-8") as fh:
        json.dump(d, fh)
    return True


def _hex_bgr(h):
    h = str(h).lstrip("#")
    return (int(h[4:6], 16), int(h[2:4], 16), int(h[0:2], 16))


def _tuile_image(cv2, np, source, z, tx, ty):
    """Tuile 256x256 (BGR) : cache disque data/tuiles, sinon telechargement (timeout 10 s). None si indisponible."""
    chemin = os.path.join(TUILES_DIR, source, str(z), "%d_%d.img" % (tx, ty))
    donnees = None
    try:
        if os.path.isfile(chemin):
            with open(chemin, "rb") as fh:
                donnees = fh.read()
        else:
            url = TUILES_SOURCES[source].replace("{z}", str(z)).replace("{x}", str(tx)).replace("{y}", str(ty)).replace("{s}", "a")
            req = urllib.request.Request(url, headers={"User-Agent": "OreTracking-video/1.0"})
            with urllib.request.urlopen(req, timeout=10) as rep:
                donnees = rep.read()
            os.makedirs(os.path.dirname(chemin), exist_ok=True)
            with open(chemin, "wb") as fh:
                fh.write(donnees)
    except Exception:
        return None
    img = cv2.imdecode(np.frombuffer(donnees, np.uint8), cv2.IMREAD_COLOR)
    return img if img is not None and img.shape[0] == 256 and img.shape[1] == 256 else None


def _fond_carte(cv2, np, source, coins_latlon, trois_latlon, largeur, hauteur, echelle):
    """Fond satellite (tuiles Web Mercator) recale sur le cadre UTM de la video. Rend (image, nb_tuiles_manquantes)
    ou (None, ...) si rien n'a pu etre charge. `trois_latlon` = (lat, lon) des coins haut-gauche, haut-droit, bas-gauche."""
    def tuile_xy(lat, lon, z):
        n = 2.0 ** z
        return (lon + 180.0) / 360.0 * n, (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    lat0 = sum(c[0] for c in coins_latlon) / len(coins_latlon)
    mpp = 1.0 / echelle
    z = int(math.ceil(math.log(156543.03392 * math.cos(math.radians(lat0)) / mpp, 2)))
    z = max(10, min(19, z))
    while True:
        pts = [tuile_xy(la, lo, z) for la, lo in coins_latlon]
        x_min, x_max = int(min(p[0] for p in pts)), int(max(p[0] for p in pts))
        y_min, y_max = int(min(p[1] for p in pts)), int(max(p[1] for p in pts))
        if (x_max - x_min + 1) * (y_max - y_min + 1) <= 160 or z <= 10:
            break
        z -= 1
    liste = [(x, y) for x in range(x_min, x_max + 1) for y in range(y_min, y_max + 1)]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as pool:
        images = list(pool.map(lambda xy: _tuile_image(cv2, np, source, z, xy[0], xy[1]), liste))
    manquantes = sum(1 for i in images if i is None)
    if manquantes == len(images):
        return None, manquantes
    mosaique = np.full(((y_max - y_min + 1) * 256, (x_max - x_min + 1) * 256, 3), (38, 40, 36), np.uint8)
    for (x, y), im in zip(liste, images):
        if im is not None:
            mosaique[(y - y_min) * 256:(y - y_min + 1) * 256, (x - x_min) * 256:(x - x_min + 1) * 256] = im
    src = np.float32([[(tuile_xy(la, lo, z)[0] - x_min) * 256.0, (tuile_xy(la, lo, z)[1] - y_min) * 256.0]
                      for la, lo in trois_latlon])
    dst = np.float32([[0, 0], [largeur, 0], [0, hauteur]])
    M = cv2.getAffineTransform(src, dst)
    return cv2.warpAffine(mosaique, M, (largeur, hauteur), flags=cv2.INTER_LINEAR), manquantes


def _pin(cv2, np, img, c, couleur=(176, 104, 22)):
    """Epingle de zone (goutte bleue, point blanc) dont la pointe est en c."""
    x, y = c
    cv2.circle(img, (x, y - 17), 11, couleur, -1, cv2.LINE_AA)
    cv2.fillPoly(img, [np.array([(x - 8, y - 11), (x + 8, y - 11), (x, y)], np.int32)], couleur, cv2.LINE_AA)
    cv2.circle(img, (x, y - 17), 4, (255, 255, 255), -1, cv2.LINE_AA)


def _etiquette(cv2, img, texte, x, y, ancrage="gauche"):
    """Etiquette sombre (texte clair) : x, y = coin haut-gauche (ou haut-centre si ancrage centre)."""
    (w, h), base = cv2.getTextSize(texte, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
    x0 = x - (w + 14) // 2 if ancrage == "centre" else x
    cv2.rectangle(img, (x0, y), (x0 + w + 14, y + h + 10), (30, 34, 28), -1)
    cv2.putText(img, texte, (x0 + 7, y + h + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (240, 240, 232), 1, cv2.LINE_AA)


def generer_video(depuis, jusqua, sortie, vitesse=120, fps=10, largeur=1280, hauteur=720):
    """Video WebM du tracking entre `depuis` et `jusqua` : la carte satellite du site (zones, pistes colorees, comme
    le dashboard), les engins en mouvement avec leurs traces, la date / heure. `vitesse` : secondes reelles par seconde
    de video (120 = 2 h de terrain en 1 min). Retourne (ok, message)."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return False, "OpenCV et NumPy sont requis pour la video (pip install opencv-python numpy)"
    lignes = tracking_lire(depuis, jusqua)
    if len(lignes) < 2:
        return False, "aucune position enregistree entre %s et %s" % (depuis, jusqua)
    t_deb = datetime.strptime(depuis, "%Y-%m-%d %H:%M:%S").timestamp()
    t_fin = datetime.strptime(jusqua, "%Y-%m-%d %H:%M:%S").timestamp()
    series = {}
    for r in lignes:
        try:
            t = datetime.strptime(r["horodatage"][:19], "%Y-%m-%d %H:%M:%S").timestamp()
            s_ = series.setdefault(r["code"], {"type": r["type"], "t": [], "x": [], "y": []})
            s_["t"].append(t); s_["x"].append(float(r["x_utm"])); s_["y"].append(float(r["y_utm"]))
        except (KeyError, ValueError):
            continue
    conn = get_conn()
    try:
        blocs, das, pistes_db = _fond_site(conn)
        zone, hemi = repere_site(conn)
    finally:
        conn.close()
    zone, hemi = zone or 30, hemi or "N"
    # decor de la carte transmis par la page Live (zones, pistes colorees, fond) ; a defaut, zones de depot de la base
    decor = fond_video_lire()
    zones_utm = []                                   # (nom, x, y)
    for z in decor.get("zones") or []:
        try:
            e, n = wgs84_vers_utm(z["lat"], z["lng"], zone, hemi)
            zones_utm.append((z["name"], e, n))
        except (KeyError, ValueError, TypeError):
            continue
    if not zones_utm:
        for nom, sommets in das:
            if sommets:
                zones_utm.append((nom, sum(p[0] for p in sommets) / len(sommets), sum(p[1] for p in sommets) / len(sommets)))
    pistes_couleur = []                              # (points UTM, couleur BGR)
    for t in decor.get("pistes") or []:
        wp = t.get("waypoints") or []
        if len(wp) < 2:
            a = next((z for z in zones_utm if z[0] == t.get("from")), None)
            b = next((z for z in zones_utm if z[0] == t.get("to")), None)
            pts = [(a[1], a[2]), (b[1], b[2])] if a and b else []
        else:
            pts = []
            for p in wp:
                try:
                    pts.append(wgs84_vers_utm(p[0], p[1], zone, hemi))
                except (ValueError, TypeError, IndexError):
                    pass
        if len(pts) >= 2:
            pistes_couleur.append((pts, _hex_bgr(t.get("color") or "#EE7A2A") if t.get("useColor", True) else (163, 179, 183)))
    # cadrage : positions + zones (la carte montre le site, pas seulement la ou les engins ont roule)
    xs = [v for s_ in series.values() for v in s_["x"]] + [z[1] for z in zones_utm]
    ys = [v for s_ in series.values() for v in s_["y"]] + [z[2] for z in zones_utm]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    marge = max(60.0, 0.10 * max(x1 - x0, y1 - y0, 100.0))
    x0 -= marge; x1 += marge; y0 -= marge; y1 += marge
    if x1 - x0 < 600.0:
        c_ = (x0 + x1) / 2.0; x0, x1 = c_ - 300.0, c_ + 300.0
    if y1 - y0 < 340.0:
        c_ = (y0 + y1) / 2.0; y0, y1 = c_ - 170.0, c_ + 170.0
    bandeau = 56
    zoneh = hauteur - bandeau
    echelle = min(largeur / (x1 - x0), zoneh / (y1 - y0))
    ox = (largeur - (x1 - x0) * echelle) / 2.0
    oy = bandeau + (zoneh - (y1 - y0) * echelle) / 2.0
    px = lambda x, y: (int(ox + (x - x0) * echelle), int(oy + (y1 - y) * echelle))
    dans = lambda x, y: x0 <= x <= x1 and y0 <= y <= y1

    def latlon(u, v):                                 # pixel de la video -> (lat, lon)
        return utm_vers_wgs84(x0 + (u - ox) / echelle, y1 - (v - oy) / echelle, zone, hemi)
    # fond : satellite (tuiles) recale sur le cadre ; sans internet / sans tuile : fond sombre uni
    fond, manquantes = None, 0
    try:
        coins = [latlon(0, 0), latlon(largeur, 0), latlon(0, hauteur), latlon(largeur, hauteur)]
        fond, manquantes = _fond_carte(cv2, np, decor.get("basemap") or "esri-sat", coins, coins[:3], largeur, hauteur, echelle)
    except Exception as e:
        print("VIDEO: fond satellite indisponible (%s)" % e)
    satellite = fond is not None
    if not satellite:
        fond = np.full((hauteur, largeur, 3), (38, 40, 36), np.uint8)
    # zones : contour fin (orebloc) / remplissage translucide (zone de depot)
    calque = fond.copy()
    for sommets in blocs:
        pts = [px(p[0], p[1]) for p in sommets if len(p) >= 2]
        if len(pts) >= 3:
            cv2.polylines(fond, [np.array(pts, np.int32)], True, (235, 235, 230) if satellite else (110, 116, 120), 1, cv2.LINE_AA)
    for nom, sommets in das:
        pts = [px(p[0], p[1]) for p in sommets if len(p) >= 2]
        if len(pts) >= 3:
            cv2.fillPoly(calque, [np.array(pts, np.int32)], (60, 130, 60))
    fond = cv2.addWeighted(calque, 0.45, fond, 0.55, 0)
    # pistes colorees (contour sombre, trait de couleur) : memes couleurs que la carte du dashboard
    for pts, couleur in pistes_couleur:
        ligne = np.array([px(a, b) for a, b in pts], np.int32)
        cv2.polylines(fond, [ligne], False, (0, 0, 0), 6, cv2.LINE_AA)
        cv2.polylines(fond, [ligne], False, couleur, 3, cv2.LINE_AA)
    if not pistes_couleur:                            # pas de decor transmis : reseau de pistes de la passerelle
        for x1_, y1_, x2_, y2_ in pistes_db:
            cv2.line(fond, px(x1_, y1_), px(x2_, y2_), (80, 160, 235) if satellite else (110, 116, 120), 1, cv2.LINE_AA)
    # epingles de zone + etiquettes
    for nom, zx, zy in zones_utm:
        c = px(zx, zy)
        if 0 <= c[0] < largeur and bandeau <= c[1] < hauteur:
            _pin(cv2, np, fond, c)
            _etiquette(cv2, fond, str(nom)[:24], c[0] + 14, c[1] - 30)
    os.makedirs(os.path.dirname(sortie), exist_ok=True)
    # WebM / VP8 : lisible par tous les navigateurs (lecteur integre de la page Live). Le MP4 « mp4v » d'OpenCV
    # (MPEG-4 partie 2) n'est lu par aucun navigateur ; H.264 exige une DLL OpenH264 que Windows n'a pas.
    part = sortie + ".part.webm"
    ecr = cv2.VideoWriter(part, cv2.VideoWriter_fourcc(*"VP80"), fps, (largeur, hauteur))
    if not ecr.isOpened():
        return False, "encodeur video indisponible (OpenCV / VP8)"
    pas = vitesse / float(fps)
    n_images = int((t_fin - t_deb) / pas) + 1
    TRACE_S = 600.0                              # 10 min de trace derriere chaque engin
    couleurs = {"DT": (37, 213, 252), "EXCA": (42, 122, 238)}
    try:
        for k in range(n_images):
            t = t_deb + k * pas
            img = fond.copy()
            vivants = 0
            for code, s_ in series.items():
                ts = np.array(s_["t"])
                if t < ts[0] - 120 or t > ts[-1] + 120:
                    continue
                xs_, ys_ = np.array(s_["x"]), np.array(s_["y"])
                cx, cy = float(np.interp(t, ts, xs_)), float(np.interp(t, ts, ys_))
                i1 = int(np.searchsorted(ts, t))
                silence = min(abs(t - ts[max(0, min(i1, len(ts) - 1))]), abs(t - ts[max(0, i1 - 1)]))
                couleur = couleurs.get(s_["type"], (200, 200, 200)) if silence <= 120 else (130, 130, 130)
                m = (ts >= t - TRACE_S) & (ts <= t)
                if m.sum() >= 2:
                    trace = np.array([px(a, b) for a, b in zip(xs_[m], ys_[m])], np.int32)
                    cv2.polylines(img, [trace], False, (0, 0, 0), 4, cv2.LINE_AA)
                    cv2.polylines(img, [trace], False, couleur, 2, cv2.LINE_AA)
                if dans(cx, cy):
                    c = px(cx, cy)
                    cv2.circle(img, c, 13, (255, 255, 255), -1, cv2.LINE_AA)       # anneau blanc, comme la carte
                    cv2.circle(img, c, 11, (60, 40, 20), -1, cv2.LINE_AA)
                    cv2.circle(img, c, 7, couleur, -1, cv2.LINE_AA)
                    _etiquette(cv2, img, code, c[0], c[1] - 40, "centre")
                if silence <= 120:
                    vivants += 1
            cv2.rectangle(img, (0, 0), (largeur, bandeau), (22, 24, 21), -1)
            cv2.putText(img, "ORE TRACKING  -  tracking enregistre", (16, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.62, (37, 213, 252), 1, cv2.LINE_AA)
            cv2.putText(img, "%s   x%d" % (datetime.fromtimestamp(t).strftime("%d/%m/%Y  %H:%M:%S"), vitesse),
                        (16, 48), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (235, 235, 230), 1, cv2.LINE_AA)
            cv2.putText(img, "%d engin(s) en direct" % vivants, (largeur - 230, 34), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (200, 200, 195), 1, cv2.LINE_AA)
            ecr.write(img)
    finally:
        ecr.release()
    os.replace(part, sortie)
    info = "satellite" if satellite else "fond uni (tuiles indisponibles : internet ?)"
    return True, "%d image(s), %d engin(s), %.1f Mo, %s" % (n_images, len(series), os.path.getsize(sortie) / 1048576.0, info)


def nom_video(depuis, jusqua):
    return "tracking-%s_%s-%s.webm" % (depuis[:10], depuis[11:16].replace(":", "h"), jusqua[:10] + "_" + jusqua[11:16].replace(":", "h"))


def lancer_video(depuis, jusqua, vitesse=120):
    """Demarre la generation (processus a part, priorite basse). Retourne (ok, nom, message)."""
    nom = nom_video(depuis, jusqua)
    sortie = os.path.join(ENREGISTREMENTS_DIR, nom)
    if os.path.exists(sortie):
        return True, nom, "deja genere"
    j = _jobs_video.get(nom)
    if j is not None and (j.poll() is None if hasattr(j, "poll") else j.is_alive()):
        return True, nom, "generation en cours"
    if len(tracking_lire(depuis, jusqua)) < 2:
        return False, nom, "aucune position enregistree sur cette periode"
    if getattr(sys, "frozen", False):                # executable : pas de second processus, on utilise un thread
        t = threading.Thread(target=lambda: generer_video(depuis, jusqua, sortie, vitesse), daemon=True)
        t.start(); _jobs_video[nom] = t
        return True, nom, "generation lancee"
    drapeaux = 0
    if os.name == "nt":
        drapeaux = 0x00004000 | 0x08000000           # BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW
    _jobs_video[nom] = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "--video", depuis, jusqua, sortie, str(int(vitesse))],
        creationflags=drapeaux, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True, nom, "generation lancee"


# Bouton « REC » (dashboard / Live) : la PASSERELLE filme, en arriere-plan, la carte REELLE du dashboard.
# Un navigateur sans ecran (Edge / Chrome, installe sur ce PC) ouvre la page du dashboard, la passerelle prend
# une capture de la zone « carte » aussi vite qu'elle le peut (3 a 5 par seconde) et en fait une video WebM a la
# vitesse REELLE (REC_VITESSE=1 ; 10 = film accelere x10). « Arreter » ferme le fichier. Aucune autorisation a donner,
# rien a laisser ouvert : l'enregistrement continue meme si la page du bouton est fermee.
RECORD_FICHIER = os.path.join(DATA_DIR, "record_en_cours.json")
RECORD_PARTIE = os.path.join(ENREGISTREMENTS_DIR, "record_en_cours.part.webm")
RECORD_MIN_IMAGES = 3
RECORD_MAX_H = 26
RECORD_CLES_PAGE = ("oreTrackingConfig", "oreTrackingTrails", "oreTrackingFiltres", "oreTrackingDiffusion")
_record_verrou = threading.Lock()
_record = {"fil": None, "arret": None, "images": 0, "erreur": ""}


def record_etat():
    """{"debut": ..., "images": n, "erreur": ""} si un enregistrement est en cours, sinon None."""
    try:
        with open(RECORD_FICHIER, encoding="utf-8") as fh:
            d = json.load(fh)
        if _RE_HORODATAGE.match(str(d.get("debut") or "")):
            return {"debut": d["debut"], "images": _record["images"], "erreur": _record["erreur"],
                    "vitesse": d.get("vitesse", 1), "qualite": d.get("qualite", "haute")}
    except (OSError, ValueError, AttributeError):
        pass
    return None


def record_nettoyer():
    """Au demarrage de la passerelle : un enregistrement laisse par un arret brutal est inexploitable."""
    for f in (RECORD_FICHIER, RECORD_PARTIE):
        try:
            os.remove(f)
        except OSError:
            pass


def _navigateur_sans_ecran():
    """Chemin d'Edge / Chrome (REC_NAVIGATEUR pour forcer), ou None."""
    forces = str(param_lire("REC_NAVIGATEUR", "")).strip()
    if forces and os.path.isfile(forces):
        return forces
    racines = [os.environ.get(v, "") for v in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
    for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe"):
        for r in racines:
            if r and os.path.isfile(os.path.join(r, rel)):
                return os.path.join(r, rel)
    return None


class _WebSocketCDP:
    """Client WebSocket minimal (texte, sans bibliotheque) vers le protocole DevTools du navigateur."""

    def __init__(self, url, delai=40):
        import socket as _socket
        u = urlparse(url)
        self.sock = _socket.create_connection((u.hostname, u.port), 10)
        self.sock.settimeout(delai)
        cle = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
                           % (u.path or "/", u.hostname, u.port, cle)).encode())
        tete = b""
        while b"\r\n\r\n" not in tete:
            morceau = self.sock.recv(4096)
            if not morceau:
                raise RuntimeError("poignee de main WebSocket refusee")
            tete += morceau
        if b" 101 " not in tete.split(b"\r\n", 1)[0]:
            raise RuntimeError("WebSocket refuse : " + tete.split(b"\r\n", 1)[0].decode("latin-1"))
        self._tampon = tete.split(b"\r\n\r\n", 1)[1]
        self._id = 0

    def _lire(self, n):
        while len(self._tampon) < n:
            morceau = self.sock.recv(65536)
            if not morceau:
                raise RuntimeError("connexion DevTools fermee")
            self._tampon += morceau
        sortie, self._tampon = self._tampon[:n], self._tampon[n:]
        return sortie

    def _envoyer(self, opcode, donnees):
        masque = os.urandom(4)
        n = len(donnees)
        tete = bytes([0x80 | opcode])
        if n < 126:
            tete += bytes([0x80 | n])
        elif n < 65536:
            tete += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            tete += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        self.sock.sendall(tete + masque + bytes(b ^ masque[i % 4] for i, b in enumerate(donnees)))

    def _message(self):
        texte = b""
        while True:
            b1, b2 = self._lire(2)
            opcode, fin, n = b1 & 0x0F, b1 & 0x80, b2 & 0x7F
            if n == 126:
                n = int.from_bytes(self._lire(2), "big")
            elif n == 127:
                n = int.from_bytes(self._lire(8), "big")
            donnees = self._lire(n)
            if opcode == 8:
                raise RuntimeError("DevTools a ferme la connexion")
            if opcode == 9:
                self._envoyer(10, donnees)
                continue
            if opcode in (0, 1, 2):
                texte += donnees
                if fin:
                    return texte.decode("utf-8", "replace")

    def appel(self, methode, params=None):
        self._id += 1
        mon_id = self._id
        self._envoyer(1, json.dumps({"id": mon_id, "method": methode, "params": params or {}}).encode())
        while True:
            m = json.loads(self._message())
            if m.get("id") == mon_id:
                if "error" in m:
                    raise RuntimeError("%s : %s" % (methode, m["error"].get("message")))
                return m.get("result") or {}

    def fermer(self):
        try:
            self.sock.close()
        except OSError:
            pass


# qualite choisie dans l'interface : (definition des captures, debit VP8 en kbit/s, qualite JPEG)
RECORD_QUALITES = {"standard": (1.0, 3000, 90), "haute": (1.5, 6000, 95), "maximale": (2.0, 14000, 98)}
# 26/11 : images ENCODEES par seconde reelle que le PC tient a chaque qualite (VP8 ~0,27 s/image en « haute »). Avant,
# la video etait toujours a 10 images/s : en « haute » l'encodeur prenait du retard, une capture toutes les 4-5 s,
# « moins de 3 images » sur un essai de 30 s et aucune video creee.
RECORD_IMAGES_PAR_S = {"standard": 5, "haute": 2, "maximale": 1}


def _filmer_carte(arret, etat_page, sortie_partie, vitesse=1.0, qualite="haute"):
    """Boucle d'enregistrement (fil de la passerelle) : ouvre le dashboard dans un navigateur sans ecran, photographie
    la zone carte toutes les REC_INTERVALLE_S s et ecrit la video WebM. Se termine quand `arret` est leve."""
    import shutil
    import socket as _socket
    import tempfile
    try:
        import cv2
        import numpy as np
    except ImportError:
        _record["erreur"] = "OpenCV et NumPy sont requis (pip install opencv-python numpy)"
        return
    exe = _navigateur_sans_ecran()
    if not exe:
        _record["erreur"] = "Edge ou Chrome introuvable sur ce PC (REC_NAVIGATEUR pour indiquer le chemin)"
        return
    intervalle = max(0.0, float(param_lire("REC_INTERVALLE_S", "0.2") or 0.2))     # pause entre deux captures
    fps = max(1, int(float(param_lire("REC_FPS", "10") or 10)))
    echelle, kbps, jpeg = RECORD_QUALITES.get(qualite, RECORD_QUALITES["haute"])    # definition / debit / jpeg
    # images/s de la video limitees a ce que l'encodeur tient (x1 haute : 2 img/s ; x10 : 10 img/s conserves)
    fps = max(1, min(fps, int(round(vitesse * RECORD_IMAGES_PAR_S.get(qualite, 2)))))
    debit = kbps * 1000
    # encodeur VP8 : debit eleve (le defaut d'OpenCV, ~200 kbit/s, donnait une image baveuse) et qualite maximale
    os.environ["OPENCV_FFMPEG_WRITER_OPTIONS"] = "b;%d|crf;6|quality;good|cpu-used;3" % debit
    saas = str(param_lire("SAAS_URL", "http://127.0.0.1:8100")).rstrip("/")
    profil = tempfile.mkdtemp(prefix="ot-rec-")
    with _socket.socket() as s_:
        s_.bind(("127.0.0.1", 0))
        port = s_.getsockname()[1]
    drapeaux = (0x08000000 if os.name == "nt" else 0)
    proc = subprocess.Popen(
        [exe, "--headless=new", "--remote-debugging-port=%d" % port, "--user-data-dir=" + profil, "--hide-scrollbars",
         "--no-first-run", "--no-default-browser-check", "--mute-audio", "--disable-extensions", "--window-size=1600,900",
         "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=drapeaux)
    ws, ecr = None, None
    ecrites, precedente, debut_mono = 0, None, time.time()
    try:
        cible = None
        for _ in range(60):                                   # le navigateur demarre (jusqu'a 30 s)
            if arret.is_set():
                return
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=2) as rep:
                    pages = [p for p in json.loads(rep.read().decode()) if p.get("type") == "page"]
                if pages:
                    cible = pages[0]["webSocketDebuggerUrl"]
                    break
            except Exception:
                pass
            time.sleep(0.5)
        if not cible:
            _record["erreur"] = "le navigateur sans ecran ne repond pas"
            return
        ws = _WebSocketCDP(cible)
        ws.appel("Page.enable")
        ws.appel("Emulation.setDeviceMetricsOverride", {"width": 1600, "height": 900, "deviceScaleFactor": 1, "mobile": False})
        # reglages de carte de l'utilisateur (filtres, pistes, fond...) : injectes avant le chargement de la page
        injection = "".join("try{localStorage.setItem(%s,%s)}catch(e){}" % (json.dumps(k), json.dumps(v))
                            for k, v in (etat_page or {}).items())
        # elements de l'interface exclus du film : le panneau titre « La flotte en mouvement »
        injection += ("document.addEventListener('DOMContentLoaded',function(){var s=document.createElement('style');"
                      "s.textContent='.map-title,#inviteSon{display:none!important}';document.head.appendChild(s)});")
        ws.appel("Page.addScriptToEvaluateOnNewDocument", {"source": injection})
        ws.appel("Page.navigate", {"url": saas + "/html/dashboard.html"})
        zone_js = ("(function(){var e=document.getElementById('map');if(!e)return null;var r=e.getBoundingClientRect();"
                   "return JSON.stringify({x:r.left,y:r.top,w:r.width,h:r.height,t:document.querySelectorAll("
                   "'#map .leaflet-tile-loaded').length})})()")
        zone = None
        for _ in range(80):                                   # page + premieres tuiles (jusqu'a 40 s)
            if arret.is_set():
                return
            try:
                v = ws.appel("Runtime.evaluate", {"expression": zone_js}).get("result", {}).get("value")
                zone = json.loads(v) if v else None
            except Exception:
                zone = None
            if zone and zone["w"] > 100 and zone["h"] > 100 and zone["t"] > 3:
                time.sleep(2.0)                               # laisse la carte se poser (zoom, marqueurs)
                break
            time.sleep(0.5)
        if not zone:
            _record["erreur"] = "dashboard illisible (le SaaS repond-il sur %s ?)" % saas
            return
        debut_mono = time.time()
        k = 0
        ecrites, precedente = 0, None
        while not arret.is_set():
            t_prevu = debut_mono + k * max(intervalle, vitesse / float(fps))
            attente = t_prevu - time.time()
            if attente > 0 and arret.wait(attente):
                break
            k += 1
            if time.time() - debut_mono > RECORD_MAX_H * 3600:
                break
            try:
                v = ws.appel("Runtime.evaluate", {"expression": zone_js}).get("result", {}).get("value")
                zone = json.loads(v) if v else zone
                rep = ws.appel("Page.captureScreenshot", {"format": "jpeg", "quality": jpeg, "clip": {
                    "x": zone["x"], "y": zone["y"], "width": zone["w"], "height": zone["h"], "scale": echelle}})
                img = cv2.imdecode(np.frombuffer(base64.b64decode(rep["data"]), np.uint8), cv2.IMREAD_COLOR)
            except Exception as e:
                _record["erreur"] = "capture interrompue : %s" % e
                break
            if img is None:
                continue
            if ecr is None:
                h0, w0 = img.shape[:2]
                taille = (w0 - w0 % 2, h0 - h0 % 2)
                ecr = cv2.VideoWriter(sortie_partie, cv2.VideoWriter_fourcc(*"VP80"), fps, taille)
                if not ecr.isOpened():
                    _record["erreur"] = "encodeur video indisponible (OpenCV / VP8)"
                    break
            img = cv2.resize(img, taille) if (img.shape[1], img.shape[0]) != taille else img
            etiquette = datetime.now().strftime("%d/%m/%Y  %H:%M:%S")      # heure reelle du cliche
            cv2.rectangle(img, (0, 0), (int(190 * echelle), int(24 * echelle)), (22, 24, 21), -1)
            cv2.putText(img, etiquette, (int(8 * echelle), int(17 * echelle)), cv2.FONT_HERSHEY_SIMPLEX, 0.5 * echelle,
                        (240, 240, 232), 1, cv2.LINE_AA)
            # duree de video = duree reelle / vitesse : l'image precedente reste affichee jusqu'a celle-ci
            cible = int((time.time() - debut_mono) / vitesse * fps)
            if precedente is not None:
                manque = max(0, cible - ecrites - 1)
                if manque > fps * 5:                   # 26/11 : jamais de rattrapage sans fin (PC surcharge)
                    ecrites += manque - fps * 5
                    manque = fps * 5
                for _ in range(manque):
                    ecr.write(precedente)
                    ecrites += 1
            ecr.write(img)
            ecrites += 1
            precedente = img
            _record["images"] += 1
    except Exception as e:
        _record["erreur"] = "%s: %s" % (type(e).__name__, e)
    finally:
        if ecr is not None:
            if precedente is not None:                         # la derniere image reste jusqu'a l'arret
                try:
                    for _ in range(max(0, int((time.time() - debut_mono) / vitesse * fps) - ecrites)):
                        ecr.write(precedente)
                except Exception:
                    pass
            ecr.release()
        if ws is not None:
            ws.fermer()
        try:
            proc.terminate()
            proc.wait(10)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(profil, ignore_errors=True)


def record_action(action, etat_page=None, vitesse=1, qualite="haute"):
    """Demarre / arrete l'enregistrement. Rend (code HTTP, reponse)."""
    with _record_verrou:
        en_cours = record_etat()
        if action == "start":
            if en_cours:
                return 200, {"ok": True, "record": en_cours, "message": "deja en cours"}
            if not _navigateur_sans_ecran():
                return 503, {"ok": False, "error": "Edge ou Chrome introuvable sur le PC de la passerelle"}
            os.makedirs(ENREGISTREMENTS_DIR, exist_ok=True)
            try:
                os.remove(RECORD_PARTIE)
            except OSError:
                pass
            propre = {}
            for cle in RECORD_CLES_PAGE:                       # reglages de carte transmis par la page : bornes et filtres
                v = (etat_page or {}).get(cle)
                if isinstance(v, str) and 0 < len(v) < 600000:
                    propre[cle] = v
            _record.update(images=0, erreur="", arret=threading.Event())
            try:
                vitesse = max(1.0, min(120.0, float(vitesse)))   # 1 = temps reel ; 10 = film accelere x10
            except (TypeError, ValueError):
                vitesse = 1.0
            _record["fil"] = threading.Thread(target=_filmer_carte, args=(_record["arret"], propre, RECORD_PARTIE, vitesse,
                                                                       qualite if qualite in RECORD_QUALITES else "haute"), daemon=True)
            d = {"debut": now_iso(), "vitesse": vitesse, "qualite": qualite if qualite in RECORD_QUALITES else "haute"}
            with open(RECORD_FICHIER, "w", encoding="utf-8") as fh:
                json.dump(d, fh)
            _record["fil"].start()
            return 200, {"ok": True, "record": record_etat(), "message": "enregistrement demarre"}
        if action == "stop":
            if not en_cours:
                return 409, {"ok": False, "error": "aucun enregistrement en cours"}
            debut, fin = en_cours["debut"], now_iso()
            if _record["arret"] is not None:
                _record["arret"].set()
            if _record["fil"] is not None:
                _record["fil"].join(60)                        # le fichier est ferme ici : la video est « bouclee »
            erreur, images = _record["erreur"], _record["images"]
            try:
                os.remove(RECORD_FICHIER)
            except OSError:
                pass
            taille = os.path.getsize(RECORD_PARTIE) if os.path.isfile(RECORD_PARTIE) else 0
            if images < RECORD_MIN_IMAGES or not taille:
                try:
                    os.remove(RECORD_PARTIE)
                except OSError:
                    pass
                return 422, {"ok": False, "error": erreur or "enregistrement trop court : moins de %d images (le navigateur met ~10 s a demarrer, laisse tourner au moins 30 s)" % RECORD_MIN_IMAGES}
            nom = nom_video(debut, fin)
            os.replace(RECORD_PARTIE, os.path.join(ENREGISTREMENTS_DIR, nom))
            msg = "enregistrement termine : %d images, %.1f Mo" % (images, taille / 1048576.0)
            return 200, {"ok": True, "fichier": nom, "message": msg + (" (arret anticipe : %s)" % erreur if erreur else ""),
                         "debut": debut, "fin": fin}
    return 400, {"ok": False, "error": "action : start ou stop"}


def enregistrements_liste():
    """CSV du tracking et videos, avec taille et etat des generations en cours."""
    res = {"csv": [], "videos": [], "record": record_etat()}
    for dossier, cle, ext in ((TRACKING_DIR, "csv", ".csv"), (ENREGISTREMENTS_DIR, "videos", (".webm", ".mp4"))):
        try:
            for f in sorted(os.listdir(dossier), reverse=True):
                if f.endswith(ext) and ".part." not in f:
                    res[cle].append({"nom": f, "octets": os.path.getsize(os.path.join(dossier, f))})
        except OSError:
            pass
    res["en_cours"] = sorted(n for n, j in _jobs_video.items() if (j.poll() is None if hasattr(j, "poll") else j.is_alive()))
    return res


def dernier_shift_termine(maintenant=None):
    """(nom, debut, fin) du dernier shift termine depuis au moins 5 min : jour 06h-18h ou nuit 18h-06h."""
    m = (maintenant or datetime.now()) - timedelta(minutes=5)
    h = m.hour
    if h >= 18:
        d = m.replace(hour=6, minute=0, second=0, microsecond=0)
        return "jour", d, d + timedelta(hours=12)
    if h >= 6:
        f = m.replace(hour=6, minute=0, second=0, microsecond=0)
        return "nuit", f - timedelta(hours=12), f
    d = (m - timedelta(days=1)).replace(hour=6, minute=0, second=0, microsecond=0)
    return "jour", d, d + timedelta(hours=12)


def boucle_video_auto():
    """A la fin de chaque shift : video du shift termine (une fois), et menage des vieilles videos."""
    while True:
        try:
            if str(param_lire("VIDEO_AUTO", "0")).strip().lower() in ("1", "oui", "true", "on"):
                _, d, f = dernier_shift_termine()
                debut, fin = d.strftime("%Y-%m-%d %H:%M:%S"), f.strftime("%Y-%m-%d %H:%M:%S")
                if not os.path.exists(os.path.join(ENREGISTREMENTS_DIR, nom_video(debut, fin))):
                    ok, nom, msg = lancer_video(debut, fin, 120)
                    if ok and msg == "generation lancee":
                        print("VIDEO: %s -> %s" % (nom, msg))
            purger_videos()       # 26/11 : duree reglable dans Parametres (0 = jamais), enregistrement en cours epargne
        except Exception as e:
            print("VIDEO: boucle automatique en erreur (%s)" % e)
        time.sleep(120)


def cli_video(args):
    """python Server_Senor_Geology.py --video DEBUT FIN SORTIE [VITESSE]"""
    depuis, jusqua, sortie = args[0], args[1], args[2]
    vitesse = int(args[3]) if len(args) > 3 else 120
    ok, msg = generer_video(depuis, jusqua, sortie, vitesse)
    print(("OK " if ok else "ECHEC ") + msg)
    return 0 if ok else 1


# ------------------------------------------------ remontees de Serveur_SG ---
# Les tablettes du terrain envoient leurs positions en 4G a Serveur_SG
# (onglet `remontees`) : elles ne passent PAS par la passerelle. Ce thread
# lit les lignes nouvelles toutes les REMONTEES_INTERVALLE_S secondes et les
# rejoue dans appliquer_position_engin (detection offroad / deviation /
# arrivee, carte du dashboard), avec l'heure de chaque observation.
# Au premier demarrage on part de la fin : l'historique ne declenche rien.
REMONTEES_INTERVALLE_DEFAUT = 10
REMONTEES_PAQUET = 2               # lignes rejouees par prise de verrou (~0,06 s)
_remontees_etat = {"actif": False, "derniere_lecture": "", "derniere_erreur": "",
                   "lignes_traitees": 0, "derniere_position": ""}


def remontees_intervalle():
    try:
        v = int(float(param_lire("REMONTEES_INTERVALLE_S", str(REMONTEES_INTERVALLE_DEFAUT))))
    except (TypeError, ValueError):
        v = REMONTEES_INTERVALLE_DEFAUT
    return max(5, min(v, 600))


def sg_lire_remontees(depuis=None, action="remontees"):
    """GET ?action=remontees (chargement) ou ?action=trajet (hors chargement)
    chez Serveur_SG. (ok, donnees, erreur)."""
    sg_url, sg_token, _ = sg_conf()
    if not sg_url or not sg_token:
        return False, {}, "Serveur_SG non configure"
    url = "%s?action=%s&token=%s" % (sg_url, action, quote(sg_token))
    if depuis is not None:
        url += "&depuis=%d" % int(depuis)
    try:
        with urllib.request.urlopen(url, timeout=60) as rep_:
            d = json.loads(rep_.read().decode("utf-8", "replace"))
    except Exception as e:
        return False, {}, "%s: %s" % (type(e).__name__, str(e)[:160])
    if d.get("ok") is False:
        return False, d, str(d.get("erreur") or "refus de Serveur_SG")
    return True, d, ""


def traiter_remontees(conn, lignes):
    """Rejoue des lignes de remontees (dans l'ordre). Rend le nombre de DT traites."""
    n = 0
    for r in lignes:
        dt = str(r.get("dt") or "").strip()
        try:
            x, y = float(r["x"]), float(r["y"])
        except (KeyError, TypeError, ValueError):
            continue
        if not dt:
            continue
        instant = str(r.get("horodatage") or "").strip()[:19].replace("T", " ") or None
        # l'excavatrice d'abord : la zone de chargement autour d'elle evite
        # de signaler le DT hors piste pendant qu'il est charge
        exca = str(r.get("exca") or "").strip()
        if exca and r.get("exca_x") is not None and r.get("exca_y") is not None:
            try:
                appliquer_position_engin(conn, exca, "EXCA", float(r["exca_x"]), float(r["exca_y"]),
                                         horodatage=instant, relayer=False, canal="http-relais", auth="aucune")
            except (TypeError, ValueError):
                pass
        appliquer_position_engin(conn, dt, "DT", x, y, horodatage=instant, relayer=False, canal="http", auth="jeton")
        n += 1
        _remontees_etat["derniere_position"] = "%s %s" % (dt, instant or "")
    return n


# Deux onglets chez Serveur_SG (17/11) : `Remontees` (points au chargement,
# avec l'excavatrice) et `remontée_trajet` (points hors chargement). Chacun a
# son pointeur de ligne ; les points des deux sont rejoues dans l'ORDRE DES
# HEURES, sinon un chargement et le trajet qui le suit seraient melanges.
SOURCES_REMONTEES = (("remontees", "REMONTEES_DERNIERE_LIGNE"),
                     ("trajet", "TRAJET_DERNIERE_LIGNE"))


def remontees_un_passage():
    """Une lecture des deux onglets. Rend (ok, positions traitees, erreur)."""
    conn = get_conn()
    try:
        lots, pointeurs, erreurs = [], {}, []
        for action, cle in SOURCES_REMONTEES:
            brut = param_lire(cle, "")
            if not brut:
                ok, d, err = sg_lire_remontees(None, action)      # point de depart = fin
                if ok:
                    param_ecrire(conn, cle, str(int(d.get("derniere_ligne") or 1)))
                elif "introuvable" not in err:   # onglet pas encore cree : rien a lire
                    erreurs.append("%s: %s" % (action, err))
                continue
            ok, d, err = sg_lire_remontees(int(brut), action)
            if not ok:
                if "introuvable" not in err:
                    erreurs.append("%s: %s" % (action, err))
                continue
            lots.extend(d.get("lignes") or [])
            pointeurs[cle] = str(int(d.get("derniere_ligne") or brut))
        # ordre chronologique (stable : a heure egale, l'ordre des onglets est garde)
        lots.sort(key=lambda r: str(r.get("horodatage") or ""))
        n = 0
        if lots:
            # PAR PAQUETS : rejouer une ligne coute ~27 ms (ecritures SQLite) ; 300 lignes tenaient le
            # verrou 8 s, et une position MQTT en direct (donc une ALERTE) attendait la fin du lot.
            # Le verrou est rendu entre deux paquets : une alerte n'attend jamais plus d'un paquet.
            for i in range(0, len(lots), REMONTEES_PAQUET):
                LOCK.route("remontees (%d-%d / %d)" % (i + 1, min(i + REMONTEES_PAQUET, len(lots)), len(lots)))
                with LOCK:
                    n += traiter_remontees(conn, lots[i:i + REMONTEES_PAQUET])
                time.sleep(0.005)          # laisse la main aux positions MQTT en attente (verrou non equitable)
        for cle, valeur in pointeurs.items():
            param_ecrire(conn, cle, valeur)
        if n:
            notifier_office(conn)       # evenements offroad / deviation vers OT
        return (not erreurs), n, "; ".join(erreurs)
    finally:
        conn.close()


def boucle_remontees():
    _remontees_etat["actif"] = True
    while True:
        try:
            ok, n, err = remontees_un_passage()
            _remontees_etat["derniere_lecture"] = now_iso()
            _remontees_etat["derniere_erreur"] = "" if ok else err
            _remontees_etat["lignes_traitees"] += n
        except Exception as e:
            _remontees_etat["derniere_erreur"] = "%s: %s" % (type(e).__name__, e)
            print("REMONTEES: erreur %s" % e)
        time.sleep(remontees_intervalle())


# ------------------------------------------------ chargements de SG (E3) ---
# KPI du dashboard calcules sur l'onglet « Chargements » de Serveur_SG (la
# base en ligne), releve toutes les CHARGEMENTS_INTERVALLE_S secondes (5 par
# defaut, decision du 19/11). Tout le jour est relu a chaque passage : SG
# reecrit les lignes d'un chargement en cours. La derniere lecture reussie
# reste en memoire : si la 4G tombe, le dashboard garde le dernier etat et
# affiche son anciennete.
CHARGEMENTS_INTERVALLE_DEFAUT = 5
_chargements_etat = {"actif": False, "jour": "", "lignes": [], "lu_le": "", "lu_ts": 0.0,
                     "derniere_tentative": "", "derniere_erreur": "", "duree_ms": 0}
_chargements_lock = threading.Lock()


def chargements_intervalle():
    try:
        v = int(float(param_lire("CHARGEMENTS_INTERVALLE_S", str(CHARGEMENTS_INTERVALLE_DEFAUT))))
    except (TypeError, ValueError):
        v = CHARGEMENTS_INTERVALLE_DEFAUT
    return max(5, min(v, 600))


def sg_lire_chargements(jour):
    """GET ?action=chargements&jour=AAAA-MM-JJ chez Serveur_SG. (ok, donnees, erreur)."""
    sg_url, sg_token, _ = sg_conf()
    if not sg_url or not sg_token:
        return False, {}, "Serveur_SG non configure"
    url = "%s?action=chargements&jour=%s&token=%s" % (sg_url, quote(jour), quote(sg_token))
    try:
        with urllib.request.urlopen(url, timeout=30) as rep_:
            d = json.loads(rep_.read().decode("utf-8", "replace"))
    except Exception as e:
        return False, {}, "%s: %s" % (type(e).__name__, str(e)[:160])
    if d.get("ok") is False:
        return False, d, str(d.get("erreur") or "refus de Serveur_SG")
    if not isinstance(d.get("lignes"), list):
        # deploiement SG anterieur au 19/11 : l'action n'existe pas encore
        return False, d, "reponse sans lignes (Serveur_SG a redeployer ?)"
    return True, d, ""


def chargements_un_passage():
    jour = datetime.now().strftime("%Y-%m-%d")
    t0 = time.time()
    ok, d, err = sg_lire_chargements(jour)
    with _chargements_lock:
        _chargements_etat["derniere_tentative"] = now_iso()
        _chargements_etat["duree_ms"] = int((time.time() - t0) * 1000)
        if ok:
            _chargements_etat.update(jour=jour, lignes=d.get("lignes") or [],
                                     lu_le=now_iso(), lu_ts=time.time(), derniere_erreur="")
        else:
            _chargements_etat["derniere_erreur"] = err
    if ok:
        try:
            voyages_depuis_chargements(d.get("lignes") or [])
        except Exception as e:
            print("VOYAGES: enregistrement depuis SG impossible (%s)" % e)
    return ok, err


def voyages_depuis_chargements(lignes):
    """25/11 (calibrage) : la table locale `voyages` (Inbox, Senor, Digplan, cumuls des blocs) n'etait plus alimentee
    depuis que les missions se closent chez Serveur_SG (seul /api/mission_fin, que personne n'appelle, y ecrivait).
    Chaque chargement de SG dont la mission est « termine » (dumping valide sur la tablette) devient ici un voyage,
    une seule fois (cle du cycle) : toutes les pages comptent alors les memes voyages que les KPI du dashboard."""
    a_creer = [r for r in lignes if str(r.get("mission_statut") or "").strip() == "termine" and str(r.get("cle") or "").strip()]
    if not a_creer:
        return 0
    conn = get_conn()
    n = 0
    try:
        for r in a_creer:
            cle = str(r["cle"]).strip()
            nom = str(r.get("orebloc") or "").strip()
            ob = conn.execute("SELECT id, grade_cutoff FROM oreblocs WHERE nom=? ORDER BY actif DESC, id DESC LIMIT 1",
                              (nom,)).fetchone() if nom else None
            if nom and ob is None:
                # 26/11 : meme bloc ecrit 257.5_ (Serveur_SG) ou 257_5_ (DXF)
                canon = nom_flitch_canonique(nom)
                for o in conn.execute("SELECT id, grade_cutoff, nom FROM oreblocs ORDER BY actif DESC, id DESC"):
                    if nom_flitch_canonique(o["nom"]) == canon:
                        ob = o
                        break
            da_nom = str(r.get("dumping_area") or "").strip()
            da = conn.execute("SELECT id FROM dumping_areas WHERE nom=? LIMIT 1", (da_nom,)).fetchone() if da_nom else None
            deja = conn.execute("SELECT id, orebloc_id, dumping_area_id FROM voyages WHERE cle_sg=?", (cle,)).fetchone()
            if deja:
                # 26/11 : ligne reclassee chez SG (chargements_recalculer) : le voyage suit son bloc et sa zone
                nouv = (ob["id"] if ob else 0, da["id"] if da else 0)
                if (deja["orebloc_id"] or 0, deja["dumping_area_id"] or 0) != nouv:
                    conn.execute("UPDATE voyages SET orebloc_id=?, dumping_area_id=? WHERE id=?", nouv + (deja["id"],))
                    n += 1
                continue
            dt = str(r.get("dt") or "").strip()
            debut = str(r.get("debut") or "").replace("T", " ")[:19]
            mi = conn.execute("SELECT id FROM missions WHERE dt_code=? AND debut=? ORDER BY id DESC LIMIT 1",
                              (dt, debut)).fetchone()
            try:
                tonnes = float(r.get("tonnes"))
            except (TypeError, ValueError):
                tonnes = 0.0
            detail = {"regle": "dumping_valide", "source": "Serveur_SG", "zone_attendue": da_nom,
                      "raison": "dumping valide par la tablette (arret dans la zone de la mission)",
                      "remarque": str(r.get("remarque") or "")}
            conn.execute(
                "INSERT INTO voyages (mission_id, dt_code, orebloc_id, dumping_area_id, teneur, volume_t, truck_factor, "
                "conformite, conformite_detail, created_at, cle_sg) VALUES (?, ?, ?, ?, ?, ?, 1, 'conforme', ?, ?, ?)",
                (mi["id"] if mi else 0, dt, ob["id"] if ob else 0, da["id"] if da else 0,
                 float(ob["grade_cutoff"]) if ob and ob["grade_cutoff"] is not None else DEFAULT_CUTOFF_GRADE,
                 tonnes, json.dumps(detail, ensure_ascii=True), debut or now_iso(), cle))
            n += 1
        if n:
            conn.commit()
            print("VOYAGES: %d voyage(s) enregistre(s) ou reclasse(s) depuis les chargements de Serveur_SG (dumping valide)" % n)
    finally:
        conn.close()
    return n


VOYAGES_RATTRAPAGE_JOURS = 3      # au demarrage : voyages des jours precedents (passerelle arretee la nuit, PC redemarre)


def rattraper_voyages():
    """26/11 : la passerelle ne lit en boucle que le jour en cours ; un PC redemarre apres minuit ne voyait jamais les
    dumpings valides de la veille (Inbox, Senor sans les voyages d'hier). Une fois au demarrage : J-1 a J-3."""
    for k in range(1, VOYAGES_RATTRAPAGE_JOURS + 1):
        jour = (datetime.now() - timedelta(days=k)).strftime("%Y-%m-%d")
        ok, d, err = sg_lire_chargements(jour)
        if ok:
            voyages_depuis_chargements(d.get("lignes") or [])
        else:
            print("VOYAGES: rattrapage du %s impossible (%s)" % (jour, err))


def boucle_chargements():
    _chargements_etat["actif"] = True
    rattrape = False
    while True:
        try:
            chargements_un_passage()
        except Exception as e:
            _chargements_etat["derniere_erreur"] = "%s: %s" % (type(e).__name__, e)
            print("CHARGEMENTS: erreur %s" % e)
        if not rattrape:
            rattrape = True
            try:
                rattraper_voyages()
            except Exception as e:
                print("VOYAGES: rattrapage impossible (%s)" % e)
        sg_url, sg_token, _ = sg_conf()
        time.sleep(chargements_intervalle() if (sg_url and sg_token) else 30)


def etat_cloud():
    """Etat de la liaison vers la base en ligne, pour le pied du dashboard."""
    sg_url, sg_token, _ = sg_conf()
    with _chargements_lock:
        e = dict(_chargements_etat)
    age = round(time.time() - e["lu_ts"], 1) if e["lu_ts"] else None
    intervalle = chargements_intervalle()
    if not (sg_url and sg_token):
        statut = "non_configure"
    elif age is None:
        statut = "ko" if e["derniere_erreur"] else "attente"
    elif age <= max(30, 4 * intervalle):
        statut = "ok"
    elif age <= 300:
        statut = "lent"
    else:
        statut = "ko"
    return {"statut": statut, "lu_le": e["lu_le"], "age_s": age, "intervalle_s": intervalle,
            "erreur": e["derniere_erreur"], "duree_ms": e["duree_ms"],
            "derniere_tentative": e["derniere_tentative"]}


# ------------------------------------------------- suivi des DT (19/11) ---
# Etat de chaque engin pour les cartes de suivi (Live, dashboard) : statut
# terrain, etat lisible, vitesse / cap (historique), mission, teneur du
# chargement, et trajets d'une periode (shift). Positions renvoyees en
# lat/lng (fuseau de Geographic System, 30N par defaut) : le navigateur n'a
# plus a deviner le repere.
SUIVI_SIGNAL_PERDU_S = 120
SUIVI_TRAJET_MAX_POINTS = 600


def _vers_latlng(x, y, zone, hemi):
    try:
        lat, lng = utm_vers_wgs84(float(x), float(y), zone, hemi)
        return round(lat, 7), round(lng, 7)
    except (TypeError, ValueError):
        return None


def _grade_bloc(nom, destination):
    code = code_teneur_bloc(nom)
    grades = codes_teneur()
    if code in grades:
        return grades[code]
    if code in codes_steriles() or str(destination or "").strip().upper() == "W":
        return "waste"
    return ""


SUIVI_ARRET_MIN = 2     # DT immobile (< SEUIL_IMMOBILISATION_M) depuis 2 min : « A l'arret » et non plus « Roulage »


def etat_suivi_dt(statut, offroad, age_s, mission, immo_min=0, en_ligne=False):
    """Etat lisible, par priorite : signal_perdu > hors_piste > chargement >
    mission > attente_loin > arret > roulage. « Roulage » n'est plus le fourre-tout : un DT
    immobile depuis SUIVI_ARRET_MIN minutes (sans chargement ni mission) est « arret ».
    25/11 : position perimee MAIS tablette connectee en MQTT (presence « en_ligne » recente) :
    « sans_position » (GNSS sans fix, module debranche), plus « signal_perdu »."""
    if age_s is None or age_s > SUIVI_SIGNAL_PERDU_S:
        return "sans_position" if en_ligne else "signal_perdu"
    if offroad:
        return "hors_piste"
    if statut == "ODT":
        return "chargement"
    if mission:
        return "mission"
    if statut == "SBDT":
        return "attente_loin"
    if immo_min >= SUIVI_ARRET_MIN:
        return "arret"
    return "roulage"


def suivi_engins(conn, depuis=None, jusqua=None):
    zone, hemi = repere_site(conn)
    zone, hemi = zone or 30, hemi or "N"
    maintenant = now_iso()
    missions = {}
    for m in conn.execute(
            "SELECT m.id, m.dt_code, datetime(m.debut, 'localtime') AS debut, m.arrivee, "
            "o.nom AS orebloc, o.destination, "  # debut : DEFAULT datetime('now') = UTC
            "d.nom AS dumping_area FROM missions m "
            "LEFT JOIN oreblocs o ON o.id = m.orebloc_id "
            "LEFT JOIN dumping_areas d ON d.id = m.dumping_area_id "
            "WHERE m.statut = 'en_cours' ORDER BY m.id"):
        missions[m["dt_code"]] = m                    # la plus recente gagne
    engins, compte = [], {}
    # 19/11 : les DT suivis sont ceux de la FLOTTE du SaaS (comptes MQTT) ; les
    # engins d'essai restes dans la table engins (DT-HEXTEST, ZZ-RELAIS-01...)
    # ne sont plus comptes. Flotte inconnue (table vide) : tous, comme avant.
    flotte_dt = {r[0] for r in conn.execute("SELECT code FROM mqtt_comptes")}
    for e in conn.execute("SELECT * FROM engins WHERE actif = 1 ORDER BY type DESC, code"):
        if e["type"] == "DT" and flotte_dt and e["code"] not in flotte_dt:
            continue
        ll = _vers_latlng(e["x"], e["y"], zone, hemi)
        if not ll or not (e["x"] or e["y"]):
            continue
        age = _secondes_depuis(e["derniere_maj"]) if e["derniere_maj"] else None
        fiche = {"code": e["code"], "type": e["type"], "statut": e["statut"],
                 "lat": ll[0], "lng": ll[1], "x": e["x"], "y": e["y"],
                 "derniere_maj": e["derniere_maj"], "age_s": None if age is None else round(age),
                 "offroad": bool(e["offroad"]), "offroad_distance": round(e["offroad_distance"] or 0),
                 "canal": e["canal"] or "", "canal_libelle": LIBELLE_CANAL.get(e["canal"] or "", ""),
                 "auth": e["auth"] or "", "auth_libelle": LIBELLE_AUTH.get(e["auth"] or "", ""),
                 "canaux": canaux_de(conn, e["code"])}
        if e["type"] == "DT":
            m = missions.get(e["code"])
            fiche["mission"] = None if not m else {
                "id": m["id"], "orebloc": m["orebloc"] or "", "dumping_area": m["dumping_area"] or "",
                "depuis": m["debut"], "arrivee": bool(m["arrivee"])}
            fiche["teneur"] = _grade_bloc(m["orebloc"], m["destination"]) if m else ""
            # vitesse et cap : deux derniers points distincts de l'historique
            pts = conn.execute(
                "SELECT x, y, horodatage FROM positions_dt WHERE code = ? "
                "ORDER BY horodatage DESC LIMIT 6", (e["code"],)).fetchall()
            vitesse, cap = 0.0, None
            for prec in pts[1:]:
                d = distance(pts[0]["x"], pts[0]["y"], prec["x"], prec["y"])
                dt_s = abs(_secondes_depuis(prec["horodatage"], pts[0]["horodatage"]))
                if d > 1.0 and dt_s > 0:
                    vitesse = d / dt_s * 3.6
                    cap = (math.degrees(math.atan2(pts[0]["x"] - prec["x"], pts[0]["y"] - prec["y"])) + 360) % 360
                    break
            vg = vitesse_gnss(e["code"], age_max_s=30)
            fiche["vitesse_source"] = "gnss" if vg is not None else "positions"
            if vg is not None:
                vitesse = vg
            fiche["vitesse_kmh"] = round(min(vitesse, 120), 1)
            fiche["cap_deg"] = None if cap is None else round(cap)
            immo = _secondes_depuis(e["immobilise_depuis"]) / 60.0 if e["immobilise_depuis"] else 0
            fiche["immobile_min"] = round(immo, 1)
            pres = _mqtt_etat["presence"].get(e["code"]) or {}
            en_ligne = pres.get("etat") == "en_ligne" and time.time() - pres.get("t", 0) < MQTT_EN_LIGNE_S
            fiche["mqtt_en_ligne"] = en_ligne
            fiche["etat"] = etat_suivi_dt(e["statut"], fiche["offroad"], age, m, immo, en_ligne)
            compte[fiche["etat"]] = compte.get(fiche["etat"], 0) + 1
        else:
            fiche["etat"] = "minage" if e["statut"] == "OMEX" else "attente"
        engins.append(fiche)

    trajets = {}
    if depuis:
        lignes = conn.execute(
            "SELECT code, x, y, horodatage FROM positions_dt WHERE horodatage >= ? "
            "AND horodatage <= ? ORDER BY code, horodatage",
            (depuis, jusqua or maintenant)).fetchall()
        par_code = {}
        for r in lignes:
            par_code.setdefault(r["code"], []).append(r)
        for code, rs in par_code.items():
            pas = max(1, -(-len(rs) // SUIVI_TRAJET_MAX_POINTS))
            garde = rs[::pas]
            if garde[-1] is not rs[-1]:
                garde.append(rs[-1])
            trajets[code] = [list(_vers_latlng(r["x"], r["y"], zone, hemi)) for r in garde]
    return {"maintenant": maintenant, "engins": engins, "compte": compte, "flotte_dt": len(flotte_dt),
            "periode": {"depuis": depuis or "", "jusqua": jusqua or ""}, "trajets": trajets}


CYCLE_OUVERT_MAX_H = 3          # un cycle « en cours » plus vieux n'est plus « en attente du dumping » mais « non clos »


def kpi_chargements(conn, lignes, jour):
    """Memes KPI que kpi_voyages, calcules sur les lignes « Chargements » de SG.
    Teneur Au (onces) et nature minerai/sterile : table locale oreblocs, par nom
    (le referentiel est pousse depuis Dig Plan). Un orebloc inconnu localement
    est classe par son code de teneur et sa destination, sans onces."""
    # 26/11 : Serveur_SG ecrit le flitch avec un point (257.5_LMP2_...), le DXF avec un souligne (257_5_LMP2_...) :
    # les deux ecritures designent le meme bloc (sinon « bloc inconnu », onces a 0)
    blocs = {}
    for r in conn.execute("SELECT nom, au_teneur, minerai, destination FROM oreblocs ORDER BY actif"):
        blocs[r["nom"]] = r
        blocs[nom_flitch_canonique(r["nom"])] = r
    res = {"jour": jour, "voyages": len(lignes), "minerais_t": 0.0, "waste_t": 0.0, "onces": 0.0,
           "grades": {g: {"tonnes": 0.0, "onces": 0.0} for g in ("high", "medium", "low", "marginal")},
           "sans_bloc": 0, "sans_tonnage": 0, "blocs_inconnus": 0, "attente_dumping": 0, "annules": 0,
           "non_clos": 0, "chargements": len(lignes)}
    # 25/11 (calibrage du dashboard) : « voyages » = dumpings VALIDES (ce qui est compte en production) ;
    # « chargements » garde le total brut.
    res["voyages"] = 0
    limite_ouvert = (datetime.now() - timedelta(hours=CYCLE_OUVERT_MAX_H)).strftime("%Y-%m-%d %H:%M:%S")
    for r in lignes:
        # 25/11 (decision utilisateur) : un chargement n'est compte qu'une fois son DUMPING VALIDE (mission « termine »).
        # Serveur_SG joint le statut de la mission du cycle (mission_statut) ; sans ce champ (SG pas encore redeploye),
        # ancien comportement : compte des le chargement.
        if "mission_statut" in r:
            st = str(r.get("mission_statut") or "").strip()
            if st in ("annulee", "abandonnee"):
                # annulee : cycle efface sur la tablette (reinitialisation, cycle perime) ; abandonnee : mission close
                # sans dumping valide (nouveau chargement, decision du bureau). Aucun des deux ne compte.
                res["annules"] += 1
                continue
            if st != "termine":
                if str(r.get("debut") or "").replace("T", " ")[:19] < limite_ouvert:
                    res["non_clos"] += 1         # cycle reste ouvert depuis des heures : tablette jamais revenue
                else:
                    res["attente_dumping"] += 1  # mission en cours : pas encore de dumping valide
                continue
        res["voyages"] += 1
        nom = str(r.get("orebloc") or "").strip()
        if not nom:
            # 25/11 : chargement hors de tout bloc du Geosum = WASTE (affectation de la tablette : « hors blast ») ;
            # compte en waste s'il a un tonnage, au lieu de rester « non classe »
            # SG ecrit la teneur vide et la remarque « hors de tout orebloc » : regle du projet (23/11) = sterile
            teneur_txt = str(r.get("teneur") or "").strip().upper()
            hors_geosum = "hors de tout orebloc" in str(r.get("remarque") or "").lower()
            if teneur_txt in ("W", "WASTE", "STERILE", "WST") or hors_geosum:
                try:
                    res["waste_t"] += float(r.get("tonnes"))
                except (TypeError, ValueError):
                    res["sans_tonnage"] += 1
                continue
            res["sans_bloc"] += 1          # chargement hors de tout orebloc, sans teneur : non classe
            continue
        try:
            t = float(r.get("tonnes"))
        except (TypeError, ValueError):
            res["sans_tonnage"] += 1       # pas de truck_factor_t : compte, sans tonnage
            continue
        b = blocs.get(nom) or blocs.get(nom_flitch_canonique(nom))
        if b is None:
            res["blocs_inconnus"] += 1
        dest = r.get("teneur") or (b["destination"] if b is not None else "")
        sterile, g = classer_voyage(nom, dest, bool(b["minerai"]) if b is not None else None)
        if sterile:
            res["waste_t"] += t
            continue
        onces = t * float((b["au_teneur"] if b is not None else 0) or 0) / GRAMMES_PAR_ONCE
        res["minerais_t"] += t
        res["onces"] += onces
        if g:
            res["grades"][g]["tonnes"] += t
            res["grades"][g]["onces"] += onces
    arrondir = lambda v: round(v, 1)
    res["minerais_t"], res["waste_t"], res["onces"] = map(arrondir, (res["minerais_t"], res["waste_t"], res["onces"]))
    for g in res["grades"].values():
        g["tonnes"], g["onces"] = arrondir(g["tonnes"]), arrondir(g["onces"])
    return res


# Journee de production : de 06h00 a 06h00 le lendemain (shift jour 06h-18h, shift
# nuit 18h-06h). Un samedi a 03h00 appartient donc a la journee de VENDREDI : les
# KPI et les alertes du dashboard ne melangent jamais deux journees de production.
PRODUCTION_HEURE_DEBUT = 6
_RE_HORODATAGE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


def jour_de_production(maintenant=None):
    """Date (AAAA-MM-JJ) de la journee de production en cours."""
    m = maintenant or datetime.now()
    if m.hour < PRODUCTION_HEURE_DEBUT:
        m = m - timedelta(days=1)
    return m.strftime("%Y-%m-%d")


def fenetre_du_jour(jour):
    """(debut, fin) « AAAA-MM-JJ HH:MM:SS » de la journee de production `jour`."""
    d = datetime.strptime(jour, "%Y-%m-%d").replace(hour=PRODUCTION_HEURE_DEBUT)
    return d.strftime("%Y-%m-%d %H:%M:%S"), (d + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")


_cache_jours_sg = {}          # jour calendaire -> (instant, lignes) pour les jours deja passes


def lignes_chargements_calendaire(jour):
    """Lignes « Chargements » du jour CALENDAIRE `jour` (None si SG illisible).
    Aujourd'hui : le releve en cache (boucle_chargements) ; un autre jour : lecture
    directe, gardee 60 s."""
    if jour == datetime.now().strftime("%Y-%m-%d"):
        with _chargements_lock:
            if _chargements_etat["jour"] == jour and _chargements_etat["lu_ts"]:
                return list(_chargements_etat["lignes"])
        return None
    c = _cache_jours_sg.get(jour)
    if c and time.time() - c[0] < 60:
        return c[1]
    ok, d, _err = sg_lire_chargements(jour)
    if not ok:
        return None
    lignes = d.get("lignes") or []
    _cache_jours_sg[jour] = (time.time(), lignes)
    try:
        voyages_depuis_chargements(lignes)      # 26/11 : un jour passe relu (filtre « shift d'hier ») complete aussi les voyages
    except Exception as e:
        print("VOYAGES: enregistrement depuis SG impossible (%s)" % e)
    return lignes


def lignes_chargements_fenetre(debut, fin):
    """Chargements dont l'heure de debut est dans [debut, fin[ (fin vide = maintenant).
    Une fenetre 06h->06h couvre deux jours calendaires. None si l'un est illisible."""
    fin_eff = fin or now_iso()
    jours, j = [], datetime.strptime(debut[:10], "%Y-%m-%d")
    dernier = datetime.strptime(fin_eff[:10], "%Y-%m-%d")
    while j <= dernier:
        jours.append(j.strftime("%Y-%m-%d"))
        j += timedelta(days=1)
    out = []
    for jc in jours:
        lignes = lignes_chargements_calendaire(jc)
        if lignes is None:
            return None
        for r in lignes:
            h = str(r.get("debut") or "").replace("T", " ")[:19]
            if debut <= h < fin_eff:
                out.append(r)
    return out


def kpi_du_jour(jour=None, debut=None, fin=None):
    """KPI pour /api/kpi sur une FENETRE de production [debut, fin[. Sans parametre : la
    journee de production en cours. `jour` = date d'une journee de production (06h->06h).
    Source : Chargements de SG ; a defaut (SG non configure ou illisible), l'ancienne table
    locale « voyages » (par jour, approximation)."""
    if not debut:
        jour = jour or jour_de_production()
        debut, fin = fenetre_du_jour(jour)
    jour = jour or debut[:10]
    sg_url, sg_token, _ = sg_conf()
    lignes = None
    if sg_url and sg_token:
        lignes = lignes_chargements_fenetre(debut, fin or "")
    conn = get_conn()
    try:
        if lignes is not None:
            res = kpi_chargements(conn, lignes, jour)
            res["source"] = "sg"
        else:
            res = kpi_voyages(conn, None if jour == jour_de_production() else jour)
            res["source"] = "voyages"
    finally:
        conn.close()
    res["debut"], res["fin"] = debut, fin or ""
    res["cloud"] = etat_cloud()
    return res


# ------------------------------------------- MQTT : comptes + positions ---
# Broker Mosquitto du PC (module Dynamic Security), expose aux tablettes par
# Tailscale Funnel. Identifiants de la passerelle (compte d'administration +
# compte « passerelle ») dans un fichier protege, ecrit par la mise en place
# du broker (deploiement/mqtt/windows/migrer_dynsec.ps1).
MQTT_CONF_FICHIER = os.environ.get("OT_MQTT_CONF", r"C:\ProgramData\OreTracking\passerelle\mqtt.json")
SAAS_DB_PATH = os.path.join(DATA_DIR, "oretracking.db")
FAMILLES_DT = ("1500", "785", "ADT")
MQTT_COMPTES_INTERVALLE_S = 30
MQTT_EN_LIGNE_S = 120          # « en ligne » = (presence en_ligne OU position) recue il y a < 2 min
_mqtt_etat = {"comptes_actif": False, "suivi_actif": False, "dernier_rapprochement": "",
              "erreur": "", "resume": {}, "ot_envoye_le": "", "ot_erreur": "",
              "abonne": False, "messages": 0, "presence": {}, "derniere_position": {}}
_mqtt_reveil = threading.Event()
_mqtt_rapprochement = threading.Lock()


def mqtt_conf():
    try:
        with open(MQTT_CONF_FICHIER, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


class DynsecAdmin:
    """Commandes $CONTROL/dynamic-security/v1 (memes regles que
    deploiement/mqtt/windows/dynsec.py)."""
    SUJET = "$CONTROL/dynamic-security/v1"

    def __init__(self, conf, timeout=8):
        if mqtt is None:
            raise RuntimeError("paho-mqtt absent")
        self.timeout, self._rep, self._rc = timeout, {}, None
        self._ev = threading.Event()
        c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                        client_id="passerelle-admin-%d" % os.getpid(), protocol=mqtt.MQTTv311)
        c.username_pw_set(conf["admin"]["identifiant"], conf["admin"]["mdp"])
        c.on_connect = self._connecte
        c.on_message = self._message
        self.c = c
        c.connect(conf.get("hote", "127.0.0.1"), int(conf.get("port", 1883)), keepalive=30)
        c.loop_start()
        t0 = time.time()
        while self._rc is None and time.time() - t0 < timeout:
            time.sleep(0.05)
        if self._rc is None or self._rc.is_failure:
            self.fermer()
            raise RuntimeError("administration du broker refusee : %s" % self._rc)
        time.sleep(0.1)

    def _connecte(self, cl, u, f, rc, p=None):
        self._rc = rc
        if not rc.is_failure:
            cl.subscribe(self.SUJET + "/response", qos=1)

    def _message(self, cl, u, m):
        try:
            for r in json.loads(m.payload.decode("utf-8")).get("responses", []):
                self._rep[r.get("correlationData")] = r
        except ValueError:
            pass
        self._ev.set()

    def commande(self, nom, **champs):
        cid = secrets.token_hex(8)
        self.c.publish(self.SUJET, json.dumps({"commands": [dict(command=nom, correlationData=cid, **champs)]}), qos=1)
        t0 = time.time()
        while cid not in self._rep and time.time() - t0 < self.timeout:
            self._ev.wait(0.2); self._ev.clear()
        r = self._rep.pop(cid, None)
        if r is None:
            return False, {"error": "pas de reponse du broker"}
        err = str(r.get("error") or "")
        return (not err or "already exists" in err), r

    def lister(self):
        ok, r = self.commande("listClients", verbose=True)
        if not ok:
            raise RuntimeError("liste des comptes impossible : %s" % r.get("error"))
        return (r.get("data") or {}).get("clients") or []

    def fermer(self):
        try:
            self.c.loop_stop(); self.c.disconnect()
        except Exception:
            pass


def _empreinte(t):
    return hashlib.sha256(str(t).encode("utf-8")).hexdigest()[:16]


def flotte_dt_saas():
    """DT de la flotte du SaaS (sync_flotte), en lecture seule. None si la
    base du SaaS est introuvable."""
    if not os.path.exists(SAAS_DB_PATH):
        return None
    uri = "file:%s?mode=ro" % urllib.request.pathname2url(SAAS_DB_PATH)
    c = sqlite3.connect(uri, uri=True, timeout=5)
    c.row_factory = sqlite3.Row
    try:
        rows = c.execute("SELECT code, famille, numero, actif FROM sync_flotte").fetchall()
    finally:
        c.close()
    out = {}
    for r in rows:
        famille = str(r["famille"] or "").strip().upper()
        code = str(r["code"] or "").strip()
        if not code or not (famille in FAMILLES_DT or code.upper().startswith("DT-")):
            continue
        out[code] = {"code": code, "famille": famille, "numero": int(r["numero"] or 0),
                     "actif": 1 if str(r["actif"]) in ("1", "True", "true") or r["actif"] is True else 0}
    return out


def ot_envoyer_comptes_mqtt(conn, conf, forcer=False):
    """Envoie a Serveur_OT les acces des DT (onglet mqtt_comptes) et
    l'adresse publique du broker, seulement s'ils ont change."""
    comptes = [{"code": r["code"], "famille": r["famille"], "numero": r["numero"],
                "mdp": r["mdp"], "actif": r["actif"]}
               for r in conn.execute("SELECT * FROM mqtt_comptes ORDER BY code")]
    pub = conf.get("public") or {}
    corps = {"comptes": comptes, "mqtt": {"hote": pub.get("hote", ""), "port": int(pub.get("port", 443)),
                                          "tls": bool(pub.get("tls", True))}}
    empreinte = _empreinte(json.dumps(corps, sort_keys=True))
    if not forcer and param_lire("MQTT_OT_EMPREINTE", "") == empreinte:
        return True
    office_url, _ = office_conf()
    if not office_url:
        _mqtt_etat["ot_erreur"] = "Serveur_OT non configure (Parametres > Liaisons)"
        return False
    if office_appel("mqtt_comptes", corps):
        param_ecrire(conn, "MQTT_OT_EMPREINTE", empreinte)
        _mqtt_etat.update(ot_envoye_le=now_iso(), ot_erreur="")
        return True
    _mqtt_etat["ot_erreur"] = "Serveur_OT a refuse ou n'a pas repondu (action mqtt_comptes deployee ?)"
    return False


def rapprocher_comptes_mqtt(forcer_ot=False):
    """Flotte du SaaS -> table mqtt_comptes -> broker (a chaud) -> Serveur_OT.
    Rend un resume. Ne leve pas."""
    with _mqtt_rapprochement:
        resume = {"crees": 0, "modifies": 0, "supprimes": 0, "broker": "", "ot": ""}
        conf = mqtt_conf()
        if not conf:
            _mqtt_etat["erreur"] = "broker non installe (fichier %s absent)" % MQTT_CONF_FICHIER
            return dict(resume, ok=False, erreur=_mqtt_etat["erreur"])
        flotte = flotte_dt_saas()
        if flotte is None:
            _mqtt_etat["erreur"] = "base du SaaS introuvable (%s)" % SAAS_DB_PATH
            return dict(resume, ok=False, erreur=_mqtt_etat["erreur"])
        conn = get_conn()
        try:
            maintenant = now_iso()
            locaux = {r["code"]: dict(r) for r in conn.execute("SELECT * FROM mqtt_comptes")}
            for code, d in flotte.items():
                l = locaux.get(code)
                if l is None:
                    conn.execute("INSERT INTO mqtt_comptes (code, famille, numero, mdp, actif, cree_le, maj_le) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                 (code, d["famille"], d["numero"], secrets.token_urlsafe(18), d["actif"],
                                  maintenant, maintenant))
                    resume["crees"] += 1
                elif (l["actif"], l["famille"], l["numero"]) != (d["actif"], d["famille"], d["numero"]):
                    conn.execute("UPDATE mqtt_comptes SET actif=?, famille=?, numero=?, maj_le=? WHERE code=?",
                                 (d["actif"], d["famille"], d["numero"], maintenant, code))
                    resume["modifies"] += 1
            for code in set(locaux) - set(flotte):
                conn.execute("DELETE FROM mqtt_comptes WHERE code=?", (code,))
                resume["supprimes"] += 1
            conn.commit()
            lignes = [dict(r) for r in conn.execute("SELECT * FROM mqtt_comptes")]

            # broker : a chaud, sans redemarrage
            try:
                adm = DynsecAdmin(conf)
            except Exception as e:
                resume["broker"] = str(e)
            else:
                try:
                    clients = {c.get("username"): c for c in adm.lister()}
                    techniques = set(conf.get("comptes_techniques") or [])
                    for r in lignes:
                        c, h = clients.get(r["code"]), _empreinte(r["mdp"])
                        if c is None:
                            adm.commande("createClient", username=r["code"], password=r["mdp"],
                                         roles=[{"rolename": "dt"}])
                            if not r["actif"]:
                                adm.commande("disableClient", username=r["code"])
                            conn.execute("UPDATE mqtt_comptes SET mdp_applique=? WHERE code=?", (h, r["code"]))
                            continue
                        if r["mdp_applique"] != h:
                            adm.commande("setClientPassword", username=r["code"], password=r["mdp"])
                            conn.execute("UPDATE mqtt_comptes SET mdp_applique=? WHERE code=?", (h, r["code"]))
                        if bool(c.get("disabled")) == bool(r["actif"]):
                            adm.commande("enableClient" if r["actif"] else "disableClient", username=r["code"])
                    codes = {r["code"] for r in lignes}
                    for nom, c in clients.items():
                        roles = {x.get("rolename") for x in c.get("roles") or []}
                        if nom not in codes and nom not in techniques and "dt" in roles:
                            adm.commande("deleteClient", username=nom)
                    conn.commit()
                    resume["broker"] = "ok"
                except Exception as e:
                    resume["broker"] = str(e)
                finally:
                    adm.fermer()

            resume["ot"] = "ok" if ot_envoyer_comptes_mqtt(conn, conf, forcer_ot) else _mqtt_etat["ot_erreur"]
        finally:
            conn.close()
        _mqtt_etat.update(dernier_rapprochement=now_iso(), resume=resume,
                          erreur="" if resume["broker"] == "ok" else "broker : " + resume["broker"])
        return dict(resume, ok=resume["broker"] == "ok")


def renouveler_mdp_mqtt(code):
    conn = get_conn()
    try:
        n = conn.execute("UPDATE mqtt_comptes SET mdp=?, maj_le=? WHERE code=?",
                         (secrets.token_urlsafe(18), now_iso(), code)).rowcount
        conn.commit()
    finally:
        conn.close()
    if not n:
        return {"ok": False, "erreur": "DT inconnu : %s" % code}
    return rapprocher_comptes_mqtt(forcer_ot=True)


def comptes_mqtt_etat():
    """Etat des comptes pour Parametres > Flotte : jamais de mot de passe."""
    conn = get_conn()
    try:
        lignes = [dict(r) for r in conn.execute(
            "SELECT code, famille, numero, actif, mdp_applique, cree_le, maj_le FROM mqtt_comptes ORDER BY code")]
        # derniere position recue par N'IMPORTE QUEL canal (MQTT ou HTTP/SG 4G) : une tablette dont
        # MQTT est tombe mais qui emet encore ses positions est bel et bien connectee.
        maj_engins = {r["code"]: r["derniere_maj"] for r in conn.execute(
            "SELECT code, derniere_maj FROM engins WHERE actif=1")}
    finally:
        conn.close()
    maintenant = time.time()
    for l in lignes:
        l["position_recente"] = age_position_s(maj_engins.get(l["code"])) <= POSITION_FRAICHE_S
        pres = _mqtt_etat["presence"].get(l["code"]) or {}
        pos = _mqtt_etat["derniere_position"].get(l["code"])
        recent = pos is not None and maintenant - pos < MQTT_EN_LIGNE_S
        pres_recente = pres.get("etat") == "en_ligne" and maintenant - pres.get("t", 0) < MQTT_EN_LIGNE_S
        l["applique_broker"] = bool(l.pop("mdp_applique"))
        # « hors_ligne » recu APRES la derniere position = deconnexion explicite :
        # la position, meme recente, ne compte plus.
        deconnecte = pres.get("etat") == "hors_ligne" and (pos is None or pres.get("t", 0) >= pos)
        l["en_ligne"] = pres_recente or (recent and not deconnecte)
        l["mqtt_en_ligne"] = l["en_ligne"]
        # connectee = MQTT OU position fraiche (tous canaux) ; mqtt_en_ligne dit si le canal temps reel est vivant
        l["en_ligne"] = l["en_ligne"] or l["position_recente"]
        l["presence"] = pres.get("etat", "")
        l["derniere_position_s"] = None if pos is None else int(maintenant - pos)
    conf = mqtt_conf() or {}
    return {"configure": bool(conf), "public": conf.get("public") or {},
            "dernier_rapprochement": _mqtt_etat["dernier_rapprochement"], "erreur": _mqtt_etat["erreur"],
            "ot_envoye_le": _mqtt_etat["ot_envoye_le"], "ot_erreur": _mqtt_etat["ot_erreur"],
            "abonne": _mqtt_etat["abonne"], "messages": _mqtt_etat["messages"],
            "publication": dict(_mqtt_desc_etat, active=_MQTT_CLIENT is not None), "comptes": lignes}


def boucle_comptes_mqtt():
    _mqtt_etat["comptes_actif"] = True
    while True:
        try:
            if mqtt_conf():
                rapprocher_comptes_mqtt()
        except Exception as e:
            _mqtt_etat["erreur"] = "%s: %s" % (type(e).__name__, e)
            print("MQTT comptes : erreur %s" % e)
        try:
            jetons_diffuser()
        except Exception as e:
            print("Jetons des tablettes : erreur %s" % e)
        try:
            reglages_diffuser()
        except Exception as e:
            print("Reglages SG : erreur %s" % e)
        try:
            connexion_diffuser()
        except Exception as e:
            _connexion_etat["erreur"] = "%s: %s" % (type(e).__name__, e)
            print("Connexion des tablettes : erreur %s" % e)
        _mqtt_reveil.wait(MQTT_COMPTES_INTERVALLE_S)
        _mqtt_reveil.clear()


def _instant_local(at):
    """ISO 8601 UTC (« ...Z ») -> « AAAA-MM-JJ HH:MM:SS » heure locale."""
    try:
        d = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        if d.tzinfo is not None:
            d = d.astimezone().replace(tzinfo=None)
        return d.strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def appliquer_message_mqtt(sujet, charge):
    """camions/<code>/position|connexion. Le code vient du SUJET : le broker
    garantit qu'un DT ne publie que sous son propre code."""
    parties = sujet.split("/")
    if len(parties) != 3 or parties[0] != "camions":
        return
    code, nature = parties[1], parties[2]
    if not charge:
        return                                   # effacement d'un message conserve
    try:
        d = json.loads(charge.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return
    _mqtt_etat["messages"] += 1
    if nature == "connexion":
        _mqtt_etat["presence"][code] = {"etat": str(d.get("etat") or ""), "le": now_iso(), "t": time.time()}
        return
    if nature == "evenement":
        traiter_evenement_tablette(code, d)
        return
    if nature != "position":
        return
    if d.get("geo"):
        _geo_tablettes[code] = {"version": str(d.get("geo")), "t": time.time()}
    try:
        x, y = float(d["x"]), float(d["y"])
    except (KeyError, TypeError, ValueError):
        return
    instant = _instant_local(d.get("at")) or now_iso()
    _mqtt_etat["derniere_position"][code] = time.time()
    conn = get_conn()
    try:
        LOCK.route("mqtt position %s" % code)
        with LOCK:
            exc = d.get("exc") or {}
            if exc.get("id") and exc.get("x") is not None and exc.get("y") is not None:
                try:
                    appliquer_position_engin(conn, str(exc["id"]), "EXCA", float(exc["x"]), float(exc["y"]),
                                             horodatage=instant, relayer=False, canal="mqtt-relais", auth="aucune")
                except (TypeError, ValueError):
                    pass
            if d.get("vit_src") == "gnss" and d.get("vit_kmh") is not None:
                noter_vitesse_gnss(code, d.get("vit_kmh"), instant)
            appliquer_position_engin(conn, code, "DT", x, y, horodatage=instant, relayer=False, canal="mqtt", auth="broker")
    finally:
        conn.close()


def boucle_suivi_mqtt():
    """Abonnement de la passerelle aux positions et presences des DT."""
    import queue
    _mqtt_etat["suivi_actif"] = True
    file_msgs = queue.Queue(maxsize=5000)

    def travailleur():
        while True:
            sujet, charge = file_msgs.get()
            try:
                appliquer_message_mqtt(sujet, charge)
            except Exception as e:
                print("MQTT suivi : message ignore (%s)" % e)

    threading.Thread(target=travailleur, daemon=True).start()
    while True:
        conf = mqtt_conf()
        if mqtt is None or not conf:
            time.sleep(60)
            continue
        try:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="passerelle-suivi",
                            protocol=mqtt.MQTTv311, clean_session=True)
            c.username_pw_set(conf["passerelle"]["identifiant"], conf["passerelle"]["mdp"])

            def connecte(cl, u, f, rc, p=None):
                _mqtt_etat["abonne"] = not rc.is_failure
                if not rc.is_failure:
                    cl.subscribe([("camions/+/position", 0), ("camions/+/connexion", 1), ("camions/+/evenement", 1)])

            def deconnecte(cl, u, f, rc, p=None):
                _mqtt_etat["abonne"] = False

            def message(cl, u, m):
                try:
                    file_msgs.put_nowait((m.topic, m.payload))
                except queue.Full:
                    pass                         # surcharge : on perd des positions, jamais la passerelle

            c.on_connect, c.on_disconnect, c.on_message = connecte, deconnecte, message
            c.reconnect_delay_set(1, 30)
            c.connect(conf.get("hote", "127.0.0.1"), int(conf.get("port", 1883)), keepalive=30)
            c.loop_forever(retry_first_connection=True)
        except Exception as e:
            _mqtt_etat["abonne"] = False
            print("MQTT suivi : %s" % e)
            time.sleep(10)


def vider_file_office(conn):
    """Vide la file office_sync vers Serveur_OT. Statut 'envoye' seulement
    si OT a reellement accuse reception (ok:true).

    - evenements terrain -> ?action=evenement (type + UUID stable) ;
    - le reste -> ?action=notification (texte), comme avant.

    Les entrees en 'echec' de moins de 24 h sont REJOUEES (avant le 16/11,
    elles etaient abandonnees malgre le commentaire). Grace a l'UUID stable,
    rejouer un evenement est sans risque. Au premier echec reseau, on arrete
    le passage : inutile d'enchainer des appels de 10 s vers un OT injoignable.
    Appelee par le thread `boucle_office`, hors du verrou global."""
    office_url, _ = office_conf()
    if not office_url:
        return
    maintenant = datetime.now()
    entrees = [dict(r) for r in conn.execute(
        "SELECT * FROM office_sync WHERE statut IN ('en_attente', 'echec') "
        "AND COALESCE(tentatives, 0) < ? "
        "AND created_at >= datetime('now', ?) "
        # les notifications au chauffeur d'abord, meme derriere un long arriere
        "ORDER BY (payload LIKE '%\"chauffeur\"%') DESC, id LIMIT ?",
        (OFFICE_TENTATIVES_MAX, "-%d hours" % OFFICE_FENETRE_REESSAI_H, OFFICE_LOT_MAX))]
    for e in entrees:
        if e["statut"] == "echec" and e.get("derniere_tentative"):
            try:
                ecoule = (maintenant - datetime.strptime(
                    e["derniere_tentative"], "%Y-%m-%d %H:%M:%S")).total_seconds()
            except ValueError:
                ecoule = OFFICE_REESSAI_MAX_S
            if ecoule < office_delai_reessai(e.get("tentatives")):
                continue
        ok = False
        try:
            payload = json.loads(e["payload"])
            if isinstance(payload.get("chauffeur"), dict) and office_age_s(e) > OFFICE_CHAUFFEUR_MAX_S:
                conn.execute("UPDATE office_sync SET statut='perime', erreur=? WHERE id=?",
                             ("notification au chauffeur perimee (non envoyee apres %d s)" % OFFICE_CHAUFFEUR_MAX_S,
                              e["id"]))
                conn.commit()
                continue
            typage = office_evenement(payload)
            if typage:
                event_id = e.get("event_id") or office_event_id(e)
                ok = office_appel("evenement", {
                    "event_id": event_id,
                    "event_type": typage[0],
                    "source_device": "PASSERELLE",
                    "gateway_id": "SENOR_GEOLOGY",
                    "timestamp": (e.get("created_at") or now_iso()).replace(" ", "T"),
                    "payload": typage[1],
                })
                if not e.get("event_id"):
                    conn.execute("UPDATE office_sync SET event_id=? WHERE id=?",
                                 (event_id, e["id"]))
            elif isinstance(payload.get("chauffeur"), dict):
                c = payload["chauffeur"]
                ok = office_appel("notification",
                                  {"profil": c.get("profil", ""), "message": c.get("message", ""),
                                   "uid": office_notification_uid(e)})
            else:
                ok = office_appel("notification",
                                  {"profil": "", "message": office_message(payload),
                                   "uid": office_notification_uid(e)})
        except Exception as ex:
            _office_derniere_erreur.update(erreur="traitement : %s" % ex, le=now_iso(), action="")
            print("OFFICE: erreur traitement office_sync#%s: %s" % (e["id"], ex))
        conn.execute(
            "UPDATE office_sync SET statut=?, envoie_le=?, derniere_tentative=?, "
            "tentatives=COALESCE(tentatives, 0) + 1, erreur=? WHERE id=?",
            ("envoye" if ok else "echec", now_iso() if ok else "", now_iso(),
             "" if ok else _office_derniere_erreur["erreur"], e["id"]))
        conn.commit()
        if not ok:
            break       # OT injoignable ou refus : on reessaiera au prochain passage
    conn.commit()


def office_age_s(entree):
    """Age (s) d'une entree office_sync (created_at est en UTC : datetime('now') de SQLite)."""
    try:
        cree = datetime.strptime(str(entree.get("created_at") or ""), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0.0
    return max(0.0, (datetime.utcnow() - cree).total_seconds())


def office_notification_uid(entree):
    """24/11 (E3) : cle d'idempotence d'une notification, stable d'un essai a l'autre (meme ligne office_sync).
    Serveur_OT (version du 24/11 (2)) ne recree pas une notification deja ecrite avec ce uid ; une version
    plus ancienne ignore le champ."""
    base = "%s|%s|%s" % (entree.get("id"), entree.get("created_at") or "", entree.get("payload") or "")
    return "PSG-%s-%s" % (entree.get("id"), hashlib.sha256(base.encode("utf-8")).hexdigest()[:12])


def enregistrer_pour_office(conn, payload_dict):
    """Enfile un payload pour le serveur Office."""
    conn.execute(
        "INSERT INTO office_sync (payload, statut) VALUES (?, 'en_attente')",
        (json.dumps(payload_dict, ensure_ascii=True),))
    conn.commit()


# ------------------------------------------ alerte vers la tablette du DT ---
# Contrat convenu avec la Zone 2 le 17/11 (REPONSE_ZONE1_A_ZONE2_2026-11-17) :
# en plus de l'evenement type (KPI, historique), chaque changement d'etat
# offroad / deviation / retour_* est depose dans l'onglet `notifications` de
# Serveur_OT, `profil` = code du DT, `message` = "[type] TEXTE ASCII". L'app
# DT lit ?action=notifications, garde les lignes de son code et les dit a
# voix haute. Pas d'accuse de lecture. Desactivable : ALERTES_CHAUFFEUR=0.
# 24/11 (decision utilisateur) : UN SEUL BUT, eviter un dumping au mauvais endroit ; UNE SEULE ALERTE au chauffeur,
# « mauvaise_direction » (+ demi_tour, retour_direction). Sortie de route, deviation, checkpoint saute / interdit,
# zones : enregistrees pour le BUREAU seulement (page Alertes : statuts Off-route / Wrong Dumping).
TYPES_ALERTE_CHAUFFEUR = ("mauvaise_direction", "demi_tour", "retour_direction")
# Classement du bureau (page Alertes) : chaque type d'alerte -> statut.
STATUT_ALERTE = {
    "offroad": "off_route", "retour_piste": "off_route", "deviation": "off_route", "retour_trajet": "off_route",
    "checkpoint": "off_route",
    "mauvaise_direction": "wrong_dumping", "demi_tour": "wrong_dumping", "retour_direction": "wrong_dumping",
    "checkpoint_interdit": "wrong_dumping", "zone_approche": "wrong_dumping", "zone_interdite": "wrong_dumping",
    "dumping_hors_zone": "wrong_dumping", "non_conforme": "wrong_dumping",
}


def statut_alerte(type_alerte):
    """Statut du bureau d'une alerte : 'off_route', 'wrong_dumping' ou '' (autres : mission, arrivee...)."""
    return STATUT_ALERTE.get(str(type_alerte or ""), "")


def alertes_chauffeur_actives():
    return param_lire("ALERTES_CHAUFFEUR", "1").strip().lower() not in ("0", "non", "false", "off")


def texte_ascii(texte):
    """Sans accents ni caracteres speciaux : lu tel quel par la synthese
    vocale de la tablette, quelle que soit sa langue."""
    t = unicodedata.normalize("NFKD", str(texte or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", t).strip()


def message_chauffeur(evenement):
    """[type] message, ou None si ce n'est pas une alerte pour le chauffeur.
    24/11 : la passerelle n'alerte plus le chauffeur (la tablette decide) sauf SERVEUR_CONTROLE_DIRECTION = 1."""
    if not serveur_controle_direction():
        return None
    typ = (evenement or {}).get("type")
    if typ not in TYPES_ALERTE_CHAUFFEUR or not evenement.get("dt_code"):
        return None
    return "[%s] %s" % (typ, texte_ascii(evenement.get("message")))


def enregistrer_alerte_office(conn, evenement):
    """Enfile l'evenement type pour OT, puis la notification au chauffeur
    (entree separee : un echec de l'une ne bloque ni ne rejoue l'autre)."""
    # 20/11 : le CHAUFFEUR d'abord. Avant, l'evenement du bureau partait en premier (un appel Apps
    # Script de 3 a 6 s) puis la notification ; et rien ne reveillait le thread d'envoi (attente
    # jusqu'a 30 s). Maintenant : notification en tete de file, envoi declenche immediatement.
    message = message_chauffeur(evenement)
    if message and alertes_chauffeur_actives():
        enregistrer_pour_office(conn, {"chauffeur": {
            "profil": evenement["dt_code"], "message": message, "type": evenement["type"]}})
    enregistrer_pour_office(conn, {"alerte": evenement, "action": evenement["type"]})
    if _office_thread_actif["oui"]:
        _office_reveil.set()               # sans attendre (jamais d'appel reseau ici : on est sous le verrou)


# ------------------------------------------------------------------ tablettes (adb) ---
# Detection automatique des tablettes branchees (USB) ou connectees (Wi-Fi), affectation a un DT et
# installation de l'APK, depuis la console. adb est lance SUR CE POSTE : aucun service cloud n'est concerne.
# Une installation = une famille de camion = un id DT : la console pousse `provision.json` dans le dossier
# de l'app (lu par ProvisionPlugin cote tablette), l'app s'ouvre directement sur ce DT.
# Les 4 applications : un profil = une application. L'identite fixee par la console depend du profil.
PROFILS = {
    "dt":      {"nom": "DT", "paquet": "com.lafigue.oretracking.dt", "apk": r"^ore-dt-.*\.apk$"},
    "exc":     {"nom": "Excavatrice", "paquet": "com.lafigue.oretracking.excavatrice",
                "apk": r"^ore-(exc|excavator|excavatrice)-.*\.apk$"},
    "spotter": {"nom": "Spotter", "paquet": "com.oretracking.ore_tracking.spotter", "apk": r"^ore-spotter-.*\.apk$"},
    "link":    {"nom": "Link", "paquet": "com.oretracking.link", "apk": r"^OreTracking-Link-.*\.apk$"},
}
PAQUET_DT = PROFILS["dt"]["paquet"]
PORT_ADB_WIFI = 5555
_RE_ADRESSE = re.compile(r"^(\d{1,3}(?:\.\d{1,3}){3})(?::(\d{2,5}))?$")
_RE_SERIE = re.compile(r"^[A-Za-z0-9._:\-]{3,64}$")
_jobs_tablettes = {}
_jobs_verrou = threading.Lock()


def adb_chemin():
    """adb : reglage ADB_CHEMIN, sinon le PATH, sinon le SDK Android (ANDROID_SDK_ROOT / ANDROID_HOME)."""
    c = param_lire("ADB_CHEMIN", "").strip()
    if c and os.path.isfile(c):
        return c
    trouve = shutil.which("adb")
    if trouve:
        return trouve
    for var in ("ANDROID_SDK_ROOT", "ANDROID_HOME"):
        base = os.environ.get(var, "")
        for nom in ("adb.exe", "adb"):
            f = os.path.join(base, "platform-tools", nom)
            if base and os.path.isfile(f):
                return f
    return ""


def adb_lancer(args, serie=None, timeout=25):
    """Lance adb [-s serie] args. Rend (code, sortie, erreur) ; code -1 = adb introuvable / delai depasse."""
    exe = adb_chemin()
    if not exe:
        return -1, "", "adb introuvable (reglage ADB_CHEMIN, PATH ou SDK Android)"
    cmd = [exe] + (["-s", serie] if serie else []) + list(args)
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=flags,
                           encoding="utf-8", errors="replace")
        return r.returncode, (r.stdout or "").replace("\r", ""), (r.stderr or "").replace("\r", "").strip()
    except subprocess.TimeoutExpired:
        return -1, "", "delai depasse (%d s) : %s" % (timeout, " ".join(args[:3]))
    except OSError as e:
        return -1, "", str(e)


def adb_peripheriques():
    """`adb devices -l` -> [{serie, etat, transport, modele}] ; transport = usb | wifi."""
    adb_lancer(["start-server"], timeout=15)
    code, sortie, err = adb_lancer(["devices", "-l"], timeout=15)
    if code != 0:
        return [], err or "adb a echoue"
    res = []
    for ligne in sortie.split("\n")[1:]:
        parts = ligne.split()
        if len(parts) < 2:
            continue
        info = {kv.split(":", 1)[0]: kv.split(":", 1)[1] for kv in parts[2:] if ":" in kv}
        res.append({"serie": parts[0], "etat": parts[1],
                    "transport": "wifi" if _RE_ADRESSE.match(parts[0]) else "usb",
                    "modele": info.get("model", "").replace("_", " ")})
    return res, ""


_SCRIPT_INFOS = (
    "echo modele=$(getprop ro.product.model); echo marque=$(getprop ro.product.manufacturer); "
    "echo android=$(getprop ro.build.version.release); echo serie=$(getprop ro.serialno); "
    "echo tcp=$(getprop service.adb.tcp.port); "
    "echo ip=$(ip -f inet addr show 2>/dev/null | grep 'inet ' | grep -v ' 127[.]' | head -1); "
    "echo batterie=$(dumpsys battery | grep level | head -1); "
    "for p in " + " ".join(x["paquet"] for x in PROFILS.values()) + "; do echo v:$p=$(dumpsys package $p | grep versionName | head -1); done; "
    "echo paquets=$(pm list packages | grep -i oretracking | tr '\\n' ' ')"
)


def adb_infos(serie):
    """Informations d'une tablette (une seule commande shell) : modele, Android, IP Wi-Fi, batterie, app DT."""
    code, sortie, _ = adb_lancer(["shell", _SCRIPT_INFOS], serie=serie, timeout=20)
    if code != 0:
        return {}
    d = {}
    for ligne in sortie.split("\n"):
        if "=" in ligne:
            k, v = ligne.split("=", 1)
            d[k.strip()] = v.strip()
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", d.get("ip", ""))
    b = re.search(r"(\d+)", d.get("batterie", ""))
    versions = {}
    for k, val in d.items():
        mv = re.search(r"versionName=(\S+)", val)
        if k.startswith("v:") and mv:
            versions[k[2:]] = mv.group(1)
    return {"modele": ((d.get("marque", "") + " " + d.get("modele", "")).strip()),
            "android": d.get("android", ""), "numero_serie": d.get("serie", ""),
            "ip": m.group(1) if m else "", "wifi_ouvert": d.get("tcp", "") == str(PORT_ADB_WIFI),
            "batterie": int(b.group(1)) if b else None,
            "versions": versions,
            "paquets": [x.replace("package:", "") for x in d.get("paquets", "").split()]}


def dossier_apk():
    """Dossier des APK : reglage APK_DOSSIER, sinon <poste>/apk, sinon le depot de l'app DT voisin."""
    c = param_lire("APK_DOSSIER", "").strip()
    candidats = [c] if c else []
    candidats += [os.path.join(BASE_DIR, "apk"),
                  os.path.join(os.path.dirname(BASE_DIR), "OT2-Tab-goodJob-DT", "apk")]
    for d in candidats:
        if d and os.path.isdir(d):
            return d
    return candidats[0] if candidats else ""


def apk_disponibles(profil="dt"):
    """APK du profil dans le dossier, du plus recent au plus ancien."""
    d = dossier_apk()
    res = []
    if d and os.path.isdir(d):
        for nom in os.listdir(d):
            if re.match(PROFILS[profil]["apk"], nom, re.I):
                f = os.path.join(d, nom)
                res.append({"nom": nom, "taille_mo": round(os.path.getsize(f) / 1048576, 1),
                            "date": datetime.fromtimestamp(os.path.getmtime(f)).strftime("%Y-%m-%d %H:%M"),
                            "test": nom.lower().endswith("-test.apk"), "chemin": f})
    res.sort(key=lambda a: a["date"], reverse=True)
    return d, res


def apk_retenu(autoriser_test=False, profil="dt"):
    """APK a installer : le dernier de PRODUCTION ; un APK de test (WebView deboguable) seulement si autorise."""
    _, liste = apk_disponibles(profil)
    prod = [a for a in liste if not a["test"]]
    if prod:
        return prod[0], ""
    if autoriser_test and liste:
        return liste[0], ""
    if liste:
        return None, "aucun APK de production (seulement des APK de test) : construisez-le ou autorisez l'APK de test"
    return None, "aucun APK %s dans %s" % (PROFILS[profil]["nom"], dossier_apk() or "le dossier des APK")


def zone_utm_site(conn):
    z, h = repere_site(conn)
    return ("%s%s" % (z, h)) if z else ""


def _etat_apps(paquets, versions):
    return {pr: {"installee": d["paquet"] in paquets or d["paquet"] in versions, "version": versions.get(d["paquet"], "")}
            for pr, d in PROFILS.items()}


def tablettes_lister(conn):
    """Tablettes detectees (adb) + tablettes connues (base), avec leur profil et leur identite."""
    periph, erreur = adb_peripheriques()
    connues = {r["serial"]: dict(r) for r in conn.execute("SELECT * FROM tablettes")}
    vus, tablettes = set(), []

    def fiche(serie, adresse, etat, transports, wifi, modele, info, k):
        apps = _etat_apps(info.get("paquets", []), info.get("versions", {}))
        profil = k.get("profil") or "dt"
        return {
            "serie": serie, "adresse_adb": adresse, "etat": etat, "connectee": etat == "device",
            "transports": transports, "adresse_wifi": wifi, "modele": modele, "android": info.get("android", ""),
            "ip": info.get("ip", ""), "wifi_ouvert": info.get("wifi_ouvert", False), "batterie": info.get("batterie"),
            "profil": profil, "ident": k.get("ident", ""), "famille": k.get("famille", ""), "equipe": k.get("equipe", ""),
            "apps": apps, "app_installee": apps[profil]["installee"], "app_version": apps[profil]["version"],
            "apk": k.get("apk", ""), "installee_le": k.get("installee_le", "")}

    for p in periph:
        info = adb_infos(p["serie"]) if p["etat"] == "device" else {}
        serie = info.get("numero_serie") or p["serie"]
        if serie in vus:
            # meme tablette branchee en USB ET en Wi-Fi : une seule ligne, le Wi-Fi est note
            for t in tablettes:
                if t["serie"] == serie:
                    t["transports"] = sorted(set(t["transports"] + [p["transport"]]))
                    if p["transport"] == "wifi":
                        t["adresse_wifi"] = p["serie"]
            continue
        vus.add(serie)
        k = connues.get(serie, {})
        modele = info.get("modele") or p["modele"] or k.get("nom", "")
        tablettes.append(fiche(serie, p["serie"], p["etat"], [p["transport"]],
                               p["serie"] if p["transport"] == "wifi" else "", modele, info, k))
        conn.execute(
            "INSERT INTO tablettes (serial, nom, derniere_vue) VALUES (?, ?, datetime('now','localtime')) "
            "ON CONFLICT(serial) DO UPDATE SET nom=excluded.nom, derniere_vue=excluded.derniere_vue", (serie, modele))
    conn.commit()
    for serie, k in connues.items():          # connues mais absentes : on garde leur affectation
        if serie not in vus:
            tablettes.append(fiche(serie, "", "absente", [], "", k.get("nom", ""), {}, k))
    dossier = dossier_apk()
    profils = {}
    for pr, d in PROFILS.items():
        _, apks = apk_disponibles(pr)
        retenu, motif = apk_retenu(False, pr)
        profils[pr] = {"nom": d["nom"], "retenu": retenu["nom"] if retenu else "", "motif": motif,
                       "fichiers": [{k: v for k, v in a.items() if k != "chemin"} for a in apks]}
    return {"adb": {"chemin": adb_chemin(), "erreur": erreur}, "tablettes": tablettes,
            "apk": {"dossier": dossier, "profils": profils},
            "connexion": {"envoyee_le": _connexion_etat["envoyee_le"], "erreur": _connexion_etat["erreur"]}}


def tablette_ouvrir_wifi(serie):
    """« Ouverture du port » : adb tcpip 5555 sur la tablette (USB), puis adb connect <ip>:5555."""
    code, _, err = adb_lancer(["tcpip", str(PORT_ADB_WIFI)], serie=serie, timeout=20)
    if code != 0:
        return {"ok": False, "erreur": err or "adb tcpip a echoue (la tablette doit etre branchee en USB)"}
    time.sleep(2.5)
    info = {}
    for _ in range(4):
        info = adb_infos(serie) or adb_infos(serie)
        if info.get("ip"):
            break
        time.sleep(1.0)
    ip = info.get("ip", "")
    if not ip:
        return {"ok": False, "erreur": "port ouvert, mais l'adresse Wi-Fi de la tablette est introuvable (Wi-Fi allume ?)"}
    code, sortie, err = adb_lancer(["connect", "%s:%d" % (ip, PORT_ADB_WIFI)], timeout=15)
    ok = code == 0 and ("connected" in sortie.lower() or "already" in sortie.lower())
    return {"ok": ok, "ip": ip, "port": PORT_ADB_WIFI, "detail": (sortie.strip() or err)}


def tablette_connecter(adresse):
    m = _RE_ADRESSE.match(str(adresse or "").strip())
    if not m:
        return {"ok": False, "erreur": "adresse attendue : 192.168.x.x ou 192.168.x.x:5555"}
    cible = "%s:%s" % (m.group(1), m.group(2) or PORT_ADB_WIFI)
    code, sortie, err = adb_lancer(["connect", cible], timeout=15)
    ok = code == 0 and ("connected" in sortie.lower() or "already" in sortie.lower())
    return {"ok": ok, "adresse": cible, "detail": (sortie.strip() or err)}


_RE_LIBRE = re.compile(r"[A-Za-z0-9 ._\-]{1,24}")
_RE_NOM = re.compile(r"[^\W_][\w .'\-]{0,39}")


def tablette_affecter(conn, serie, profil, ident, famille, equipe):
    """Fixe le profil de la tablette et son identite. dt : DT + famille ; exc : id + famille ;
    spotter et link : rien (le spotter se connecte lui-meme). Une identite n'appartient qu'a UNE tablette."""
    serie = str(serie or "").strip()
    profil = str(profil or "dt").strip().lower()
    ident = str(ident or "").strip()
    famille = str(famille or "").strip()
    equipe = str(equipe or "").strip()
    if not _RE_SERIE.match(serie):
        return 400, {"error": "numero de serie invalide"}
    if profil not in PROFILS:
        return 400, {"error": "profil inconnu : %s" % profil}
    if profil in ("link", "spotter"):        # le spotter se connecte lui-meme : rien n'est fige sur la tablette
        ident, famille, equipe = "", "", ""
    else:
        equipe = ""
        if ident:
            if profil == "dt":
                brut = ident.upper().replace("DT-", "").replace("DT", "")
                if not re.fullmatch(r"\d{1,3}", brut):
                    return 400, {"error": "id DT attendu : un numero (4 ou DT-04)"}
                ident = "DT-%02d" % int(brut)
            else:
                ident = ident.upper()
                if not _RE_LIBRE.fullmatch(ident):
                    return 400, {"error": "id de l'excavatrice invalide"}
        if famille and not _RE_LIBRE.fullmatch(famille):
            return 400, {"error": "famille invalide (1500, 785, ADT...)"}
    if ident:
        autre = conn.execute("SELECT serial, nom FROM tablettes WHERE profil=? AND ident=? AND serial<>?",
                             (profil, ident, serie)).fetchone()
        if autre:
            return 409, {"error": "%s est deja affecte a la tablette %s (%s)" % (ident, autre["serial"], autre["nom"] or "sans nom")}
    conn.execute("INSERT OR IGNORE INTO tablettes (serial) VALUES (?)", (serie,))
    conn.execute("UPDATE tablettes SET profil=?, ident=?, famille=?, equipe=?, dt_code='' WHERE serial=?",
                 (profil, ident, famille, equipe, serie))
    conn.commit()
    return 200, {"ok": True, "serie": serie, "profil": profil, "ident": ident, "famille": famille, "equipe": equipe}


def tablette_incomplete(k):
    """Motif si l'identite du profil n'est pas complete (sinon chaine vide)."""
    pr = k.get("profil") or "dt"
    if pr == "dt" and not (k.get("ident") and k.get("famille")):
        return "choisissez le DT de cette tablette (sa famille suit)"
    if pr == "exc" and not (k.get("ident") and k.get("famille")):
        return "renseignez l'id et la famille de l'excavatrice"
    return ""


def connexion_dt(conn, code):
    """Parametres de connexion d'un DT, tels que saisis dans la console (Settings > Connexion) :
    serveur n°1 = Serveur_OT, serveur n°2 = Serveur_SG, et l'acces MQTT PROPRE a ce DT (jamais celui d'un autre)."""
    res = {}
    url1, jeton1 = office_conf()
    url2, jeton2, _ = sg_conf()
    # jeton PROPRE a la tablette quand les serveurs le connaissent ; sinon (transition) le jeton maitre, que l'on retirera
    propre = jeton_courant(conn, code) if (conn is not None and jetons_actifs()) else ""
    if url1:
        res["serveur1"] = {"url": url1, "token": propre or jeton1}
    if url2:
        res["serveur2"] = {"url": url2, "token": propre or jeton2}
    conf = mqtt_conf() or {}
    pub = conf.get("public") or {}
    cpt = conn.execute("SELECT mdp FROM mqtt_comptes WHERE code=? AND actif=1", (code,)).fetchone() if conn else None
    if pub.get("hote") and cpt and cpt["mdp"]:
        res["mqtt"] = {"hote": pub["hote"], "port": int(pub.get("port", 443)), "tls": bool(pub.get("tls", True)),
                       "identifiant": code, "mdp": cpt["mdp"]}
    return res


def provision_contenu(k, zone, conn=None):
    """Contenu de provision.json selon le profil (None : Spotter et Link n'ont pas d'identite fixee)."""
    pr = k.get("profil") or "dt"
    if pr == "dt":
        contenu = {"profil": "dt", "dt": k["ident"].replace("DT-", ""), "camion": k["famille"], "zone": zone or ""}
        connexion = connexion_dt(conn, k["ident"]) if conn is not None else {}
        cle = cle_radio_courante(conn, k["ident"]) if conn is not None else ""
        if cle:
            # cle du DT (signature de ses positions de secours) + cles des pelles (qui parle ?) + exigence
            connexion["xbee"] = {"cle": cle, "pelles": cles_pelles(conn), "exiger": radio_exiger_signature()}
        if connexion:
            contenu["connexion"] = connexion
        return contenu
    if pr == "exc":
        contenu = {"profil": "exc", "id": k["ident"], "famille": k["famille"], "zone": zone or ""}
        # l'excavatrice ne parle qu'au serveur n°1 : adresse + jeton PROPRE a cette tablette
        serveur1 = (connexion_dt(conn, k["ident"]) if conn is not None else {}).get("serveur1")
        connexion = {"serveur1": serveur1} if serveur1 else {}
        cle = cle_radio_courante(conn, k["ident"]) if conn is not None else ""
        if cle:
            connexion["xbee"] = {"cle": cle}          # signature des trames de CETTE pelle
        if connexion:
            contenu["connexion"] = connexion
        return contenu
    return None


def _job_log(job, texte, ok=True):
    with _jobs_verrou:
        _jobs_tablettes[job]["etapes"].append({"texte": texte, "ok": ok})


def tablette_installer(job, serie, adresse_adb, k, zone, autoriser_test, reinstaller):
    """Installation complete (thread) : APK -> premier lancement -> provision.json -> relance."""
    cible = adresse_adb or serie
    profil = k.get("profil") or "dt"
    paquet = PROFILS[profil]["paquet"]
    try:
        apk, motif = apk_retenu(autoriser_test, profil)
        if not apk:
            raise RuntimeError(motif)
        _job_log(job, "APK %s : %s (%s Mo%s)" % (PROFILS[profil]["nom"], apk["nom"], apk["taille_mo"], ", TEST" if apk["test"] else ""))
        if reinstaller:
            adb_lancer(["uninstall", paquet], serie=cible, timeout=60)
            _job_log(job, "Ancienne version desinstallee (donnees de l'app effacees)")
        code, sortie, err = adb_lancer(["install", "-r", apk["chemin"]], serie=cible, timeout=300)
        texte = (sortie + " " + err).strip()
        if code != 0 or "Success" not in texte:
            if "UPDATE_INCOMPATIBLE" in texte:
                raise RuntimeError("une version signee avec une AUTRE cle est installee : relancez avec « reinstaller » "
                                   "(les donnees de l'app seront effacees)")
            raise RuntimeError("installation refusee : %s" % texte[-240:])
        code, liste, _ = adb_lancer(["shell", "pm", "list", "packages", paquet], serie=cible, timeout=15)
        if "package:" + paquet not in liste:
            raise RuntimeError("l'APK installe n'est pas le paquet attendu pour le profil %s (%s)" % (PROFILS[profil]["nom"], paquet))
        _job_log(job, "APK installe")
        lancer = ["shell", "monkey", "-p", paquet, "-c", "android.intent.category.LAUNCHER", "1"]
        conn_p = get_conn()
        try:
            contenu = provision_contenu(k, zone, conn_p)
        finally:
            conn_p.close()
        if contenu is not None:
            # premier lancement : l'app cree son dossier de fichiers externes (getExternalFilesDir)
            adb_lancer(lancer, serie=cible, timeout=20)
            time.sleep(4.0)
            erreur = ecrire_provision(cible, serie, paquet, contenu)
            if erreur:
                raise RuntimeError(erreur)
            _job_log(job, "Identite ecrite sur la tablette : %s" % libelle_identite(k))
            if contenu.get("connexion"):
                _job_log(job, "Connexion ecrite : %s" % ", ".join(
                    {"serveur1": "serveur n°1", "serveur2": "serveur n°2", "mqtt": "MQTT", "xbee": "cle radio"}.get(x, x) for x in contenu["connexion"]))
            adb_lancer(["shell", "am", "force-stop", paquet], serie=cible, timeout=15)
        adb_lancer(lancer, serie=cible, timeout=20)
        _job_log(job, "Application %s lancee" % PROFILS[profil]["nom"])
        conn = get_conn()
        try:
            conn.execute("UPDATE tablettes SET apk=?, installee_le=datetime('now','localtime') WHERE serial=?", (apk["nom"], serie))
            conn.commit()
        finally:
            conn.close()
        with _jobs_verrou:
            _jobs_tablettes[job]["etat"] = "ok"
    except Exception as e:
        _job_log(job, str(e), ok=False)
        with _jobs_verrou:
            _jobs_tablettes[job]["etat"] = "erreur"
            _jobs_tablettes[job]["erreur"] = str(e)


# ------------------------------------------------------------------ cles radio par DT ---
# Cle HMAC-SHA256 propre a chaque DT : la tablette signe ses positions envoyees par radio XBee (liaison sans 4G) ;
# la passerelle ne les accepte que si la signature est valide et recente. La cle est GENEREE ICI, envoyee a la
# tablette dans provision.json, et n'est jamais renvoyee par l'API (seule son empreinte est visible).
CLE_RADIO_TOLERANCE_S = 7 * 24 * 3600


def cle_radio_courante(conn, code):
    """Cle (hex) du DT, creee au premier besoin ; '' si le code n'est pas valable ou si la cle est revoquee."""
    code = str(code or "").strip()
    if not re.match(r"^[A-Za-z0-9_-]{2,40}$", code):
        return ""
    r = conn.execute("SELECT cle_hex, actif FROM cles_dt WHERE dt_code=?", (code,)).fetchone()
    if r and r["actif"]:
        return r["cle_hex"]
    if r and not r["actif"]:
        return ""
    cle = secrets.token_hex(32)
    conn.execute("INSERT INTO cles_dt (dt_code, cle_hex, actif, maj_le) VALUES (?, ?, 1, ?)", (code, cle, now_iso()))
    conn.commit()
    return cle


def cle_radio_renouveler(conn, code):
    """Nouvelle cle ; l'ancienne reste acceptee 7 jours (le temps que la tablette recoive la nouvelle). Reactive une cle revoquee."""
    code = str(code or "").strip()
    if not re.match(r"^[A-Za-z0-9_-]{2,40}$", code):
        return 400, {"error": "code DT invalide"}
    r = conn.execute("SELECT cle_hex, actif FROM cles_dt WHERE dt_code=?", (code,)).fetchone()
    nouvelle = secrets.token_hex(32)
    if not r:
        conn.execute("INSERT INTO cles_dt (dt_code, cle_hex, actif, maj_le) VALUES (?, ?, 1, ?)", (code, nouvelle, now_iso()))
    else:
        precedente = r["cle_hex"] if r["actif"] else ""
        conn.execute("UPDATE cles_dt SET cle_prec=?, prec_jusqua=?, cle_hex=?, actif=1, maj_le=? WHERE dt_code=?",
                     (precedente, time.time() + CLE_RADIO_TOLERANCE_S if precedente else 0, nouvelle, now_iso(), code))
    conn.commit()
    return 200, {"ok": True, "dt_code": code, "empreinte": _empreinte(nouvelle)[:8], "tolerance_jours": CLE_RADIO_TOLERANCE_S // 86400}


def cle_radio_revoquer(conn, code):
    code = str(code or "").strip()
    conn.execute("UPDATE cles_dt SET actif=0, cle_prec='', prec_jusqua=0, maj_le=? WHERE dt_code=?", (now_iso(), code))
    conn.commit()
    return 200, {"ok": True, "dt_code": code}


def cles_pelles(conn):
    """Cles radio des excavatrices du site (id de la pelle -> cles valables : la courante, et l'ancienne pendant la tolerance).
    Donnees a chaque DT pour savoir QUELLE pelle parle."""
    maintenant = time.time()
    res = {}
    for t in conn.execute("SELECT ident FROM tablettes WHERE profil='exc' AND ident<>''").fetchall():
        r = conn.execute("SELECT cle_hex, actif, cle_prec, prec_jusqua FROM cles_dt WHERE dt_code=?", (t["ident"],)).fetchone()
        if not r:
            cle = cle_radio_courante(conn, t["ident"])
            cles = [cle] if cle else []
        elif r["actif"]:
            cles = [r["cle_hex"]] + ([r["cle_prec"]] if r["cle_prec"] and (r["prec_jusqua"] or 0) > maintenant else [])
        else:
            cles = []
        if cles:
            res[t["ident"]] = cles
    return res


def radio_exiger_signature():
    """La console exige-t-elle que les DT ignorent les trames de pelle non signees ? (arrete par defaut : mise en service progressive)"""
    return str(param_lire_base("RADIO_EXIGER_SIGNATURE", "")).strip() in ("1", "oui", "true")


def cles_radio_lister(conn):
    """Etat des cles : jamais la cle elle-meme, seulement son empreinte."""
    maintenant = time.time()
    return [{"dt_code": r["dt_code"], "actif": bool(r["actif"]), "empreinte": _empreinte(r["cle_hex"])[:8],
             "maj_le": r["maj_le"] or r["created_at"], "tolerance": bool(r["cle_prec"] and (r["prec_jusqua"] or 0) > maintenant)}
            for r in conn.execute("SELECT * FROM cles_dt ORDER BY dt_code")]


# ------------------------------------------------------------------ jetons par tablette ---
# Le jeton « maitre » (OFFICE_TOKEN / SG_TOKEN) ne sert plus qu'a la console, a la passerelle et au SaaS. Chaque tablette
# DT / excavatrice recoit SON jeton : <code>.<generation>.<signature>. Les serveurs (Apps Script) le verifient avec le meme
# secret, ne lui ouvrent que les actions d'une tablette, et le refusent des qu'il est revoque.
JETON_TOLERANCE_S = 7 * 24 * 3600      # un jeton renouvele : l'ancien reste valable 7 jours, le temps de recevoir le nouveau
_RE_CODE_APP = re.compile(r"^[A-Za-z0-9_-]{2,40}$")
_jetons_etat = {"ot": "", "sg": "", "dernier_essai": 0.0}


def jeton_secret(conn):
    """Secret de signature (cree au premier besoin, jamais affiche ni journalise)."""
    secret = param_lire("JETON_SECRET", "")
    if len(secret) < 32:
        secret = secrets.token_urlsafe(48)
        param_ecrire(conn, "JETON_SECRET", secret)
    return secret


def jeton_calculer(secret, code, gen):
    """Meme calcul que les Apps Script : HMAC-SHA256 de « code|generation », base64 URL sans bourrage, 32 caracteres."""
    mac = hmac.new(secret.encode("utf-8"), ("%s|%d" % (code, gen)).encode("utf-8"), hashlib.sha256).digest()
    return "%s.%d.%s" % (code, gen, base64.urlsafe_b64encode(mac).decode("ascii").rstrip("=")[:32])


def jetons_inscrire(conn):
    """Une ligne par tablette qui doit pouvoir se connecter : DT de la flotte et tablettes DT / excavatrice affectees."""
    codes = set((flotte_dt_saas() or {}).keys())
    for r in conn.execute("SELECT profil, ident FROM tablettes WHERE ident<>'' AND profil IN ('dt','exc')"):
        codes.add(r["ident"])
    for code in sorted(c for c in codes if _RE_CODE_APP.match(c or "")):
        conn.execute("INSERT OR IGNORE INTO jetons_appareils (code, maj_le, action) VALUES (?, ?, 'inscrit')", (code, now_iso()))
    conn.commit()


def jeton_courant(conn, code):
    """Jeton de la tablette, ou '' si le code n'est pas inscrit."""
    if not _RE_CODE_APP.match(str(code or "")):
        return ""
    jetons_inscrire(conn)
    r = conn.execute("SELECT gen FROM jetons_appareils WHERE code=?", (code,)).fetchone()
    return jeton_calculer(jeton_secret(conn), code, r["gen"]) if r else ""


def jeton_renouveler(conn, code):
    r = conn.execute("SELECT gen FROM jetons_appareils WHERE code=?", (code,)).fetchone()
    if not r:
        return 404, {"error": "tablette inconnue"}
    conn.execute("UPDATE jetons_appareils SET prev=gen, prev_jusqua=?, gen=gen+1, maj_le=?, action='renouvele' WHERE code=?",
                 (time.time() + JETON_TOLERANCE_S, now_iso(), code))
    conn.commit()
    _jetons_etat["dernier_essai"] = 0.0
    return 200, {"ok": True, "code": code, "gen": r["gen"] + 1, "tolerance_jours": JETON_TOLERANCE_S // 86400}


def jeton_revoquer(conn, code):
    r = conn.execute("SELECT gen FROM jetons_appareils WHERE code=?", (code,)).fetchone()
    if not r:
        return 404, {"error": "tablette inconnue"}
    conn.execute("UPDATE jetons_appareils SET prev=0, prev_jusqua=0, gen=gen+1, maj_le=?, action='revoque' WHERE code=?",
                 (now_iso(), code))
    conn.commit()
    _jetons_etat["dernier_essai"] = 0.0
    return 200, {"ok": True, "code": code, "gen": r["gen"] + 1}


def jetons_corps(conn):
    """Corps envoye aux serveurs : secret + generations valables (jamais les jetons eux-memes)."""
    jetons_inscrire(conn)
    maintenant = time.time()
    appareils = {}
    for r in conn.execute("SELECT * FROM jetons_appareils"):
        a = {"gen": r["gen"]}
        if r["prev"] and r["prev_jusqua"] > maintenant:
            a["prev"] = r["prev"]
            a["prev_jusqua"] = int(r["prev_jusqua"] * 1000)
        appareils[r["code"]] = a
    return {"secret": jeton_secret(conn), "appareils": appareils}


def appel_gas(url, jeton, action, corps_dict, delai=15):
    """POST ?action=<action> vers un Apps Script avec le jeton maitre. True si le serveur repond ok:true."""
    if not url or not jeton:
        return False
    cible = "%s?action=%s&token=%s" % (url, action, quote(jeton))
    requete = urllib.request.Request(cible, data=json.dumps(corps_dict, ensure_ascii=True).encode("utf-8"),
                                     method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(requete, timeout=delai) as rep:
            return bool(json.loads(rep.read().decode("utf-8")).get("ok"))
    except Exception as e:
        print("JETONS: appel '%s' echoue: %s" % (action, e))
        return False


def marge_affectation_m():
    """Tolerance (m) autour d'un bloc de minerai pour l'AFFECTATION : reglage de la console, 0 par defaut (polygone strict).
    Distincte de MARGE_OREBLOC_M, qui ne sert qu'au statut des engins."""
    try:
        v = float(str(param_lire("MARGE_AFFECTATION_M", "")).replace(",", "."))
    except ValueError:
        return MARGE_AFFECTATION_DEFAUT_M
    return v if 0 <= v <= 50 else MARGE_AFFECTATION_DEFAUT_M


_reglages_etat = {"sg": "", "dernier_essai": 0.0}


def reglages_diffuser(forcer=False):
    """Envoie la marge d'affectation a Serveur_SG quand elle change (empreinte : valeur + adresse), echec retente toutes les 10 min."""
    url, jeton, _ = sg_conf()
    if not url or not jeton:
        _reglages_etat["sg"] = "serveur non configure"
        return
    valeur = marge_affectation_m()
    rz = reglage_z_flitch()                    # 24/11 : Z de la pelle (choix du flitch), meme regle chez SG
    empreinte = _empreinte("marge|%s|%s|%s" % (valeur, json.dumps(rz, sort_keys=True), url))
    if not forcer and param_lire_base("REGLAGES_EMPREINTE_SG", "") == empreinte:
        return
    if not forcer and _reglages_etat["sg"] and time.time() - _reglages_etat["dernier_essai"] < CONNEXION_REESSAI_S:
        return
    _reglages_etat["dernier_essai"] = time.time()
    if appel_gas(url, jeton, "reglages_ecrire", {"marge_affectation_m": valeur, "z": rz}):
        conn = get_conn()
        try:
            param_ecrire(conn, "REGLAGES_EMPREINTE_SG", empreinte)
        finally:
            conn.close()
        _reglages_etat["sg"] = ""
    else:
        _reglages_etat["sg"] = "refuse ou sans reponse (action reglages_ecrire deployee ?)"


def jetons_diffuser(forcer=False):
    """Envoie secret + generations a Serveur_OT et Serveur_SG quand elles changent (empreinte), echec retente toutes les 10 min."""
    conn = get_conn()
    try:
        corps = jetons_corps(conn)
        # l'empreinte ignore l'echeance des tolerances : seul un changement de generation ou de secret renvoie
        stable = {"secret": corps["secret"], "appareils": {c: {"gen": a["gen"], "prev": a.get("prev", 0)} for c, a in corps["appareils"].items()}}
        base_empreinte = json.dumps(stable, sort_keys=True)
        cibles = (("ot", office_conf(), "JETONS_EMPREINTE_OT"), ("sg", sg_conf()[:2], "JETONS_EMPREINTE_SG"))
        for nom, (url, jeton), param in cibles:
            if not url:
                _jetons_etat[nom] = "serveur non configure"
                continue
            empreinte = _empreinte(base_empreinte + "|" + url)    # une nouvelle adresse de serveur recoit de nouveau le secret
            if not forcer and param_lire(param, "") == empreinte:
                continue
            if not forcer and _jetons_etat[nom] and time.time() - _jetons_etat["dernier_essai"] < CONNEXION_REESSAI_S:
                continue
            _jetons_etat["dernier_essai"] = time.time()
            if appel_gas(url, jeton, "jetons_ecrire", corps):
                param_ecrire(conn, param, empreinte)
                _jetons_etat[nom] = ""
            else:
                _jetons_etat[nom] = "refuse ou sans reponse (action jetons_ecrire deployee ?)"
    finally:
        conn.close()


def jetons_actifs():
    """Les deux serveurs connaissent les jetons actuels : on peut provisionner les tablettes avec LEUR jeton."""
    return (param_lire("JETONS_EMPREINTE_OT", "") != "" and param_lire("JETONS_EMPREINTE_SG", "") != ""
            and not _jetons_etat["ot"] and not _jetons_etat["sg"])


def jetons_lister(conn):
    jetons_inscrire(conn)
    maintenant = time.time()
    return {"jetons": [{"code": r["code"], "gen": r["gen"], "maj_le": r["maj_le"], "action": r["action"],
                        "tolerance": bool(r["prev"] and r["prev_jusqua"] > maintenant)}
                       for r in conn.execute("SELECT * FROM jetons_appareils ORDER BY code")],
            "actifs": jetons_actifs(), "ot": _jetons_etat["ot"], "sg": _jetons_etat["sg"]}


# ---- diffusion de la connexion aux tablettes (via Serveur_OT) ----
# Les serveurs n°1 (Serveur_OT) et n°2 (Serveur_SG) saisis dans la console sont envoyes a Serveur_OT
# (action connexion_ecrire) ; les tablettes les relisent dans referentiels et les adoptent apres test.
_RE_EXEC = re.compile(r"^https://script\.google\.com/macros/s/[A-Za-z0-9_-]{20,}/exec$")
_RE_JETON_SRV = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_connexion_etat = {"envoyee_le": "", "erreur": "", "dernier_essai": 0.0}
CONNEXION_REESSAI_S = 600


def connexion_corps():
    """(corps a envoyer, motifs de refus). Un serveur dont l'adresse ou le jeton est invalide n'est pas envoye."""
    corps, motifs = {}, []
    url1, _ = office_conf()
    url2, _, _ = sg_conf()
    for cle, nom, url in (("serveur1", "n°1", url1), ("serveur2", "n°2", url2)):
        if not url:
            continue
        if not _RE_EXEC.match(url):
            motifs.append("adresse du serveur %s : une /exec Apps Script est attendue" % nom)
        else:
            corps[cle] = {"url": url}      # jamais de jeton ici : chaque tablette recoit le sien
    return corps, motifs


def connexion_diffuser(forcer=False):
    """Envoie la connexion a Serveur_OT si elle a change (empreinte) ; un echec n'est retente que toutes les 10 min."""
    corps, motifs = connexion_corps()
    if not office_conf()[0]:
        _connexion_etat["erreur"] = "Serveur_OT non configure (Parametres > Liaisons)"
        return False
    if not corps:
        _connexion_etat["erreur"] = "; ".join(motifs) or "aucun serveur a diffuser"
        return False
    empreinte = _empreinte(json.dumps(corps, sort_keys=True))
    if not forcer and param_lire("CONNEXION_OT_EMPREINTE", "") == empreinte:
        _connexion_etat["erreur"] = "; ".join(motifs)
        return True
    if not forcer and time.time() - _connexion_etat["dernier_essai"] < CONNEXION_REESSAI_S and _connexion_etat["erreur"]:
        return False
    _connexion_etat["dernier_essai"] = time.time()
    if office_appel("connexion_ecrire", corps):
        conn = get_conn()
        try:
            param_ecrire(conn, "CONNEXION_OT_EMPREINTE", empreinte)
        finally:
            conn.close()
        _connexion_etat.update(envoyee_le=now_iso(), erreur="; ".join(motifs))
        return True
    _connexion_etat["erreur"] = "Serveur_OT a refuse ou n'a pas repondu (action connexion_ecrire deployee ?)"
    return False


def ecrire_provision(cible, serie, paquet, contenu):
    """Pousse provision.json dans le dossier de l'app et le RELIT. Retourne '' si tout va bien, sinon le motif."""
    texte = json.dumps(contenu, ensure_ascii=True)
    tmp = os.path.join(tempfile.gettempdir(), "provision_%s.json" % re.sub(r"[^A-Za-z0-9]", "", serie))
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(texte)
        dest = "/sdcard/Android/data/%s/files" % paquet
        adb_lancer(["shell", "mkdir", "-p", dest], serie=cible, timeout=15)
        code, _, err = adb_lancer(["push", tmp, dest + "/provision.json"], serie=cible, timeout=30)
        if code != 0:
            return "fichier de provisionnement non ecrit : %s" % err[-200:]
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    code, relu, _ = adb_lancer(["shell", "cat", dest + "/provision.json"], serie=cible, timeout=15)
    if code != 0 or texte not in relu:
        return "provisionnement non verifie sur la tablette"
    return ""


def tablette_envoyer_parametres(conn, serie, adresse_adb):
    """Renvoie provision.json (identite + connexion) sans reinstaller : l'app le relit d'elle-meme (60 s)."""
    row = conn.execute("SELECT * FROM tablettes WHERE serial=?", (serie,)).fetchone()
    k = dict(row) if row else None
    if not k:
        return 404, {"error": "tablette inconnue"}
    manque = tablette_incomplete(k)
    if manque:
        return 409, {"error": manque}
    contenu = provision_contenu(k, zone_utm_site(conn), conn)
    if contenu is None:
        return 409, {"error": "ce profil n'a pas de parametres a envoyer"}
    paquet = PROFILS[k.get("profil") or "dt"]["paquet"]
    erreur = ecrire_provision(adresse_adb or serie, serie, paquet, contenu)
    if erreur:
        return 502, {"error": erreur}
    return 200, {"ok": True, "connexion": sorted((contenu.get("connexion") or {}).keys())}


def libelle_identite(k):
    pr = k.get("profil") or "dt"
    if pr == "dt":
        return "%s, famille %s" % (k.get("ident", ""), k.get("famille", ""))
    if pr == "exc":
        return "%s, famille %s" % (k.get("ident", ""), k.get("famille", ""))
    return PROFILS[pr]["nom"]


def tablette_reinitialiser(serie, adresse_adb, profil="dt"):
    cible = adresse_adb or serie
    paquet = PROFILS.get(profil, PROFILS["dt"])["paquet"]
    code, _, err = adb_lancer(["shell", "rm", "-f", "/sdcard/Android/data/%s/files/provision.json" % paquet], serie=cible, timeout=15)
    adb_lancer(["shell", "am", "force-stop", paquet], serie=cible, timeout=15)
    return {"ok": code == 0, "erreur": err}


# ------------------------------------------------------------------ GNSS (recepteurs) ---
# Un recepteur = une puce reconnue (lue par le navigateur, Web Serial) -> un ID GNSS-NN -> une configuration
# commune (profil du site, reglable) -> UNE tablette. La configuration materielle (UBX) est faite par le
# navigateur branche sur le recepteur ; la passerelle garde l'inventaire, le profil et l'affectation.
GNSS_BAUDS = (9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600)
GNSS_TRAMES = ("GGA", "RMC", "VTG", "GSA", "GSV", "GLL")
_RE_PUCE = re.compile(r"^[0-9A-F]{8,16}$")


def gnss_profil():
    """Profil de configuration des recepteurs du site (reglages GNSS_BAUD, GNSS_HZ, GNSS_TRAMES)."""
    try:
        baud = int(param_lire("GNSS_BAUD", "115200"))
    except ValueError:
        baud = 115200
    try:
        hz = int(param_lire("GNSS_HZ", "1"))
    except ValueError:
        hz = 1
    trames = [t for t in re.split(r"[,; ]+", param_lire("GNSS_TRAMES", "GGA,RMC,VTG").upper()) if t in GNSS_TRAMES]
    return {"baud": baud if baud in GNSS_BAUDS else 115200, "hz": min(10, max(1, hz)),
            "trames": trames or ["GGA", "RMC"]}


def gnss_signature(profil):
    return "%d|%d|%s" % (profil["baud"], profil["hz"], ",".join(sorted(profil["trames"])))


def gnss_profil_ecrire(conn, d):
    try:
        baud, hz = int(d.get("baud")), int(d.get("hz"))
    except (TypeError, ValueError):
        return 400, {"error": "vitesse et frequence : des nombres"}
    trames = [t for t in (d.get("trames") or []) if t in GNSS_TRAMES]
    if baud not in GNSS_BAUDS:
        return 400, {"error": "vitesse non prise en charge : %s" % baud}
    if not 1 <= hz <= 10:
        return 400, {"error": "frequence entre 1 et 10 Hz"}
    if "GGA" not in trames:
        return 400, {"error": "la trame GGA est indispensable (position et qualite du fix)"}
    param_ecrire(conn, "GNSS_BAUD", baud)
    param_ecrire(conn, "GNSS_HZ", hz)
    param_ecrire(conn, "GNSS_TRAMES", ",".join(trames))
    return 200, {"ok": True, "profil": gnss_profil()}


def gnss_enregistrer(conn, chip, modele=""):
    """Reconnait la puce ; si elle est nouvelle, lui attribue le premier ID GNSS-NN libre."""
    chip = str(chip or "").strip().upper()
    if not _RE_PUCE.match(chip):
        return 400, {"error": "identifiant de puce invalide"}
    existant = conn.execute("SELECT id FROM gnss WHERE chip=?", (chip,)).fetchone()
    if existant:
        if modele:
            conn.execute("UPDATE gnss SET modele=? WHERE chip=?", (str(modele)[:60], chip))
            conn.commit()
        return 200, {"ok": True, "chip": chip, "id": existant["id"], "nouveau": False}
    pris = {r["id"] for r in conn.execute("SELECT id FROM gnss")}
    n = 1
    while "GNSS-%02d" % n in pris:
        n += 1
    ident = "GNSS-%02d" % n
    conn.execute("INSERT INTO gnss (chip, id, modele) VALUES (?, ?, ?)", (chip, ident, str(modele or "")[:60]))
    conn.commit()
    return 200, {"ok": True, "chip": chip, "id": ident, "nouveau": True}


def gnss_affecter(conn, chip, tablette):
    """Affecte le recepteur a une tablette (vide = dissocier). Une tablette porte UN recepteur ; le Spotter n'en a pas."""
    chip = str(chip or "").strip().upper()
    tablette = str(tablette or "").strip()
    g = conn.execute("SELECT id FROM gnss WHERE chip=?", (chip,)).fetchone()
    if not g:
        return 404, {"error": "recepteur inconnu"}
    if tablette:
        t = conn.execute("SELECT serial, nom, profil FROM tablettes WHERE serial=?", (tablette,)).fetchone()
        if not t:
            return 404, {"error": "tablette inconnue (detectez-la d'abord dans l'onglet Tablettes)"}
        if t["profil"] == "spotter":
            return 409, {"error": "le Spotter n'a pas de recepteur GNSS"}
        autre = conn.execute("SELECT id FROM gnss WHERE tablette=? AND chip<>?", (tablette, chip)).fetchone()
        if autre:
            return 409, {"error": "cette tablette porte deja %s" % autre["id"]}
    conn.execute("UPDATE gnss SET tablette=? WHERE chip=?", (tablette, chip))
    conn.commit()
    return 200, {"ok": True, "id": g["id"], "tablette": tablette}


def gnss_configure_note(conn, chip):
    """Memorise qu'un recepteur a ete configure ET verifie avec le profil actuel."""
    chip = str(chip or "").strip().upper()
    if not conn.execute("SELECT 1 FROM gnss WHERE chip=?", (chip,)).fetchone():
        return 404, {"error": "recepteur inconnu"}
    conn.execute("UPDATE gnss SET configure_le=datetime('now','localtime'), config_sig=? WHERE chip=?",
                 (gnss_signature(gnss_profil()), chip))
    conn.commit()
    return 200, {"ok": True}


def gnss_supprimer(conn, chip):
    conn.execute("DELETE FROM gnss WHERE chip=?", (str(chip or "").strip().upper(),))
    conn.commit()
    return 200, {"ok": True}


def gnss_lister(conn):
    profil = gnss_profil()
    sig = gnss_signature(profil)
    tabs = {r["serial"]: dict(r) for r in conn.execute("SELECT serial, nom, profil, ident, famille FROM tablettes")}
    liste = []
    for r in conn.execute("SELECT * FROM gnss ORDER BY id"):
        t = tabs.get(r["tablette"]) if r["tablette"] else None
        engin = ""
        if t:
            engin = t["ident"] if t["profil"] in ("dt", "exc") and t["ident"] else ("Mesure" if t["profil"] == "link" else "")
        liste.append({"chip": r["chip"], "id": r["id"], "modele": r["modele"], "configure_le": r["configure_le"],
                      "a_jour": bool(r["configure_le"]) and r["config_sig"] == sig,
                      "tablette": r["tablette"], "tablette_nom": (t or {}).get("nom", ""),
                      "profil_tablette": (t or {}).get("profil", ""), "engin": engin,
                      "famille": (t or {}).get("famille", "")})
    portees = {g["tablette"] for g in liste if g["tablette"]}
    tablettes = [{"serial": k, "nom": v["nom"], "profil": v["profil"], "engin": v["ident"], "famille": v["famille"],
                  "libre": k not in portees}
                 for k, v in tabs.items() if v["profil"] != "spotter"]
    return {"gnss": liste, "tablettes": tablettes, "profil": profil, "bauds": list(GNSS_BAUDS), "trames": list(GNSS_TRAMES)}


# ------------------------------------------------ authentification des ecritures ---
# 20/11 : la passerelle n'avait AUCUNE authentification : toute machine du reseau pouvait
# changer les reglages (sortie de route, liaisons, MQTT...). Les LECTURES restent ouvertes
# (dashboard, cartes) ; toute ECRITURE (POST / PUT / DELETE) exige un jeton Firebase de
# l'application dont le profil (admin / senor / geologue, Firestore) est assez eleve.
# Le jeton est verifie par le SaaS de CE poste (:8100, /api/auth/role) : meme verification
# des signatures que pour ses propres API, role lu dans Firestore, mis en cache 60 s.
# Exemptions : le poste de la passerelle lui-meme (127.0.0.1 : script, console, XBee) ;
# AUTH_ECRITURES=0 coupe le controle (depannage).
RANG_ROLE = {"geologue": 1, "senor": 2, "admin": 3}
# Prefixes d'ecritures : le plus restrictif d'abord. Ecriture inconnue = admin (refus par defaut).
ECRITURES_ADMIN = ("/api/relais", "/api/tablettes", "/api/gnss", "/api/jetons", "/api/radio", "/api/offroad", "/api/mqtt/", "/api/sg/", "/api/cles_dt", "/api/calibration_",
                   "/api/ot_reconcilier", "/api/pistes_pousser", "/api/travels_roads_reprojeter",
                   "/api/oreblocs_nettoyer", "/api/orebloc_maj", "/api/affecter", "/api/verifier_deviation",
                   "/api/missions/", "/api/engins", "/api/mission_fin")
ECRITURES_SENOR = ("/api/oreblocs", "/api/dumping_area", "/api/teneur", "/api/sources", "/api/checkpoints", "/api/trajectoire",
                   "/api/travels_roads", "/api/geographic_system", "/api/geofencing")
# 21/11 : /api/minage_declare = le SPOTTER declare depuis son app (tout profil connecte) ; le formulaire du bureau l'utilise aussi
ECRITURES_TOUS = ("/api/alertes", "/api/blasts", "/api/ot_blasts", "/api/enregistrements", "/api/minage_declare")
_roles_cache = {}                       # jeton -> (role | None, expire)


def auth_ecritures_active():
    return str(param_lire("AUTH_ECRITURES", "1")).strip().lower() not in ("0", "non", "false", "off")


def role_requis(path):
    """Profil minimal pour ECRIRE sur ce chemin."""
    for prefixes, role in ((ECRITURES_ADMIN, "admin"), (ECRITURES_SENOR, "senor"), (ECRITURES_TOUS, "geologue")):
        if any(path == p or path.startswith(p) for p in prefixes):
            return role
    return "admin"


def role_du_jeton(jeton):
    """Profil (admin / senor / geologue) du porteur du jeton, ou None. Verifie aupres du SaaS local."""
    if not jeton:
        return None
    maintenant = time.time()
    c = _roles_cache.get(jeton)
    if c and c[1] > maintenant:
        return c[0]
    role = None
    try:
        base = param_lire("SAAS_URL", "http://127.0.0.1:8100").rstrip("/")
        req = urllib.request.Request(base + "/api/auth/role", headers={"Authorization": "Bearer " + jeton})
        with urllib.request.urlopen(req, timeout=6) as rep:
            d = json.loads(rep.read().decode("utf-8", "replace"))
        r = str(d.get("role") or "")
        role = r if r in RANG_ROLE else None
    except Exception as e:
        print("AUTH: verification du jeton impossible (%s)" % e)
        return None                      # SaaS injoignable : pas de mise en cache d'un refus
    if len(_roles_cache) > 200:
        _roles_cache.clear()
    _roles_cache[jeton] = (role, maintenant + (60 if role else 10))
    return role


def ecriture_autorisee(adresse, path, entete_authorization):
    """(autorise, code, message)."""
    if not auth_ecritures_active():
        return True, 200, ""
    if adresse in ("127.0.0.1", "::1", "localhost"):
        return True, 200, ""
    requis = role_requis(path)
    jeton = ""
    if entete_authorization and entete_authorization.lower().startswith("bearer "):
        jeton = entete_authorization[7:].strip()
    if not jeton:
        return False, 401, "authentification requise : connectez-vous a l'application"
    role = role_du_jeton(jeton)
    if role is None:
        return False, 401, "jeton refuse ou verification impossible"
    if RANG_ROLE[role] < RANG_ROLE[requis]:
        return False, 403, "profil insuffisant : %s requis (vous etes %s)" % (requis, role)
    return True, 200, ""


# --------------------------------------------------------------- handler HTTP ---
class Handler(BaseHTTPRequestHandler):
    server_version = "SenorGeology/1.0"
    protocol_version = "HTTP/1.1"

    def _send_fichier(self, code, corps, type_mime, nom, inline=False, plage=None):
        """Reponse binaire (CSV, video) : telechargement, ou lecture dans la page (inline) avec plages d'octets."""
        try:
            self.send_response(code)
            self.send_header("Content-Type", type_mime)
            self.send_header("Content-Disposition", '%s; filename="%s"' % ("inline" if inline else "attachment", nom))
            self.send_header("Accept-Ranges", "bytes")
            if plage:
                self.send_header("Content-Range", "bytes %d-%d/%d" % plage)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Length", str(len(corps)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(corps)
        except Exception as e:
            print("ERREUR _send_fichier:", e)

    def _send_json(self, code, obj):
        try:
            body = json.dumps(obj, ensure_ascii=True).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            # page SAAS sur :8100 -> API SG sur :8101 : CORS ouvert (LAN)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except Exception as e:
            print("ERREUR _send_json:", e)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > 0:
            return self.rfile.read(length)
        return b""

    def _route_tablettes(self, path, method):
        sous = path[len("/api/tablettes"):].strip("/")
        conn = get_conn()
        try:
            if method == "GET" and sous == "":
                return self._send_json(200, tablettes_lister(conn))
            if method == "GET" and sous == "installation":
                job = (self._qs().get("job") or [""])[0]
                with _jobs_verrou:
                    d = dict(_jobs_tablettes.get(job) or {})
                if not d:
                    return self._send_json(404, {"error": "installation inconnue"})
                return self._send_json(200, d)
            if method != "POST":
                return self._send_json(405, {"error": "methode non supportee"})
            body = json.loads(self._read_body() or b"{}")
            serie = str(body.get("serie") or "").strip()
            if sous == "connecter":
                return self._send_json(200, tablette_connecter(body.get("adresse")))
            if not _RE_SERIE.match(serie):
                return self._send_json(400, {"error": "numero de serie invalide"})
            if sous == "supprimer":
                # 23/11 : oublie une tablette deja detectee (ligne de la base). Refuse si elle est connectee
                # (elle serait redetectee aussitot). Une adresse Wi-Fi perimee est aussi deconnectee d'adb.
                t = next((x for x in tablettes_lister(conn)["tablettes"] if x["serie"] == serie), None)
                if t and t["connectee"]:
                    return self._send_json(409, {"error": "tablette connectee : debranchez-la d'abord"})
                conn.execute("DELETE FROM tablettes WHERE serial=?", (serie,))
                conn.commit()
                for adr in {serie, (t or {}).get("adresse_adb") or ""}:
                    if _RE_ADRESSE.match(adr) and ":" in adr:
                        adb_lancer(["disconnect", adr], timeout=10)
                return self._send_json(200, {"ok": True, "serie": serie})
            if sous == "wifi":
                return self._send_json(200, tablette_ouvrir_wifi(str(body.get("adresse_adb") or serie)))
            if sous == "affecter":
                code, rep_ = tablette_affecter(conn, serie, body.get("profil"), body.get("ident"),
                                               body.get("famille"), body.get("equipe"))
                return self._send_json(code, rep_)
            if sous == "parametres":
                code, rep_ = tablette_envoyer_parametres(conn, serie, str(body.get("adresse_adb") or ""))
                return self._send_json(code, rep_)
            if sous == "reinitialiser":
                k = conn.execute("SELECT profil FROM tablettes WHERE serial=?", (serie,)).fetchone()
                return self._send_json(200, tablette_reinitialiser(serie, str(body.get("adresse_adb") or ""), (k["profil"] if k else "dt")))
            if sous == "installer":
                row = conn.execute("SELECT * FROM tablettes WHERE serial=?", (serie,)).fetchone()
                k = dict(row) if row else None
                manque = tablette_incomplete(k) if k else "affectez d'abord un profil a cette tablette"
                if manque:
                    return self._send_json(409, {"error": manque})
                with _jobs_verrou:
                    if any(j["serie"] == serie and j["etat"] == "en_cours" for j in _jobs_tablettes.values()):
                        return self._send_json(409, {"error": "une installation est deja en cours sur cette tablette"})
                    job = "inst_%d" % int(time.time() * 1000)
                    _jobs_tablettes[job] = {"job": job, "serie": serie, "etat": "en_cours", "etapes": [], "erreur": ""}
                threading.Thread(target=tablette_installer, daemon=True, args=(
                    job, serie, str(body.get("adresse_adb") or ""), k, zone_utm_site(conn),
                    bool(body.get("autoriser_test")), bool(body.get("reinstaller")))).start()
                return self._send_json(202, {"job": job})
            return self._send_json(404, {"error": "endpoint inconnu: /api/tablettes/" + sous})
        finally:
            conn.close()

    def _route_jetons(self, path, method):
        sous = path[len("/api/jetons"):].strip("/")
        conn = get_conn()
        try:
            if method == "GET" and sous == "":
                return self._send_json(200, jetons_lister(conn))
            if method != "POST":
                return self._send_json(405, {"error": "methode non supportee"})
            body = json.loads(self._read_body() or b"{}")
            code = str(body.get("code") or "").strip()
            if sous == "renouveler":
                res = jeton_renouveler(conn, code)
            elif sous == "revoquer":
                res = jeton_revoquer(conn, code)
            else:
                return self._send_json(404, {"error": "endpoint inconnu: /api/jetons/" + sous})
            if res[0] == 200:
                try:
                    jetons_diffuser(forcer=True)
                except Exception as e:
                    print("Jetons : diffusion immediate impossible (%s)" % e)
            return self._send_json(res[0], res[1])
        finally:
            conn.close()

    def _route_gnss(self, path, method):
        sous = path[len("/api/gnss"):].strip("/")
        conn = get_conn()
        try:
            if method == "GET" and sous == "":
                return self._send_json(200, gnss_lister(conn))
            if method != "POST":
                return self._send_json(405, {"error": "methode non supportee"})
            body = json.loads(self._read_body() or b"{}")
            if sous == "enregistrer":
                code, rep_ = gnss_enregistrer(conn, body.get("chip"), body.get("modele"))
            elif sous == "affecter":
                code, rep_ = gnss_affecter(conn, body.get("chip"), body.get("tablette"))
            elif sous == "configure":
                code, rep_ = gnss_configure_note(conn, body.get("chip"))
            elif sous == "supprimer":
                code, rep_ = gnss_supprimer(conn, body.get("chip"))
            elif sous == "profil":
                code, rep_ = gnss_profil_ecrire(conn, body)
            else:
                return self._send_json(404, {"error": "endpoint inconnu: /api/gnss/" + sous})
            return self._send_json(code, rep_)
        finally:
            conn.close()

    def _qs(self):
        return parse_qs(urlparse(self.path).query)

    def log_message(self, fmt, *args):
        pass  # silencieux en production

    # ----------------------------- GET -----------------------------
    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path.startswith("/api/"):
                return self.route_api("GET", path)
            return self.serve_static(path)
        except Exception as e:
            print("ERREUR GET", path, ":", e)
            return self._send_json(500, {"error": str(e)})

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            return self.route_api("POST", path)
        except Exception as e:
            print("ERREUR POST", path, ":", e)
            return self._send_json(500, {"error": str(e)})

    def do_DELETE(self):
        path = unquote(urlparse(self.path).path)
        try:
            return self.route_api("DELETE", path)
        except Exception as e:
            print("ERREUR DELETE", path, ":", e)
            return self._send_json(500, {"error": str(e)})

    def do_PUT(self):
        path = unquote(urlparse(self.path).path)
        try:
            return self.route_api("PUT", path)
        except Exception as e:
            print("ERREUR PUT", path, ":", e)
            return self._send_json(500, {"error": str(e)})

    def route_api(self, method, path):
        if method != "GET":                  # les lectures sondees chaque seconde ne polluent plus la console
            print("ROUTE_API:", method, path)
        if method in ("POST", "PUT", "DELETE"):
            ok, code, message = ecriture_autorisee(self.client_address[0], path, self.headers.get("Authorization", ""))
            if not ok:
                print("AUTH: refus %s %s depuis %s (%s)" % (method, path, self.client_address[0], message))
                return self._send_json(code, {"error": message})
        # Sante SANS verrou : aucune base lue. Derriere LOCK, elle disait la
        # passerelle « injoignable » des qu'une autre route le gardait.
        if path == "/api/health" and method == "GET":
            return self._send_json(200, {
                "status": "ok",
                "server": "SenorGeology/1.0",
                "db": DB_PATH,
                "port": PORT,
                "time": now_iso()
            })
        # Diff local <-> classeur : les appels au cloud HORS verrou. Sous LOCK
        # (6 a 9 s mesures, 60 s max par appel), le panneau de synchro du SaaS
        # qui l'appelle en boucle figeait toute la passerelle (12/11).
        if path == "/api/ot_diff" and method == "GET":
            ok, distant, err = ot_etat_distant()
            if not ok:
                return self._send_json(502, {"error": err})
            conn = get_conn()
            try:
                LOCK.route("GET /api/ot_diff (comparaison)")
                with LOCK:
                    diff, err = ot_comparer(conn, distant)
            finally:
                conn.close()
            if diff is None:
                return self._send_json(502, {"error": err})
            return self._send_json(200, diff)
        # Comptes MQTT des DT (19/11) : etat sans mot de passe, rapprochement
        # immediat (apres un changement de flotte), renouvellement d'un mot de passe.
        if path == "/api/mqtt/comptes" and method == "GET":
            return self._send_json(200, comptes_mqtt_etat())
        if path == "/api/mqtt/rapprocher" and method == "POST":
            return self._send_json(200, rapprocher_comptes_mqtt())
        if path == "/api/mqtt/renouveler" and method == "POST":
            body = json.loads(self._read_body() or b"{}")
            code = str(body.get("code") or "").strip()
            if not re.match(r"^[A-Za-z0-9_-]{2,40}$", code):
                return self._send_json(400, {"error": "code de DT invalide"})
            return self._send_json(200, renouveler_mdp_mqtt(code))
        # Enregistrements du tracking (20/11) : lectures / telechargements, hors verrou
        if path == "/api/tracking/export.csv" and method == "GET":
            qs = self._qs()
            depuis = (qs.get("depuis") or [""])[0].replace("T", " ")[:19]
            jusqua = (qs.get("jusqua") or [""])[0].replace("T", " ")[:19]
            if not _RE_HORODATAGE.match(depuis) or (jusqua and not _RE_HORODATAGE.match(jusqua)):
                return self._send_json(400, {"error": "depuis (obligatoire) / jusqua : AAAA-MM-JJ HH:MM:SS"})
            codes = [c for c in (qs.get("codes") or [""])[0].split(",") if c] or None
            return self._send_fichier(200, tracking_csv(depuis, jusqua, codes).encode("utf-8"), "text/csv; charset=utf-8",
                                      "tracking-%s_%s.csv" % (depuis[:10], depuis[11:16].replace(":", "h")))
        # 23/11 : rapports de dumping (verdict par mission) et de sorties de route. depuis / jusqua = ms epoch.
        if path in ("/api/rapports/dumping", "/api/rapports/offroad") and method == "GET":
            qs = self._qs()
            try:
                d_ms = float((qs.get("depuis") or ["0"])[0] or 0)
                j_ms = float((qs.get("jusqua") or ["0"])[0] or 0) or time.time() * 1000 + 86400000
            except ValueError:
                return self._send_json(400, {"error": "depuis / jusqua : millisecondes"})
            dt = (qs.get("dt") or [""])[0].strip().upper()
            fmt = (qs.get("format") or [""])[0]
            conn = get_conn()
            try:
                if path.endswith("dumping"):
                    loc = lambda ms: datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
                    rep_ = rapport_dumping(conn, loc(d_ms), loc(j_ms), dt)
                    if fmt == "csv":
                        cols = ["mission", "mission_sg", "dt_code", "debut", "fin", "matiere", "zone_prevue", "verdict",
                                "zone_dumping", "demi_tours"]
                        return self._send_fichier(200, _csv(rep_["missions"], cols).encode("utf-8"),
                                                  "text/csv; charset=utf-8", "dumping-report.csv")
                else:
                    utc = lambda ms: datetime.utcfromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M:%S")
                    rep_ = rapport_offroad(conn, utc(d_ms), utc(j_ms), dt)
                    if fmt == "csv":
                        cols = ["dt_code", "created_at", "distance_m", "retour_le", "retour_duree_s", "retour_cause", "message"]
                        return self._send_fichier(200, _csv(rep_["offroad"], cols).encode("utf-8"),
                                                  "text/csv; charset=utf-8", "offroad-report.csv")
                return self._send_json(200, rep_)
            finally:
                conn.close()
        if path == "/api/geofencing/reglages" and method == "GET":
            return self._send_json(200, reglages_zones())
        if path == "/api/enregistrements" and method == "GET":
            return self._send_json(200, enregistrements_liste())
        if path == "/api/enregistrements/fichier" and method == "GET":
            nom = (self._qs().get("nom") or [""])[0]
            if not re.match(r"^[A-Za-z0-9_.-]+\.(csv|mp4|webm)$", nom) or ".." in nom:
                return self._send_json(400, {"error": "nom de fichier invalide"})
            chemin = os.path.join(TRACKING_DIR if nom.endswith(".csv") else ENREGISTREMENTS_DIR, nom)
            if not os.path.isfile(chemin):
                return self._send_json(404, {"error": "fichier introuvable"})
            mime = "text/csv; charset=utf-8" if nom.endswith(".csv") else ("video/webm" if nom.endswith(".webm") else "video/mp4")
            taille = os.path.getsize(chemin)
            debut, fin, code = 0, taille - 1, 200
            m = re.match(r"^bytes=(\d*)-(\d*)$", self.headers.get("Range", "").strip())
            if m and (m.group(1) or m.group(2)) and taille:        # lecteur video : lecture par morceaux + avance rapide
                if m.group(1):
                    debut = int(m.group(1)); fin = int(m.group(2)) if m.group(2) else taille - 1
                else:
                    debut = max(0, taille - int(m.group(2))); fin = taille - 1
                fin = min(fin, taille - 1)
                if debut > fin:
                    self.send_response(416); self.send_header("Content-Range", "bytes */%d" % taille)
                    self.send_header("Content-Length", "0"); self.end_headers(); return
                code = 206
            with open(chemin, "rb") as fh:
                fh.seek(debut)
                corps = fh.read(fin - debut + 1)
            en_ligne = (self._qs().get("inline") or [""])[0] == "1"
            return self._send_fichier(code, corps, mime, nom, inline=en_ligne,
                                      plage=(debut, fin, taille) if code == 206 else None)
        if path == "/api/enregistrements/record" and method == "POST":
            b = json.loads(self._read_body() or b"{}")
            code, rep = record_action(str(b.get("action") or ""), b.get("etat") if isinstance(b.get("etat"), dict) else None,
                                      b.get("vitesse") or 1, str(b.get("qualite") or "haute"))
            return self._send_json(code, rep)
        if path == "/api/enregistrements/video" and method == "POST":
            b = json.loads(self._read_body() or b"{}")
            depuis, jusqua = str(b.get("depuis") or ""), str(b.get("jusqua") or "")
            if not (_RE_HORODATAGE.match(depuis) and _RE_HORODATAGE.match(jusqua)) or jusqua <= depuis:
                return self._send_json(400, {"error": "depuis / jusqua : AAAA-MM-JJ HH:MM:SS, jusqua apres depuis"})
            if datetime.strptime(jusqua, "%Y-%m-%d %H:%M:%S") - datetime.strptime(depuis, "%Y-%m-%d %H:%M:%S") > timedelta(hours=26):
                return self._send_json(400, {"error": "periode de 26 h au plus (un shift ou une journee de production)"})
            try:
                vitesse = max(10, min(int(b.get("vitesse") or 120), 1800))
            except (TypeError, ValueError):
                vitesse = 120
            if isinstance(b.get("fond"), dict):
                try:
                    fond_video_ecrire(b["fond"])         # decor de la carte (zones, pistes colorees) pour la video
                except Exception as e:
                    print("VIDEO: decor ignore (%s)" % e)
            ok, nom, message = lancer_video(depuis, jusqua, vitesse)
            return self._send_json(200 if ok else 422, {"ok": ok, "fichier": nom, "message": message})
        # 20/11 : /api/offroad et /api/alertes sont sondees chaque seconde par CHAQUE dashboard ouvert ;
        # sous le verrou global elles se disputaient la place des positions (donc des alertes).
        # Lecture seule : hors verrou, comme /api/suivi_dt et /api/kpi.
        # Bloc (minerai, a defaut sterile) qui contient un point : ?x=&y= (UTM) ou ?lat=&lng= (degres, projetes)
        if path == "/api/bloc_au_point" and method == "GET":
            qs = self._qs()
            try:
                if qs.get("lat") and qs.get("lng"):
                    conn_lecture = get_conn()
                    try:
                        pt, _ = points_en_utm(conn_lecture, [[float(qs["lng"][0]), float(qs["lat"][0])]])
                    finally:
                        conn_lecture.close()
                    x, y = pt[0]
                else:
                    x, y = float(qs["x"][0]), float(qs["y"][0])
            except (KeyError, ValueError, IndexError):
                return self._send_json(400, {"error": "x et y (UTM) ou lat et lng (degres) attendus"})
            conn = get_conn()
            try:
                return self._send_json(200, {"x": x, "y": y, "bloc": bloc_au_point(conn, x, y)})
            finally:
                conn.close()
        if path == "/api/minages" and method == "GET":
            conn = get_conn()
            try:
                return self._send_json(200, {"minages": minages_en_cours(conn), "maintenant": now_iso()})
            finally:
                conn.close()
        if path == "/api/couleurs" and method == "GET":
            conn = get_conn()
            try:
                return self._send_json(200, couleurs_teneur(conn))
            finally:
                conn.close()
        if path == "/api/codes_teneur" and method == "GET":
            return self._send_json(200, {"codes": codes_teneur(), "steriles": list(codes_steriles()),
                                         "grades": list(GRADES_VALIDES), "couleurs": couleurs_grades()})
        if path == "/api/offroad" and method == "GET":
            conn = get_conn()
            try:
                return self._send_json(200, etat_offroad(conn))
            finally:
                conn.close()
        if path == "/api/alertes" and method == "GET":
            depuis = (self._qs().get("depuis") or [""])[0]
            jusqua = (self._qs().get("jusqua") or [""])[0]
            for v in (depuis, jusqua):
                if v and not _RE_HORODATAGE.match(v):
                    return self._send_json(400, {"error": "depuis / jusqua attendus au format AAAA-MM-JJ HH:MM:SS"})
            conn = get_conn()
            try:
                return self._send_json(200, {"alertes": alertes_liste(conn, depuis, jusqua)})
            finally:
                conn.close()
        # Suivi des DT (19/11) : lecture seule, HORS verrou (sondee toutes les
        # 2-3 s par chaque carte ouverte). ?depuis=&jusqua= (AAAA-MM-JJ HH:MM:SS)
        # ajoute les trajets de la periode.
        if path == "/api/suivi_dt" and method == "GET":
            qs = self._qs()
            depuis = (qs.get("depuis") or [""])[0].replace("T", " ")[:19]
            jusqua = (qs.get("jusqua") or [""])[0].replace("T", " ")[:19]
            motif = r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?$"
            for v in (depuis, jusqua):
                if v and not re.match(motif, v):
                    return self._send_json(400, {"error": "depuis/jusqua : AAAA-MM-JJ HH:MM:SS attendu"})
            conn = get_conn()
            try:
                return self._send_json(200, suivi_engins(conn, depuis or None, jusqua or None))
            finally:
                conn.close()
        # KPI du jour : depuis le releve des Chargements de SG (19/11). HORS
        # verrou : un autre jour declenche un appel au cloud (quelques s).
        # 25/11 (calibrage) : objectifs « Planned » des KPI, gardes sur la passerelle (et plus seulement dans le
        # navigateur de la page Live) : le dashboard de tout poste lit les memes objectifs.
        if path == "/api/kpi/objectifs":
            if method == "GET":
                try:
                    obj = json.loads(param_lire_base("KPI_OBJECTIFS", "") or "{}")
                except ValueError:
                    obj = {}
                return self._send_json(200, {"objectifs": obj if isinstance(obj, dict) else {}})
            if method == "POST":
                try:
                    body = json.loads(self._read_body() or b"{}")
                except ValueError:
                    return self._send_json(400, {"error": "JSON attendu"})
                obj = body.get("objectifs") if isinstance(body, dict) else None
                if not isinstance(obj, dict):
                    return self._send_json(400, {"error": "objectifs attendus : {cle: {planned, alerte}}"})
                propre = {}
                for k, v in obj.items():
                    if not re.match(r"^[a-z]{2,10}$", str(k)) or not isinstance(v, dict):
                        continue
                    e = {}
                    for prop in ("planned", "alerte"):
                        try:
                            if v.get(prop) not in (None, ""):
                                e[prop] = float(v.get(prop))
                        except (TypeError, ValueError):
                            pass
                    propre[k] = e
                conn = get_conn()
                try:
                    param_ecrire(conn, "KPI_OBJECTIFS", json.dumps(propre))
                finally:
                    conn.close()
                return self._send_json(200, {"ok": True, "objectifs": propre})
            return self._send_json(405, {"error": "methode non supportee"})
        if path == "/api/kpi" and method == "GET":
            jour = (self._qs().get("jour") or [None])[0]
            depuis = (self._qs().get("depuis") or [""])[0]
            jusqua = (self._qs().get("jusqua") or [""])[0]
            if jour and not re.match(r"^\d{4}-\d{2}-\d{2}$", jour):
                return self._send_json(400, {"error": "jour attendu au format AAAA-MM-JJ"})
            for v in (depuis, jusqua):
                if v and not _RE_HORODATAGE.match(v):
                    return self._send_json(400, {"error": "depuis / jusqua attendus au format AAAA-MM-JJ HH:MM:SS"})
            return self._send_json(200, kpi_du_jour(jour, depuis or None, jusqua or None))
        # Tablettes (adb) : detection, Wi-Fi, affectation, installation. Hors verrou : adb prend plusieurs secondes.
        if path.startswith("/api/tablettes"):
            return self._route_tablettes(path, method)
        if path.startswith("/api/gnss"):
            return self._route_gnss(path, method)
        if path.startswith("/api/jetons"):
            return self._route_jetons(path, method)
        if path == "/api/radio/emettre" and method == "POST":
            conn = get_conn()
            try:
                body = json.loads(self._read_body() or b"{}")
                ligne = trame_pelle_test(conn, str(body.get("pelle") or ""), str(body.get("mode") or ""), str(body.get("dt") or "DT-01"), str(body.get("type") or "prop"),
                                         ((body.get("E"), body.get("N")) if body.get("E") not in (None, "") and body.get("N") not in (None, "") else None))
                ok, motif = xbee_emettre(ligne)
            except (ValueError, TypeError) as e:
                return self._send_json(400, {"error": str(e)})
            finally:
                conn.close()
            return self._send_json(200 if ok else 503, {"ok": ok, "erreur": motif, "ligne": ligne})
        if path == "/api/radio":
            if method == "GET":
                return self._send_json(200, xbee_etat_public())
            if method == "POST":
                conn = get_conn()
                try:
                    body = json.loads(self._read_body() or b"{}")
                    if "exiger_signature" in body and "port" not in body:
                        param_ecrire(conn, "RADIO_EXIGER_SIGNATURE", "1" if body.get("exiger_signature") else "")
                        code, rep_ = 200, {"ok": True, "exiger_signature": radio_exiger_signature()}
                    else:
                        code, rep_ = xbee_ecrire_conf(conn, body.get("port"), body.get("baud"))
                finally:
                    conn.close()
                return self._send_json(code, rep_)
            return self._send_json(405, {"error": "methode non supportee"})
        # Test des liaisons sortantes : appels au cloud, donc HORS verrou.
        if path == "/api/relais/tester" and method == "POST":
            return self._send_json(200, relais_tester())
        # Synchro du referentiel de Serveur_SG (apres une poussee vers OT) :
        # appel au cloud (jusqu'a 1-2 min), donc HORS verrou.
        if path == "/api/sg/sync_ref" and method == "POST":
            ok, d, err = sg_sync_ref()
            if not ok:
                return self._send_json(502, {"ok": False, "error": err})
            return self._send_json(200, {k: d.get(k) for k in (
                "ok", "oreblocs", "dumping_areas", "teneurs", "pistes",
                "checkpoints", "teneurs_sans_consigne", "note")})
        conn = get_conn()
        try:
            LOCK.route("%s %s" % (method, path))
            with LOCK:

                # ------------------------------------------------- bl --
                # Blasts : un blast = numero + fichier source (CSV de
                # polygones [x,y] ou [nom,x,y] / DXF contenant des
                # POLYLINE / LWPOLYLINE). Chaque polyline devient un
                # orebloc rattache au blast (blast_id).
                if path == "/api/blasts" and method == "GET":
                    rows = [dict(r) for r in conn.execute(
                        "SELECT * FROM blasts WHERE actif=1 ORDER BY id DESC")]
                    for b in rows:
                        # Carte affichable dans la page : copie locale. Voir
                        # apercu_carte() -- l'URL Drive est bloquee a
                        # l'integration par le navigateur.
                        b["map_apercu"] = apercu_carte(b.get("name_map"))
                        b["oreblocs"] = [dict(r) for r in conn.execute(
                            "SELECT id, nom, grade_cutoff, sommets, source_fichier, "
                            "volume, tonnes, au_teneur, density, destination, litho, minerai "
                            "FROM oreblocs WHERE blast_id=? AND actif=1 ORDER BY id", (b["id"],))]
                    return self._send_json(200, {"blasts": rows})

                # champs de metadonnee "Dig Plan" optionnels, communs a POST (creation)
                # et PUT (modification) d'un blast.

                if path == "/api/blasts" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    numero = (body.get("numero") or "").strip()
                    source_fichier = (body.get("source_fichier") or "").strip()
                    contenu = body.get("contenu") or ""
                    base_contenu = (body.get("base_contenu") or "")
                    if not numero:
                        return self._send_json(400, {"error": "numero de blast requis"})
                    # --- Anti-doublon -------------------------------------------
                    # Un meme numero dans la meme source miniere est unique :
                    # sans cette garde, un double-clic sur Enregistrer (ou un
                    # import relance) cree N blasts identiques avec chacun
                    # leurs oreblocs, et tout le suivi (missions, cumuls)
                    # devient faux.
                    source_nom = (body.get("source_nom") or "").strip().upper()
                    deja = conn.execute(
                        "SELECT id FROM blasts WHERE numero=? COLLATE NOCASE "
                        "AND UPPER(COALESCE(source_nom,''))=? AND actif=1",
                        (numero, source_nom)).fetchone()
                    if deja:
                        return self._send_json(409, {
                            "error": "le blast %s existe deja dans %s (id %d)" % (
                                numero, body.get("source_nom") or "?", deja["id"]),
                            "blast_id": deja["id"]})

                    # --- Garde-fou source / code de blast -----------------------
                    # Le 3e segment du nom porte le code de source
                    # (275_OM_LMP2_280_270_119 -> LMP2). Chaque source connait
                    # ses codes admis (table sources.codes). Si le code du
                    # blast n'appartient pas a la source visee : rejet + trace
                    # (evite d'ajouter un LWP2 dans Main Pit par erreur).
                    def codes_source(conn, source_nom):
                        if not source_nom:
                            return []
                        row = conn.execute(
                            "SELECT codes FROM sources WHERE UPPER(nom)=UPPER(?) AND actif=1",
                            (source_nom,)).fetchone()
                        if not row:
                            return []
                        return [c.strip().upper() for c in (row["codes"] or "").split(",") if c.strip()]

                    def code_blast(numero):
                        """Code de source suppose du numero. La nomenclature
                        officielle place le code en 3e segment (275_OM_LMP2_),
                        mais le formulaire Dig Plan pose des noms courts
                        (LMP2_280_270_119) ou technique (TEST_BASE_277) ou il
                        n'existe pas. On accepte le numero si L'UN de ses
                        segments est un code admis de la source ; a defaut on
                        renvoie le candidat le plus plausible (3e, sinon 1er)
                        pour un message d'erreur clair."""
                        segs = [s.upper() for s in re.split(r"[_\-]+", numero or "") if s]
                        if not segs:
                            return ""
                        if len(segs) >= 3:
                            return segs[2]
                        return segs[0]

                    codes_admis = codes_source(conn, body.get("source_nom"))
                    if codes_admis:
                        segs = [s.upper() for s in re.split(r"[_\-]+", numero or "") if s]
                        # le numero passe si L'UN de ses segments est un code
                        # admis (nom long 275_OM_LMP2_, nom court LMP2_, ou
                        # nom technique sans code : on ne bloque pas un test).
                        if not any(s in codes_admis for s in segs):
                            code = code_blast(numero)
                            # trace persistee : une tentative d'ajout erronee
                            # doit laisser une trace dans les alertes
                            creer_alerte(conn, "blast_rejete", "-",
                                         "Blast %s refuse : code %s n'appartient pas a %s "
                                         "(codes admis : %s)" % (numero, code,
                                         body.get("source_nom") or "?", ", ".join(codes_admis)),
                                         voix_off=0, dest_office=0)
                            return self._send_json(422, {
                                "error": "Blast refuse : le code '%s' n'appartient pas a la source '%s' "
                                         "(codes admis : %s). Verifiez le nom ou la source." % (
                                             code, body.get("source_nom") or "?", ", ".join(codes_admis))})
                    # --- Garde-fou source / TENEUR des blocs (18/11) ---------
                    # Le nom peut etre de la bonne source (LMP2) et les blocs
                    # de l'autre pit (F5W) : constate sur 260_OM_LMP2_280_270_201,
                    # accepte dans Main Pit. Un seul bloc de minerai de l'autre
                    # pit suffit a refuser le blast (avant toute ecriture).
                    pit_src = pit_de_source(codes_admis, body.get("source_nom"))
                    intrus = blocs_autre_pit(
                        [body.get("geosum_contenu") or "", body.get("base_contenu") or ""], pit_src)
                    if intrus:
                        autre = "West Pit" if pit_src == "M" else "Main Pit"
                        detail = ", ".join("%s (%s)" % (b, tn) for b, tn in intrus[:8])
                        if len(intrus) > 8:
                            detail += " ... (+%d)" % (len(intrus) - 8)
                        creer_alerte(conn, "blast_rejete", "-",
                                     "Blast %s refuse : %d bloc(s) de %s dans la source %s : %s" % (
                                         numero, len(intrus), autre, body.get("source_nom") or "?", detail),
                                     voix_off=0, dest_office=0)
                        return self._send_json(422, {
                            "error": "Blast refuse : %d bloc(s) ont une teneur de %s alors que la source est "
                                     "'%s' : %s. Verifiez la source ou le Geosum." % (
                                         len(intrus), autre, body.get("source_nom") or "?", detail),
                            "blocs_autre_pit": [{"bloc": b, "teneur": tn} for b, tn in intrus]})
                    # --- JAMAIS DE BLAST SANS CARTE (18/11, regle client) ---
                    # Un blast (RL) sans plan ne peut ni etre affiche ni etre
                    # suivi sur la tablette : la creation est refusee, avec
                    # une trace dans les alertes, avant toute ecriture.
                    if not body.get("map_contenu") and not str(body.get("map_url") or "").strip():
                        creer_alerte(conn, "blast_rejete", "-",
                                     "Blast %s refuse : carte (plan) absente" % numero,
                                     voix_off=0, dest_office=0)
                        return self._send_json(422, {
                            "error": "Blast refuse : la carte (plan image) est obligatoire. "
                                     "Joignez la Map avant d'enregistrer."})
                    extraits = []
                    if contenu:
                        bas = source_fichier.lower()
                        if bas.endswith(".dxf"):
                            extraits = extraire_oreblocs_dxf(contenu, prefixe="Orebloc %s" % numero)
                        else:
                            extraits = extraire_oreblocs_csv(contenu, prefixe="Orebloc %s" % numero)
                    elif base_contenu:
                        # Champ "Base de donnees" (CSV des polygones) : il sert
                        # aussi de source de blocs quand aucun DXF n'est fourni
                        # (nouveau flux Dig Plan). Sans ce repli, un blast cree
                        # avec Base seule sortait sans aucun orebloc local.
                        extraits = extraire_oreblocs_csv(base_contenu, prefixe="Orebloc %s" % numero)
                    if not extraits:
                        return self._send_json(422, {
                            "error": "aucun polygone exploitable (>= 3 points) dans ce fichier"})
                    # 26/11 (decision utilisateur) : BANCS SUPERPOSES / DOUBLONS. Un blast est identifie par son nom
                    # integral, mais les noms de ses blocs ne portent pas la profondeur. Un autre blast actif de meme
                    # flitch / source / numero, ou des blocs dont le nom est deja pris, se melangeraient : on alerte et
                    # on ne valide pas (avant : blocs renommes « (2) », que le fichier de base et le Geosum ne
                    # reconnaissaient plus).
                    pref = prefixe_blast(numero)
                    if pref:
                        for autre in conn.execute("SELECT id, numero FROM blasts WHERE actif=1 AND id>0"):
                            if autre["numero"] != numero and prefixe_blast(autre["numero"]) == pref:
                                return self._send_json(409, {
                                    "error": "Import refuse (superposition) : le blast %s est encore actif et ses blocs "
                                             "portent les memes noms (%s_...). Verifier, puis supprimer l'ancien blast "
                                             "avant d'importer %s." % (autre["numero"], pref, numero),
                                    "blast_id": autre["id"]})
                    pris = []
                    for nom, _ in extraits:
                        prop = proprietaire_nom_bloc(conn, nom)
                        if prop:
                            pris.append("%s (blast %s)" % (nom, prop))
                    if pris:
                        return self._send_json(409, {
                            "error": "Import refuse : %d bloc(s) de ce fichier existent deja dans un blast actif : %s%s. "
                                     "Verifier le fichier, ou supprimer l'ancien blast avant de reimporter."
                                     % (len(pris), ", ".join(pris[:3]), " ..." if len(pris) > 3 else "")})
                    # carte : le contenu part sur disque, seule l'URL est stockee
                    if body.get("map_contenu"):
                        body["map_url"] = deposer_carte(
                            body.get("name_map") or (numero + ".jpg"),
                            body["map_contenu"],
                            self.headers.get("Host") or ("localhost:%d" % PORT))
                    meta_cols = [c for c in BLAST_META_FIELDS if body.get(c)]
                    meta_vals = [str(body[c]).strip() for c in meta_cols]
                    sql = ("INSERT INTO blasts (numero, source_fichier, nb_blocs" +
                           "".join(", " + c for c in meta_cols) +
                           ") VALUES (?, ?, ?" + ", ?" * len(meta_cols) + ")")
                    cur = conn.execute(sql, tuple([numero, source_fichier, len(extraits)] + meta_vals))
                    blast_id = cur.lastrowid
                    blocs_crees = []
                    for nom, sommets in extraits:
                        # 26/11 : noms deja pris refuses plus haut : le nom du fichier est garde tel quel
                        nom_final = nom
                        # teneur de coupure heritee d'un eventuel champ grade_cutoff
                        grade = _num(body.get("grade_cutoff")) or DEFAULT_CUTOFF_GRADE
                        c2 = conn.execute(
                            "INSERT INTO oreblocs (nom, grade_cutoff, sommets, source_fichier, blast_id) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (nom_final, grade, json.dumps(sommets), source_fichier, blast_id))
                        blocs_crees.append({"id": c2.lastrowid, "nom": nom_final,
                                            "nb_sommets": len(sommets)})
                    conn.commit()

                    # Le Geosum qualifie les blocs qu'on vient de creer : il
                    # doit donc passer APRES l'insertion des polygones. Il
                    # porte tonnage, teneur mesuree, densite, litho et
                    # famille de destination, et distingue minerai et sterile.
                    # La base porte les attributs de TOUS les blocs, waste compris :
                    # elle passe avant le Geosum, qui garde le dernier mot.
                    base_rapport = None
                    if base_contenu:
                        base_rapport = appliquer_base_attributs(conn, blast_id, base_contenu)
                    geosum_rapport = None
                    if body.get("geosum_contenu"):
                        geosum = extraire_geosum(body["geosum_contenu"])
                        if geosum:
                            geosum_rapport = appliquer_geosum(conn, blast_id, geosum)
                        else:
                            geosum_rapport = {"erreur":
                                "Geosum illisible : CSV ou .xlsx attendu, avec une "
                                "colonne de noms (label, Sources ou PolygonID)"}

                    # Miroir vers le classeur. Un echec reseau ne doit pas
                    # perdre la saisie : le blast reste en base, marque non
                    # synchronise, et repoussable par /api/ot_blasts.
                    ot_ok, ot_err = ot_pousser_blast(conn, blast_id)
                    # Base de donnees + Geosum -> onglets dig_bloc / ore_dig_bloc :
                    # c'est ce que lit la tablette pour les polygones precis.
                    # Echec non bloquant : le blast existe, les onglets seront
                    # repoussables (le push de sync reprend ot_pousser_onglets).
                    onglets_err = ""
                    if base_contenu or body.get("geosum_contenu"):
                        ok_onglets, onglets_err = ot_pousser_onglets_blocs(
                            conn, blast_id, numero, base_contenu,
                            body.get("geosum_contenu") or "")
                        if not ok_onglets:
                            print("OT: onglets blocs #%s non synchronises (%s)" % (
                                blast_id, onglets_err))
                    # 26/11 (essai DT-15) : Serveur_SG doit recopier le referentiel tout de suite ; sinon il classe les
                    # chargements avec ses anciens blocs (« hors de tout orebloc » -> compte en waste au lieu du minerai)
                    threading.Thread(target=sg_sync_ref_fond, args=("blast %s cree" % numero,), daemon=True).start()
                    return self._send_json(201, {
                        "ok": True, "id": blast_id, "numero": numero,
                        "nb_blocs": len(blocs_crees), "oreblocs": blocs_crees,
                        "geosum": geosum_rapport, "base": base_rapport,
                        "ot_sync": ot_ok, "ot_erreur": ot_err,
                        "onglets_sync": not onglets_err, "onglets_erreur": onglets_err})

                if path == "/api/blasts" and method == "PUT":
                    body = json.loads(self._read_body() or b"{}")
                    bid = int(body.get("id", 0))
                    if not bid:
                        return self._send_json(400, {"error": "id requis"})
                    ex = conn.execute("SELECT id, clos_le FROM blasts WHERE id=? AND actif=1",
                                      (bid,)).fetchone()
                    if not ex:
                        return self._send_json(404, {"error": "blast introuvable"})
                    if (ex["clos_le"] or "").strip():
                        return self._send_json(409, {"error": "blast clos : modification impossible"})
                    if body.get("numero"):
                        conn.execute("UPDATE blasts SET numero=? WHERE id=?",
                                     (str(body["numero"]).strip(), bid))
                    # carte : depot sur disque, on ne conserve que l'URL
                    if body.get("map_contenu"):
                        body["map_url"] = deposer_carte(
                            body.get("name_map") or ("blast_%d.jpg" % bid),
                            body["map_contenu"],
                            self.headers.get("Host") or ("localhost:%d" % PORT))
                    for c in BLAST_META_FIELDS:
                        if c in body:
                            conn.execute("UPDATE blasts SET %s=? WHERE id=?" % c,
                                         (str(body[c] or "").strip(), bid))
                    conn.commit()
                    ot_ok, ot_err = ot_pousser_blast(conn, bid)
                    # Base de donnees jointe en modification : elle part dans
                    # `dig_bloc`, comme a la creation. Seul cet onglet est
                    # pousse : `ore_dig_bloc` (Geosum) n'est pas touche.
                    onglets_err = ""
                    base_contenu = body.get("base_contenu") or ""
                    base_rapport = None
                    if base_contenu:
                        # Attributs des blocs (waste compris) depuis la base ; le
                        # Geosum, envoye ensuite par la page, garde le dernier mot.
                        base_rapport = appliquer_base_attributs(conn, bid, base_contenu)
                        numero_b = conn.execute(
                            "SELECT numero FROM blasts WHERE id=?", (bid,)).fetchone()[0]
                        ok_onglets, onglets_err = ot_pousser_onglets_blocs(
                            conn, bid, numero_b, base_contenu, "")
                        if not ok_onglets:
                            print("OT: onglet dig_bloc #%s non synchronise (%s)" % (
                                bid, onglets_err))
                    return self._send_json(200, {"ok": True, "id": bid, "base": base_rapport,
                                                 "ot_sync": ot_ok, "ot_erreur": ot_err,
                                                 "onglets_sync": not onglets_err,
                                                 "onglets_erreur": onglets_err})

                # Application d'un Geosum a un blast existant. Separe de
                # /api/blasts_import : celui-la cree des polygones, celui-ci
                # qualifie ceux qui existent deja.
                if path == "/api/blasts_geosum" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    bid = int(body.get("blast_id", 0))
                    contenu = body.get("contenu") or ""
                    if not bid or not contenu:
                        return self._send_json(400, {"error": "blast_id et contenu requis"})
                    if not conn.execute("SELECT id FROM blasts WHERE id=? AND actif=1",
                                        (bid,)).fetchone():
                        return self._send_json(404, {"error": "blast introuvable"})
                    geosum = extraire_geosum(contenu)
                    if not geosum:
                        return self._send_json(422, {
                            "error": "Geosum illisible : CSV ou .xlsx attendu, avec une "
                                     "colonne de noms (label, Sources ou PolygonID)"})
                    rapport = appliquer_geosum(conn, bid, geosum)
                    ot_ok, ot_err = ot_pousser_blast(conn, bid)
                    rapport["ot_sync"] = ot_ok
                    rapport["ot_erreur"] = ot_err
                    # Le Geosum est aussi l'onglet `ore_dig_bloc` des tablettes :
                    # en modification, il n'etait jamais pousse (12/11).
                    numero_b = conn.execute(
                        "SELECT numero FROM blasts WHERE id=?", (bid,)).fetchone()[0]
                    ok_ore, err_ore = ot_pousser_onglets_blocs(
                        conn, bid, numero_b, "", contenu)
                    if not ok_ore:
                        print("OT: onglet ore_dig_bloc #%s non synchronise (%s)" % (
                            bid, err_ore))
                    rapport["onglets_sync"] = ok_ore
                    rapport["onglets_erreur"] = err_ore
                    return self._send_json(200, rapport)

                # import d'un fichier CSV/DXF supplementaire dans un blast existant
                # (ajoute des oreblocs sans creer un nouveau blast).
                if path == "/api/blasts_import" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    bid = int(body.get("blast_id", 0))
                    source_fichier = (body.get("source_fichier") or "fichier").strip()
                    contenu = body.get("contenu") or ""
                    if not bid:
                        return self._send_json(400, {"error": "blast_id requis"})
                    b = conn.execute("SELECT id, numero FROM blasts WHERE id=? AND actif=1", (bid,)).fetchone()
                    if not b:
                        return self._send_json(404, {"error": "blast introuvable"})
                    if not contenu:
                        return self._send_json(400, {"error": "contenu du fichier requis"})
                    bas = source_fichier.lower()
                    if bas.endswith(".dxf"):
                        extraits = extraire_oreblocs_dxf(contenu, prefixe="Orebloc %s" % b["numero"])
                    else:
                        extraits = extraire_oreblocs_csv(contenu, prefixe="Orebloc %s" % b["numero"])
                    if not extraits:
                        return self._send_json(422, {
                            "error": "aucun polygone exploitable (>= 3 points) dans ce fichier"})
                    grade = _num(body.get("grade_cutoff")) or DEFAULT_CUTOFF_GRADE
                    blocs_crees = []
                    blocs_remplaces = []
                    # Un polygone deja present dans CE blast (meme cle : le DXF
                    # ecrit 277_5 la ou la base ecrit 277.5) remplace sa
                    # geometrie : rejoindre le DXF en modification ne doit pas
                    # dupliquer les blocs (blast 38, 12/11 : 11 blocs ` (2)`).
                    existants = {
                        cle_bloc(o["nom"]): o["id"] for o in conn.execute(
                            "SELECT id, nom FROM oreblocs WHERE blast_id=? AND actif=1",
                            (bid,))}
                    # 26/11 : un bloc NOUVEAU dont le nom appartient deja a un autre blast actif : refus (plus de « (2) »)
                    pris = ["%s (blast %s)" % (nom, proprietaire_nom_bloc(conn, nom)) for nom, _ in extraits
                            if not existants.get(cle_bloc(nom)) and proprietaire_nom_bloc(conn, nom)]
                    if pris:
                        return self._send_json(409, {
                            "error": "Modification refusee : %d bloc(s) existent deja dans un autre blast actif : %s%s. "
                                     "Verifier le fichier." % (len(pris), ", ".join(pris[:3]), " ..." if len(pris) > 3 else "")})
                    for nom, sommets in extraits:
                        oid = existants.get(cle_bloc(nom))
                        if oid:
                            conn.execute(
                                "UPDATE oreblocs SET sommets=?, source_fichier=? WHERE id=?",
                                (json.dumps(sommets), source_fichier, oid))
                            blocs_remplaces.append({"id": oid, "nom": nom,
                                                    "nb_sommets": len(sommets)})
                            continue
                        nom_final = nom
                        if nom_bloc_pris(conn, nom_final):
                            n = 2
                            while nom_bloc_pris(conn, "%s (%d)" % (nom, n)):
                                n += 1
                            nom_final = "%s (%d)" % (nom, n)
                        c2 = conn.execute(
                            "INSERT INTO oreblocs (nom, grade_cutoff, sommets, source_fichier, blast_id) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (nom_final, grade, json.dumps(sommets), source_fichier, bid))
                        blocs_crees.append({"id": c2.lastrowid, "nom": nom_final,
                                            "nb_sommets": len(sommets)})
                    conn.execute("UPDATE blasts SET nb_blocs = nb_blocs + ? WHERE id=?",
                                 (len(blocs_crees), bid))
                    conn.commit()
                    return self._send_json(201, {"ok": True, "id": bid,
                                                  "nb_blocs_ajoutes": len(blocs_crees),
                                                  "nb_blocs_remplaces": len(blocs_remplaces),
                                                  "oreblocs": blocs_crees + blocs_remplaces})

                # ETAT CLOS (18/11, demande client) : le blast est termine.
                # Il reste en base (consultable), n'est plus modifiable ni
                # repousse, et Serveur_OT le publie dans `clos` : c'est le
                # signal qui autorise les tablettes a purger leurs donnees
                # (une fois leurs envois recus).
                if path == "/api/blasts_clore" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    bid = int(body.get("id", 0))
                    if not bid:
                        return self._send_json(400, {"error": "id requis"})
                    if not conn.execute("SELECT id FROM blasts WHERE id=? AND actif=1",
                                        (bid,)).fetchone():
                        return self._send_json(404, {"error": "blast introuvable"})
                    clore_blast_local(conn, bid)
                    ot_ok, ot_err = ot_clore_blast(conn, bid)
                    clos_le = conn.execute("SELECT clos_le FROM blasts WHERE id=?",
                                           (bid,)).fetchone()[0]
                    return self._send_json(200, {"ok": True, "id": bid, "clos_le": clos_le,
                                                 "ot_sync": ot_ok, "ot_erreur": ot_err})

                if path == "/api/blasts" and method == "DELETE":
                    qs = self._qs()
                    bid = int(qs.get("id", ["0"])[0])
                    if not bid:
                        return self._send_json(400, {"error": "id requis"})
                    ligne = conn.execute("SELECT numero FROM blasts WHERE id=?",
                                         (bid,)).fetchone()
                    numero = ligne["numero"] if ligne else ""
                    # Supprimer = clore puis retirer (18/11) : sans la
                    # cloture, la tablette ne verrait qu'une ABSENCE et
                    # garderait le blast 24 h.
                    # 26/11 : la cloture vise le NOM INTEGRAL ; si un autre blast actif porte ce nom (doublon), elle le
                    # clorait aussi : on ne l'envoie pas (le blast est seulement retire)
                    homonyme = conn.execute("SELECT id FROM blasts WHERE numero=? AND actif=1 AND id<>?",
                                            (numero, bid)).fetchone() if numero else None
                    if not homonyme:
                        clore_blast_local(conn, bid)
                        ot_clore_blast(conn, bid)
                    conn.execute("UPDATE blasts SET actif=0 WHERE id=?", (bid,))
                    conn.execute("UPDATE oreblocs SET actif=0 WHERE blast_id=?", (bid,))
                    conn.commit()
                    # Le classeur doit oublier le blast lui aussi, sinon il
                    # ressortirait au prochain sync_ref de Serveur_SG.
                    ot_ok, ot_err = ot_supprimer_blast(numero)
                    # Les lignes du blast disparaissent aussi des onglets
                    # dig_bloc / ore_dig_bloc, en INTEGRALITE : c'est la seule
                    # voie pour que la tablette n'affiche plus ses polygones.
                    # 26/11 : la purge des onglets se fait par prefixe de bloc ; un autre blast actif de meme prefixe
                    # (superposition anterieure aux refus) perdrait ses lignes : on ne purge pas dans ce cas
                    pref = prefixe_blast(numero)
                    voisin = pref and any(prefixe_blast(r["numero"]) == pref for r in conn.execute(
                        "SELECT numero FROM blasts WHERE actif=1 AND id<>?", (bid,)))
                    if voisin:
                        ok_onglets, onglets_err = False, "purge des onglets evitee : un autre blast actif a les memes noms de blocs"
                    else:
                        ok_onglets, onglets_err = ot_supprimer_blast_onglets(
                            numero_lot_onglets(conn, bid, numero))
                    threading.Thread(target=sg_sync_ref_fond, args=("blast %s supprime" % numero,), daemon=True).start()
                    return self._send_json(200, {"ok": True, "id": bid,
                                                 "ot_sync": ot_ok, "ot_erreur": ot_err,
                                                 "onglets_sync": ok_onglets,
                                                 "onglets_erreur": onglets_err})

                # adjustment teneur de coupure / renommage / champs Dig Plan d'un orebloc
                if path == "/api/orebloc_maj" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    oid = int(body.get("id", 0))
                    if not oid:
                        return self._send_json(400, {"error": "id requis"})
                    ex = conn.execute("SELECT id FROM oreblocs WHERE id=? AND actif=1", (oid,)).fetchone()
                    if not ex:
                        return self._send_json(404, {"error": "orebloc introuvable"})
                    if "grade_cutoff" in body and body["grade_cutoff"] not in (None, ""):
                        conn.execute("UPDATE oreblocs SET grade_cutoff=? WHERE id=?",
                                     (float(_num(body["grade_cutoff"]) or DEFAULT_CUTOFF_GRADE), oid))
                    if body.get("nom"):
                        conn.execute("UPDATE oreblocs SET nom=? WHERE id=?", (body["nom"].strip(), oid))
                    for c in ("volume", "tonnes", "au_teneur", "density"):
                        if c in body and body[c] not in (None, ""):
                            conn.execute("UPDATE oreblocs SET %s=? WHERE id=?" % c,
                                         (float(_num(body[c]) or 0), oid))
                    for c in ("destination", "litho"):
                        if c in body:
                            conn.execute("UPDATE oreblocs SET %s=? WHERE id=?" % c,
                                         (str(body[c] or "").strip(), oid))
                    conn.commit()
                    return self._send_json(200, {"ok": True, "id": oid})

                if path == "/api/dumping_areas" and method == "DELETE":
                    qs = self._qs()
                    did = int(qs.get("id", ["0"])[0])
                    if not did:
                        return self._send_json(400, {"error": "id requis"})
                    conn.execute("UPDATE dumping_areas SET actif=0 WHERE id=?", (did,))
                    # Suppression douce : la zone reste en base mais sort des
                    # listes. Les teneurs qui la visaient garderaient sinon un
                    # dumping_area_id orphelin -- la page affiche "Aucune" alors
                    # que la base dit 39, et pousserReferentiel() enverrait une
                    # consigne VIDE en ligne sans rien signaler. On remet donc
                    # ces teneurs a 0 : l'etat devient honnete.
                    cur = conn.execute(
                        "SELECT nom FROM teneurs WHERE dumping_area_id=?", (did,))
                    liberees = [r[0] for r in cur.fetchall()]
                    if liberees:
                        conn.execute(
                            "UPDATE teneurs SET dumping_area_id=0 WHERE dumping_area_id=?",
                            (did,))
                    conn.commit()
                    return self._send_json(200, {
                        "ok": True, "id": did, "teneurs_liberees": liberees})

                if path == "/api/travels_roads" and method == "DELETE":
                    qs = self._qs()
                    rid = int(qs.get("id", ["0"])[0])
                    if not rid:
                        return self._send_json(400, {"error": "id requis"})
                    conn.execute("UPDATE travels_roads SET actif=0 WHERE id=?", (rid,))
                    conn.commit()
                    return self._send_json(200, {"ok": True, "id": rid})

                if path == "/api/oreblocs" and method == "DELETE":
                    qs = self._qs()
                    oid = int(qs.get("id", ["0"])[0])
                    if not oid:
                        return self._send_json(400, {"error": "id requis"})
                    conn.execute("UPDATE oreblocs SET actif=0 WHERE id=?", (oid,))
                    conn.commit()
                    return self._send_json(200, {"ok": True, "id": oid})

                if path == "/api/engins" and method == "GET":
                    engins = statuts_engins(conn)
                    return self._send_json(200, {"engins": engins})

                if path == "/api/engins" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    code = body.get("code")
                    typ = body.get("type", "DT")
                    x = float(body.get("x", 0))
                    y = float(body.get("y", 0))
                    if not code:
                        return self._send_json(400, {"error": "code requis"})
                    appliquer_position_engin(conn, code, typ, x, y, canal="console", auth="console")
                    # recalcul statuts + affectation automatique si DT devient ODT
                    statuts_engins(conn)
                    verifier_affectations_auto(conn)
                    notifier_office(conn)
                    return self._send_json(201, {"ok": True, "code": code})

                if path == "/api/oreblocs" and method == "GET":
                    rows = [dict(r) for r in conn.execute("SELECT * FROM oreblocs WHERE actif=1")]
                    return self._send_json(200, {"oreblocs": rows})

                if path == "/api/oreblocs" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    nom = body.get("nom", "Orebloc")
                    sommets = body.get("sommets", [])
                    grade = float(body.get("grade_cutoff", DEFAULT_CUTOFF_GRADE))
                    src = body.get("source_fichier", "")
                    cur = conn.execute(
                        "INSERT INTO oreblocs (nom, grade_cutoff, sommets, source_fichier) VALUES (?, ?, ?, ?)",
                        (nom, grade, json.dumps(sommets), src))
                    conn.commit()
                    return self._send_json(201, {"id": cur.lastrowid, "ok": True})

                # ------------------------------------------- checkpoints --
                # Ligne de passage (deux extremites), importee par CSV
                # "Y, X, id, Nom" (deux lignes par checkpoint). Le nom est
                # l'identifiant : un nom deja present est MIS A JOUR (on
                # recharge le fichier corrige sans tout supprimer).
                # 24/11 : paquet geo des DT (affectation + checkpoints sur la tablette) : contenu, anomalies, depot OT.
                if path == "/api/geo_dt" and method == "GET":
                    paquet = geo_dt_construire(conn)
                    if self._qs().get("resume", ["0"])[0] == "1":
                        paquet = dict({k: paquet[k] for k in ("version", "genere_le", "tolerance_m", "depart_m", "anomalies")}, **{
                            "blasts": [{"numero": b["numero"], "flitch": b["flitch"], "source": b["source"],
                                        "blocs": len(b["blocs"]), "minerai": sum(o["minerai"] for o in b["blocs"])}
                                       for b in paquet["blasts"]],
                            "zones": len(paquet["zones"]), "consignes": len(paquet["consignes"]),
                            "checkpoints": len(paquet["checkpoints"]),
                            "trajectoires": [{"source": t["source"], "zone": t["zone"], "checkpoints": t["checkpoints"]}
                                             for t in paquet["trajectoires"]]})
                    paquet["depot_ot"] = dict(_geo_dt_etat, version_ot=param_lire_base("GEO_DT_VERSION_OT", ""))
                    paquet["tablettes"] = {c: {"version": g["version"], "vu_il_y_a_s": int(time.time() - g["t"]),
                                               "controle_checkpoints": geo_tablette_active(c)}
                                           for c, g in _geo_tablettes.items()}
                    paquet["evenements_tablettes"] = _evt_tablette_etat
                    return self._send_json(200, paquet)

                if path == "/api/geo_dt/pousser" and method == "POST":
                    ok, err = pousser_geo_dt(conn, force=True)
                    return self._send_json(200 if ok else 502, {"ok": ok, "erreur": err,
                                                                "version": param_lire_base("GEO_DT_VERSION_OT", "")})

                if path == "/api/checkpoints" and method == "GET":
                    rows = [dict(r) for r in conn.execute(
                        "SELECT id, nom, x1, y1, x2, y2, created_at, COALESCE(interdit_waste, 1) AS interdit_waste, "
                        "COALESCE(interdit_ore, 0) AS interdit_ore FROM checkpoints WHERE actif=1 ORDER BY id")]
                    for r in rows:
                        r["longueur_m"] = round(distance(r["x1"], r["y1"], r["x2"], r["y2"]), 1)
                    return self._send_json(200, {"checkpoints": rows})

                # 23/11 : reglages du geofencing ore / waste {"actif", "approche_m", "arret_s", "rayon_m"}.
                if path == "/api/geofencing/reglages" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    if "actif" in body:
                        param_ecrire(conn, "ZONES_ACTIF", "1" if body["actif"] else "0")
                    for champ, cle, bornes in (("approche_m", "ZONE_APPROCHE_M", (0, 1000)), ("arret_s", "ARRET_DUMP_S", (5, 1800)),
                                               ("rayon_m", "ARRET_RAYON_M", (2, 100)), ("vitesse_kmh", "ARRET_VITESSE_KMH", (0.1, 10)),
                                               ("depart_m", "GEOFENCE_DEPART_M", (0, 500))):
                        if champ in body:
                            try:
                                v = float(body[champ])
                            except (TypeError, ValueError):
                                return self._send_json(400, {"error": "%s : nombre attendu" % champ})
                            if not bornes[0] <= v <= bornes[1]:
                                return self._send_json(400, {"error": "%s : entre %g et %g" % (champ, bornes[0], bornes[1])})
                            param_ecrire(conn, cle, "%g" % v)
                    return self._send_json(200, reglages_zones())

                # 23/11 : trajectoire WASTE = les checkpoints INTERDITS a un DT charge de waste.
                # {"interdits": [ids]} : ces checkpoints passent a 1, tous les autres checkpoints actifs a 0.
                # 23/11 : idem pour le ORE (/api/checkpoints_ore) : checkpoints interdits a un DT charge de minerai.
                if path in ("/api/checkpoints_waste", "/api/checkpoints_ore") and method == "POST":
                    colonne = "interdit_ore" if path.endswith("_ore") else "interdit_waste"
                    body = json.loads(self._read_body() or b"{}")
                    liste = body.get("interdits")
                    if not isinstance(liste, list):
                        return self._send_json(400, {"error": "interdits (liste d'ids) requis"})
                    try:
                        ids = sorted({int(i) for i in liste})
                    except (TypeError, ValueError):
                        return self._send_json(400, {"error": "ids invalides"})
                    actifs = {r["id"] for r in conn.execute("SELECT id FROM checkpoints WHERE actif=1")}
                    inconnus = [i for i in ids if i not in actifs]
                    if inconnus:
                        return self._send_json(404, {"error": "checkpoint(s) introuvable(s) : %s" % inconnus})
                    conn.execute("UPDATE checkpoints SET %s=0 WHERE actif=1" % colonne)
                    for i in ids:
                        conn.execute("UPDATE checkpoints SET %s=1 WHERE id=?" % colonne, (i,))
                    conn.commit()
                    return self._send_json(200, {"ok": True, "interdits": ids, "autorises": sorted(actifs - set(ids))})

                if path == "/api/checkpoints_import" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    contenu = body.get("contenu") or ""
                    if not contenu:
                        return self._send_json(400, {"error": "contenu du fichier requis"})
                    extraits, rejets = extraire_checkpoints_csv(contenu)
                    if not extraits:
                        return self._send_json(422, {
                            "error": "aucun checkpoint exploitable (2 lignes par Nom, colonnes Y, X, Nom)",
                            "rejets": [{"nom": n, "motif": m} for n, m in rejets]})
                    crees, maj, convertis = [], [], 0
                    for nom, pts in extraits:
                        (a, b), conv = points_en_utm(conn, pts)
                        convertis += 1 if conv else 0
                        r = conn.execute("SELECT id FROM checkpoints WHERE LOWER(nom)=LOWER(?) AND actif=1",
                                         (nom,)).fetchone()
                        if r:
                            conn.execute("UPDATE checkpoints SET x1=?, y1=?, x2=?, y2=? WHERE id=?",
                                         (a[0], a[1], b[0], b[1], r["id"]))
                            maj.append(nom)
                        else:
                            conn.execute("INSERT INTO checkpoints (nom, x1, y1, x2, y2) VALUES (?, ?, ?, ?, ?)",
                                         (nom, a[0], a[1], b[0], b[1]))
                            crees.append(nom)
                    conn.commit()
                    return self._send_json(201, {
                        "ok": True, "crees": crees, "mis_a_jour": maj,
                        "rejets": [{"nom": n, "motif": m} for n, m in rejets],
                        "convertis_utm": convertis})

                # ------------------------------------------- trajectoires --
                # {id, source_id, source, dumping_area_id, dumping_area, checkpoints:[{id, nom, longueur_m}]}
                if path == "/api/trajectoires" and method == "GET":
                    rows = []
                    for t in conn.execute(
                            "SELECT t.id, t.source_id, s.nom AS source, t.dumping_area_id, d.nom AS dumping_area, "
                            "t.checkpoints FROM trajectoires t "
                            "LEFT JOIN sources s ON s.id = t.source_id LEFT JOIN dumping_areas d ON d.id = t.dumping_area_id "
                            "ORDER BY s.nom, d.nom"):
                        try:
                            ids = [int(i) for i in json.loads(t["checkpoints"] or "[]")]
                        except (TypeError, ValueError):
                            ids = []
                        cps = []
                        for i in ids:
                            c = conn.execute("SELECT id, nom, x1, y1, x2, y2 FROM checkpoints WHERE id=? AND actif=1", (i,)).fetchone()
                            if c:
                                cps.append({"id": c["id"], "nom": c["nom"],
                                            "longueur_m": round(distance(c["x1"], c["y1"], c["x2"], c["y2"]), 1)})
                        rows.append({"id": t["id"], "source_id": t["source_id"], "source": t["source"] or "",
                                     "dumping_area_id": t["dumping_area_id"], "dumping_area": t["dumping_area"] or "",
                                     "checkpoints": cps})
                    return self._send_json(200, {"trajectoires": rows})

                if path == "/api/trajectoires" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    sid, did = int(body.get("source_id", 0)), int(body.get("dumping_area_id", 0))
                    if not sid or not did:
                        return self._send_json(400, {"error": "source_id et dumping_area_id requis"})
                    if not conn.execute("SELECT id FROM sources WHERE id=? AND actif=1", (sid,)).fetchone():
                        return self._send_json(404, {"error": "source introuvable"})
                    if not conn.execute("SELECT id FROM dumping_areas WHERE id=? AND actif=1", (did,)).fetchone():
                        return self._send_json(404, {"error": "dumping area introuvable"})
                    if conn.execute("SELECT id FROM trajectoires WHERE source_id=? AND dumping_area_id=?", (sid, did)).fetchone():
                        return self._send_json(409, {"error": "cette trajectoire existe deja"})
                    cur = conn.execute("INSERT INTO trajectoires (source_id, dumping_area_id) VALUES (?, ?)", (sid, did))
                    conn.commit()
                    return self._send_json(201, {"ok": True, "id": cur.lastrowid})

                # liste ORDONNEE des checkpoints d'une trajectoire : {"id": N, "checkpoints": [ids]}
                if path == "/api/trajectoire_checkpoints" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    tid = int(body.get("id", 0))
                    liste = body.get("checkpoints")
                    if not tid or not isinstance(liste, list):
                        return self._send_json(400, {"error": "id et checkpoints (liste) requis"})
                    ids = []
                    for i in liste:
                        i = int(i)
                        if i in ids:
                            return self._send_json(400, {"error": "checkpoint en double dans la liste"})
                        if not conn.execute("SELECT id FROM checkpoints WHERE id=? AND actif=1", (i,)).fetchone():
                            return self._send_json(404, {"error": "checkpoint %d introuvable" % i})
                        ids.append(i)
                    if not conn.execute("SELECT id FROM trajectoires WHERE id=?", (tid,)).fetchone():
                        return self._send_json(404, {"error": "trajectoire introuvable"})
                    conn.execute("UPDATE trajectoires SET checkpoints=? WHERE id=?", (json.dumps(ids), tid))
                    conn.commit()
                    return self._send_json(200, {"ok": True, "id": tid, "checkpoints": ids})

                if path == "/api/trajectoires" and method == "DELETE":
                    tid = int(self._qs().get("id", ["0"])[0])
                    if not tid:
                        return self._send_json(400, {"error": "id requis"})
                    conn.execute("DELETE FROM trajectoires WHERE id=?", (tid,))
                    conn.commit()
                    return self._send_json(200, {"ok": True, "id": tid})

                if path == "/api/checkpoints" and method == "DELETE":
                    qs = self._qs()
                    if qs.get("tous", ["0"])[0] == "1":
                        n = conn.execute("UPDATE checkpoints SET actif=0 WHERE actif=1").rowcount
                        conn.commit()
                        return self._send_json(200, {"ok": True, "supprimes": n})
                    cid = int(qs.get("id", ["0"])[0])
                    if not cid:
                        return self._send_json(400, {"error": "id requis"})
                    conn.execute("UPDATE checkpoints SET actif=0 WHERE id=?", (cid,))
                    conn.commit()
                    return self._send_json(200, {"ok": True, "id": cid})

                if path == "/api/dumping_areas" and method == "GET":
                    rows = [dict(r) for r in conn.execute("SELECT * FROM dumping_areas WHERE actif=1")]
                    return self._send_json(200, {"dumping_areas": rows})

                if path == "/api/dumping_areas" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    nom = (body.get("nom") or "Dumping Area").strip()
                    if conn.execute("SELECT id FROM dumping_areas WHERE LOWER(nom)=LOWER(?) AND actif=1",
                                     (nom,)).fetchone():
                        return self._send_json(409, {"error": "une dumping area nommee \"%s\" existe deja" % nom})
                    poly, err = sommets_depuis_body(body)
                    if poly is not None:
                        poly, _ = points_en_utm(conn, poly)
                    if poly is None:
                        # compat legacy : un point (x, y, rayon_m) seul
                        # -> carre de cote 2*rayon
                        x0, y0 = _num(body.get("x")), _num(body.get("y"))
                        if x0 is None or y0 is None:
                            return self._send_json(400, {"error": err})
                        poly = polygone_depuis_point(x0, y0, _num(body.get("rayon_m")) or 50)
                    cx, cy = centroide_polygone(poly)
                    cur = conn.execute(
                        "INSERT INTO dumping_areas (nom, x, y, rayon_m, sommets, couleur) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (nom, cx, cy, math.sqrt(aire_polygone(poly)), json.dumps(poly),
                         body.get("couleur") or "blue"))
                    conn.commit()
                    return self._send_json(201, {"id": cur.lastrowid, "ok": True,
                                                 "x": cx, "y": cy,
                                                 "aire_m2": aire_polygone(poly)})

                # maj d'une ligne de dumping area (edition dans la page)
                if path == "/api/dumping_area_maj" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    did = int(body.get("id", 0))
                    if not did:
                        return self._send_json(400, {"error": "id requis"})
                    ex = conn.execute("SELECT id FROM dumping_areas WHERE id=? AND actif=1", (did,)).fetchone()
                    if not ex:
                        return self._send_json(404, {"error": "dumping area introuvable"})
                    if body.get("nom") is not None:
                        nouveau_nom = str(body["nom"]).strip() or "Dumping Area"
                        autre = conn.execute(
                            "SELECT id FROM dumping_areas WHERE LOWER(nom)=LOWER(?) AND actif=1 AND id<>?",
                            (nouveau_nom, did)).fetchone()
                        if autre:
                            return self._send_json(409, {
                                "error": "une dumping area nommee \"%s\" existe deja" % nouveau_nom})
                        conn.execute("UPDATE dumping_areas SET nom=? WHERE id=?", (nouveau_nom, did))
                    if body.get("sommets") is not None:
                        poly, err = sommets_depuis_body(body)
                        if poly is None:
                            return self._send_json(400, {"error": err})
                        poly, _ = points_en_utm(conn, poly)
                        cx, cy = centroide_polygone(poly)
                        conn.execute(
                            "UPDATE dumping_areas SET sommets=?, x=?, y=?, rayon_m=? WHERE id=?",
                            (json.dumps(poly), cx, cy, math.sqrt(aire_polygone(poly)), did))
                    for champ in ("x", "y", "rayon_m"):
                        if champ in body and body[champ] not in (None, ""):
                            conn.execute("UPDATE dumping_areas SET %s=? WHERE id=?" % champ,
                                         (float(_num(body[champ]) or 0), did))
                    if body.get("couleur") in ("blue", "green", "yellow", "orange"):
                        conn.execute("UPDATE dumping_areas SET couleur=? WHERE id=?", (body["couleur"], did))
                    conn.commit()
                    return self._send_json(200, {"ok": True, "id": did})

                # import CSV de dumping areas (polygones) : une Dumping Area est
                # une AIRE (m2), pas un point. Le CSV regroupe les sommets par
                # nom (meme convention que le CSV de blasts/oreblocs) :
                # "nom,x,y" repete pour chaque sommet du meme polygone.
                if path == "/api/dumping_areas_import" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    contenu = body.get("contenu") or ""
                    if not contenu:
                        return self._send_json(400, {"error": "contenu du fichier requis"})
                    couleur = body.get("couleur") if body.get("couleur") in (
                        "blue", "green", "yellow", "orange") else "blue"
                    # Le nom du fichier sert de nom de zone quand le CSV n'a
                    # pas de colonne nom : "RomPad 111.csv" -> "RomPad 111".
                    # Sans cela chaque fichier anonyme produisait le meme
                    # "Dumping Area 1" et seul le premier passait.
                    nom_defaut = (body.get("nom") or "").strip() or None
                    extraits = extraire_oreblocs_csv(
                        contenu, prefixe="Dumping Area", nom_defaut=nom_defaut)
                    if not extraits:
                        return self._send_json(422, {
                            "error": "aucun polygone exploitable (>= 3 sommets) dans ce fichier"})
                    # le nom est l'identifiant unique d'une dumping area : une entree
                    # deja existante (active) est ignoree, pas renommee/dupliquee.
                    crees = []
                    doublons = []
                    convertis = 0
                    for nom, sommets in extraits:
                        if conn.execute("SELECT id FROM dumping_areas WHERE LOWER(nom)=LOWER(?) AND actif=1",
                                        (nom,)).fetchone():
                            doublons.append(nom)
                            continue
                        # 17/11 : un CSV en degres (Y = lat, X = lon) etait
                        # stocke tel quel ; il est maintenant projete en UTM.
                        sommets, conv = points_en_utm(conn, sommets)
                        convertis += 1 if conv else 0
                        cx, cy = centroide_polygone(sommets)
                        cur = conn.execute(
                            "INSERT INTO dumping_areas (nom, x, y, rayon_m, sommets, couleur) "
                            "VALUES (?, ?, ?, ?, ?, ?)",
                            (nom, cx, cy, math.sqrt(aire_polygone(sommets)),
                             json.dumps(sommets), couleur))
                        crees.append({"id": cur.lastrowid, "nom": nom,
                                      "nb_sommets": len(sommets),
                                      "aire_m2": aire_polygone(sommets)})
                    conn.commit()
                    return self._send_json(201, {"ok": True, "nb": len(crees), "dumping_areas": crees,
                                                 "doublons": doublons, "convertis_utm": convertis})

                if path == "/api/travels_roads" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    nom = body.get("nom", "Travels Road")
                    geojson = body.get("geojson", {})
                    # Un GeoJSON arrive presque toujours en degres (CRS84)
                    # alors que les oreblocs sont en UTM. Sans reprojection
                    # les deux couches ne se superposent pas et Dijkstra ne
                    # trouve jamais de noeud proche : aucun itineraire.
                    zone, hemi = repere_site(conn)
                    reprojetes = 0
                    if zone and semble_degres(geojson_points(geojson)):
                        reprojetes = geojson_reprojeter(geojson, zone, hemi)
                    cur = conn.execute(
                        "INSERT INTO travels_roads (nom, geojson) VALUES (?, ?)",
                        (nom, json.dumps(geojson, ensure_ascii=True)))
                    conn.commit()
                    return self._send_json(201, {"id": cur.lastrowid, "ok": True,
                                                 "reprojetes": reprojetes,
                                                 "zone": zone})
                # ----------------------------------------- calibration GPS --
                # Points de reference (meme point physique mesure au RTK et
                # connu sur le plan CSV/DXF) servant a calculer la
                # transformation affine GPS -> repere local (gps_vers_local).
                if path == "/api/calibration_points" and method == "GET":
                    rows = [dict(r) for r in conn.execute(
                        "SELECT * FROM calibration_points WHERE actif=1 ORDER BY id")]
                    return self._send_json(200, {"calibration_points": rows})

                if path == "/api/calibration_points" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    lat = _num(body.get("lat"))
                    lon = _num(body.get("lon"))
                    x = _num(body.get("x"))
                    y = _num(body.get("y"))
                    if lat is None or lon is None or x is None or y is None:
                        return self._send_json(400, {"error": "lat, lon, x, y requis"})
                    label = (body.get("label") or "").strip()
                    cur = conn.execute(
                        "INSERT INTO calibration_points (label, lat, lon, x, y) VALUES (?, ?, ?, ?, ?)",
                        (label, lat, lon, x, y))
                    conn.commit()
                    resultat = {"id": cur.lastrowid, "ok": True}
                    # recalcul automatique des que l'on a de quoi ajuster (>=3 points)
                    nb_actifs = conn.execute(
                        "SELECT COUNT(*) FROM calibration_points WHERE actif=1").fetchone()[0]
                    if nb_actifs >= 3:
                        resultat["calibration"] = calculer_calibration(conn)
                    return self._send_json(201, resultat)

                if path == "/api/calibration_points" and method == "DELETE":
                    qs = self._qs()
                    pid = int(qs.get("id", ["0"])[0])
                    if not pid:
                        return self._send_json(400, {"error": "id requis"})
                    conn.execute("UPDATE calibration_points SET actif=0 WHERE id=?", (pid,))
                    conn.commit()
                    nb_actifs = conn.execute(
                        "SELECT COUNT(*) FROM calibration_points WHERE actif=1").fetchone()[0]
                    cal = calculer_calibration(conn) if nb_actifs >= 3 else None
                    return self._send_json(200, {"ok": True, "id": pid, "calibration": cal})

                if path == "/api/calibration_calculer" and method == "POST":
                    cal = calculer_calibration(conn)
                    if not cal:
                        return self._send_json(422, {
                            "error": "au moins 3 points de calibration non colineaires sont requis"})
                    return self._send_json(200, cal)

                if path == "/api/calibration" and method == "GET":
                    cal = conn.execute(
                        "SELECT * FROM calibration ORDER BY id DESC LIMIT 1").fetchone()
                    if not cal:
                        return self._send_json(200, {"calibre": False})
                    d = dict(cal)
                    d["calibre"] = True
                    return self._send_json(200, d)

                # --------------------------------- systeme geo (WGS84/UTM) --
                # Ligne unique (id=1). GET renvoie le choix en cours (ou les
                # par defaut WGS84) ; POST enregistre (upsert) le choix.
                # Comparaison local <-> classeur. Lecture seule : c'est le
                # tableau de bord de l'alignement, sans aucun effet de bord.
                # Nettoyage des sommets aberrants. Les DXF produits avant
                # le correctif du parseur POLYLINE portent un point fictif
                # (0,0) issu de l'en-tete de l'entite : il tire le centroide
                # du bloc de plusieurs dizaines de kilometres, donc la
                # destination de la mission avec lui. On retire les sommets
                # trop loin du gros du polygone, (0,0) compris.
                #
                # `appliquer=false` (defaut) : simulation, rien n'est ecrit.
                if path == "/api/oreblocs_nettoyer" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    appliquer = bool(body.get("appliquer"))
                    seuil = _num(body.get("seuil_m")) or 5000.0
                    rapport = []
                    for r in conn.execute(
                            "SELECT id, nom, sommets FROM oreblocs WHERE actif=1"):
                        try:
                            pts = json.loads(r["sommets"] or "[]")
                        except Exception:
                            continue
                        if len(pts) < 4:
                            continue      # retirer un sommet casserait le polygone
                        # mediane par axe : insensible aux points aberrants,
                        # contrairement a la moyenne que le (0,0) deplace.
                        xs = sorted(p[0] for p in pts)
                        ys = sorted(p[1] for p in pts)
                        mx = xs[len(xs) // 2]
                        my = ys[len(ys) // 2]
                        gardes = [p for p in pts
                                  if distance(p[0], p[1], mx, my) <= seuil]
                        retires = len(pts) - len(gardes)
                        if not retires or len(gardes) < 3:
                            continue
                        avant_x = sum(p[0] for p in pts) / len(pts)
                        avant_y = sum(p[1] for p in pts) / len(pts)
                        apres_x = sum(p[0] for p in gardes) / len(gardes)
                        apres_y = sum(p[1] for p in gardes) / len(gardes)
                        if appliquer:
                            conn.execute("UPDATE oreblocs SET sommets=? WHERE id=?",
                                         (json.dumps(gardes), r["id"]))
                        rapport.append({
                            "bloc": r["nom"], "sommets_retires": retires,
                            "sommets_restants": len(gardes),
                            "centroide_avant": [round(avant_x, 1), round(avant_y, 1)],
                            "centroide_apres": [round(apres_x, 1), round(apres_y, 1)],
                            "deplacement_m": round(
                                distance(avant_x, avant_y, apres_x, apres_y), 1),
                        })
                    if appliquer:
                        conn.commit()
                    return self._send_json(200, {
                        "ok": True, "applique": appliquer, "seuil_m": seuil,
                        "blocs": rapport, "nb": len(rapport)})

                # Reconciliation. Par defaut "constat" : ce qui manque d'un
                # cote est recopie, mais aucune divergence n'est tranchee.
                # arbitrage=distant -> le classeur fait foi.
                # arbitrage=local   -> la base locale fait foi.
                if path == "/api/ot_reconcilier" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    arbitrage = str(body.get("arbitrage") or "constat").lower()
                    if arbitrage not in ("constat", "distant", "local"):
                        return self._send_json(400, {
                            "error": "arbitrage attendu : constat, distant ou local"})
                    rapport, err = ot_reconcilier(conn, arbitrage)
                    if rapport is None:
                        return self._send_json(502, {"error": err})
                    return self._send_json(200, rapport)

                # Etat du miroir des blasts, et rattrapage. Sert aussi bien
                # au premier alignement (tout l'historique local n'a jamais
                # ete pousse) qu'au reessai apres une coupure Internet.
                if path == "/api/ot_blasts" and method == "GET":
                    rows = [dict(r) for r in conn.execute(
                        "SELECT id, numero, nom, nb_blocs, ot_sync, ot_erreur, ot_maj_le "
                        "FROM blasts WHERE actif=1 ORDER BY id")]
                    return self._send_json(200, {
                        "blasts": rows,
                        "en_retard": [r["numero"] for r in rows if not r["ot_sync"]],
                    })

                if path == "/api/ot_blasts" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    tous = bool(body.get("tous"))
                    sql = "SELECT id, numero FROM blasts WHERE actif=1"
                    if not tous:
                        sql += " AND (ot_sync IS NULL OR ot_sync=0)"
                    cibles = [dict(r) for r in conn.execute(sql + " ORDER BY id")]
                    pousses, echecs = [], []
                    for c in cibles:
                        ok, err = ot_pousser_blast(conn, c["id"])
                        (pousses if ok else echecs).append(
                            c["numero"] if ok else {"numero": c["numero"], "erreur": err})
                    return self._send_json(200, {
                        "ok": not echecs, "pousses": pousses, "echecs": echecs,
                        "total": len(cibles)})

                # Reglages des deux liaisons sortantes (Serveur_OT et
                # Serveur_SG). Les jetons ne ressortent JAMAIS en clair : on
                # ne renvoie que leur presence, comme le fait deja le SaaS.
                if path == "/api/relais" and method == "GET":
                    office_url, office_token = office_conf()
                    sg_url, sg_token, sg_intervalle = sg_conf()
                    return self._send_json(200, {
                        "office_url": office_url,
                        "office_token_pose": bool(office_token),
                        "sg_url": sg_url,
                        "sg_token_pose": bool(sg_token),
                        "sg_intervalle_s": sg_intervalle,
                        "office_actif": bool(office_url and office_token),
                        "sg_actif": bool(sg_url and sg_token),
                        "office_token_empreinte": empreinte_jeton(office_token),
                        "sg_token_empreinte": empreinte_jeton(sg_token),
                        "remontees_intervalle_s": remontees_intervalle(),
                        "chargements_intervalle_s": chargements_intervalle(),
                        "chargements": etat_cloud(),
                        "remontees": dict(_remontees_etat,
                                          derniere_ligne=param_lire("REMONTEES_DERNIERE_LIGNE", ""),
                                          derniere_ligne_trajet=param_lire("TRAJET_DERNIERE_LIGNE", "")),
                        "alerte_rouge_min": alerte_rouge_minutes(),
                        "marge_affectation_m": marge_affectation_m(),
                        "z_flitch": reglage_z_flitch(),
                        "precision_geo": reglage_precision_geo(),
                        "marge_affectation_sg": _reglages_etat["sg"],
                        "missions_source": missions_source(),
                        "videos_jours": videos_jours(),
                        "surcharge_env": [c for c in _PARAMS_RELAIS
                                          if os.environ.get(c, "")],
                    })

                if path == "/api/relais" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    connus = {"office_url": "OFFICE_URL",
                              "office_token": "OFFICE_TOKEN",
                              "sg_url": "SG_URL",
                              "sg_token": "SG_TOKEN",
                              "sg_intervalle_s": "SG_INTERVALLE_S",
                              "alerte_rouge_min": "ALERTE_ROUGE_MIN",
                              "marge_affectation_m": "MARGE_AFFECTATION_M",
                              "missions_source": "MISSIONS_SOURCE",
                              "remontees_intervalle_s": "REMONTEES_INTERVALLE_S",
                              "chargements_intervalle_s": "CHARGEMENTS_INTERVALLE_S",
                              # 24/11 : Z de la pelle dans le choix du flitch (reglage_z_flitch)
                              "z_flitch_actif": "Z_FLITCH_ACTIF",
                              "z_hauteur_antenne_m": "Z_HAUTEUR_ANTENNE_M",
                              "z_marge_m": "Z_MARGE_M",
                              # 24/11 : precision des positions et distance de franchissement (reglage_precision_geo)
                              "geo_precision_mode": "GEO_PRECISION_MODE",
                              "geo_franchissement_m": "GEO_FRANCHISSEMENT_M",
                              "geo_vitesse_min_kmh": "GEO_VITESSE_MIN_KMH",
                              # 26/11 : conservation des videos du tracking (jours, 0 = jamais supprimees)
                              "videos_jours": "VIDEOS_JOURS"}
                    ecrits = []
                    for champ, cle in connus.items():
                        if champ not in body:
                            continue          # absent = inchange, "" = efface
                        valeur = str(body[champ] or "").strip()
                        if cle == "MISSIONS_SOURCE":
                            valeur = valeur.lower()
                            if valeur not in ("cloud", "local"):
                                return self._send_json(400, {"error": "missions_source : cloud ou local"})
                        if cle.endswith("_URL"):
                            valeur = valeur.rstrip("/")
                        if cle == "MARGE_AFFECTATION_M":
                            try:
                                m = float(valeur.replace(",", "."))
                            except ValueError:
                                m = -1
                            if not (0 <= m <= 50):
                                return self._send_json(400, {"error": "marge d'affectation : un nombre de 0 a 50 metres"})
                            valeur = "%g" % m
                        if cle in ("Z_HAUTEUR_ANTENNE_M", "Z_MARGE_M") and valeur != "":
                            try:
                                m = float(valeur.replace(",", "."))
                            except ValueError:
                                m = -1
                            if not (0 <= m <= 20):
                                return self._send_json(400, {"error": "%s : un nombre de 0 a 20 metres" % champ})
                            valeur = "%g" % m
                        if cle == "GEO_PRECISION_MODE" and valeur != "":
                            valeur = valeur.lower()
                            if valeur not in ("gnss", "rtk", "differentiel", "tous"):
                                return self._send_json(400, {"error": "geo_precision_mode : gnss, rtk, differentiel ou tous"})
                        if cle in ("GEO_FRANCHISSEMENT_M", "GEO_VITESSE_MIN_KMH") and valeur != "":
                            try:
                                m = float(valeur.replace(",", "."))
                            except ValueError:
                                m = -1
                            bornes = (0.5, 50) if cle == "GEO_FRANCHISSEMENT_M" else (0, 30)
                            if not (bornes[0] <= m <= bornes[1]):
                                return self._send_json(400, {"error": "%s : un nombre de %g a %g" % (champ, bornes[0], bornes[1])})
                            valeur = "%g" % m
                        if cle == "VIDEOS_JOURS":
                            try:
                                j = int(float(valeur.replace(",", ".") or "30"))
                            except ValueError:
                                j = -1
                            if not (0 <= j <= 3650):
                                return self._send_json(400, {"error": "conservation des videos : 0 (jamais) a 3650 jours"})
                            valeur = str(j)
                        if cle == "Z_FLITCH_ACTIF" and valeur != "":
                            valeur = "0" if valeur.lower() in ("0", "non", "false", "off") else "1"
                        param_ecrire(conn, cle, valeur)
                        ecrits.append(champ)
                    if "videos_jours" in ecrits:
                        threading.Thread(target=purger_videos, daemon=True).start()     # applique tout de suite
                    if "marge_affectation_m" in ecrits or any(c.startswith("z_") for c in ecrits):
                        threading.Thread(target=reglages_diffuser, kwargs={"forcer": True}, daemon=True).start()
                    gene = [c for c in _PARAMS_RELAIS if os.environ.get(c, "")]
                    return self._send_json(200, {
                        "ok": True, "enregistres": ecrits,
                        # Un reglage surcharge par l'environnement est ecrit en
                        # base mais reste sans effet tant que la variable existe.
                        "ignores_car_env": gene})

                if path == "/api/geographic_system" and method == "GET":
                    row = conn.execute(
                        "SELECT * FROM geographic_system WHERE id=1").fetchone()
                    if not row:
                        return self._send_json(200, {
                            "systeme": "WGS84", "zone": None,
                            "hemisphere": "N", "datum": "WGS 84"})
                    return self._send_json(200, dict(row))

                if path == "/api/geographic_system" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    systeme = (body.get("systeme") or "WGS84").strip().upper()
                    if systeme not in ("WGS84", "UTM"):
                        return self._send_json(400, {"error": "systeme: WGS84 ou UTM"})
                    zone = _num(body.get("zone"))
                    if systeme == "UTM":
                        if zone is None or not (1 <= int(zone) <= 60):
                            return self._send_json(400, {
                                "error": "zone UTM valide (1-60) requise"})
                        zone = int(zone)
                    else:
                        zone = None
                    hemisphere = (body.get("hemisphere") or "N").strip().upper()
                    if hemisphere not in ("N", "S"):
                        hemisphere = "N"
                    datum = (body.get("datum") or "WGS 84").strip()
                    conn.execute(
                        "INSERT INTO geographic_system (id, systeme, zone, hemisphere, datum, updated_at) "
                        "VALUES (1, ?, ?, ?, ?, datetime('now')) "
                        "ON CONFLICT(id) DO UPDATE SET systeme=excluded.systeme, "
                        "zone=excluded.zone, hemisphere=excluded.hemisphere, "
                        "datum=excluded.datum, updated_at=excluded.updated_at",
                        (systeme, zone, hemisphere, datum))
                    conn.commit()
                    row = conn.execute(
                        "SELECT * FROM geographic_system WHERE id=1").fetchone()
                    return self._send_json(200, dict(row))

                # ---------------------------------------- cles HMAC par DT --
                # Provisionnement des cles secretes partagees avec les
                # tablettes embarquees (authentification des trames XBee).
                if path == "/api/cles_dt" and method == "GET":
                    return self._send_json(200, {"cles_dt": cles_radio_lister(conn)})   # empreintes seulement

                if path == "/api/cles_dt" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    code, rep_ = cle_radio_renouveler(conn, body.get("dt_code"))
                    return self._send_json(201 if code == 200 else code, rep_)

                if path == "/api/cles_dt" and method == "DELETE":
                    qs = self._qs()
                    dt_code = (qs.get("dt_code", [""])[0] or "").strip()
                    if not dt_code:
                        return self._send_json(400, {"error": "dt_code requis"})
                    code, rep_ = cle_radio_revoquer(conn, dt_code)
                    return self._send_json(code, rep_)

                # ------------------------------------------------- sources --
                # Delegue les sources minieres d'une mine (Main Pit,
                # Second Pit...) : la source alimente les missions DT.
                if path == "/api/sources" and method == "GET":
                    rows = [dict(r) for r in conn.execute(
                        "SELECT * FROM sources WHERE actif=1 ORDER BY nom")]
                    return self._send_json(200, {"sources": rows})

                if path == "/api/sources" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    nom = (body.get("nom") or "").strip()
                    if not nom:
                        return self._send_json(400, {"error": "nom requis"})
                    if conn.execute("SELECT id FROM sources WHERE nom=? AND actif=1", (nom,)).fetchone():
                        return self._send_json(409, {"error": "cette source existe deja"})
                    # codes admis dans le nom des blasts (ex. "LMP2, LMP3")
                    codes = (body.get("codes") or "").strip().upper()
                    cur = conn.execute("INSERT INTO sources (nom, codes) VALUES (?, ?)",
                                       (nom, codes))
                    conn.commit()
                    return self._send_json(201, {"id": cur.lastrowid, "ok": True, "nom": nom})

                if path == "/api/sources" and method == "PUT":
                    # mise a jour du nom et/ou des codes d'une source
                    body = json.loads(self._read_body() or b"{}")
                    sid = int(body.get("id") or 0)
                    if not sid:
                        return self._send_json(400, {"error": "id requis"})
                    row = conn.execute("SELECT * FROM sources WHERE id=? AND actif=1", (sid,)).fetchone()
                    if not row:
                        return self._send_json(404, {"error": "source introuvable"})
                    if body.get("nom") is not None:
                        nom = (body.get("nom") or "").strip()
                        if nom:
                            conn.execute("UPDATE sources SET nom=? WHERE id=?", (nom, sid))
                    if body.get("codes") is not None:
                        codes = ",".join(
                            sorted({c.strip().upper() for c in (body.get("codes") or "").split(",") if c.strip()}))
                        conn.execute("UPDATE sources SET codes=? WHERE id=?", (codes, sid))
                    conn.commit()
                    return self._send_json(200, {"ok": True, "id": sid})

                if path == "/api/sources" and method == "DELETE":
                    qs = self._qs()
                    sid = int(qs.get("id", ["0"])[0])
                    if not sid:
                        return self._send_json(400, {"error": "id requis"})
                    conn.execute("UPDATE sources SET actif=0 WHERE id=?", (sid,))
                    conn.execute("UPDATE teneurs SET actif=0 WHERE source_id=?", (sid,))
                    conn.commit()
                    return self._send_json(200, {"ok": True, "id": sid})

                # -------------------------------------------------- teneurs --
                # Les teneurs (familles F5M, F2M...) element d'une source.
                if path == "/api/teneurs" and method == "GET":
                    sid = self._qs().get("source_id", [None])[0]
                    if sid:
                        rows = [dict(r) for r in conn.execute(
                            "SELECT * FROM teneurs WHERE source_id=? AND actif=1 ORDER BY id",
                            (int(sid),))]
                    else:
                        rows = [dict(r) for r in conn.execute(
                            "SELECT * FROM teneurs WHERE actif=1 ORDER BY id")]
                    return self._send_json(200, {"teneurs": rows})

                if path == "/api/teneurs" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    sid = int(body.get("source_id", 0))
                    nom = (body.get("nom") or "").strip()
                    if not sid or not nom:
                        return self._send_json(400, {"error": "source_id et nom requis"})
                    if not conn.execute("SELECT id FROM sources WHERE id=? AND actif=1", (sid,)).fetchone():
                        return self._send_json(404, {"error": "source introuvable"})
                    valeur_min = _num(body.get("valeur_min")) or 0
                    valeur_max = _num(body.get("valeur_max")) or 0
                    codes = normaliser_codes(body.get("codes"))
                    cur = conn.execute(
                        "INSERT INTO teneurs (source_id, nom, valeur_min, valeur_max, codes, couleur) "
                        "VALUES (?, ?, ?, ?, ?, ?)",
                        (sid, nom, valeur_min, valeur_max, codes, couleur_valide(body.get("couleur"))))
                    conn.commit()
                    return self._send_json(201, {"id": cur.lastrowid, "ok": True,
                                                 "nom": nom, "codes": codes})

                if path == "/api/teneurs" and method == "DELETE":
                    qs = self._qs()
                    tid = int(qs.get("id", ["0"])[0])
                    if not tid:
                        return self._send_json(400, {"error": "id requis"})
                    conn.execute("UPDATE teneurs SET actif=0 WHERE id=?", (tid,))
                    conn.commit()
                    return self._send_json(200, {"ok": True, "id": tid})

                # Dumping Instruction : affecte (ou modifie) la Dumping Area
                # obligatoire d'une teneur. L'expert geologue y definit la
                # consigne appliquee par le serveur en ligne pour instruire les DT.
                # Mise a jour d'une teneur : nom, bornes, et surtout `codes`.
                # Les teneurs du site existaient avant ce champ : sans
                # endpoint de maj, impossible de les renseigner autrement
                # qu'en les supprimant et en les recreant.
                if path == "/api/teneur_maj" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    tid = int(body.get("id", 0))
                    if not tid:
                        return self._send_json(400, {"error": "id requis"})
                    if not conn.execute("SELECT id FROM teneurs WHERE id=? AND actif=1",
                                        (tid,)).fetchone():
                        return self._send_json(404, {"error": "teneur introuvable"})
                    if body.get("nom"):
                        conn.execute("UPDATE teneurs SET nom=? WHERE id=?",
                                     (str(body["nom"]).strip(), tid))
                    if "codes" in body:
                        conn.execute("UPDATE teneurs SET codes=? WHERE id=?",
                                     (normaliser_codes(body["codes"]), tid))
                    if "couleur" in body:      # "" = retour a la couleur du grade
                        conn.execute("UPDATE teneurs SET couleur=? WHERE id=?",
                                     (couleur_valide(body["couleur"]), tid))
                    vmin = _num(body.get("valeur_min")) if "valeur_min" in body else None
                    vmax = _num(body.get("valeur_max")) if "valeur_max" in body else None
                    if vmin is not None and vmax is not None and vmin > vmax:
                        vmin, vmax = vmax, vmin
                    if vmin is not None:
                        conn.execute("UPDATE teneurs SET valeur_min=? WHERE id=?", (vmin, tid))
                    if vmax is not None:
                        conn.execute("UPDATE teneurs SET valeur_max=? WHERE id=?", (vmax, tid))
                    conn.commit()
                    ligne = conn.execute("SELECT * FROM teneurs WHERE id=?", (tid,)).fetchone()
                    return self._send_json(200, {"ok": True, "teneur": dict(ligne)})

                if path == "/api/teneur_dumping_area" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    tid = int(body.get("teneur_id", 0))
                    # dumping_area_id = 0 : RETRAIT de la consigne (la teneur
                    # redevient non affectee). Seule la teneur est obligatoire.
                    did = int(body.get("dumping_area_id", 0))
                    if not tid:
                        return self._send_json(400, {"error": "teneur_id requis"})
                    if not conn.execute("SELECT id FROM teneurs WHERE id=? AND actif=1", (tid,)).fetchone():
                        return self._send_json(404, {"error": "teneur introuvable"})
                    if did and not conn.execute(
                            "SELECT id FROM dumping_areas WHERE id=? AND actif=1", (did,)).fetchone():
                        return self._send_json(404, {"error": "dumping area introuvable"})
                    conn.execute("UPDATE teneurs SET dumping_area_id=? WHERE id=?", (did, tid))
                    conn.commit()
                    return self._send_json(200, {"ok": True, "teneur_id": tid, "dumping_area_id": did})

                # upload du fichier GeoTiff des travels roads (tout le reseaux
                # de polylines du trafic) : corps binaire brut + ?nom=
                if path == "/api/travels_roads_upload" and method == "POST":
                    qs = self._qs()
                    nom_fichier = (qs.get("nom", ["geotiff.tiff"])[0] or "geotiff.tiff")
                    length = int(self.headers.get("Content-Length", 0) or 0)
                    if not length:
                        return self._send_json(400, {"error": "fichier vide"})
                    if length > 100 * 1024 * 1024:
                        return self._send_json(413, {"error": "fichier trop volumineux (100 Mo max)"})
                    data = self.rfile.read(length)
                    roads_dir = os.path.join(DATA_DIR, "roads")
                    os.makedirs(roads_dir, exist_ok=True)
                    safe = "".join(c for c in nom_fichier if c.isalnum() or c in "._- ") or "geotiff.tiff"
                    cible = os.path.join(roads_dir, "%d_%s" % (int(time.time()), safe))
                    with open(cible, "wb") as f:
                        f.write(data)
                    cur = conn.execute(
                        "INSERT INTO travels_roads (nom, geojson, type, fichier) VALUES (?, '{}', 'geotiff', ?)",
                        (os.path.splitext(safe)[0], cible.replace("\\", "/")))
                    conn.commit()
                    return self._send_json(201, {
                        "id": cur.lastrowid, "ok": True, "nom": os.path.splitext(safe)[0],
                        "fichier": cible, "taille": length})

                # Calcul d'itineraire entre deux points, par les pistes.
                # Sert a verifier le reseau et a tracer un trajet dans la page
                # sans avoir a creer une mission.
                if path == "/api/itineraire" and method == "GET":
                    qs = self._qs()
                    try:
                        x1 = float(qs.get("x1", [""])[0])
                        y1 = float(qs.get("y1", [""])[0])
                        x2 = float(qs.get("x2", [""])[0])
                        y2 = float(qs.get("y2", [""])[0])
                    except (ValueError, IndexError):
                        return self._send_json(400, {
                            "error": "x1, y1, x2 et y2 requis (coordonnees du repere du site)"})
                    chemin, info = itineraire(conn, x1, y1, x2, y2)
                    return self._send_json(200, {"chemin": chemin, "info": info})

                # Etat du reseau de pistes : sert a diagnostiquer un trajet
                # impossible (graphe en morceaux, pistes non reprojetees...).
                if path == "/api/pistes_reseau" and method == "GET":
                    noeuds, adjacence = construire_graphe_pistes(conn)
                    degres = [len(v) for v in adjacence.values()]
                    xs = [p[0] for p in noeuds.values()]
                    ys = [p[1] for p in noeuds.values()]
                    return self._send_json(200, {
                        "noeuds": len(noeuds),
                        "aretes": sum(degres) // 2,
                        "jonctions": sum(1 for d in degres if d > 2),
                        "culs_de_sac": sum(1 for d in degres if d == 1),
                        "isoles": sum(1 for d in degres if d == 0),
                        "etendue": {"x": [min(xs), max(xs)], "y": [min(ys), max(ys)]}
                                   if noeuds else None,
                    })

                # Pousse les pistes REPROJETEES vers Serveur_OT. L'ancien
                # bouton du SaaS envoyait le fichier brut data/*.geojson, donc
                # en degres : il aurait remis du lon/lat dans un classeur dont
                # tout le reste est en UTM.
                # Pousse le reseau de pistes vers Serveur_OT, sous deux
                # formes : les ARETES dans dig_pistes (ce que lit Serveur_SG
                # pour router les missions) et le GeoJSON sur Drive (ce que
                # lit le calcul de parcours historique de Serveur_OT).
                # L'ancien bouton du SaaS envoyait le fichier brut
                # data/*.geojson, donc en degres : il aurait remis du lon/lat
                # dans un classeur dont tout le reste est en UTM.
                if path == "/api/pistes_pousser" and method == "POST":
                    rows = [dict(r) for r in conn.execute(
                        "SELECT nom, geojson FROM travels_roads "
                        "WHERE actif=1 AND type='geojson'")]
                    if not rows:
                        return self._send_json(400, {"error": "aucune piste enregistree"})
                    en_degres = []
                    for r in rows:
                        try:
                            geo = json.loads(r["geojson"] or "{}")
                        except Exception:
                            continue
                        if semble_degres(geojson_points(geo)):
                            en_degres.append(r["nom"])
                    if en_degres:
                        return self._send_json(409, {
                            "error": "pistes encore en degres : lancer "
                                     "/api/travels_roads_reprojeter d'abord",
                            "pistes": en_degres})

                    aretes = aretes_pistes(conn)
                    if not aretes:
                        return self._send_json(422, {
                            "error": "aucune arete exploitable dans les pistes"})
                    ok, rep, err = ot_appel("dig_pistes", payload={"aretes": aretes})
                    resultat = {"aretes_envoyees": len(aretes) if ok else 0,
                                "erreur_aretes": err}

                    # Le GeoJSON sur Drive reste utile au parcours historique
                    # de Serveur_OT ; son echec n'invalide pas l'essentiel.
                    for r in rows:
                        try:
                            geo = json.loads(r["geojson"] or "{}")
                        except Exception:
                            continue
                        ok2, _, err2 = ot_appel("pistes_upload", payload={
                            "nom": (r["nom"] or "pistes") + ".geojson",
                            "contenu": json.dumps(geo, ensure_ascii=True)})
                        resultat.setdefault("drive", []).append(
                            {"nom": r["nom"], "ok": ok2, "erreur": err2})
                    resultat["ok"] = ok
                    return self._send_json(200 if ok else 502, resultat)

                # Reprojection des pistes deja stockees. Les anciennes ont ete
                # importees en degres, avant que la conversion existe : elles
                # restent inexploitables tant qu'on ne les rattrape pas.
                if path == "/api/travels_roads_reprojeter" and method == "POST":
                    zone, hemi = repere_site(conn)
                    if not zone:
                        return self._send_json(400, {
                            "error": "Geographic System doit declarer un fuseau UTM"})
                    rapport = []
                    for r in conn.execute(
                            "SELECT id, nom, geojson FROM travels_roads "
                            "WHERE actif=1 AND type='geojson'"):
                        try:
                            geo = json.loads(r["geojson"] or "{}")
                        except Exception:
                            rapport.append({"id": r["id"], "nom": r["nom"],
                                            "etat": "geojson illisible"})
                            continue
                        pts = geojson_points(geo)
                        if not pts:
                            rapport.append({"id": r["id"], "nom": r["nom"],
                                            "etat": "aucune coordonnee"})
                            continue
                        if not semble_degres(pts):
                            rapport.append({"id": r["id"], "nom": r["nom"],
                                            "etat": "deja en UTM", "points": len(pts)})
                            continue
                        n = geojson_reprojeter(geo, zone, hemi)
                        conn.execute("UPDATE travels_roads SET geojson=? WHERE id=?",
                                     (json.dumps(geo, ensure_ascii=True), r["id"]))
                        rapport.append({"id": r["id"], "nom": r["nom"],
                                        "etat": "reprojete", "points": n})
                    conn.commit()
                    return self._send_json(200, {
                        "ok": True, "zone": zone, "hemisphere": hemi,
                        "pistes": rapport})

                # liste des travels roads (source GeoTiff deciphered localement)
                if path == "/api/travels_roads" and method == "GET":
                    rows = [dict(r) for r in conn.execute(
                        "SELECT id, nom, type, fichier, actif, created_at "
                        "FROM travels_roads WHERE actif=1 ORDER BY id")]
                    return self._send_json(200, {"travels_roads": rows})

                # Fin de mission : statue la mission, enregistre le voyage
                # (teneur du bloc + truck factor), cumul, felicitations voix
                # off + notification au serveur Office (server_OT).
                if path == "/api/mission_fin" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    mission_id = int(body.get("mission_id", 0))
                    truck_factor = max(1, int(body.get("truck_factor", 1)))
                    if not mission_id:
                        return self._send_json(400, {"error": "mission_id requis"})
                    mission = conn.execute(
                        "SELECT * FROM missions WHERE id=? AND statut='en_cours'",
                        (mission_id,)).fetchone()
                    if not mission:
                        return self._send_json(404, {"error": "mission absente ou deja terminee"})
                    orebloc = None
                    if mission["orebloc_id"]:
                        orebloc = conn.execute(
                            "SELECT * FROM oreblocs WHERE id=?",
                            (mission["orebloc_id"],)).fetchone()
                    teneur = float(orebloc["grade_cutoff"]) if orebloc else DEFAULT_CUTOFF_GRADE
                    # calibrage reglable (Parametres > Connection) : la charge
                    # moyenne remplace la constante historique 30 t
                    volume_t = chargement_moyen_t(conn) * truck_factor

                    # --- Conformite dumping (Dumping Instruction) -----------
                    # 21/11 : verdict d'apres la position REELLE du DT (derniere position fraiche), plus
                    # d'apres la zone affectee ; sans position exploitable -> « A controler »
                    detail_conf = {}
                    conformite = verifier_conformite_dumping(
                        conn, mission, position_dt_exploitable(conn, mission["dt_code"]), a_controler=True,
                        detail=detail_conf)

                    conn.execute(
                        "UPDATE missions SET statut='termine', fin=? WHERE id=?",
                        (now_iso(), mission_id))
                    fin_surveillance_dt(conn, mission["dt_code"])
                    cur = conn.execute(
                        "INSERT INTO voyages (mission_id, dt_code, orebloc_id, dumping_area_id, "
                        "teneur, volume_t, truck_factor, conformite, conformite_detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (mission_id, mission["dt_code"], mission["orebloc_id"],
                         mission["dumping_area_id"], teneur, volume_t, truck_factor, conformite,
                         json.dumps(detail_conf, ensure_ascii=True)))
                    voyage_id = cur.lastrowid
                    felicitation = (
                        "Felicitations %s ! Voyage termine : %d t a %.1f vers dumping area #%d. "
                        "Continue comme ca !" % (mission["dt_code"], int(volume_t), teneur,
                                                 mission["dumping_area_id"]))
                    creer_alerte(conn, "fin_mission", mission["dt_code"], felicitation)
                    # cumul du bloc (voyages, tonnage) mis a jour par CE voyage, pour
                    # notifier l'Office de la somme cumulee (cahier des charges, section Notifier)
                    cumul = conn.execute(
                        "SELECT COUNT(*) AS voyages, COALESCE(SUM(volume_t), 0) AS tonnes "
                        "FROM voyages WHERE orebloc_id=?", (mission["orebloc_id"],)).fetchone()
                    enregistrer_pour_office(conn, {
                        "action": "fin_mission",
                        "mission_id": mission_id,
                        "dt_code": mission["dt_code"],
                        "orebloc_id": mission["orebloc_id"],
                        "teneur": teneur,
                        "volume_t": volume_t,
                        "truck_factor": truck_factor,
                        "cumul_bloc_t": cumul["tonnes"],
                        "cumul_bloc_voyages": cumul["voyages"],
                    })
                    notifier_office(conn)
                    conn.commit()
                    return self._send_json(200, {
                        "ok": True, "voyage_id": voyage_id, "mission_id": mission_id,
                        "dt_code": mission["dt_code"], "teneur": teneur,
                        "volume_t": volume_t, "truck_factor": truck_factor,
                        "felicitation": felicitation,
                    })

                    # Cumuls par orebloc : nombre de voyages, tonnage cumule,
                    # teneur moyenne et truck factor.
                if path == "/api/cumul" and method == "GET":
                    rows = [dict(r) for r in conn.execute(
                        "SELECT COALESCE(o.nom, 'Hors bloc') AS bloc, "
                        "COUNT(v.id) AS voyages, "
                        "COALESCE(SUM(v.volume_t), 0) AS tonnes, "
                        "COALESCE(AVG(v.teneur), 0) AS _moy_teneur, "
                        "COALESCE(SUM(v.truck_factor), 0) AS truck_factor "
                        "FROM voyages v LEFT JOIN oreblocs o ON o.id = v.orebloc_id "
                        "GROUP BY v.orebloc_id ORDER BY v.orebloc_id")]
                    return self._send_json(200, {"cumul": rows})

                if path == "/api/progression_blast" and method == "GET":
                    # Materiau mine par bloc d'un blast : le cumul REEL des
                    # voyages (passerelle), croise avec le theorique (Geosum,
                    # cote SaaS) fourni par le front. C'est ce qui alimente
                    # le graph online "Statut de blast".
                    qs = self._qs()
                    try:
                        blast_id = int(qs.get("blast_id", ["0"])[0])
                    except ValueError:
                        blast_id = 0
                    if not blast_id:
                        return self._send_json(400, {"error": "blast_id requis"})
                    rows = [dict(r) for r in conn.execute(
                        "SELECT o.nom AS bloc, "
                        "COUNT(v.id) AS voyages, "
                        "COALESCE(SUM(v.volume_t), 0) AS tonnes, "
                        "COALESCE(AVG(v.teneur), 0) AS teneur "
                        "FROM oreblocs o "
                        "LEFT JOIN voyages v ON v.orebloc_id = o.id "
                        "WHERE o.blast_id=? AND o.actif=1 "
                        "GROUP BY o.id ORDER BY o.nom", (blast_id,))]
                    return self._send_json(200, {"progression": rows})

                if path == "/api/minage_actuel" and method == "GET":
                    # Etat du minage pour le Progress (canal A + D combine) :
                    # declaration spotter fraiche (< 30 min) + preuve d'activite
                    # (dernier voyage du bloc < 15 min). Tout ce qui est plus
                    # vieux est considere mort : pas de clignotement fantome.
                    decl = [dict(r) for r in conn.execute(
                        "SELECT m.exca_code, m.orebloc_id, o.nom AS bloc, datetime(m.declared_at, 'localtime') AS declared_at "
                        "FROM minage_actuel m LEFT JOIN oreblocs o ON o.id = m.orebloc_id")]
                    # 21/11 : la table voyages n'a pas de colonne `horodatage` (c'est created_at, en UTC) :
                    # la requete echouait, donc TOUT l'etat du minage (Dig Plan, Home) restait muet.
                    # Heures rendues en heure locale, comme `maintenant`.
                    activite = [dict(r) for r in conn.execute(
                        "SELECT v.orebloc_id, o.nom AS bloc, datetime(MAX(v.created_at), 'localtime') AS dernier_voyage "
                        "FROM voyages v LEFT JOIN oreblocs o ON o.id = v.orebloc_id "
                        "WHERE v.orebloc_id IS NOT NULL "
                        "GROUP BY v.orebloc_id")]
                    # le minage deduit du DT alimente aussi le clignotement du Progress (Dig Plan)
                    deja = {d["exca_code"].upper() for d in decl}
                    for d in minages_deduits_dt(conn):
                        if d["exca_code"].upper() not in deja:
                            decl.append({"exca_code": d["exca_code"], "orebloc_id": d["orebloc_id"],
                                         "bloc": d["bloc"], "declared_at": now_iso()})
                    return self._send_json(200, {
                        "declarations": decl,
                        "activite": activite,
                        "maintenant": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    })

                if path == "/api/minage_declare" and method == "POST":
                    # Canal A : le spotter declare la pelle qui mine quel bloc.
                    body = json.loads(self._read_body() or b"{}")
                    exca = str(body.get("exca_code", "")).strip()
                    bloc_id = body.get("orebloc_id")
                    exca = exca.upper()
                    if body.get("stop") and exca:          # arret : la pelle seule suffit
                        conn.execute("DELETE FROM minage_actuel WHERE UPPER(exca_code)=?", (exca,))
                        conn.commit()
                        return self._send_json(200, {"stopped": exca})
                    if not exca or not bloc_id:
                        return self._send_json(400, {"error": "exca_code et orebloc_id requis"})
                    origine = str(body.get("origine") or "bureau").strip().lower()
                    origine = origine if origine in ("spotter", "bureau") else "bureau"
                    existe = conn.execute("SELECT id FROM oreblocs WHERE id=?", (bloc_id,)).fetchone()
                    if not existe:
                        return self._send_json(404, {"error": "bloc inconnu"})
                    # un exca ne mine qu'un bloc a la fois : on remplace sa ligne
                    conn.execute("DELETE FROM minage_actuel WHERE exca_code=?", (exca,))
                    if body.get("stop"):
                        conn.commit()
                        return self._send_json(200, {"stopped": exca})
                    conn.execute("INSERT INTO minage_actuel (exca_code, orebloc_id, origine) VALUES (?, ?, ?)",
                                 (exca, bloc_id, origine))
                    conn.commit()
                    enregistrer_pour_office(conn, {"minage": exca, "orebloc_id": bloc_id,
                                                   "action": "declaration_minage"})
                    notifier_office(conn)
                    return self._send_json(201, {"exca_code": exca, "orebloc_id": bloc_id})

                if path == "/api/missions" and method == "GET":
                    rows = [dict(r) for r in conn.execute("SELECT * FROM missions ORDER BY id DESC")]
                    return self._send_json(200, {"missions": rows})

                if path == "/api/engins" and method == "GET":
                    rows = [dict(r) for r in conn.execute(
                        "SELECT * FROM engins ORDER BY code")]
                    return self._send_json(200, {"engins": rows})

                if path == "/api/voyages" and method == "GET":
                    rows = [dict(r) for r in conn.execute(
                        "SELECT v.*, o.nom AS orebloc_nom, o.destination AS orebloc_destination, "
                        "o.minerai AS orebloc_minerai, d.nom AS da_nom "
                        "FROM voyages v "
                        "LEFT JOIN oreblocs o ON o.id = v.orebloc_id "
                        "LEFT JOIN dumping_areas d ON d.id = v.dumping_area_id "
                        "ORDER BY v.id DESC LIMIT 500")]
                    # classification calculee ICI (meme regle que les KPI) : la page n'a rien a deviner
                    for r in rows:
                        r["sterile"], r["grade"] = classer_voyage(
                            r["orebloc_nom"], r.pop("orebloc_destination", ""), r.pop("orebloc_minerai", None))
                    return self._send_json(200, {"voyages": rows})

                # Alignement immediat des missions sur Serveur_SG (la boucle le fait
                # deja toutes les 6 s) ; sert au diagnostic.
                if path == "/api/missions/synchroniser" and method == "POST":
                    liste = sg_lire_missions_en_cours()
                    if liste is None:
                        return self._send_json(502, {"error": "Serveur_SG injoignable ou non configure : rien n'a ete modifie"})
                    res = reconcilier_missions(conn, liste)
                    res["missions_sg_en_cours"] = len(liste)
                    return self._send_json(200, res)

                if path == "/api/affecter" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    dt_code = body.get("dt_code")
                    if not dt_code:
                        return self._send_json(400, {"error": "dt_code requis"})
                    if missions_source() == "cloud":
                        return self._send_json(409, {"error": "Les missions sont decidees par Serveur_SG "
                                                     "(MISSIONS_SOURCE=cloud) : affectation locale refusee"})
                    if conn.execute("SELECT 1 FROM missions WHERE dt_code=? AND statut='en_cours'",
                                    (dt_code,)).fetchone():
                        return self._send_json(409, {"error": "ce DT a deja une mission en cours"})
                    mission = generer_affectation(conn, dt_code)
                    if not mission:
                        return self._send_json(400, {"error": "DT non trouve ou pas en statut ODT"})
                    enregistrer_pour_office(conn, {"mission": mission, "action": "affectation"})
                    notifier_office(conn)
                    return self._send_json(201, mission)

                if path == "/api/verifier_deviation" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    dt_code = body.get("dt_code")
                    if not dt_code:
                        return self._send_json(400, {"error": "dt_code requis"})
                    # verifier_deviation enfile deja l'evenement pour l'Office
                    alerte = verifier_deviation(conn, dt_code)
                    if alerte:
                        notifier_office(conn)
                        return self._send_json(200, alerte)
                    m = conn.execute(
                        "SELECT devie, ecart_m FROM missions WHERE dt_code=? AND "
                        "statut='en_cours' ORDER BY id DESC LIMIT 1", (dt_code,)).fetchone()
                    if m and m["devie"]:
                        return self._send_json(200, {
                            "type": "deviation", "dt_code": dt_code, "distance": m["ecart_m"],
                            "message": "%s toujours devie (%d m du trajet)." % (dt_code, int(m["ecart_m"]))})
                    return self._send_json(200, {"statut": "ok", "message": "pas de deviation"})

                if path == "/api/offroad" and method == "GET":
                    return self._send_json(200, etat_offroad(conn))

                # Reglages de la detection, edites depuis la page Live.
                # {"actif": true, "seuil_m": 15, "retour_m": 10, "duree_s": 10,
                #  "marge_zone_m": 10, "rayon_exca_m": 50} ; champ absent = inchange.
                if path == "/api/offroad/reglages" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    connus = {"seuil_m": "OFFROAD_SEUIL_M",
                              "retour_m": "OFFROAD_RETOUR_M",
                              "duree_s": "OFFROAD_DUREE_S",
                              "marge_zone_m": "OFFROAD_MARGE_ZONE_M",
                              "rayon_exca_m": "OFFROAD_RAYON_EXCA_M"}
                    ecrits = []
                    if "actif" in body:
                        param_ecrire(conn, "OFFROAD_ACTIF", "1" if body["actif"] else "0")
                        ecrits.append("actif")
                    for champ, cle in connus.items():
                        if champ not in body:
                            continue
                        try:
                            v = float(body[champ])
                        except (TypeError, ValueError):
                            return self._send_json(400, {"error": "%s : nombre attendu" % champ})
                        if v < 0 or v > 10000:
                            return self._send_json(400, {"error": "%s hors limites" % champ})
                        param_ecrire(conn, cle, ("%g" % v))
                        ecrits.append(champ)
                    return self._send_json(200, {"ok": True, "enregistres": ecrits,
                                                 "reglages": offroad_reglages()})

                # Codes de teneur (Settings) : {"codes": {"HG": "high", ...}, "steriles": ["WST"]}
                if path == "/api/codes_teneur" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    codes = body.get("codes")
                    steriles = body.get("steriles")
                    if not isinstance(codes, dict) or not isinstance(steriles, list):
                        return self._send_json(400, {"error": "codes (objet) et steriles (liste) attendus"})
                    propre = {}
                    for k, v in codes.items():
                        k = str(k).strip().upper()
                        v = str(v).strip().lower()
                        if not re.fullmatch(r"[A-Z0-9]{1,8}", k):
                            return self._send_json(400, {"error": "code invalide : %r (1 a 8 lettres/chiffres)" % k})
                        if v not in GRADES_VALIDES:
                            return self._send_json(400, {"error": "grade invalide pour %s : %r" % (k, v)})
                        propre[k] = v
                    st = []
                    for c in steriles:
                        c = str(c).strip().upper()
                        if not re.fullmatch(r"[A-Z0-9]{1,8}", c):
                            return self._send_json(400, {"error": "code sterile invalide : %r" % c})
                        if c in propre:
                            return self._send_json(400, {"error": "%s est a la fois un grade et sterile" % c})
                        st.append(c)
                    if not propre:
                        return self._send_json(400, {"error": "au moins un code de teneur requis"})
                    couleurs = body.get("couleurs")
                    if couleurs is not None:
                        if not isinstance(couleurs, dict):
                            return self._send_json(400, {"error": "couleurs : objet {grade: #rrggbb} attendu"})
                        propres = {}
                        for g, c in couleurs.items():
                            if g not in COULEURS_GRADES_DEFAUT or not couleur_valide(c):
                                return self._send_json(400, {"error": "couleur invalide pour %r" % g})
                            propres[g] = couleur_valide(c)
                        param_ecrire(conn, "COULEURS_GRADES", json.dumps(propres, sort_keys=True))
                    param_ecrire(conn, "CODES_TENEUR", json.dumps(propre, sort_keys=True))
                    param_ecrire(conn, "CODES_STERILES", ",".join(st))
                    return self._send_json(200, {"ok": True, "codes": codes_teneur(), "steriles": list(codes_steriles()),
                                                 "couleurs": couleurs_grades()})

                if path == "/api/alertes" and method == "POST":
                    # acquittement : {"id": N} ou {"tous": true}
                    body = json.loads(self._read_body() or b"{}")
                    qui = str(body.get("par", ""))
                    if body.get("tous"):
                        conn.execute(
                            "UPDATE alertes SET acquittee=1, acquittee_par=?, "
                            "acquittee_le=? WHERE acquittee=0", (qui, now_iso()))
                    elif body.get("id"):
                        conn.execute(
                            "UPDATE alertes SET acquittee=1, acquittee_par=?, "
                            "acquittee_le=? WHERE id=?",
                            (qui, now_iso(), int(body["id"])))
                    else:
                        return self._send_json(400, {"error": "id ou tous requis"})
                    conn.commit()
                    return self._send_json(200, {"ok": True})

                if path == "/api/office/sync" and method == "GET":
                    rows = [dict(r) for r in conn.execute("SELECT * FROM office_sync ORDER BY id DESC")]
                    return self._send_json(200, {"office_sync": rows, "liaison": dict(_office_derniere_erreur)})

                if path == "/" and method == "GET":
                    return self.serve_static("/index.html")

                return self._send_json(404, {"error": "endpoint inconnu: " + path})
        finally:
            conn.close()

    # ----------------------------- statique -----------------------------
    def serve_static(self, path):
        if path == "/" or path == "":
            path = "/index.html"
        fichier = os.path.join(BASE_DIR, path.lstrip("/"))
        if not os.path.isfile(fichier):
            fichier = os.path.join(BASE_DIR, "index.html")
        if not os.path.isfile(fichier):
            return self._send_json(404, {"error": "fichier introuvable"})
        with open(fichier, "rb") as f:
            data = f.read()
        self.send_response(200)
        if fichier.endswith(".html"):
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif fichier.endswith(".css"):
            self.send_header("Content-Type", "text/css; charset=utf-8")
        elif fichier.endswith(".js"):
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
        elif fichier.endswith(".json"):
            self.send_header("Content-Type", "application/json; charset=utf-8")
        elif fichier.endswith((".jpg", ".jpeg")):
            self.send_header("Content-Type", "image/jpeg")
        elif fichier.endswith(".png"):
            self.send_header("Content-Type", "image/png")
        else:
            self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


# --------------------------------------------------------------- main -----------
def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = get_conn()
    # migration legacy : colonne blast_id sur oreblocs, couleur sur DA
    cols = [r[1] for r in conn.execute("PRAGMA table_info(oreblocs)")]
    if "blast_id" not in cols:
        conn.execute("ALTER TABLE oreblocs ADD COLUMN blast_id INTEGER DEFAULT 0")
        conn.commit()
    # alertes : acquittement (vue/traitee par un operateur)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(alertes)")]
    for nom, decl in (("acquittee", "INTEGER DEFAULT 0"),
                      ("acquittee_par", "TEXT DEFAULT ''"),
                      ("acquittee_le", "TEXT DEFAULT ''"),
                      ("retour_le", "TEXT DEFAULT ''"),          # 21/11 : retour du DT (cloture cote DT)
                      ("retour_duree_s", "INTEGER DEFAULT 0"),
                      ("retour_cause", "TEXT DEFAULT ''")):
        if nom not in cols:
            conn.execute("ALTER TABLE alertes ADD COLUMN %s %s" % (nom, decl))
    # 21/11 : alertes anterieures au cycle de vie -> retour deduit de l'evenement « retour » suivant du meme DT
    for typ, retour in (("offroad", "retour_piste"), ("deviation", "retour_trajet")):
        conn.execute(
            "UPDATE alertes SET "
            " retour_le = (SELECT r.created_at FROM alertes r WHERE r.dt_code = alertes.dt_code AND r.type = ? "
            "              AND r.id > alertes.id ORDER BY r.id LIMIT 1), "
            " retour_duree_s = MAX(0, CAST((julianday((SELECT r.created_at FROM alertes r WHERE r.dt_code = alertes.dt_code "
            "              AND r.type = ? AND r.id > alertes.id ORDER BY r.id LIMIT 1)) - julianday(created_at)) * 86400 AS INTEGER)) "
            "WHERE type = ? AND retour_le = '' AND EXISTS (SELECT 1 FROM alertes r WHERE r.dt_code = alertes.dt_code "
            "              AND r.type = ? AND r.id > alertes.id)", (retour, retour, typ, retour))
    conn.commit()
    # voyages : conformite dumping (conforme / compromis / non_conforme)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(voyages)")]
    if "conformite" not in cols:
        conn.execute("ALTER TABLE voyages ADD COLUMN conformite TEXT DEFAULT ''")
    if "conformite_detail" not in cols:
        conn.execute("ALTER TABLE voyages ADD COLUMN conformite_detail TEXT DEFAULT ''")
        conn.commit()
    if "cle_sg" not in cols:
        # 25/11 : cle du cycle Serveur_SG (un chargement au dumping valide = un voyage, une seule fois)
        conn.execute("ALTER TABLE voyages ADD COLUMN cle_sg TEXT DEFAULT ''")
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS ix_voyages_cle_sg ON voyages(cle_sg)")
    conn.commit()
    # sources : codes admis dans le nom des blasts (garde-fou LMP2/LWP2)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(sources)")]
    if "codes" not in cols:
        conn.execute("ALTER TABLE sources ADD COLUMN codes TEXT DEFAULT ''")
        conn.commit()
    # Garde-fou anti-doublons : un numero de blast actif est unique par source.
    # (Index partiel : les blasts desactives -- actif=0 -- n'empachent pas la
    # recreation d'un numero apres suppression.)
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_blasts_numero_source "
        "ON blasts (numero COLLATE NOCASE, UPPER(source_nom)) WHERE actif=1")
    conn.commit()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(dumping_areas)")]
    if "couleur" not in cols:
        conn.execute("ALTER TABLE dumping_areas ADD COLUMN couleur TEXT DEFAULT 'blue'")
        conn.commit()
    if "sommets" not in cols:
        conn.execute("ALTER TABLE dumping_areas ADD COLUMN sommets TEXT DEFAULT '[]'")
        conn.commit()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(oreblocs)")]
    if "minerai" not in cols:
        conn.execute("ALTER TABLE oreblocs ADD COLUMN minerai INTEGER DEFAULT 1")
        conn.commit()

    cols = [r[1] for r in conn.execute("PRAGMA table_info(minage_actuel)")]
    if cols and "origine" not in cols:
        conn.execute("ALTER TABLE minage_actuel ADD COLUMN origine TEXT DEFAULT 'bureau'")
        conn.commit()

    cols = [r[1] for r in conn.execute("PRAGMA table_info(teneurs)")]
    if "codes" not in cols:
        conn.execute("ALTER TABLE teneurs ADD COLUMN codes TEXT DEFAULT ''")
        conn.commit()
    if "couleur" not in cols:
        conn.execute("ALTER TABLE teneurs ADD COLUMN couleur TEXT DEFAULT ''")
        conn.commit()

    cols = [r[1] for r in conn.execute("PRAGMA table_info(blasts)")]
    for colonne, definition in (("ot_sync", "INTEGER DEFAULT 0"),
                                ("ot_erreur", "TEXT DEFAULT ''"),
                                ("ot_maj_le", "TEXT DEFAULT ''"),
                                # 18/11 : etat CLOS, pose par le client
                                # (bouton Clore de Dig Plan) ; clos_ot = 1
                                # quand Serveur_OT l'a enregistre.
                                ("clos_le", "TEXT DEFAULT ''"),
                                ("clos_ot", "INTEGER DEFAULT 0")):
        if colonne not in cols:
            conn.execute("ALTER TABLE blasts ADD COLUMN %s %s" % (colonne, definition))
            conn.commit()

    # office_sync : rejeu des echecs + UUID des evenements envoyes a OT
    cols = [r[1] for r in conn.execute("PRAGMA table_info(office_sync)")]
    for nom, decl in (("tentatives", "INTEGER DEFAULT 0"),
                      ("derniere_tentative", "TEXT DEFAULT ''"),
                      ("event_id", "TEXT DEFAULT ''"),
                      ("erreur", "TEXT DEFAULT ''")):          # 24/11 : raison du dernier echec
        if nom not in cols:
            conn.execute("ALTER TABLE office_sync ADD COLUMN %s %s" % (nom, decl))
    conn.commit()
    # engins : etat de sortie de route (detection offroad)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(engins)")]
    for nom, decl in (("canal", "TEXT DEFAULT ''"), ("auth", "TEXT DEFAULT ''")):
        if nom not in cols:
            conn.execute("ALTER TABLE engins ADD COLUMN %s %s" % (nom, decl))
    for nom, decl in (("offroad", "INTEGER DEFAULT 0"),
                      ("offroad_depuis", "TEXT DEFAULT ''"),
                      ("offroad_candidat_depuis", "TEXT DEFAULT ''"),
                      ("offroad_distance", "REAL DEFAULT 0")):
        if nom not in cols:
            conn.execute("ALTER TABLE engins ADD COLUMN %s %s" % (nom, decl))
    conn.commit()
    cols_k = [r[1] for r in conn.execute("PRAGMA table_info(cles_dt)")]
    for nom, decl in (("cle_prec", "TEXT DEFAULT ''"), ("prec_jusqua", "REAL DEFAULT 0"), ("maj_le", "TEXT DEFAULT ''")):
        if nom not in cols_k:
            conn.execute("ALTER TABLE cles_dt ADD COLUMN %s %s" % (nom, decl))
    cols_t = [r[1] for r in conn.execute("PRAGMA table_info(tablettes)")]
    for nom, decl in (("profil", "TEXT DEFAULT 'dt'"), ("ident", "TEXT DEFAULT ''"), ("equipe", "TEXT DEFAULT ''")):
        if nom not in cols_t:
            conn.execute("ALTER TABLE tablettes ADD COLUMN %s %s" % (nom, decl))
    conn.execute("UPDATE tablettes SET ident=dt_code WHERE ident='' AND dt_code<>''")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_tablettes_ident ON tablettes(profil, ident) WHERE ident <> ''")
    conn.commit()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(missions)")]
    if "arrivee" not in cols:
        conn.execute("ALTER TABLE missions ADD COLUMN arrivee INTEGER DEFAULT 0")
        conn.commit()
    # missions : etat de deviation (la mission reste en_cours quand le DT devie)
    for nom, decl in (("devie", "INTEGER DEFAULT 0"),
                      ("devie_depuis", "TEXT DEFAULT ''"),
                      ("devie_candidat_depuis", "TEXT DEFAULT ''"),
                      ("ecart_m", "REAL DEFAULT 0")):
        if nom not in cols:
            conn.execute("ALTER TABLE missions ADD COLUMN %s %s" % (nom, decl))
    # missions : lien avec la mission de Serveur_SG (alignement 20/11)
    for nom, decl in (("id_sg", "INTEGER"), ("cle_sg", "TEXT DEFAULT ''")):
        if nom not in cols:
            conn.execute("ALTER TABLE missions ADD COLUMN %s %s" % (nom, decl))
    # missions : checkpoints de l'itineraire (lus chez Serveur_SG) et leur suivi
    for nom, decl in (("cp_json", "TEXT DEFAULT '[]'"),
                      ("cp_etat", "TEXT DEFAULT '{}'"),
                      ("alertes_vues", "TEXT DEFAULT '{}'")):     # 24/11 : alertes deja signalees (survit a une relance)
        if nom not in cols:
            conn.execute("ALTER TABLE missions ADD COLUMN %s %s" % (nom, decl))
    # checkpoints : interdiction pour le WASTE (23/11). Tout checkpoint est interdit par defaut (1) ;
    # la section « Trajectoire Waste » de Senor en retire ceux que le waste peut franchir.
    cols_cp = [r[1] for r in conn.execute("PRAGMA table_info(checkpoints)")]
    if "interdit_waste" not in cols_cp:
        conn.execute("ALTER TABLE checkpoints ADD COLUMN interdit_waste INTEGER DEFAULT 1")
        conn.commit()
    if "interdit_ore" not in cols_cp:
        conn.execute("ALTER TABLE checkpoints ADD COLUMN interdit_ore INTEGER DEFAULT 0")
        conn.commit()
    # Les missions 'devoie' de l'ancienne detection restent telles quelles
    # (historique) : les reactiver pourrait bloquer l'affectation d'un DT.
    conn.commit()
    # backfill : les dumping areas legacy (point x,y + rayon_m) deviennent un
    # vrai polygone (carre de cote 2*rayon) pour le geofencing "arrivee".
    for da in conn.execute("SELECT id, x, y, rayon_m, sommets FROM dumping_areas").fetchall():
        poly = json.loads(da["sommets"] or "[]")
        if len(poly) >= 3:
            continue
        poly = polygone_depuis_point(da["x"] or 0, da["y"] or 0, da["rayon_m"] or 50)
        conn.execute("UPDATE dumping_areas SET sommets=? WHERE id=?",
                     (json.dumps(poly), da["id"]))
    conn.commit()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(travels_roads)")]
    if "type" not in cols:
        conn.execute("ALTER TABLE travels_roads ADD COLUMN type TEXT DEFAULT 'geojson'")
        conn.execute("ALTER TABLE travels_roads ADD COLUMN fichier TEXT DEFAULT ''")
        conn.commit()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(teneurs)")]
    if "valeur_min" not in cols:
        conn.execute("ALTER TABLE teneurs ADD COLUMN valeur_min REAL DEFAULT 0")
        conn.execute("ALTER TABLE teneurs ADD COLUMN valeur_max REAL DEFAULT 0")
        conn.commit()
    if "dumping_area_id" not in cols:
        conn.execute("ALTER TABLE teneurs ADD COLUMN dumping_area_id INTEGER DEFAULT 0")
        conn.commit()
    # migration page Dig Plan : champs riches par orebloc (volume/tonnes/Au/densite/
    # destination/litho) et metadonnees de planche par blast (nom, Geosum, survey,
    # map, bench/heading/flitch/echelle).
    cols = [r[1] for r in conn.execute("PRAGMA table_info(oreblocs)")]
    if "volume" not in cols:
        for col, decl in (("volume", "REAL DEFAULT 0"), ("tonnes", "REAL DEFAULT 0"),
                           ("au_teneur", "REAL DEFAULT 0"), ("density", "REAL DEFAULT 0"),
                           ("destination", "TEXT DEFAULT ''"), ("litho", "TEXT DEFAULT ''")):
            conn.execute("ALTER TABLE oreblocs ADD COLUMN %s %s" % (col, decl))
        conn.commit()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(blasts)")]
    if "nom" not in cols:
        for col, decl in (("nom", "TEXT DEFAULT ''"), ("name_geosum", "TEXT DEFAULT ''"),
                           ("name_survey", "TEXT DEFAULT ''"), ("name_map", "TEXT DEFAULT ''"),
                           ("source_nom", "TEXT DEFAULT ''"), ("destination", "TEXT DEFAULT ''"),
                           ("bench", "TEXT DEFAULT ''"), ("heading", "TEXT DEFAULT ''"),
                           ("flitch", "TEXT DEFAULT ''"), ("plan_date", "TEXT DEFAULT ''"),
                           ("scale", "TEXT DEFAULT ''"), ("page_label", "TEXT DEFAULT ''")):
            conn.execute("ALTER TABLE blasts ADD COLUMN %s %s" % (col, decl))
        conn.commit()
    if "name_dxf" not in cols:
        conn.execute("ALTER TABLE blasts ADD COLUMN name_dxf TEXT DEFAULT ''")
        conn.commit()
    if "map_image" not in cols:
        conn.execute("ALTER TABLE blasts ADD COLUMN map_image TEXT DEFAULT ''")
        conn.commit()
    if "map_url" not in cols:
        conn.execute("ALTER TABLE blasts ADD COLUMN map_url TEXT DEFAULT ''")
        conn.commit()
    if "name_base" not in cols:
        conn.execute("ALTER TABLE blasts ADD COLUMN name_base TEXT DEFAULT ''")
        conn.commit()
    # (20/11 : la source « MAIN PIT » et ses teneurs de demo n'etaient plus creees d'office : les sources et
    #  teneurs viennent de Settings / Dig Plan, jamais d'un jeu d'essai)
    conn.close()

    # 20/11 : plus AUCUNE donnee de demonstration au demarrage. Avant, une base vide recevait un « Orebloc A »,
    # une « Dumping Area Principale », les engins EXCA-01 / DT-01 et un blast fictif LWP2_235_230_107 :
    # ils apparaissaient dans Dig Plan, le suivi et les compteurs comme des donnees reelles.

    threading.Thread(target=ecouteur_xbee, daemon=True).start()
    # envoi vers Serveur_OT hors du verrou global (16/11)
    threading.Thread(target=boucle_office, daemon=True).start()
    # positions reelles des DT : remontees de Serveur_SG (17/11)
    threading.Thread(target=boucle_remontees, daemon=True).start()
    # chargements de SG pour les KPI du dashboard, toutes les 5 s (19/11)
    threading.Thread(target=boucle_chargements, daemon=True).start()
    threading.Thread(target=boucle_checkpoints, daemon=True).start()
    # alignement des missions locales sur Serveur_SG (20/11)
    threading.Thread(target=boucle_missions_sg, daemon=True).start()
    # statuts WEX/OMEX/WDT/SBDT/ODT recalcules toutes les 30 s (positions fraiches seulement, 20/11)
    threading.Thread(target=boucle_statuts, daemon=True).start()
    # video automatique de chaque shift termine (20/11)
    record_nettoyer()
    threading.Thread(target=boucle_video_auto, daemon=True).start()
    # MQTT (19/11) : comptes des DT d'apres la flotte + positions en direct
    threading.Thread(target=boucle_comptes_mqtt, daemon=True).start()
    threading.Thread(target=boucle_suivi_mqtt, daemon=True).start()
    # 26/11 : client de PUBLICATION vers les DT (alertes, missions, reglages) connecte des le demarrage, et plus
    # seulement au premier envoi : la console montre tout de suite si la descente MQTT fonctionne.
    threading.Thread(target=_mqtt_client, daemon=True).start()

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print("Server_Senor_Geology demarre sur http://%s:%d" % (HOST, PORT))
    print("  DB : %s" % DB_PATH)
    print("  API : http://localhost:%d/api/health" % PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nArret.")
        httpd.server_close()


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "--video":      # processus de generation video (20/11)
        sys.exit(cli_video(sys.argv[2:]))
    main()
