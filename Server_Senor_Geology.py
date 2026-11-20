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
import sqlite3
import subprocess
import sys
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
                  "SG_INTERVALLE_S", "MISSIONS_SOURCE")
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


def appliquer_position_engin(conn, code, typ, x, y, horodatage=None, relayer=True):
    """Met a jour (ou cree) un engin a la position (x, y) et gere le chrono
    d'immobilisation (seuil SEUIL_IMMOBILISATION_M). Utilise par la route
    HTTP /api/engins, l'ecouteur radio XBee et la lecture des remontees.

    `horodatage` : heure de l'observation (remontees) ; par defaut maintenant.
    `relayer=False` : ne pas renvoyer la position a Serveur_SG (elle en vient)."""
    instant = horodatage or now_iso()
    existe = conn.execute("SELECT * FROM engins WHERE code=?", (code,)).fetchone()
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
            "UPDATE engins SET x=?, y=?, immobilise_depuis=?, derniere_maj=? WHERE code=?",
            (x, y, immobilise_depuis, instant, code))
    else:
        conn.execute(
            "INSERT INTO engins (code, type, x, y, derniere_maj) VALUES (?, ?, ?, ?, ?)",
            (code, typ, x, y, instant))
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


def verifier_arrivee_da(conn, dt_code):
    """Geofencing dumping area : des que le DT entre dans le polygone de la
    dumping area de sa mission en cours, la mission est marquee "arrivee"
    (une seule fois) et une alerte "vous etes arrive" part au camion."""
    mission = mission_a_surveiller(conn, dt_code)
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


