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
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import uuid
from datetime import datetime
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
XBEE_PORT = os.environ.get("XBEE_PORT", "")
XBEE_BAUD = int(os.environ.get("XBEE_BAUD", "9600"))

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
                  "SG_INTERVALLE_S")
_params_cache = {}
_params_verrou = threading.Lock()


def param_lire(cle, defaut=""):
    """Valeur d'un reglage : environnement d'abord, puis base, puis defaut."""
    env = os.environ.get(cle, "")
    if env:
        return env
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
    offroad_distance REAL DEFAULT 0         -- distance a la piste la plus proche (m)
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
    created_at TEXT DEFAULT (datetime('now'))
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
CODES_STERILE = ("WST", "WASTE", "ST", "STERILE")

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
    if code in CODES_STERILE:
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

# Valeurs de `Destination` qui designent du STERILE. Le fichier complet
# (un sommet par ligne) liste le sterile avec son volume et son tonnage,
# marque `W` ; le fichier de reserve allege, lui, ne liste que le minerai
# et le sterile s'y reconnait a son absence. Les deux formes sont lues.
GEOSUM_DESTINATIONS_STERILE = ("W", "WST", "WASTE", "STERILE")


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
        bloc["_minerai"] = 0 if dest in GEOSUM_DESTINATIONS_STERILE else 1
        # La destination du sterile (`W`) est CONSERVEE : la table doit dire
        # ce que le fichier dit. Aucun risque de la confondre avec une
        # consigne de dumping area -- une famille inconnue du referentiel
        # retombe sur la zone la plus proche, ce qui est justement le
        # comportement voulu pour du sterile. Et si un jour une teneur `W`
        # est creee avec sa verse, l'acheminement devient explicite.
        out[cle] = bloc
    return out


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
    rapport = {"minerai": [], "sterile": [], "absents_du_dxf": []}
    vus = set()
    for o in conn.execute(
            "SELECT id, nom FROM oreblocs WHERE blast_id=? AND actif=1", (blast_id,)):
        donnees = geosum.get(cle_bloc(o["nom"]))
        if donnees is None:
            # Absent du fichier : sterile, et sans donnees de reserve.
            conn.execute("UPDATE oreblocs SET minerai=0 WHERE id=?", (o["id"],))
            rapport["sterile"].append({"bloc": o["nom"], "tonnes": None})
            continue
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
        return 0
    appliquer_geosum(conn, blast_id, attributs)
    return len(attributs)


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


def prefixe_lot_label(label):
    """Prefixe `flitch_site_blast` d'un label, '' si introuvable.

    270_LMP2_119_WST_FR_044_OP -> '270_LMP2_119' : tout jusqu'au premier
    segment entier pur (1 a 4 chiffres) apres le flitch, regle du numero de
    blast de Serveur_OT (`numeroBlastDepuisLabel`)."""
    seg = str(label or "").strip().split("_")
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


def appliquer_position_engin(conn, code, typ, x, y):
    """Met a jour (ou cree) un engin a la position (x, y) et gere le chrono
    d'immobilisation (seuil SEUIL_IMMOBILISATION_M). Utilise aussi bien par
    la route HTTP /api/engins que par l'ecouteur radio XBee."""
    existe = conn.execute("SELECT * FROM engins WHERE code=?", (code,)).fetchone()
    if existe:
        deplacement = distance(x, y, existe["x"], existe["y"])
        if deplacement <= SEUIL_IMMOBILISATION_M:
            immobilise_depuis = existe["immobilise_depuis"] or now_iso()
        else:
            immobilise_depuis = ""
        conn.execute(
            "UPDATE engins SET x=?, y=?, immobilise_depuis=?, derniere_maj=? WHERE code=?",
            (x, y, immobilise_depuis, now_iso(), code))
    else:
        conn.execute(
            "INSERT INTO engins (code, type, x, y, derniere_maj) VALUES (?, ?, ?, ?, ?)",
            (code, typ, x, y, now_iso()))
    conn.commit()
    # Relais vers Serveur_SG : point d'entree UNIQUE des positions (HTTP et
    # radio XBee passent tous deux ici), donc rien ne peut etre oublie.
    sg_relayer_position(code, typ or "DT", x, y)
    sg_vider_file()
    # geofencing dumping area : le DT est-il entre dans la zone de sa mission ?
    if (typ or "DT") == "DT":
        verifier_arrivee_da(conn, code)
        # sortie de route : hors de toute piste (hors zones autorisees)
        try:
            verifier_offroad(conn, code)
        except Exception as e:      # ne jamais perdre une position pour ca
            print("OFFROAD: verification impossible pour %s: %s" % (code, e))
        # apres l'offroad : un DT hors piste n'est pas aussi signale devie
        try:
            verifier_deviation(conn, code)
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


def verifier_signature_trame(conn, dt_code, corps_signe, ts, sig):
    """Verifie la signature HMAC-SHA256 d'une trame XBee pour dt_code.
    Rejette si : aucune cle active enregistree pour ce DT, horodatage
    absent/hors tolerance (rejeu ou derive d'horloge), ou signature
    invalide. Retourne True/False."""
    row = conn.execute(
        "SELECT cle_hex FROM cles_dt WHERE dt_code=? AND actif=1", (dt_code,)).fetchone()
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
    try:
        cle = bytes.fromhex(row["cle_hex"])
    except Exception:
        return False
    attendu = hmac.new(cle, corps_signe.encode("utf-8"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(attendu, str(sig))


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
    appliquer_position_engin(conn, code, typ, x, y)
    statuts_engins(conn)
    verifier_affectations_auto(conn)
    notifier_office(conn)
    return (code, x, y)


def ecouteur_xbee():
    """Thread d'ecoute radio XBee (montee des positions DT via la Base RTK +
    relais DigiMesh). Se desactive proprement si pyserial n'est pas
    installe ou si XBEE_PORT n'est pas configure (poste de dev sans
    radio branchee) ; le reste du serveur continue de fonctionner."""
    if serial is None:
        print("XBEE: pyserial non installe, ecoute radio desactivee (pip install pyserial)")
        return
    if not XBEE_PORT:
        print("XBEE: XBEE_PORT non configure, ecoute radio desactivee")
        return
    while True:
        try:
            with serial.Serial(XBEE_PORT, XBEE_BAUD, timeout=5) as ser:
                print("XBEE: connecte sur %s @ %d bauds" % (XBEE_PORT, XBEE_BAUD))
                while True:
                    brut = ser.readline()
                    if not brut:
                        continue
                    conn = get_conn()
                    try:
                        with LOCK:
                            traiter_trame_xbee(conn, brut.decode("utf-8", errors="replace"))
                    finally:
                        conn.close()
        except Exception as e:
            print("XBEE: erreur liaison serie (%s), nouvelle tentative dans 5s" % e)
            time.sleep(5)


# ----------------------------------------------------------- logique metier -----
def statuts_engins(conn):
    """Recalcule les statuts WEX/OMEX (exca) et WDT/SBDT/ODT (DT) selon les positions."""
    engins = [dict(r) for r in conn.execute("SELECT * FROM engins WHERE actif=1")]
    excas = [e for e in engins if e["type"] == "EXCA"]
    dts = [e for e in engins if e["type"] == "DT"]
    oreblocs = [dict(r) for r in conn.execute("SELECT * FROM oreblocs WHERE actif=1")]

    # 1) Statut excavatrice : OMEX si dans un orebloc, sinon WEX
    for e in excas:
        dedans = False
        for ob in oreblocs:
            poly = json.loads(ob["sommets"] or "[]")
            if point_in_polygon(e["x"], e["y"], poly):
                dedans = True
                break
        nouveau = "OMEX" if dedans else "WEX"
        if nouveau != e["statut"]:
            conn.execute(
                "UPDATE engins SET statut=?, derniere_maj=? WHERE id=?",
                (nouveau, now_iso(), e["id"]))
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

        nouveau = d["statut"]
        if exca_proche and dist_min < DT_PROCHE_EXCA_ODT and immo_min >= IMMOBILISATION_ODT_MIN and dans_orebloc:
            nouveau = "ODT"
        elif exca_proche and dist_min > DT_LOIN_EXCA_SBDT and exca_proche["statut"] == "OMEX" and immo_min >= IMMOBILISATION_SBDT_MIN:
            nouveau = "SBDT"
        else:
            nouveau = "WDT"

        if nouveau != d["statut"]:
            conn.execute(
                "UPDATE engins SET statut=?, derniere_maj=? WHERE id=?",
                (nouveau, now_iso(), d["id"]))
            d["statut"] = nouveau

    conn.commit()
    return [dict(r) for r in conn.execute("SELECT * FROM engins WHERE actif=1")]


def generer_affectation(conn, dt_code):
    """Cree une mission pour un DT en statut ODT : destination + Dumping Area + chemin."""
    dt = conn.execute("SELECT * FROM engins WHERE code=? AND type='DT'", (dt_code,)).fetchone()
    if not dt or dt["statut"] != "ODT":
        return None

    oreblocs = [dict(r) for r in conn.execute("SELECT * FROM oreblocs WHERE actif=1")]
    # trouver l'orebloc contenant (ou le plus proche, meme marge que le
    # passage ODT) le DT
    orebloc_id = 0
    for ob in oreblocs:
        poly = json.loads(ob["sommets"] or "[]")
        if dans_ou_pres_polygone(dt["x"], dt["y"], poly, MARGE_OREBLOC_M):
            orebloc_id = ob["id"]
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


def verifier_arrivee_da(conn, dt_code):
    """Geofencing dumping area : des que le DT entre dans le polygone de la
    dumping area de sa mission en cours, la mission est marquee "arrivee"
    (une seule fois) et une alerte "vous etes arrive" part au camion."""
    mission = conn.execute(
        "SELECT * FROM missions WHERE dt_code=? AND statut='en_cours' ORDER BY id DESC LIMIT 1",
        (dt_code,)).fetchone()
    if not mission or mission["arrivee"] or not mission["dumping_area_id"]:
        return None
    da = conn.execute(
        "SELECT * FROM dumping_areas WHERE id=? AND actif=1",
        (mission["dumping_area_id"],)).fetchone()
    if not da:
        return None
    dt = conn.execute("SELECT * FROM engins WHERE code=?", (dt_code,)).fetchone()
    if not dt:
        return None
    poly = json.loads(da["sommets"] or "[]")
    if not point_in_polygon(dt["x"], dt["y"], poly):
        return None
    conn.execute("UPDATE missions SET arrivee=1 WHERE id=?", (mission["id"],))
    conn.commit()
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
    }


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


def verifier_deviation(conn, dt_code):
    """Met a jour l'etat de deviation de la mission en cours du DT apres une
    nouvelle position. Retourne l'evenement {"type": "deviation" |
    "retour_trajet", ...} lors d'un changement d'etat, sinon None."""
    reglages = deviation_reglages()
    if not reglages["actif"]:
        return None
    mission = conn.execute(
        "SELECT * FROM missions WHERE dt_code=? AND statut='en_cours' ORDER BY id DESC LIMIT 1",
        (dt_code,)).fetchone()
    if not mission or mission["arrivee"]:
        return None     # pas de mission, ou deja arrive dans la dumping area
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
    maintenant = now_iso()
    evenement = None

    if mission["devie"]:
        if zone or ecart <= reglages["retour_m"]:
            message = "%s de retour sur le trajet de sa mission." % dt_code
            conn.execute(
                "UPDATE missions SET devie=0, devie_depuis='', devie_candidat_depuis='', "
                "ecart_m=? WHERE id=?", (round(ecart, 1), mission["id"]))
            conn.commit()
            creer_alerte(conn, "retour_trajet", dt_code, message)
            evenement = {"type": "retour_trajet", "dt_code": dt_code, "x": x, "y": y,
                         "mission_id": mission["id"], "distance": ecart, "message": message}
        else:
            conn.execute("UPDATE missions SET ecart_m=? WHERE id=?",
                         (round(ecart, 1), mission["id"]))
            conn.commit()
    elif not zone and not deja_offroad and ecart > reglages["seuil_m"]:
        candidat = mission["devie_candidat_depuis"] or maintenant
        if _secondes_depuis(candidat) >= reglages["duree_s"]:
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
        enregistrer_pour_office(conn, {"alerte": evenement, "action": evenement["type"]})
    return evenement


# ---------------------------------------------------- sortie de route ----
# "Offroad" = le DT roule hors de TOUTE piste (independant de sa mission).
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


def _secondes_depuis(horodatage):
    try:
        t = datetime.strptime(horodatage, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return 0.0
    return (datetime.now() - t).total_seconds()


def verifier_offroad(conn, dt_code):
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
    aretes = offroad_aretes(conn)
    if not aretes or pistes_en_degres(aretes):
        return None         # pas de reseau exploitable : on ne devine pas

    x, y = dt["x"], dt["y"]
    dist = distance_piste_la_plus_proche(aretes, x, y)
    zone = zone_hors_piste_autorisee(conn, x, y, reglages)
    maintenant = now_iso()
    evenement = None

    if dt["offroad"]:
        if zone or dist <= reglages["retour_m"]:
            duree = int(_secondes_depuis(dt["offroad_depuis"]))
            message = "%s de retour sur piste (%s apres %d s hors piste)." % (
                dt_code, zone or "%d m de la piste" % int(dist), duree)
            conn.execute(
                "UPDATE engins SET offroad=0, offroad_depuis='', "
                "offroad_candidat_depuis='', offroad_distance=? WHERE code=?",
                (round(dist, 1), dt_code))
            conn.commit()
            creer_alerte(conn, "retour_piste", dt_code, message)
            evenement = {"type": "retour_piste", "dt_code": dt_code, "x": x, "y": y,
                         "distance": dist, "duree_s": duree, "message": message}
        else:
            conn.execute("UPDATE engins SET offroad_distance=? WHERE code=?",
                         (round(dist, 1), dt_code))
            conn.commit()
    elif not zone and dist > reglages["seuil_m"]:
        candidat = dt["offroad_candidat_depuis"] or maintenant
        if _secondes_depuis(candidat) >= reglages["duree_s"]:
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
        enregistrer_pour_office(conn, {"alerte": evenement, "action": evenement["type"]})
    return evenement


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
        "ORDER BY offroad_depuis")]
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
KPI_GRADES = {"HG": "high", "SHG": "high", "MG": "medium", "MG1": "medium",
              "MG2": "medium", "LG": "low", "LG1": "low", "LG2": "low", "MO": "marginal"}