def charger_checkpoints_missions():
    """Une passe : les missions en cours sans checkpoints les recoivent."""
    conn = get_conn()
    try:
        sans = [dict(r) for r in conn.execute(
            "SELECT id, dt_code FROM missions WHERE statut='en_cours' "
            "AND (cp_json IS NULL OR cp_json IN ('', '[]'))")]
        if not sans:
            return 0
        par_dt = sg_lire_itineraires()
        n = 0
        for m in sans:
            cps = par_dt.get(m["dt_code"])
            if cps:
                conn.execute("UPDATE missions SET cp_json=?, cp_etat='{}' WHERE id=?",
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


def verifier_checkpoints(conn, dt_code, instant=None):
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
        return [dict(r) for r in conn.execute(
            "SELECT * FROM alertes WHERE datetime(created_at, 'localtime') >= ? "
            "AND (? = '' OR datetime(created_at, 'localtime') < ?) ORDER BY id DESC LIMIT 200",
            (depuis, jusqua, jusqua))]
    return [dict(r) for r in conn.execute("SELECT * FROM alertes ORDER BY id DESC LIMIT 200")]


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


def verifier_conformite_dumping(conn, mission, position=None, strict=False):
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
    vue chez SG : le DT a pu abandonner sa mission en route)."""
    attendue, teneur = 0, None
    if mission["orebloc_id"]:
        ob = conn.execute("SELECT * FROM oreblocs WHERE id=?", (mission["orebloc_id"],)).fetchone()
        if ob:
            teneur = float(ob["grade_cutoff"]) if ob["grade_cutoff"] is not None else DEFAULT_CUTOFF_GRADE
            instr = conn.execute(
                "SELECT dumping_area_id FROM teneurs WHERE valeur_min <= ? AND ? <= valeur_max "
                "AND dumping_area_id > 0 LIMIT 1", (teneur, teneur)).fetchone()
            if instr:
                attendue = instr["dumping_area_id"]
    regle = "teneur %.1f" % teneur if attendue else "consigne de Serveur_SG"
    if not attendue:
        if position is None:
            return ""
        attendue = mission["dumping_area_id"]
    reelle = mission["dumping_area_id"]
    if position is not None:
        trouvee = 0
        for da in conn.execute("SELECT id, sommets FROM dumping_areas WHERE actif=1"):
            try:
                if point_in_polygon(position[0], position[1], json.loads(da["sommets"] or "[]")):
                    trouvee = da["id"]
                    break
            except (TypeError, ValueError):
                continue
        if trouvee:
            reelle = trouvee
        elif strict:
            return ""
    if not attendue or not reelle:
        return ""
    if attendue == reelle:
        return "conforme"
    def nom(i):
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
    for l in [dict(r) for r in conn.execute(
            "SELECT id, dt_code, id_sg FROM missions WHERE statut='en_cours'")]:
        if l["id_sg"]:
            if l["id_sg"] in ids_sg:
                continue
            # le DT a decharge : la dumping area ATTEINTE (position) est-elle celle que
            # la teneur impose ? (avant : ce controle n'existait que dans /api/mission_fin,
            # que personne n'appelle)
            mission = conn.execute("SELECT * FROM missions WHERE id=?", (l["id"],)).fetchone()
            dt = conn.execute("SELECT x, y FROM engins WHERE code=?", (l["dt_code"],)).fetchone()
            try:
                verifier_conformite_dumping(conn, mission, (dt["x"], dt["y"]) if dt else None, strict=True)
            except Exception as e:
                print("MISSIONS: controle de conformite impossible (%s)" % e)
            conn.execute("UPDATE missions SET statut='termine', fin=? WHERE id=?", (maintenant, l["id"]))
            res["fermees"] += 1
        else:
            if local and l["dt_code"] not in par_dt:
                continue                           # decision d'ici, SG ne la connait pas encore
            conn.execute("UPDATE missions SET statut='abandonnee', fin=? WHERE id=?", (maintenant, l["id"]))
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
                rattraper_blasts_ot(conn)
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
        ok, err = ot_clore_blast(conn, c["id"])
        print("OT: rattrapage cloture %s -> %s" % (c["numero"], "ok" if ok else err))


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


def generer_video(depuis, jusqua, sortie, vitesse=120, fps=10, largeur=1280, hauteur=720):
    """MP4 du tracking entre `depuis` et `jusqua` : plan du site, engins en mouvement, traces, heure.
    `vitesse` : secondes reelles par seconde de video (120 = 2 h de terrain en 1 min). Retourne (ok, message)."""
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
        blocs, das, pistes = _fond_site(conn)
    finally:
        conn.close()
    # cadrage : les positions, avec une marge ; le fond hors cadre est ignore
    xs = [v for s_ in series.values() for v in s_["x"]]; ys = [v for s_ in series.values() for v in s_["y"]]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    marge = max(60.0, 0.12 * max(x1 - x0, y1 - y0, 100.0))
    x0 -= marge; x1 += marge; y0 -= marge; y1 += marge
    # cadre minimal (engins immobiles : on garde le contexte du site : pistes, zones voisines)
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
    # fond (une fois)
    fond = np.full((hauteur, largeur, 3), (38, 40, 36), np.uint8)
    # tout le fond est dessine (OpenCV rogne ce qui sort du cadre) : pistes et zones traversant le cadre restent visibles
    for sommets in blocs:
        pts = [px(p[0], p[1]) for p in sommets if len(p) >= 2]
        if len(pts) >= 3:
            cv2.fillPoly(fond, [np.array(pts, np.int32)], (62, 66, 60))
    for x1_, y1_, x2_, y2_ in pistes:
        cv2.line(fond, px(x1_, y1_), px(x2_, y2_), (110, 116, 120), 1, cv2.LINE_AA)
    for nom, sommets in das:
        pts = [px(p[0], p[1]) for p in sommets if len(p) >= 2]
        if len(pts) >= 3:
            cv2.fillPoly(fond, [np.array(pts, np.int32)], (60, 90, 60))
            inside = [q for q in pts if 0 <= q[0] < largeur and bandeau <= q[1] < hauteur]
            if inside:
                cv2.putText(fond, str(nom)[:22], inside[0], cv2.FONT_HERSHEY_SIMPLEX, 0.4, (170, 210, 170), 1, cv2.LINE_AA)
    os.makedirs(os.path.dirname(sortie), exist_ok=True)
    part = sortie + ".part.mp4"
    ecr = cv2.VideoWriter(part, cv2.VideoWriter_fourcc(*"mp4v"), fps, (largeur, hauteur))
    if not ecr.isOpened():
        return False, "encodeur MP4 indisponible (OpenCV)"
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
                couleur = couleurs.get(s_["type"], (200, 200, 200)) if silence <= 120 else (110, 110, 110)
                m = (ts >= t - TRACE_S) & (ts <= t)
                if m.sum() >= 2:
                    trace = np.array([px(a, b) for a, b in zip(xs_[m], ys_[m])], np.int32)
                    cv2.polylines(img, [trace], False, tuple(int(c * 0.6) for c in couleur), 1, cv2.LINE_AA)
                if dans(cx, cy):
                    c = px(cx, cy)
                    cv2.circle(img, c, 6 if s_["type"] == "DT" else 8, couleur, -1, cv2.LINE_AA)
                    cv2.putText(img, code, (c[0] + 9, c[1] - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (235, 235, 230), 1, cv2.LINE_AA)
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
    return True, "%d image(s), %d engin(s), %.1f Mo" % (n_images, len(series), os.path.getsize(sortie) / 1048576.0)


def nom_video(depuis, jusqua):
    return "tracking-%s_%s-%s.mp4" % (depuis[:10], depuis[11:16].replace(":", "h"), jusqua[:10] + "_" + jusqua[11:16].replace(":", "h"))


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


def enregistrements_liste():
    """CSV du tracking et videos, avec taille et etat des generations en cours."""
    res = {"csv": [], "videos": []}
    for dossier, cle, ext in ((TRACKING_DIR, "csv", ".csv"), (ENREGISTREMENTS_DIR, "videos", ".mp4")):
        try:
            for f in sorted(os.listdir(dossier), reverse=True):
                if f.endswith(ext) and not f.endswith(".part.mp4"):
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
            if str(param_lire("VIDEO_AUTO", "1")).strip().lower() not in ("0", "non", "false", "off"):
                _, d, f = dernier_shift_termine()
                debut, fin = d.strftime("%Y-%m-%d %H:%M:%S"), f.strftime("%Y-%m-%d %H:%M:%S")
                if not os.path.exists(os.path.join(ENREGISTREMENTS_DIR, nom_video(debut, fin))):
                    ok, nom, msg = lancer_video(debut, fin, 120)
                    if ok and msg == "generation lancee":
                        print("VIDEO: %s -> %s" % (nom, msg))
            limite = datetime.now() - timedelta(days=_entier_param("VIDEOS_JOURS", 30))
            if os.path.isdir(ENREGISTREMENTS_DIR):
                for f in os.listdir(ENREGISTREMENTS_DIR):
                    chemin = os.path.join(ENREGISTREMENTS_DIR, f)
                    if f.endswith(".mp4") and datetime.fromtimestamp(os.path.getmtime(chemin)) < limite:
                        os.remove(chemin)
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
                                         horodatage=instant, relayer=False)
            except (TypeError, ValueError):
                pass
        appliquer_position_engin(conn, dt, "DT", x, y, horodatage=instant, relayer=False)
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
    return ok, err


def boucle_chargements():
    _chargements_etat["actif"] = True
    while True:
        try:
            chargements_un_passage()
        except Exception as e:
            _chargements_etat["derniere_erreur"] = "%s: %s" % (type(e).__name__, e)
            print("CHARGEMENTS: erreur %s" % e)
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
    if code in KPI_GRADES:
        return KPI_GRADES[code]
    if code == "WST" or str(destination or "").strip().upper() == "W":
        return "waste"
    return ""


SUIVI_ARRET_MIN = 2     # DT immobile (< SEUIL_IMMOBILISATION_M) depuis 2 min : « A l'arret » et non plus « Roulage »


def etat_suivi_dt(statut, offroad, age_s, mission, immo_min=0):
    """Etat lisible, par priorite : signal_perdu > hors_piste > chargement >
    mission > attente_loin > arret > roulage. « Roulage » n'est plus le fourre-tout : un DT
    immobile depuis SUIVI_ARRET_MIN minutes (sans chargement ni mission) est « arret »."""
    if age_s is None or age_s > SUIVI_SIGNAL_PERDU_S:
        return "signal_perdu"
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
                 "offroad": bool(e["offroad"]), "offroad_distance": round(e["offroad_distance"] or 0)}
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
            fiche["vitesse_kmh"] = round(min(vitesse, 120), 1)
            fiche["cap_deg"] = None if cap is None else round(cap)
            immo = _secondes_depuis(e["immobilise_depuis"]) / 60.0 if e["immobilise_depuis"] else 0
            fiche["immobile_min"] = round(immo, 1)
            fiche["etat"] = etat_suivi_dt(e["statut"], fiche["offroad"], age, m, immo)
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


def kpi_chargements(conn, lignes, jour):
    """Memes KPI que kpi_voyages, calcules sur les lignes « Chargements » de SG.
    Teneur Au (onces) et nature minerai/sterile : table locale oreblocs, par nom
    (le referentiel est pousse depuis Dig Plan). Un orebloc inconnu localement
    est classe par son code de teneur et sa destination, sans onces."""
    blocs = {r["nom"]: r for r in conn.execute(
        "SELECT nom, au_teneur, minerai, destination FROM oreblocs")}
    res = {"jour": jour, "voyages": len(lignes), "minerais_t": 0.0, "waste_t": 0.0, "onces": 0.0,
           "grades": {g: {"tonnes": 0.0, "onces": 0.0} for g in ("high", "medium", "low", "marginal")},
           "sans_bloc": 0, "sans_tonnage": 0, "blocs_inconnus": 0}
    for r in lignes:
        nom = str(r.get("orebloc") or "").strip()
        if not nom:
            res["sans_bloc"] += 1          # chargement hors de tout orebloc : non classe
            continue
        try:
            t = float(r.get("tonnes"))
        except (TypeError, ValueError):
            res["sans_tonnage"] += 1       # pas de truck_factor_t : compte, sans tonnage
            continue
        b = blocs.get(nom)
        if b is None:
            res["blocs_inconnus"] += 1
        code = code_teneur_bloc(nom)
        dest = str(r.get("teneur") or (b["destination"] if b is not None else "") or "").strip().upper()
        minerai = bool(b["minerai"]) if b is not None else (code != "WST" and dest != "W")
        sterile = code == "WST" or dest == "W" or not minerai
        if sterile and code not in KPI_GRADES:
            res["waste_t"] += t
            continue
        onces = t * float((b["au_teneur"] if b is not None else 0) or 0) / GRAMMES_PAR_ONCE
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
            "abonne": _mqtt_etat["abonne"], "messages": _mqtt_etat["messages"], "comptes": lignes}


def boucle_comptes_mqtt():
    _mqtt_etat["comptes_actif"] = True
    while True:
        try:
            if mqtt_conf():
                rapprocher_comptes_mqtt()
        except Exception as e:
            _mqtt_etat["erreur"] = "%s: %s" % (type(e).__name__, e)
            print("MQTT comptes : erreur %s" % e)
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
    if nature != "position":
        return
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
                                             horodatage=instant, relayer=False)
                except (TypeError, ValueError):
                    pass
            appliquer_position_engin(conn, code, "DT", x, y, horodatage=instant, relayer=False)
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
                    cl.subscribe([("camions/+/position", 0), ("camions/+/connexion", 1)])

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
            elif isinstance(payload.get("chauffeur"), dict):
                c = payload["chauffeur"]
                ok = office_appel("notification",
                                  {"profil": c.get("profil", ""), "message": c.get("message", "")})
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


# ------------------------------------------ alerte vers la tablette du DT ---
# Contrat convenu avec la Zone 2 le 17/11 (REPONSE_ZONE1_A_ZONE2_2026-11-17) :
# en plus de l'evenement type (KPI, historique), chaque changement d'etat
# offroad / deviation / retour_* est depose dans l'onglet `notifications` de
# Serveur_OT, `profil` = code du DT, `message` = "[type] TEXTE ASCII". L'app
# DT lit ?action=notifications, garde les lignes de son code et les dit a
# voix haute. Pas d'accuse de lecture. Desactivable : ALERTES_CHAUFFEUR=0.
TYPES_ALERTE_CHAUFFEUR = ("offroad", "deviation", "retour_piste", "retour_trajet", "checkpoint")


def alertes_chauffeur_actives():
    return param_lire("ALERTES_CHAUFFEUR", "1").strip().lower() not in ("0", "non", "false", "off")


def texte_ascii(texte):
    """Sans accents ni caracteres speciaux : lu tel quel par la synthese
    vocale de la tablette, quelle que soit sa langue."""
    t = unicodedata.normalize("NFKD", str(texte or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", " ", t).strip()


def message_chauffeur(evenement):
    """[type] message, ou None si ce n'est pas une alerte pour le chauffeur."""
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
ECRITURES_ADMIN = ("/api/relais", "/api/offroad", "/api/mqtt/", "/api/sg/", "/api/cles_dt", "/api/calibration_",
                   "/api/ot_reconcilier", "/api/pistes_pousser", "/api/travels_roads_reprojeter",
                   "/api/oreblocs_nettoyer", "/api/orebloc_maj", "/api/affecter", "/api/verifier_deviation",
                   "/api/missions/", "/api/engins", "/api/mission_fin")