def code_teneur_bloc(nom):
    """Code du 4e segment du nom (260_LMP2_113_MG_FR_021_OP -> MG), '' sinon."""
    parties = str(nom or "").split("_")
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
        code = code_teneur_bloc(r["nom"])
        dest = str(r["destination"] or "").strip().upper()
        sterile = code == "WST" or dest == "W" or not r["minerai"]
        if sterile and code not in KPI_GRADES:
            res["waste_t"] += t
            continue
        onces = t * float(r["au_teneur"] or 0) / GRAMMES_PAR_ONCE
        res["minerais_t"] += t
        res["onces"] += onces
        g = KPI_GRADES.get(code)
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


def _mqtt_client():
    """Cree et connecte (une seule fois, paresseusement) le client MQTT vers
    MQTT_HOST/MQTT_PORT. Retourne None si paho-mqtt n'est pas installe, si
    MQTT_HOST n'est pas configure, ou si la connexion echoue (meme logique
    de degradation propre que serial pour l'ecouteur XBee) : le reste du
    serveur continue de fonctionner sans la descente MQTT."""
    global _MQTT_CLIENT
    if mqtt is None or not MQTT_HOST:
        return None
    with _MQTT_LOCK:
        if _MQTT_CLIENT is not None:
            return _MQTT_CLIENT
        client = mqtt.Client(client_id="senor-geology-%d" % os.getpid())
        if MQTT_USER:
            client.username_pw_set(MQTT_USER, MQTT_PASS)
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
            client.loop_start()
            _MQTT_CLIENT = client
            print("MQTT: connecte a %s:%d" % (MQTT_HOST, MQTT_PORT))
        except Exception as e:
            print("MQTT: connexion impossible (%s), publication desactivee" % e)
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
        return info.rc == mqtt.MQTT_ERR_SUCCESS
    except Exception as e:
        print("MQTT: erreur publication sur %s: %s" % (topic, e))
        return False


def creer_alerte(conn, type_alerte, dt_code, message, voix_off=1, dest_office=1):
    """Enregistre une alerte en base ET la publie en temps reel sur le canal
    MQTT dedie du camion (camions/<dt_code>/alerte), pour declenchement
    effectif de la voix off cote tablette (deviation, fin de mission...)."""
    conn.execute(
        "INSERT INTO alertes (type, dt_code, message, voix_off, dest_office) "
        "VALUES (?, ?, ?, ?, ?)",
        (type_alerte, dt_code, message, voix_off, dest_office))
    conn.commit()
    mqtt_publier("camions/%s/alerte" % dt_code, {
        "type": type_alerte, "dt_code": dt_code, "message": message,
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
            "destination_y, dumping_area_id, chemin, statut) "
            "VALUES (?, ?, ?, ?, ?, ?, 'en_cours')",
            (code, orebloc_id, float(m.get("destination_x") or 0),
             float(m.get("destination_y") or 0), da_id, json.dumps(chemin)))
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
            corps.setdefault("name_map", os.path.basename(unquote(url.split("?")[0])))
    return corps


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
            reponse = json.loads(rep.read().decode("utf-8"))
            return bool(reponse.get("ok"))
    except Exception as e:
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
OFFICE_TENTATIVES_MAX = 10       # au-dela, l'entree reste en echec (visible)
OFFICE_REESSAI_S = 30            # delai minimal entre deux essais d'une entree
OFFICE_FENETRE_REESSAI_H = 24    # on ne rejoue pas d'evenements plus vieux
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
            finally:
                conn.close()
        except Exception as e:
            print("OFFICE: boucle d'envoi en erreur: %s" % e)


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
        "AND created_at >= datetime('now', ?) ORDER BY id LIMIT ?",
        (OFFICE_TENTATIVES_MAX, "-%d hours" % OFFICE_FENETRE_REESSAI_H, OFFICE_LOT_MAX))]
    for e in entrees:
        if e["statut"] == "echec" and e.get("derniere_tentative"):
            try:
                ecoule = (maintenant - datetime.strptime(
                    e["derniere_tentative"], "%Y-%m-%d %H:%M:%S")).total_seconds()
            except ValueError:
                ecoule = OFFICE_REESSAI_S
            if ecoule < OFFICE_REESSAI_S:
                continue
        ok = False
        try:
            payload = json.loads(e["payload"])
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
            else:
                ok = office_appel("notification",
                                  {"profil": "", "message": office_message(payload)})
        except Exception as ex:
            print("OFFICE: erreur traitement office_sync#%s: %s" % (e["id"], ex))
        conn.execute(
            "UPDATE office_sync SET statut=?, envoie_le=?, derniere_tentative=?, "
            "tentatives=COALESCE(tentatives, 0) + 1 WHERE id=?",
            ("envoye" if ok else "echec", now_iso() if ok else "", now_iso(), e["id"]))
        conn.commit()
        if not ok:
            break       # OT injoignable ou refus : on reessaiera au prochain passage
    conn.commit()