ECRITURES_SENOR = ("/api/oreblocs", "/api/dumping_area", "/api/teneur", "/api/sources", "/api/checkpoints",
                   "/api/travels_roads", "/api/geographic_system", "/api/minage_declare")
ECRITURES_TOUS = ("/api/alertes", "/api/blasts", "/api/ot_blasts", "/api/enregistrements")
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

    def _send_fichier(self, code, corps, type_mime, nom):
        """Reponse binaire (CSV, video) a telecharger."""
        try:
            self.send_response(code)
            self.send_header("Content-Type", type_mime)
            self.send_header("Content-Disposition", 'attachment; filename="%s"' % nom)
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
        if path == "/api/enregistrements" and method == "GET":
            return self._send_json(200, enregistrements_liste())
        if path == "/api/enregistrements/fichier" and method == "GET":
            nom = (self._qs().get("nom") or [""])[0]
            if not re.match(r"^[A-Za-z0-9_.-]+\.(csv|mp4)$", nom) or ".." in nom:
                return self._send_json(400, {"error": "nom de fichier invalide"})
            chemin = os.path.join(TRACKING_DIR if nom.endswith(".csv") else ENREGISTREMENTS_DIR, nom)
            if not os.path.isfile(chemin):
                return self._send_json(404, {"error": "fichier introuvable"})
            with open(chemin, "rb") as fh:
                return self._send_fichier(200, fh.read(), "text/csv; charset=utf-8" if nom.endswith(".csv") else "video/mp4", nom)
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
            ok, nom, message = lancer_video(depuis, jusqua, vitesse)
            return self._send_json(200 if ok else 422, {"ok": ok, "fichier": nom, "message": message})
        # 20/11 : /api/offroad et /api/alertes sont sondees chaque seconde par CHAQUE dashboard ouvert ;
        # sous le verrou global elles se disputaient la place des positions (donc des alertes).
        # Lecture seule : hors verrou, comme /api/suivi_dt et /api/kpi.
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

                # ------------------------------------------- checkpoints --
                # Ligne de passage (deux extremites), importee par CSV
                # "Y, X, id, Nom" (deux lignes par checkpoint). Le nom est
                # l'identifiant : un nom deja present est MIS A JOUR (on
                # recharge le fichier corrige sans tout supprimer).
                if path == "/api/checkpoints" and method == "GET":
                    rows = [dict(r) for r in conn.execute(
                        "SELECT id, nom, x1, y1, x2, y2, created_at FROM checkpoints "
                        "WHERE actif=1 ORDER BY id")]
                    for r in rows:
                        r["longueur_m"] = round(distance(r["x1"], r["y1"], r["x2"], r["y2"]), 1)
                    return self._send_json(200, {"checkpoints": rows})

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
                        "missions_source": missions_source(),
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
                              "missions_source": "MISSIONS_SOURCE",
                              "remontees_intervalle_s": "REMONTEES_INTERVALLE_S",
                              "chargements_intervalle_s": "CHARGEMENTS_INTERVALLE_S"}
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
                    conformite = verifier_conformite_dumping(conn, mission)

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
    # missions : lien avec la mission de Serveur_SG (alignement 20/11)
    for nom, decl in (("id_sg", "INTEGER"), ("cle_sg", "TEXT DEFAULT ''")):
        if nom not in cols:
            conn.execute("ALTER TABLE missions ADD COLUMN %s %s" % (nom, decl))
    # missions : checkpoints de l'itineraire (lus chez Serveur_SG) et leur suivi
    for nom, decl in (("cp_json", "TEXT DEFAULT '[]'"),
                      ("cp_etat", "TEXT DEFAULT '{}'")):
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
    threading.Thread(target=boucle_video_auto, daemon=True).start()
    # MQTT (19/11) : comptes des DT d'apres la flotte + positions en direct
    threading.Thread(target=boucle_comptes_mqtt, daemon=True).start()
    threading.Thread(target=boucle_suivi_mqtt, daemon=True).start()

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