def enregistrer_pour_office(conn, payload_dict):
    """Enfile un payload pour le serveur Office."""
    conn.execute(
        "INSERT INTO office_sync (payload, statut) VALUES (?, 'en_attente')",
        (json.dumps(payload_dict, ensure_ascii=True),))
    conn.commit()


# --------------------------------------------------------------- handler HTTP ---
class Handler(BaseHTTPRequestHandler):
    server_version = "SenorGeology/1.0"
    protocol_version = "HTTP/1.1"

    def _send_json(self, code, obj):
        try:
            body = json.dumps(obj, ensure_ascii=True).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            # page SAAS sur :8100 -> API SG sur :8101 : CORS ouvert (LAN)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
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
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > 0:
            return self.rfile.read(length)
        return b""

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
        print("ROUTE_API:", method, path)
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
        # Test des liaisons sortantes : appels au cloud, donc HORS verrou.
        if path == "/api/relais/tester" and method == "POST":
            return self._send_json(200, relais_tester())
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
                        # unicite du nom d'orebloc dans la base
                        nom_final = nom
                        if nom_bloc_pris(conn, nom_final):
                            n = 2
                            while nom_bloc_pris(conn, "%s (%d)" % (nom, n)):
                                n += 1
                            nom_final = "%s (%d)" % (nom, n)
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
                    if base_contenu:
                        appliquer_base_attributs(conn, blast_id, base_contenu)
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
                    return self._send_json(201, {
                        "ok": True, "id": blast_id, "numero": numero,
                        "nb_blocs": len(blocs_crees), "oreblocs": blocs_crees,
                        "geosum": geosum_rapport,
                        "ot_sync": ot_ok, "ot_erreur": ot_err,
                        "onglets_sync": not onglets_err, "onglets_erreur": onglets_err})

                if path == "/api/blasts" and method == "PUT":
                    body = json.loads(self._read_body() or b"{}")
                    bid = int(body.get("id", 0))
                    if not bid:
                        return self._send_json(400, {"error": "id requis"})
                    ex = conn.execute("SELECT id FROM blasts WHERE id=? AND actif=1", (bid,)).fetchone()
                    if not ex:
                        return self._send_json(404, {"error": "blast introuvable"})
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
                    if base_contenu:
                        # Attributs des blocs (waste compris) depuis la base ; le
                        # Geosum, envoye ensuite par la page, garde le dernier mot.
                        appliquer_base_attributs(conn, bid, base_contenu)
                        numero_b = conn.execute(
                            "SELECT numero FROM blasts WHERE id=?", (bid,)).fetchone()[0]
                        ok_onglets, onglets_err = ot_pousser_onglets_blocs(
                            conn, bid, numero_b, base_contenu, "")
                        if not ok_onglets:
                            print("OT: onglet dig_bloc #%s non synchronise (%s)" % (
                                bid, onglets_err))
                    return self._send_json(200, {"ok": True, "id": bid,
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

                if path == "/api/blasts" and method == "DELETE":
                    qs = self._qs()
                    bid = int(qs.get("id", ["0"])[0])
                    if not bid:
                        return self._send_json(400, {"error": "id requis"})
                    ligne = conn.execute("SELECT numero FROM blasts WHERE id=?",
                                         (bid,)).fetchone()
                    numero = ligne["numero"] if ligne else ""
                    conn.execute("UPDATE blasts SET actif=0 WHERE id=?", (bid,))
                    conn.execute("UPDATE oreblocs SET actif=0 WHERE blast_id=?", (bid,))
                    conn.commit()
                    # Le classeur doit oublier le blast lui aussi, sinon il
                    # ressortirait au prochain sync_ref de Serveur_SG.
                    ot_ok, ot_err = ot_supprimer_blast(numero)
                    # Les lignes du blast disparaissent aussi des onglets
                    # dig_bloc / ore_dig_bloc, en INTEGRALITE : c'est la seule
                    # voie pour que la tablette n'affiche plus ses polygones.
                    ok_onglets, onglets_err = ot_supprimer_blast_onglets(
                        numero_lot_onglets(conn, bid, numero))
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
                    appliquer_position_engin(conn, code, typ, x, y)
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
                    for nom, sommets in extraits:
                        if conn.execute("SELECT id FROM dumping_areas WHERE LOWER(nom)=LOWER(?) AND actif=1",
                                        (nom,)).fetchone():
                            doublons.append(nom)
                            continue
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
                                                 "doublons": doublons})

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
                        "alerte_rouge_min": alerte_rouge_minutes(),
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
                              "alerte_rouge_min": "ALERTE_ROUGE_MIN"}
                    ecrits = []
                    for champ, cle in connus.items():
                        if champ not in body:
                            continue          # absent = inchange, "" = efface
                        valeur = str(body[champ] or "").strip()
                        if cle.endswith("_URL"):
                            valeur = valeur.rstrip("/")
                        param_ecrire(conn, cle, valeur)
                        ecrits.append(champ)
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
                    rows = [dict(r) for r in conn.execute(
                        "SELECT * FROM cles_dt WHERE actif=1 ORDER BY dt_code")]
                    return self._send_json(200, {"cles_dt": rows})

                if path == "/api/cles_dt" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    dt_code = (body.get("dt_code") or "").strip()
                    if not dt_code:
                        return self._send_json(400, {"error": "dt_code requis"})
                    cle_hex = secrets.token_hex(32)
                    conn.execute(
                        "INSERT INTO cles_dt (dt_code, cle_hex, actif) VALUES (?, ?, 1) "
                        "ON CONFLICT(dt_code) DO UPDATE SET cle_hex=excluded.cle_hex, actif=1",
                        (dt_code, cle_hex))
                    conn.commit()
                    return self._send_json(201, {"ok": True, "dt_code": dt_code, "cle_hex": cle_hex})

                if path == "/api/cles_dt" and method == "DELETE":
                    qs = self._qs()
                    dt_code = (qs.get("dt_code", [""])[0] or "").strip()
                    if not dt_code:
                        return self._send_json(400, {"error": "dt_code requis"})
                    conn.execute("UPDATE cles_dt SET actif=0 WHERE dt_code=?", (dt_code,))
                    conn.commit()
                    return self._send_json(200, {"ok": True, "dt_code": dt_code})

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
                        "INSERT INTO teneurs (source_id, nom, valeur_min, valeur_max, codes) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (sid, nom, valeur_min, valeur_max, codes))
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
                    # La teneur du bloc doit correspondre a la dumping area
                    # efectivement utilisee par la mission. La reference est
                    # la table teneurs : chaque famille de teneur a sa DA
                    # obligatoire (Dumping Instruction, page Senor).
                    conformite = ""
                    if mission["dumping_area_id"]:
                        # famille de teneur dont l'intervalle contient la teneur
                        # du bloc (min <= teneur <= max), DA attendue = sa DA
                        instr = conn.execute(
                            "SELECT dumping_area_id FROM teneurs "
                            "WHERE valeur_min <= ? AND ? <= valeur_max "
                            "AND dumping_area_id > 0 LIMIT 1",
                            (teneur, teneur)).fetchone()
                        if instr:
                            attendue = instr["dumping_area_id"]
                            if attendue == mission["dumping_area_id"]:
                                conformite = "conforme"
                            else:
                                conformite = "non_conforme"
                                da_attendue = conn.execute(
                                    "SELECT nom FROM dumping_areas WHERE id=?",
                                    (attendue,)).fetchone()
                                da_reelle = conn.execute(
                                    "SELECT nom FROM dumping_areas WHERE id=?",
                                    (mission["dumping_area_id"],)).fetchone()
                                creer_alerte(
                                    conn, "non_conforme", mission["dt_code"],
                                    "Dumping non conforme : %s devait decharger en %s "
                                    "(teneur %.1f), a decharge en %s" % (
                                        mission["dt_code"],
                                        da_attendue["nom"] if da_attendue else "DA #%d" % attendue,
                                        teneur,
                                        da_reelle["nom"] if da_reelle else "DA #%d" % mission["dumping_area_id"]))

                    conn.execute(
                        "UPDATE missions SET statut='termine', fin=? WHERE id=?",
                        (now_iso(), mission_id))
                    cur = conn.execute(
                        "INSERT INTO voyages (mission_id, dt_code, orebloc_id, dumping_area_id, "
                        "teneur, volume_t, truck_factor, conformite) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (mission_id, mission["dt_code"], mission["orebloc_id"],
                         mission["dumping_area_id"], teneur, volume_t, truck_factor, conformite))
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
                        "SELECT m.exca_code, m.orebloc_id, o.nom AS bloc, m.declared_at "
                        "FROM minage_actuel m LEFT JOIN oreblocs o ON o.id = m.orebloc_id")]
                    activite = [dict(r) for r in conn.execute(
                        "SELECT v.orebloc_id, o.nom AS bloc, MAX(v.horodatage) AS dernier_voyage "
                        "FROM voyages v LEFT JOIN oreblocs o ON o.id = v.orebloc_id "
                        "WHERE v.orebloc_id IS NOT NULL "
                        "GROUP BY v.orebloc_id")]
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
                    if not exca or not bloc_id:
                        return self._send_json(400, {"error": "exca_code et orebloc_id requis"})
                    exca = exca.upper()
                    existe = conn.execute("SELECT id FROM oreblocs WHERE id=?", (bloc_id,)).fetchone()
                    if not existe:
                        return self._send_json(404, {"error": "bloc inconnu"})
                    # un exca ne mine qu'un bloc a la fois : on remplace sa ligne
                    conn.execute("DELETE FROM minage_actuel WHERE exca_code=?", (exca,))
                    if body.get("stop"):
                        conn.commit()
                        return self._send_json(200, {"stopped": exca})
                    conn.execute("INSERT INTO minage_actuel (exca_code, orebloc_id) VALUES (?, ?)",
                                 (exca, bloc_id))
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
                        "SELECT v.*, o.nom AS orebloc_nom, d.nom AS da_nom "
                        "FROM voyages v "
                        "LEFT JOIN oreblocs o ON o.id = v.orebloc_id "
                        "LEFT JOIN dumping_areas d ON d.id = v.dumping_area_id "
                        "ORDER BY v.id DESC LIMIT 500")]
                    return self._send_json(200, {"voyages": rows})

                if path == "/api/affecter" and method == "POST":
                    body = json.loads(self._read_body() or b"{}")
                    dt_code = body.get("dt_code")
                    if not dt_code:
                        return self._send_json(400, {"error": "dt_code requis"})
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

                if path == "/api/kpi" and method == "GET":
                    jour = (self._qs().get("jour") or [None])[0]
                    if jour and not re.match(r"^\d{4}-\d{2}-\d{2}$", jour):
                        return self._send_json(400, {"error": "jour attendu au format AAAA-MM-JJ"})
                    return self._send_json(200, kpi_voyages(conn, jour))

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

                if path == "/api/alertes" and method == "GET":
                    rows = [dict(r) for r in conn.execute(
                        "SELECT * FROM alertes ORDER BY id DESC LIMIT 200")]
                    return self._send_json(200, {"alertes": rows})

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
                    return self._send_json(200, {"office_sync": rows})

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
                      ("acquittee_le", "TEXT DEFAULT ''")):
        if nom not in cols:
            conn.execute("ALTER TABLE alertes ADD COLUMN %s %s" % (nom, decl))
    conn.commit()
    # voyages : conformite dumping (conforme / compromis / non_conforme)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(voyages)")]
    if "conformite" not in cols:
        conn.execute("ALTER TABLE voyages ADD COLUMN conformite TEXT DEFAULT ''")
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

    cols = [r[1] for r in conn.execute("PRAGMA table_info(teneurs)")]
    if "codes" not in cols:
        conn.execute("ALTER TABLE teneurs ADD COLUMN codes TEXT DEFAULT ''")
        conn.commit()

    cols = [r[1] for r in conn.execute("PRAGMA table_info(blasts)")]
    for colonne, definition in (("ot_sync", "INTEGER DEFAULT 0"),
                                ("ot_erreur", "TEXT DEFAULT ''"),
                                ("ot_maj_le", "TEXT DEFAULT ''")):
        if colonne not in cols:
            conn.execute("ALTER TABLE blasts ADD COLUMN %s %s" % (colonne, definition))
            conn.commit()

    # office_sync : rejeu des echecs + UUID des evenements envoyes a OT
    cols = [r[1] for r in conn.execute("PRAGMA table_info(office_sync)")]
    for nom, decl in (("tentatives", "INTEGER DEFAULT 0"),
                      ("derniere_tentative", "TEXT DEFAULT ''"),
                      ("event_id", "TEXT DEFAULT ''")):
        if nom not in cols:
            conn.execute("ALTER TABLE office_sync ADD COLUMN %s %s" % (nom, decl))
    conn.commit()
    # engins : etat de sortie de route (detection offroad)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(engins)")]
    for nom, decl in (("offroad", "INTEGER DEFAULT 0"),
                      ("offroad_depuis", "TEXT DEFAULT ''"),
                      ("offroad_candidat_depuis", "TEXT DEFAULT ''"),
                      ("offroad_distance", "REAL DEFAULT 0")):
        if nom not in cols:
            conn.execute("ALTER TABLE engins ADD COLUMN %s %s" % (nom, decl))
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
    if conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0:
        cur = conn.execute("INSERT INTO sources (nom) VALUES ('MAIN PIT')")
        # teneurs de demo, comme sur le schema de la page
        for nom, val_min, val_max in (("F5M", 5.0, 7.0), ("F2M", 2.0, 5.0), ("F4M", 4.0, 6.0)):
            conn.execute("INSERT INTO teneurs (source_id, nom, valeur_min, valeur_max) VALUES (?, ?, ?, ?)",
                         (cur.lastrowid, nom, val_min, val_max))
        conn.commit()
    conn.close()

    # donnees de demo si base vide
    conn = get_conn()
    with LOCK:
        if conn.execute("SELECT COUNT(*) FROM oreblocs").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO oreblocs (nom, grade_cutoff, sommets, source_fichier) VALUES (?, ?, ?, ?)",
                ("Orebloc A", DEFAULT_CUTOFF_GRADE,
                 json.dumps([[0, 0], [100, 0], [100, 100], [0, 100]]), "demo.csv"))
        if conn.execute("SELECT COUNT(*) FROM dumping_areas").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO dumping_areas (nom, x, y, rayon_m) VALUES (?, ?, ?, ?)",
                ("Dumping Area Principale", 500, 500, 50))
        if conn.execute("SELECT COUNT(*) FROM engins").fetchone()[0] == 0:
            conn.execute("INSERT INTO engins (code, type, x, y, statut) VALUES (?, ?, ?, ?, ?)",
                         ("EXCA-01", "EXCA", 50, 50, "OMEX"))
            conn.execute("INSERT INTO engins (code, type, x, y, statut) VALUES (?, ?, ?, ?, ?)",
                         ("DT-01", "DT", 55, 55, "WDT"))
        # blast de demo pour la page Dig Plan (donnees simulees, pas un vrai
        # export OREPro 3D) : un plan avec 3 oreblocs representatifs.
        if conn.execute("SELECT COUNT(*) FROM blasts").fetchone()[0] == 0:
            curb = conn.execute(
                "INSERT INTO blasts (numero, nom, name_dxf, name_geosum, name_survey, name_map, "
                "source_nom, destination, bench, heading, flitch, plan_date, scale, "
                "page_label, source_fichier, nb_blocs) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("LWP2_235_230_107", "LWP2_235_230_107", "demo_digplan.dxf", "Geosum_LWP2_235",
                 "Survey_2026_10_11", "Map_LWP2_235_230_107", "MAIN PIT", "F5W", "230", "W", "232.5",
                 "2026-10-11", "1:410.74", "Page 2 of 3", "demo_digplan.csv", 3))
            blast_id = curb.lastrowid
            demo_blocs = [
                ("232.5_LWP2_107_WST_FR_005_OP", 70, 69, 0.049, 0.988, "W", "IMAF",
                 [[10, 10], [40, 10], [40, 35], [20, 40], [10, 25]]),
                ("232.5_LWP2_107_HG_FR_006_OP", 1771, 3593, 2.568, 2.029, "F5W", "IMAF",
                 [[45, 60], [70, 90], [85, 95], [95, 80], [90, 55], [75, 40], [55, 45]]),
                ("232.5_LWP2_107_WST_FR_007_OP", 5859, 12288, 0.004, 2.097, "W", "IMAF",
                 [[5, 50], [35, 50], [55, 45], [75, 40], [95, 55], [95, 10], [50, 5], [5, 15]]),
            ]
            for nom, volume, tonnes, au, dens, dest, litho, sommets in demo_blocs:
                conn.execute(
                    "INSERT INTO oreblocs (nom, grade_cutoff, sommets, source_fichier, blast_id, "
                    "volume, tonnes, au_teneur, density, destination, litho) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (nom, DEFAULT_CUTOFF_GRADE, json.dumps(sommets), "demo_digplan.csv", blast_id,
                     volume, tonnes, au, dens, dest, litho))
        conn.commit()
    conn.close()

    threading.Thread(target=ecouteur_xbee, daemon=True).start()
    # envoi vers Serveur_OT hors du verrou global (16/11)
    threading.Thread(target=boucle_office, daemon=True).start()

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
    main()
