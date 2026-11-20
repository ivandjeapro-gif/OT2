"""
OreTracking V2 - Serveur applicatif de gestion du minage.

Usage:
    python server.py [port]

Sert les fichiers statiques du dossier (index.html, dashboard.html, ...) et
expose une API REST complete de gestion des donnees de minage :

    GET    /api/health                -> etat du serveur et de la base
    GET    /api/tables                -> liste des tables disponibles (auth)
    GET    /api/rows?table=X          -> lire les lignes d'une table (auth)
    POST   /api/rows                  -> {table, row:{...}}       inserer (auth)
    PUT    /api/rows                  -> {table, id, row:{...}}   modifier (auth)
    DELETE /api/rows?table=X&id=N     -> supprimer une ligne (auth)
    GET    /api/config                -> configuration (params, seuils) (public)
    POST   /api/config                -> enregistrer la configuration (auth)
    GET    /api/alerts                -> alertes recentes (public)
    POST   /api/alerts                -> {text, type, cat}         creer (auth)
    DELETE /api/alerts?id=N           -> supprimer une alerte (auth)
    GET    /api/stats                 -> KPI aggregees pour le dashboard (public)
    GET    /api/sync                  -> horodatage de la derniere synchro (auth)

Authentification : Authorization: Bearer <ID token Firebase> (verifie via
Google), ou session historique SQLite (comptes). Tous les endpoints non
publics repondent 401 sans jeton valide.

Securite :
    - HTTPS (certificat auto-signe genere au 1er lancement, port 8443)
    - rate-limiting sur POST /api/auth/login : 5 echecs par identifiant
      = blocage 5 min, 30 echecs par IP = blocage 15 min (429)

La base est un fichier SQLite unique : data/oretracking.db (cree au premier
lancement, pre-remplie avec les donnees de demarrage).
"""
import json
import math
import os
import sqlite3
import sys
import time
import hashlib
import hmac
import secrets
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote, quote, urlencode

# En executable PyInstaller, __file__ pointe dans le dossier TEMPORAIRE
# d'extraction : html/, js/, css/ et la base seraient introuvables. On vise
# le dossier de l'executable, ou le projet est reellement deploye.
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "data", "oretracking.db")


def _port_demande(defaut):
    """Port depuis la ligne de commande, en ignorant ce qui n'est pas un
    entier : sous le lanceur, sys.argv appartient au lanceur."""
    if len(sys.argv) > 1:
        try:
            return int(sys.argv[1])
        except (TypeError, ValueError):
            pass
    return defaut


PORT = _port_demande(8100)
CONFIG_FILE = os.path.join(BASE_DIR, "data", "server_config.json")

# ---------------------------------------------------------------- schema ----

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (cle TEXT PRIMARY KEY, valeur TEXT);

CREATE TABLE IF NOT EXISTS zones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    type TEXT DEFAULT 'zone',
    lat REAL DEFAULT 0,
    lng REAL DEFAULT 0,
    color TEXT DEFAULT 'blue',
    data TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    from_zone TEXT DEFAULT '',
    to_zone TEXT DEFAULT '',
    color TEXT DEFAULT '#f97316',
    geojson TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS engins (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,
    nom TEXT DEFAULT '',
    reservoir_l REAL DEFAULT 300,
    fuel_pct REAL DEFAULT 100,
    conso_lh REAL DEFAULT 25,
    statut TEXT DEFAULT 'actif',
    lat REAL DEFAULT 0,
    lng REAL DEFAULT 0,
    famille TEXT DEFAULT '',
    actif INTEGER DEFAULT 1,
    source TEXT DEFAULT 'local',
    remote_hash TEXT DEFAULT ''
);


CREATE TABLE IF NOT EXISTS voyages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    engin TEXT NOT NULL,
    trail TEXT DEFAULT '',
    from_zone TEXT DEFAULT '',
    to_zone TEXT DEFAULT '',
    tonnes REAL DEFAULT 0,
    grade REAL DEFAULT 0,
    materiel TEXT DEFAULT 'ore',
    horodatage TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS alertes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    type TEXT DEFAULT 'warn',
    cat TEXT DEFAULT 'chemin',
    engin TEXT DEFAULT '',
    lu INTEGER DEFAULT 0,
    horodatage TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS equipements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    equip_id TEXT NOT NULL,
    config TEXT DEFAULT '{}',
    actif INTEGER DEFAULT 1,
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(type, equip_id)
);

CREATE TABLE IF NOT EXISTS params (
    cle TEXT PRIMARY KEY,
    valeur TEXT
);

CREATE TABLE IF NOT EXISTS comptes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT UNIQUE NOT NULL,
    email TEXT DEFAULT '',
    role TEXT DEFAULT 'viewer',
    salt TEXT,
    hash TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY,
    compte_id INTEGER NOT NULL,
    expires REAL NOT NULL,
    cree_le TEXT DEFAULT (datetime('now'))
);

-- Miroirs des onglets Google Sheets synchronises avec l'App Script.
-- Chaque table porte le contenu en ligne + le hash de la version distante
-- (permet de dire "identique" ou "different" sans re-ecrire le Sheet).
-- Les colonnes `id` permettent l'usage du CRUD generique /api/rows.
CREATE TABLE IF NOT EXISTS sync_profils (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    libelle TEXT DEFAULT '',
    titre TEXT DEFAULT '',
    role_cdc TEXT DEFAULT '',
    icone TEXT DEFAULT '',
    ordre INTEGER DEFAULT 0,
    actif INTEGER DEFAULT 1,
    remote_hash TEXT DEFAULT '',
    maj_le TEXT DEFAULT '',
    UNIQUE (code)
);

CREATE TABLE IF NOT EXISTS sync_identifiants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profil TEXT NOT NULL,
    identifiant TEXT NOT NULL,
    nom TEXT DEFAULT '',
    role TEXT DEFAULT '',
    equipe TEXT DEFAULT '',
    actif INTEGER DEFAULT 1,
    remote_hash TEXT DEFAULT '',
    maj_le TEXT DEFAULT '',
    UNIQUE (profil, identifiant)
);

CREATE TABLE IF NOT EXISTS sync_flotte (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    famille TEXT DEFAULT '',
    numero INTEGER DEFAULT 0,
    actif INTEGER DEFAULT 1,
    remote_hash TEXT DEFAULT '',
    maj_le TEXT DEFAULT '',
    UNIQUE (code)
);

CREATE TABLE IF NOT EXISTS sync_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    easting REAL DEFAULT 0,
    northing REAL DEFAULT 0,
    remote_hash TEXT DEFAULT '',
    maj_le TEXT DEFAULT '',
    UNIQUE (code)
);

CREATE TABLE IF NOT EXISTS sync_affectation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cle TEXT NOT NULL,
    valeur TEXT DEFAULT '',
    remote_hash TEXT DEFAULT '',
    maj_le TEXT DEFAULT '',
    UNIQUE (cle)
);

CREATE TABLE IF NOT EXISTS sync_blasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    numero TEXT NOT NULL,
    carte TEXT DEFAULT '',
    libelle_carte TEXT DEFAULT '',
    blocs TEXT DEFAULT '',
    plan_url TEXT DEFAULT '',
    remote_hash TEXT DEFAULT '',
    maj_le TEXT DEFAULT '',
    UNIQUE (numero)
);

CREATE TABLE IF NOT EXISTS sync_notifications (
    id INTEGER PRIMARY KEY,
    profil TEXT DEFAULT '',
    message TEXT DEFAULT '',
    horodatage TEXT DEFAULT '',
    remote_hash TEXT DEFAULT '',
    maj_le TEXT DEFAULT ''
);

-- Appareils Android detects par cable (declaration cote tablette).
CREATE TABLE IF NOT EXISTS android_devices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT DEFAULT '',
    modele TEXT DEFAULT '',
    android_id TEXT NOT NULL,
    serial TEXT DEFAULT '',
    actif INTEGER DEFAULT 1,
    dernier_seen TEXT DEFAULT '',
    UNIQUE (android_id)
);

-- Tablettes enregistrees (declaration cote Flutter/app : nom, modele, id appareil).
CREATE TABLE IF NOT EXISTS tablettes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nom TEXT DEFAULT '',
    modele TEXT DEFAULT '',
    device_id TEXT NOT NULL,
    serial TEXT DEFAULT '',
    type TEXT DEFAULT 'autre',
    actif INTEGER DEFAULT 1,
    dernier_seen TEXT DEFAULT '',
    UNIQUE (device_id)
);

-- Etat de la synchronisation, un enregistrement par dataset.
CREATE TABLE IF NOT EXISTS sync_state (
    dataset TEXT PRIMARY KEY,
    etat TEXT DEFAULT 'inconnu',
    local_count INTEGER DEFAULT 0,
    remote_count INTEGER DEFAULT 0,
    dernier_pull TEXT DEFAULT '',
    dernier_push TEXT DEFAULT '',
    message TEXT DEFAULT '',
    dirty INTEGER DEFAULT 0
);
"""


DEFAULT_PARAMS = {
    "api_url": "http://localhost:8100",
    "auth_mode": "password",
    "auth_timeout": "30",
    "serveur": "192.168.1.100",
    "port_db": "5432",
    "base": "oretracking_db",
    "user": "admin",
    "mode_simulation": "true",
    "rafraichissement": "5",
    "theme": "Sombre",
    "animations": "true",
    "seuil_grade": "10",
    "temp_max": "40",
    "temp_min": "10",
    "humidite_max": "80",
    "fuel_seuil": "15",
    "alerte_mauvais_chemin": "true",
    "alerte_offroad": "true",
}

DEFAULT_ENGINS = [
    ("DT-01", 300, 72, 25), ("DT-02", 300, 45, 25), ("DT-03", 300, 12, 25),
    ("DT-04", 300, 88, 25), ("DT-05", 300, 56, 25),
]

DEFAULT_ZONES = [
    ("Main Pit", "zone", 320460, 913696, "blue"),
    ("ROM Pad 1", "zone", 320610, 913720, "green"),
    ("ROM Pad 2", "zone", 320540, 913810, "orange"),
    ("Waste Dump", "zone", 320380, 913760, "red"),
]

DEFAULT_TRAILS = [
    ("Trail A", "Main Pit", "ROM Pad 1", "#f97316"),
    ("Trail B", "Main Pit", "ROM Pad 2", "#22c55e"),
    ("Trail C", "Main Pit", "Waste Dump", "#ef4444"),
]


def get_conn():
    """Connexion SQLite. timeout=15 s : le serveur est multithread, deux
    ecritures simultanees (sync + CRUD) ne doivent jamais casser en
    « database is locked » avant d'avoir attendu la fin de l'autre."""
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn



def hash_password(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return salt, h.hex()


# Sessions persistees en base (table sessions) : survivent au redemarrage
# et fonctionnent pour tous les clients connectes au meme serveur.
SESSION_TTL = 30 * 60  # 30 min, renouvelable via /api/auth/me


def session_create(compte):
    token = secrets.token_hex(32)
    expires = time.time() + SESSION_TTL
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO sessions (token, compte_id, expires) VALUES (?,?,?)",
            (token, compte["id"], expires))
        conn.commit()
    finally:
        conn.close()
    return token


def session_get(token):
    if not token:
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT s.token, s.expires, c.id, c.nom, c.email, c.role "
            "FROM sessions s JOIN comptes c ON c.id = s.compte_id "
            "WHERE s.token = ?", (token,)).fetchone()
        if not row:
            return None
        if row["expires"] < time.time():
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            conn.commit()
            return None
        # glissant : prolonger
        conn.execute("UPDATE sessions SET expires=? WHERE token=?",
                     (time.time() + SESSION_TTL, token))
        conn.commit()
        return {
            "compte_id": row["id"],
            "nom": row["nom"],
            "email": row["email"],
            "role": row["role"],
        }
    finally:
        conn.close()


def session_drop(token):
    if not token:
        return
    conn = get_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------- authentification API --------
# Verifie un ID token Firebase (emis par Firebase Auth) :
#   1. signature RS256 locale contre les cles publiques Google (JWKS, cache 1h)
#   2. issuer + expiration + uid
# Pas de dependance externe hors `cryptography` (present sur la machine).
import base64 as _base64
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

FIREBASE_ISSUER = "https://securetoken.google.com/ot-test-3c6df"
JWKS_URL = "https://www.googleapis.com/service_accounts/v1/jwk/securetoken@system.gserviceaccount.com"
X509_URL = "https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com"
FB_TOKEN_CACHE = {}   # token -> (payload, expire_at)
FB_CACHE_TTL = 300    # 5 min
_pubkeys_cache = {"keys": {}, "at": 0.0}


def _b64u_decode(s):
    return _base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _public_keys(refresh=False):
    """Cles publiques de securetoken@system.gserviceaccount.com.

    Charge les deux jeux possibles :
      - JWKS JSON (kid + modules n/e)
      - certs X509 PEM (kid = fingerprint du certificat, format utilise
        par les ID tokens Firebase)
    Retourne {kid: RSAPublicKey}.
    """
    import cryptography.x509 as _x509
    now = time.time()
    if not refresh and _pubkeys_cache["keys"] and now - _pubkeys_cache["at"] < 3600:
        return _pubkeys_cache["keys"]
    keys = {}
    try:
        req = urllib.request.Request(JWKS_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for k in data.get("keys", []):
            if k.get("kty") == "RSA" and k.get("kid"):
                n = int.from_bytes(_b64u_decode(k["n"]), "big")
                e = int.from_bytes(_b64u_decode(k["e"]), "big")
                keys[k["kid"]] = rsa.RSAPublicNumbers(e, n).public_key()
    except Exception:
        pass
    try:
        req = urllib.request.Request(X509_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            certs = json.loads(resp.read().decode("utf-8"))
        for kid, pem in certs.items():
            try:
                cert = _x509.load_pem_x509_certificate(pem.encode("utf-8"))
                keys.setdefault(kid, cert.public_key())
            except Exception:
                continue
    except Exception:
        pass
    if keys:
        _pubkeys_cache.update(keys=keys, at=now)
    return keys


def _signature_valid(jwt_str):
    parts = jwt_str.split(".")
    if len(parts) != 3:
        return False
    try:
        header = json.loads(_b64u_decode(parts[0]).decode("utf-8"))
    except Exception:
        return False
    if header.get("alg") != "RS256":
        return False
    kid = header.get("kid")
    pub = _public_keys().get(kid)
    if pub is None:
        # kid inconnu : une seule fois, recharge (rotation des cles)
        try:
            pub = _public_keys(refresh=True).get(kid)
        except Exception:
            pub = None
    if pub is None:
        return False
    try:
        pub.verify(
            _b64u_decode(parts[2]),
            (parts[0] + "." + parts[1]).encode("utf-8"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:
        return False


def fb_verify(token):
    if not token or len(token) > 4096:
        return None
    cached = FB_TOKEN_CACHE.get(token)
    now = time.time()
    if cached and cached[1] > now:
        return cached[0]
    if not _signature_valid(token):
        return None
    try:
        claims = json.loads(_b64u_decode(token.split(".")[1]).decode("utf-8"))
    except Exception:
        return None
    if claims.get("iss") != FIREBASE_ISSUER:
        return None
    if claims.get("aud") not in ("ot-test-3c6df", ["ot-test-3c6df"]):
        return None
    try:
        exp = int(claims.get("exp", 0))
    except (TypeError, ValueError):
        return None
    if exp <= now:
        return None
    payload = {
        "uid": claims.get("sub") or "",
        "email": claims.get("email") or "",
        "exp": exp,
    }
    if not payload["uid"]:
        return None
    FB_TOKEN_CACHE[token] = (payload, min(now + FB_CACHE_TTL, exp))
    return payload


def auth_context(authorizer):
    """Retourne le contexte authentifie ou None.

    Accepte (dans l'ordre) :
      - Bearer <ID token Firebase>  -> {"via": "firebase", "uid":..., "email":...}
      - session historique SQLite   -> {"via": "session", "role":...}
    """
    tok = authorizer._token()
    if tok:
        fb = fb_verify(tok)
        if fb:
            return {"via": "firebase", "uid": fb["uid"], "email": fb["email"]}
        sess = session_get(tok)
        if sess:
            return {"via": "session", **sess}
    return None


# --------------------------- profil de l'utilisateur (admin / senor / geologue) ---
# Le profil vit dans Firestore (users/{uid}.role, base « ot-test »), pas dans le jeton Firebase.
# Lecture par l'API REST de Firestore AVEC le jeton de l'utilisateur (comme le fait la page : il lit
# son propre document), mis en cache 60 s. Sert a la passerelle (:8101) pour autoriser ses ecritures.
FIRESTORE_DOC = "https://firestore.googleapis.com/v1/projects/ot-test-3c6df/databases/ot-test/documents/users/"
_ROLE_CACHE = {}          # uid -> (role, expire)


def normaliser_role(r):
    r = str(r or "").strip().lower()
    if r == "admin":
        return "admin"
    if r in ("senor", "senor_geology", "senorgeology"):
        return "senor"
    return "geologue"         # viewer / inconnu : le plus restreint des profils d'usage


def role_firestore(jeton, uid):
    """Profil de l'utilisateur ; None si illisible (reseau, regle Firestore) ou compte desactive."""
    c = _ROLE_CACHE.get(uid)
    if c and c[1] > time.time():
        return c[0]
    role = None
    try:
        req = urllib.request.Request(FIRESTORE_DOC + uid, headers={"Authorization": "Bearer " + jeton})
        with urllib.request.urlopen(req, timeout=6) as rep:
            champs = (json.loads(rep.read().decode("utf-8")) or {}).get("fields") or {}
        if not (champs.get("desactive") or {}).get("booleanValue"):
            role = normaliser_role((champs.get("role") or {}).get("stringValue"))
    except Exception as e:
        print("  ROLE: lecture Firestore impossible pour %s (%s)" % (uid, e))
        return None
    _ROLE_CACHE[uid] = (role, time.time() + 60)
    return role


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_conn()
    conn.executescript(SCHEMA)
    migrate_schema(conn)
    if conn.execute("SELECT COUNT(*) FROM params").fetchone()[0] == 0:
        for k, v in DEFAULT_PARAMS.items():

            conn.execute("INSERT INTO params VALUES (?, ?)", (k, v))
    if conn.execute("SELECT COUNT(*) FROM engins").fetchone()[0] == 0:
        for code, res, fuel, conso in DEFAULT_ENGINS:
            conn.execute(
                "INSERT INTO engins (code, reservoir_l, fuel_pct, conso_lh) VALUES (?,?,?,?)",
                (code, res, fuel, conso))
    if conn.execute("SELECT COUNT(*) FROM zones").fetchone()[0] == 0:
        for z in DEFAULT_ZONES:
            conn.execute("INSERT INTO zones (name, type, lat, lng, color) VALUES (?,?,?,?,?)", z)
    if conn.execute("SELECT COUNT(*) FROM trails").fetchone()[0] == 0:
        for t in DEFAULT_TRAILS:
            conn.execute(
                "INSERT INTO trails (name, from_zone, to_zone, color) VALUES (?,?,?,?)", t)
    if conn.execute("SELECT COUNT(*) FROM comptes").fetchone()[0] == 0:
        creer_comptes_initiaux(conn)
    conn.commit()
    conn.close()


# Fichier ou sont notes les mots de passe des comptes crees au premier
# demarrage. Dans data/, donc jamais versionne (.gitignore).
COMPTES_INITIAUX_FILE = os.path.join(BASE_DIR, "data", "comptes_initiaux.txt")


def creer_comptes_initiaux(conn):
    """Comptes crees sur une base VIDE, avec des mots de passe ALEATOIRES.

    Avant le 17/11, les mots de passe etaient ecrits en dur dans ce fichier
    ("password", "viewer") : publies avec le code, ils ouvraient toute
    installation restee sur ces valeurs. Ils sont desormais tires au hasard,
    affiches une fois dans la console et notes dans data/ (hors depot). A
    changer apres la premiere connexion. Une base existante n'est pas touchee.
    """
    comptes = [
        ("Gaston Albert", "gaston.albert@oresys.com", "admin"),
        ("Operateur 1", "operateur1@oresys.com", "viewer"),
    ]
    lignes = []
    for nom, email, role in comptes:
        mot_de_passe = secrets.token_urlsafe(12)
        salt, h = hash_password(mot_de_passe)
        conn.execute(
            "INSERT INTO comptes (nom, email, role, salt, hash) VALUES (?,?,?,?,?)",
            (nom, email, role, salt, h))
        lignes.append("%s (%s) : %s" % (email, role, mot_de_passe))
    try:
        os.makedirs(os.path.dirname(COMPTES_INITIAUX_FILE), exist_ok=True)
        with open(COMPTES_INITIAUX_FILE, "w", encoding="utf-8") as f:
            f.write("Comptes crees au premier demarrage - changez ces mots de passe.\n")
            f.write("\n".join(lignes) + "\n")
    except OSError as e:
        print("  COMPTES: impossible d'ecrire %s (%s)" % (COMPTES_INITIAUX_FILE, e))
    print("  COMPTES INITIAUX (a changer apres connexion) :")
    for l in lignes:
        print("    " + l)
    print("  -> notes aussi dans %s" % COMPTES_INITIAUX_FILE)


def migrate_schema(conn):
    """Ajoute les colonnes apparues apres la creation initiale de la base.

    SQLite ne sait pas ajouter une colonne a une table deja peuplee via
    `CREATE TABLE IF NOT EXISTS` : on inspecte `PRAGMA table_info` et on
    fait les `ALTER TABLE` manquants. Idempotent.
    """
    ajouts = {
        "engins": [
            ("famille", "TEXT DEFAULT ''"),
            ("actif", "INTEGER DEFAULT 1"),
            ("source", "TEXT DEFAULT 'local'"),
            ("remote_hash", "TEXT DEFAULT ''"),
        ],
        # Modele equipe : role (SPOTER / CHEF_EQUIPE) + code d'equipe (SP-01...)
        # couleur : couleur de la carte de l'equipe, choisie a la creation
        "sync_identifiants": [
            ("role", "TEXT DEFAULT ''"),
            ("equipe", "TEXT DEFAULT ''"),
            ("couleur", "TEXT DEFAULT ''"),
        ],
    }
    for table, colonnes in ajouts.items():
        existantes = {r["name"] for r in conn.execute("PRAGMA table_info(" + table + ")")}
        for nom, decl in colonnes:
            if nom not in existantes:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, nom, decl))


def save_server_config(cfg):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)



def load_server_config():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"port": PORT, "created": time.strftime("%Y-%m-%d %H:%M:%S")}


# --------------------------------------------- rate-limiting login ----------
# Anti force brute sur POST /api/auth/login :
#   - 5 echecs par identifiant (email/nom) -> blocage 5 min
#   - 30 echecs par IP en 15 min           -> blocage 15 min
# Compteurs en memoire (un redemarrage serveur remet a zero).
LOGIN_WINDOW = 15 * 60          # fenetre glissante 15 min
LOGIN_MAX_FAIL_IDENT = 5        # echecs avant blocage par identifiant
LOGIN_MAX_FAIL_IP = 30          # echecs avant blocage par IP
LOGIN_LOCK_IDENT = 5 * 60       # duree du blocage identifiant
LOGIN_LOCK_IP = 15 * 60         # duree du blocage IP
_login_attempts = {}            # cle -> [echecs, debut_fenetre, bloque_jusqua]


def _login_blocked(ident, ip):
    """Retourne (bloque, secondes_restantes)."""
    now = time.time()
    for key in ("i:" + ident.lower() if ident else "", "ip:" + ip):
        st = _login_attempts.get(key)
        if not st:
            continue
        if st[2] > now:
            return True, int(st[2] - now) + 1
        if now - st[1] > LOGIN_WINDOW:
            _login_attempts.pop(key, None)
    return False, 0


def _login_fail(ident, ip):
    now = time.time()
    # nettoyage periodique des entrees expirees
    if len(_login_attempts) > 500:
        for k in [k for k, s in _login_attempts.items()
                  if s[2] < now and now - s[1] > LOGIN_WINDOW]:
            _login_attempts.pop(k, None)
    for key, max_fails, lock in (
            ("i:" + ident.lower(), LOGIN_MAX_FAIL_IDENT, LOGIN_LOCK_IDENT),
            ("ip:" + ip, LOGIN_MAX_FAIL_IP, LOGIN_LOCK_IP)):
        st = _login_attempts.get(key)
        if not st or now - st[1] > LOGIN_WINDOW:
            st = [0, now, 0.0]
            _login_attempts[key] = st
        st[0] += 1
        if st[0] >= max_fails:
            st[2] = now + lock


def _login_ok(ident, ip):
    # succes : remise a zero du compteur de l'identifiant
    if ident:
        _login_attempts.pop("i:" + ident.lower(), None)


# ============================================================================
#  SYNCHRONISATION GOOGLE SHEETS <-> APP SCRIPT
# ============================================================================
#  Contrat reellement deploye (App Script, onglet `server_OT`) :
#    GET  ?action=sante&token=..              diagnostic de deploiement
#    GET  ?action=referentiels&token=..       profils / identifiants / dts
#    GET  ?action=affectation&token=..        mission en cours
#    GET  ?action=blasts&token=..             blasts + cartes + blocs
#    GET  ?action=notifications&depuis=N&..   notifications
#    GET  ?action=donnees&onglet=X&token=..  lecture brute d'un onglet
#    POST ?action=donnees&token=..            ecriture d'un onglet
#    POST ?action=notification&token=..       { profil, message }
#
#  POINT CRITIQUE : Apps Script route sur `e.parameter`, donc `action` et
#  `token` voyagent TOUJOURS dans la query string. Un corps JSON POST ne
#  sert qu'a la charge utile. C'est la cause historique de l'erreur
#  « token absent ou refuse ».

GAS_TIMEOUT = 45
GAS_DATASETS = ("profils", "identifiants", "flotte", "positions",
                "affectation", "blasts", "notifications", "dig_plan")

# dataset -> table miroir locale
GAS_TABLES = {
    "profils": "sync_profils",
    "identifiants": "sync_identifiants",
    "flotte": "sync_flotte",
    "positions": "sync_positions",
    "affectation": "sync_affectation",
    "blasts": "sync_blasts",
    "notifications": "sync_notifications",
}

# Datasets poussables : blasts et notifications sont generes par le serveur
# (evenements tablettes, ordres du Boss) — les ecraser serait une perte.
GAS_PUSH_DATASETS = ("profils", "identifiants", "flotte", "positions", "affectation")

# table locale editable -> dataset concerne (sert a marquer « dirty »)
GAS_DIRTY_TABLES = {
    "engins": "flotte",
    "sync_profils": "profils",
    "sync_identifiants": "identifiants",
    "sync_flotte": "flotte",   # machines ajoutees via Settings (incl. excavatrices)
    "sync_positions": "positions",
    "sync_affectation": "affectation",
}

# Message quand l'App Script en ligne ne connait pas encore l'ecriture.
GAS_WRITE_UNSUPPORTED = (
    "L'App Script en ligne ne supporte pas encore l'ecriture "
    "(action=donnees). Redeployez server_OT : Editeur Apps Script > "
    "Deployer > Modifier > Version : Nouvelle version > Deployer."
)


def gas_config(conn):
    """URL + jeton de l'App Script, lus dans la table params."""
    rows = {r["cle"]: (r["valeur"] or "") for r in conn.execute(
        "SELECT cle, valeur FROM params WHERE cle IN ('gas_url','gas_token')")}
    return rows.get("gas_url", "").strip(), rows.get("gas_token", "").strip()


def gas_call(action, gas_url, gas_token, method="GET", params=None,
             payload=None, timeout=GAS_TIMEOUT):
    """Appelle l'App Script et normalise la reponse.

    `action` et `token` sont places dans la query string (contrait Apps
    Script). Le corps JSON n'est envoye que pour les POST.
    """
    if not gas_url:
        return {"ok": False, "http_status": 0, "data": None,
                "error": "URL App Script non configuree", "latency_ms": 0}
    qs = {"action": action}
    if gas_token:
        qs["token"] = gas_token
    for k, v in (params or {}).items():
        if v is None or v == "":
            continue
        qs[k] = str(v)
    url = gas_url + ("&" if "?" in gas_url else "?") + urlencode(qs)
    data = None
    headers = {"User-Agent": "OreTracking/2.0"}
    if method.upper() == "POST":
        data = json.dumps(payload if payload is not None else {}).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method.upper())
    t0 = time.time()
    status = 0
    raw = ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:
            raw = ""
    except Exception as e:
        return {"ok": False, "http_status": 0, "data": None,
                "error": "App Script injoignable: %s" % e,
                "latency_ms": int((time.time() - t0) * 1000)}
    latency = int((time.time() - t0) * 1000)
    try:
        body = json.loads(raw) if raw.strip() else {}
    except Exception:
        body = None
    ok = isinstance(body, dict) and body.get("ok") is True
    err = ""
    if not ok:
        if isinstance(body, dict):
            err = str(body.get("erreur") or body.get("error") or "")
        if not err:
            err = (raw[:200] or ("HTTP %s" % status))
    return {"ok": ok, "http_status": status, "data": body if isinstance(body, dict) else None,
            "error": err, "latency_ms": latency}


def _num(v, defaut=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return defaut


def _bool(v):
    if isinstance(v, bool):
        return 1 if v else 0
    if isinstance(v, (int, float)):
        return 1 if v else 0
    return 1 if str(v).strip().lower() in ("1", "true", "oui", "yes", "vrai", "actif") else 0


def _txt(v):
    return str(v).strip() if v is not None else ""


def _canon_hash(donnees):
    """Hash stable d'une ligne canonique (ordre des cles sans effet)."""
    norm = {}
    for k, v in donnees.items():
        if str(k).startswith("remote_hash"):
            continue
        if isinstance(v, bool):
            norm[k] = 1 if v else 0
        elif isinstance(v, (int, float)):
            norm[k] = round(float(v), 6)
        else:
            norm[k] = str(v).strip()
    return hashlib.sha1(json.dumps(norm, sort_keys=True,
                                   ensure_ascii=False).encode("utf-8")).hexdigest()


# ------------------------------------------------------- canoniques dataset --

def canon_profils(rows):
    out = {}
    for p in rows or []:
        code = _txt(p.get("code"))
        if not code:
            continue
        out[code] = {
            "code": code,
            "libelle": _txt(p.get("libelle")),
            "titre": _txt(p.get("titre")),
            "role_cdc": _txt(p.get("role_cdc")),
            "icone": _txt(p.get("icone")),
            "ordre": _num(p.get("ordre")),
            "actif": _bool(p.get("actif")),
        }
    return out


def canon_identifiants(rows):
    """Personnes de connexion (spotters equipes, + a terme operateurs).
    `role` (SPOTER / CHEF_EQUIPE) et `equipe` ne servent qu'au profil SPOT.
    """
    out = {}
    for e in rows or []:
        profil = _txt(e.get("profil"))
        ident = _txt(e.get("id") or e.get("identifiant"))
        if not profil or not ident:
            continue
        out[profil + "::" + ident] = {
            "profil": profil,
            "identifiant": ident,
            "nom": _txt(e.get("nom")),
            "role": _txt(e.get("role")),
            "equipe": _txt(e.get("equipe")),
            "actif": _bool(e.get("actif")),
        }
    return out


# Prefixe de code par famille d'engin : les familles de camions (1500/785/
# ADT) portent le prefixe historique DT ; les autres abbeviennent leur nom
# (EXCAVATOR -> EXCA). Cle complete = prefixe + numero zero-pad.
FAMILLES_DT = ("1500", "785", "ADT")


def prefixe_engin(famille):
    return "DT" if famille in FAMILLES_DT else famille[:4].upper()


def role_famille(famille):
    """Profil proprietaire d'une famille d'engins (colonne `role` de l'onglet
    `flotte` en ligne). Ce sont les seules familles du site aujourd'hui :
    camions 1500/785/ADT -> profil DT, tout le reste est du profil
    EXCAVATOR (excavatrices Hitachi EX, Komatsu PC, marques closes HYUNDAI,
    VOLVO, etc.). Une famille inconnue n'a pas de profil : role vide.
    """
    if famille in FAMILLES_DT:
        return "DT"
    return "EXCAVATOR"


def canon_flotte(rows):
    """Rows = {type/famille, id/numero, actif} -> cle DT-01, EXCA-01..."""
    out = {}
    for d in rows or []:
        famille = _txt(d.get("type") or d.get("famille"))
        numero = _txt(d.get("id") or d.get("numero"))
        if not famille or not numero.isdigit():
            continue
        n = int(numero)
        code = "%s-%02d" % (prefixe_engin(famille), n)
        out[code] = {
            "code": code,
            "famille": famille,
            "numero": n,
            "actif": _bool(d.get("actif")),
        }
    return out


def canon_positions(rows):
    out = {}
    for p in rows or []:
        code = _txt(p.get("code"))
        if not code:
            continue
        out[code] = {
            "code": code,
            "easting": _num(p.get("easting")),
            "northing": _num(p.get("northing")),
        }
    return out


def canon_affectation(affectation):
    """Cles mission comparees ; `parcours` (calcule) et `valide_le` ignores."""
    a = affectation or {}
    out = {}
    for cle in ("destination", "dumping_area", "mission", "alerte"):
        out[cle] = {"cle": cle, "valeur": _txt(a.get(cle))}
    return out


def canon_blasts(blasts):
    out = {}
    for b in blasts or []:
        numero = _txt(b.get("numero"))
        if not numero:
            continue
        blocs = b.get("blocs") or []
        if not isinstance(blocs, list):
            blocs = [blocs]
        out[numero] = {
            "numero": numero,
            "carte": _txt(b.get("carte")),
            "libelle_carte": _txt(b.get("libelle_carte")),
            "blocs": ",".join(sorted(_txt(x) for x in blocs)),
        }
    return out


def canon_notifications(rows):
    out = {}
    for n in rows or []:
        try:
            nid = int(_txt(n.get("id")))
        except ValueError:
            continue
        out[str(nid)] = {
            "id": nid,
            "profil": _txt(n.get("profil")),
            "message": _txt(n.get("message")),
            "horodatage": _txt(n.get("horodatage")),
        }
    return out


# ------------------------------------------------------ etat local (miroirs) --

def local_canon(conn, dataset):
    """Contenu local canonique d'un dataset (source de la comparaison)."""
    if dataset == "profils":
        rows = [{"code": r["code"], "libelle": r["libelle"], "titre": r["titre"],
                 "role_cdc": r["role_cdc"], "icone": r["icone"], "ordre": r["ordre"],
                 "actif": r["actif"]}
                for r in conn.execute("SELECT * FROM sync_profils")]
        return canon_profils(rows)
    if dataset == "identifiants":
        rows = [{"profil": r["profil"], "id": r["identifiant"], "nom": r["nom"],
                 "role": r["role"], "equipe": r["equipe"], "actif": r["actif"]}
                for r in conn.execute("SELECT * FROM sync_identifiants")]
        return canon_identifiants(rows)
    if dataset == "flotte":
        # source de verite = table engins (la liste DT du Parametres)
        rows = []
        for r in conn.execute("SELECT code, famille, actif FROM engins"):
            famille = _txt(r["famille"])
            numero = ""
            for part in _txt(r["code"]).replace("_", "-").split("-"):
                if part.isdigit():
                    numero = str(int(part))
                    break
            if not famille or not numero.isdigit():
                continue
            rows.append({"code": _txt(r["code"]), "famille": famille,
                         "numero": numero, "actif": r["actif"]})
        return canon_flotte(rows)
    if dataset == "positions":
        rows = [{"code": r["code"], "easting": r["easting"], "northing": r["northing"]}
                for r in conn.execute("SELECT * FROM sync_positions")]
        return canon_positions(rows)
    if dataset == "affectation":
        affectation = {}
        for r in conn.execute("SELECT cle, valeur FROM sync_affectation"):
            affectation[r["cle"]] = r["valeur"]
        return canon_affectation(affectation)
    if dataset == "blasts":
        rows = [{"numero": r["numero"], "carte": r["carte"],
                 "libelle_carte": r["libelle_carte"], "blocs": r["blocs"]}
                for r in conn.execute("SELECT * FROM sync_blasts")]
        return canon_blasts(rows)
    if dataset == "dig_plan":
        # Blasts Dig Plan : ils vivent dans la passerelle (senor_geology.db),
        # pas dans ce SaaS. On lit sa base directement (fichier voisin) pour
        # compter les blasts actifs ; le diff vs classeur se fait dans la
        # branche distante (source()).
        sg = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "senor_geology.db")
        if not os.path.isfile(sg):
            sg = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "data", "senor_geology.db")
        try:
            c2 = sqlite3.connect(sg)
            c2.row_factory = sqlite3.Row
            n = c2.execute("SELECT COUNT(*) FROM blasts WHERE actif=1").fetchone()[0]
            c2.close()
            return ["b%d" % i for i in range(n)]
        except Exception:
            return []
    if dataset == "notifications":
        rows = [{"id": r["id"], "profil": r["profil"], "message": r["message"],
                 "horodatage": r["horodatage"]}
                for r in conn.execute("SELECT * FROM sync_notifications")]
        return canon_notifications(rows)
    return {}


# ------------------------------------------------------------ synchronisation --

def _set_state(conn, dataset, etat, local_count=0, remote_count=0,
               message="", pulled=False, pushed=False):
    row = conn.execute("SELECT * FROM sync_state WHERE dataset=?", (dataset,)).fetchone()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    if row is None:
        conn.execute(
            "INSERT INTO sync_state (dataset, etat, local_count, remote_count, "
            "dernier_pull, dernier_push, message, dirty) VALUES (?,?,?,?,?,?,?,0)",
            (dataset, etat, local_count, remote_count,
             now if pulled else "", now if pushed else "", message))
    else:
        conn.execute(
            "UPDATE sync_state SET etat=?, local_count=?, remote_count=?, "
            "message=?, dernier_pull=?, dernier_push=? WHERE dataset=?",
            (etat, local_count, remote_count, message,
             now if pulled else row["dernier_pull"],
             now if pushed else row["dernier_push"], dataset))


def _mark_dirty(conn, dataset):
    conn.execute(
        "INSERT INTO sync_state (dataset, etat, message, dirty) VALUES (?,?,?,1) "
        "ON CONFLICT(dataset) DO UPDATE SET dirty=1, message='modification locale'",
        (dataset, "local", "modification locale"))


def sync_profils_local(conn, profils):
    maintenant = time.strftime("%Y-%m-%d %H:%M:%S")
    canon = canon_profils(profils)
    for code, p in canon.items():
        conn.execute(
            "INSERT INTO sync_profils (code, libelle, titre, role_cdc, icone, ordre, "
            "actif, remote_hash, maj_le) VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET libelle=excluded.libelle, "
            "titre=excluded.titre, role_cdc=excluded.role_cdc, icone=excluded.icone, "
            "ordre=excluded.ordre, actif=excluded.actif, "
            "remote_hash=excluded.remote_hash, maj_le=excluded.maj_le",
            (code, p["libelle"], p["titre"], p["role_cdc"], p["icone"], int(p["ordre"]),
             p["actif"], _canon_hash(p), maintenant))
    return len(canon)


def sync_identifiants_local(conn, identifiants):
    maintenant = time.strftime("%Y-%m-%d %H:%M:%S")
    canon = canon_identifiants(identifiants)
    for cle, e in canon.items():
        conn.execute(
            "INSERT INTO sync_identifiants (profil, identifiant, nom, role, "
            "equipe, actif, remote_hash, maj_le) VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(profil, identifiant) DO UPDATE SET nom=excluded.nom, "
            "role=excluded.role, equipe=excluded.equipe, actif=excluded.actif, "
            "remote_hash=excluded.remote_hash, maj_le=excluded.maj_le",
            (e["profil"], e["identifiant"], e["nom"], e["role"], e["equipe"],
             e["actif"], _canon_hash(e), maintenant))
    return len(canon)


def sync_flotte_local(conn, dts):
    """Met a jour le miroir `flotte` ET la liste operationnelle `engins`."""
    maintenant = time.strftime("%Y-%m-%d %H:%M:%S")
    canon = canon_flotte(dts)
    for code, f in canon.items():
        conn.execute(
            "INSERT INTO sync_flotte (code, famille, numero, actif, remote_hash, maj_le) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(code) DO UPDATE SET "
            "famille=excluded.famille, numero=excluded.numero, actif=excluded.actif, "
            "remote_hash=excluded.remote_hash, maj_le=excluded.maj_le",
            (code, f["famille"], f["numero"], f["actif"],
             _canon_hash(f), maintenant))
        # La liste DT (table engins) suit la flotte en ligne ; les donnees
        # locales (reservoir, carburant, conso) ne sont PAS touchees.
        engin = conn.execute("SELECT id FROM engins WHERE code=?", (code,)).fetchone()
        statut = "actif" if f["actif"] else "inactif"
        if engin is None:
            conn.execute(
                "INSERT INTO engins (code, nom, reservoir_l, fuel_pct, conso_lh, "
                "statut, famille, actif, source) VALUES (?,?,300,100,25,?,?,?,'gas')",
                (code, f["famille"], statut, f["famille"], f["actif"]))
        else:
            conn.execute(
                "UPDATE engins SET famille=?, actif=?, statut=?, source='gas', "
                "nom=CASE WHEN nom='' THEN ? ELSE nom END WHERE id=?",
                (f["famille"], f["actif"], statut, f["famille"], engin["id"]))
    # Les camions venus du Sheet mais absents en ligne sont desactives.
    for row in conn.execute("SELECT code FROM engins WHERE source='gas'").fetchall():
        if row["code"] not in canon:
            conn.execute("UPDATE engins SET actif=0, statut='inactif' WHERE code=?",
                         (row["code"],))
    return len(canon)


def sync_positions_local(conn, positions):
    maintenant = time.strftime("%Y-%m-%d %H:%M:%S")
    canon = canon_positions(positions)
    conn.execute("DELETE FROM sync_positions")
    for code, p in canon.items():
        conn.execute(
            "INSERT INTO sync_positions (code, easting, northing, remote_hash, maj_le) "
            "VALUES (?,?,?,?,?)",
            (code, p["easting"], p["northing"], _canon_hash(p), maintenant))
    return len(canon)


def sync_affectation_local(conn, affectation, valide_le=""):
    maintenant = time.strftime("%Y-%m-%d %H:%M:%S")
    a = affectation or {}
    count = 0
    for cle in ("destination", "dumping_area", "mission", "alerte", "parcours"):
        if cle not in a:
            continue
        valeur = _txt(a.get(cle))
        conn.execute(
            "INSERT INTO sync_affectation (cle, valeur, remote_hash, maj_le) "
            "VALUES (?,?,?,?) ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur, "
            "remote_hash=excluded.remote_hash, maj_le=excluded.maj_le",
            (cle, valeur, _canon_hash({"cle": cle, "valeur": valeur}), maintenant))
        count += 1
    if valide_le:
        conn.execute(
            "INSERT INTO sync_affectation (cle, valeur, remote_hash, maj_le) "
            "VALUES ('valide_le', ?, '', ?) ON CONFLICT(cle) DO UPDATE SET "
            "valeur=excluded.valeur, maj_le=excluded.maj_le",
            (_txt(valide_le), maintenant))
    return count


def sync_blasts_local(conn, blasts):
    maintenant = time.strftime("%Y-%m-%d %H:%M:%S")
    canon = canon_blasts(blasts)
    plans = {}
    for b in blasts or []:
        numero = _txt(b.get("numero"))
        if numero:
            plans[numero] = _txt(b.get("plan_url"))
    conn.execute("DELETE FROM sync_blasts")
    for numero, b in canon.items():
        conn.execute(
            "INSERT INTO sync_blasts (numero, carte, libelle_carte, blocs, plan_url, "
            "remote_hash, maj_le) VALUES (?,?,?,?,?,?,?)",
            (numero, b["carte"], b["libelle_carte"], b["blocs"], plans.get(numero, ""),
             _canon_hash(b), maintenant))
    return len(canon)


def sync_notifications_local(conn, notifications):
    maintenant = time.strftime("%Y-%m-%d %H:%M:%S")
    canon = canon_notifications(notifications)
    for cle, n in canon.items():
        conn.execute(
            "INSERT INTO sync_notifications (id, profil, message, horodatage, "
            "remote_hash, maj_le) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET profil=excluded.profil, "
            "message=excluded.message, horodatage=excluded.horodatage, "
            "remote_hash=excluded.remote_hash, maj_le=excluded.maj_le",
            (n["id"], n["profil"], n["message"], n["horodatage"],
             _canon_hash(n), maintenant))
    return len(canon)


def gas_remote_canon(conn, dataset, gas_url, gas_token):
    """Contenu EN LIGNE canonique d'un dataset + message d'erreur."""
    if dataset in ("profils", "identifiants", "flotte"):
        rep = gas_call("referentiels", gas_url, gas_token)
        if not rep["ok"]:
            return None, rep["error"] or "referentiels indisponibles"
        data = rep["data"] or {}
        if dataset == "profils":
            return canon_profils(data.get("profils")), ""
        if dataset == "identifiants":
            return canon_identifiants(data.get("equipements")), ""
        return canon_flotte(data.get("dts")), ""
    if dataset == "affectation":
        rep = gas_call("affectation", gas_url, gas_token)
        if not rep["ok"]:
            return None, rep["error"] or "affectation indisponible"
        data = rep["data"] or {}
        return canon_affectation(data.get("affectation")), ""
    if dataset == "blasts":
        rep = gas_call("blasts", gas_url, gas_token)
        if not rep["ok"]:
            return None, rep["error"] or "blasts indisponibles"
        return canon_blasts((rep["data"] or {}).get("blasts")), ""
    if dataset == "notifications":
        rep = gas_call("notifications", gas_url, gas_token, params={"depuis": 0})
        if not rep["ok"]:
            return None, rep["error"] or "notifications indisponibles"
        return canon_notifications((rep["data"] or {}).get("notifications")), ""
    if dataset == "positions":
        rep = gas_call("donnees", gas_url, gas_token, params={"onglet": "positions"})
        if not rep["ok"]:
            return None, rep["error"] or "positions indisponibles"
        return canon_positions((rep["data"] or {}).get("lignes")), ""
    return None, "dataset inconnu"


def gas_fetch_all(gas_url, gas_token):
    """Recupere TOUTES les sources distinctes en parallele (HTTP pur).

    Apps Script met 5-10 s par appel (initialiser + lecture de Spreadsheet) :
    en serie, une synchronisation complete prendrait plus d'une minute. Les
    threads ne touchent AUCUNE base de donnees — les ecritures locales se
    font apres coup, en serie, dans le thread de la requete.

    Les notifications sont toujours lues en ENTIER (depuis=0) : la
    comparaison local/en ligne porte sur toutes les lignes, pas sur les
    dernieres seulement ; l'onglet reste petit (canal d'ordre du Boss).

    Retourne un dict de reponses brutes, cles par source :
        {'referentiels': rep, 'affectation': rep, 'blasts': rep,
         'notifications': rep, 'positions': rep}
    """
    from concurrent.futures import ThreadPoolExecutor
    taches = {
        "referentiels": lambda: gas_call("referentiels", gas_url, gas_token),
        "affectation": lambda: gas_call("affectation", gas_url, gas_token),
        "blasts": lambda: gas_call("blasts", gas_url, gas_token),
        "notifications": lambda: gas_call("notifications", gas_url, gas_token,
                                          params={"depuis": 0}),
        "positions": lambda: gas_call("donnees", gas_url, gas_token,
                                      params={"onglet": "positions"}),
    }
    out = {}
    with ThreadPoolExecutor(max_workers=len(taches)) as pool:
        futures = {nom: pool.submit(fn) for nom, fn in taches.items()}
        for nom, fut in futures.items():
            try:
                out[nom] = fut.result()
            except Exception as e:
                out[nom] = {"ok": False, "http_status": 0, "data": None,
                            "error": "echec du thread : %s" % e}
    return out


def comparer(local, remote):
    lk, rk = set(local), set(remote)
    differents = [k for k in sorted(lk & rk)
                   if _canon_hash(local[k]) != _canon_hash(remote[k])]
    return {
        "identique": not (lk - rk) and not (rk - lk) and not differents,
        "local_seul": sorted(lk - rk),
        "online_seul": sorted(rk - lk),
        "differents": differents,
        "local_count": len(local),
        "remote_count": len(remote),
    }


def gas_pull(conn, gas_url, gas_token):
    """Récupère l'état en ligne et met à jour les miroirs locaux.

    Phase 1 : tous les appels HTTP en parallele (aucun acces DB).
    Phase 2 : mise a jour des miroirs + commit au fur et a mesure (un
    verrou d'ecriture snap, jamais tenu pendant les appels GAS).
    """
    resultats = {}

    reps = gas_fetch_all(gas_url, gas_token)
    ref = reps["referentiels"]

    # --- referentiels : profils + identifiants + flotte ---
    if ref["ok"]:
        data = ref["data"] or {}
        n = sync_profils_local(conn, data.get("profils") or [])
        resultats["profils"] = {"lignes": n, "erreur": ""}
        n = sync_identifiants_local(conn, data.get("equipements") or [])
        resultats["identifiants"] = {"lignes": n, "erreur": ""}
        n = sync_flotte_local(conn, data.get("dts") or [])
        resultats["flotte"] = {"lignes": n, "erreur": ""}
    else:
        msg = ref["error"] or "referentiels indisponibles"
        for ds in ("profils", "identifiants", "flotte"):
            resultats[ds] = {"lignes": 0, "erreur": msg}
    conn.commit()

    # --- affectation ---
    rep = reps["affectation"]
    if rep["ok"]:
        data = rep["data"] or {}
        n = sync_affectation_local(conn, data.get("affectation") or {},
                                   data.get("valide_le") or "")
        resultats["affectation"] = {"lignes": n, "erreur": ""}
    else:
        resultats["affectation"] = {"lignes": 0, "erreur": rep["error"] or "affectation indisponible"}
    conn.commit()

    # --- blasts ---
    rep = reps["blasts"]
    if rep["ok"]:
        n = sync_blasts_local(conn, (rep["data"] or {}).get("blasts") or [])
        resultats["blasts"] = {"lignes": n, "erreur": ""}
    else:
        resultats["blasts"] = {"lignes": 0, "erreur": rep["error"] or "blasts indisponibles"}
    conn.commit()

    # --- notifications (incremental : id croissants) ---
    rep = reps["notifications"]
    if rep["ok"]:
        n = sync_notifications_local(conn, (rep["data"] or {}).get("notifications") or [])
        resultats["notifications"] = {"lignes": n, "erreur": ""}
    else:
        resultats["notifications"] = {"lignes": 0, "erreur": rep["error"] or "notifications indisponibles"}
    conn.commit()

    # --- positions (lecture generique : necessite le redeploiement) ---
    rep = reps["positions"]
    if rep["ok"]:
        n = sync_positions_local(conn, (rep["data"] or {}).get("lignes") or [])
        resultats["positions"] = {"lignes": n, "erreur": ""}
    else:
        resultats["positions"] = {"lignes": 0, "erreur": rep["error"] or "positions indisponibles"}
    conn.commit()

    # --- etat de comparaison local/online (sur les reponses deja recues) ---
    def canon_ds(ds):
        if not ref["ok"]:
            return None, "referentiels indisponibles"
        data = ref["data"] or {}
        if ds == "profils":
            return canon_profils(data.get("profils")), ""
        if ds == "identifiants":
            return canon_identifiants(data.get("equipements")), ""
        if ds == "flotte":
            return canon_flotte(data.get("dts")), ""
        if ds == "affectation":
            return canon_affectation((reps["affectation"]["data"] or {}).get("affectation")), ""
        if ds == "blasts":
            return canon_blasts((reps["blasts"]["data"] or {}).get("blasts")), ""
        if ds == "notifications":
            return canon_notifications((reps["notifications"]["data"] or {}).get("notifications")), ""
        if ds == "positions":
            if not reps["positions"]["ok"]:
                return None, (reps["positions"]["error"] or "positions indisponibles")
            return canon_positions((reps["positions"]["data"] or {}).get("lignes")), ""
        return None, "dataset inconnu"

    for dataset in GAS_DATASETS:
        local = local_canon(conn, dataset)
        remote, err = canon_ds(dataset)
        if remote is None:
            _set_state(conn, dataset, "erreur", len(local), 0, err, pulled=True)
            conn.commit()
            continue
        cmp = comparer(local, remote)
        etat = "identique" if cmp["identique"] else "different"
        _set_state(conn, dataset, etat, cmp["local_count"], cmp["remote_count"],
                   "" if cmp["identique"] else "ecarts: %d" % (
                       len(cmp["differents"]) + len(cmp["local_seul"]) + len(cmp["online_seul"])),
                   pulled=True)
        conn.commit()
    return resultats, erreurs_of(resultats)


def erreurs_of(resultats):
    return [ds + ": " + v["erreur"] for ds, v in resultats.items() if v.get("erreur")]


def _flotte_detail(conn, remote_flotte):
    """Decompose la flotte (locale et en ligne) en lignes affichees sur la
    page Sync : Camions DT (familles 1500/785/ADT) et Excavatrices (tout le
    reste : Hitachi EX, Komatsu PC, HYUNDAI, VOLVO...). Lignes DERIVEES :
    jamais ecrites dans sync_state, jamais poussees — le push continue de
    n'envoyer que les datasets reels.
    """
    local_flotte = canon_flotte([
        {"famille": r["famille"], "numero": r["numero"], "actif": r["actif"]}
        for r in conn.execute("SELECT famille, numero, actif FROM sync_flotte")]) or {}

    def codes_par_type(flotte):
        out = {"dt": set(), "exc": set()}
        for code, e in (flotte or {}).items():
            (out["dt"] if e["famille"] in FAMILLES_DT else out["exc"]).add(code)
        return out

    loc = codes_par_type(local_flotte)
    rem = codes_par_type(remote_flotte)
    lignes = []
    for label, cle in (("Camions DT", "dt"), ("Excavatrices", "exc")):
        lg, rg = loc[cle], rem[cle]
        if remote_flotte is None:
            etat, msg = "erreur", "flotte en ligne indisponible"
        elif lg == rg:
            etat, msg = "identique", ""
        else:
            parts = []
            if lg - rg:
                parts.append("%d a pousser" % len(lg - rg))
            if rg - lg:
                parts.append("%d a recuperer" % len(rg - lg))
            etat, msg = "different", ", ".join(parts) or "ecarts"
        lignes.append({"dataset": label, "etat": etat,
                       "local_count": len(lg), "remote_count": (len(rg) if remote_flotte is not None else None),
                       "dernier_pull": "", "dernier_push": "", "message": msg,
                       "dirty": 0, "local_seul": sorted(lg - rg)[:5],
                       "online_seul": sorted(rg - lg)[:5], "differents": []})
    return lignes


def facteurs_familles(conn):
    """Tonnes par voyage, par famille de DT (Parametres > Flotte, 17/11).
    Stocke en JSON dans params.truck_factor_familles : {"1500": 30, ...}.
    Seules les valeurs > 0 sont gardees ; famille en majuscules."""
    ligne = conn.execute(
        "SELECT valeur FROM params WHERE cle='truck_factor_familles'").fetchone()
    try:
        brut = json.loads(ligne["valeur"]) if ligne and ligne["valeur"] else {}
    except (ValueError, TypeError):
        brut = {}
    out = {}
    for fam, val in (brut.items() if isinstance(brut, dict) else []):
        try:
            v = float(str(val).replace(",", "."))
        except (ValueError, TypeError):
            continue
        if v > 0 and math.isfinite(v):
            out[_txt(fam).upper()] = int(v) if v == int(v) else round(v, 2)
    return out


def gas_push_rows(dataset, conn):
    """Lignes a envoyer en ligne pour un dataset (source : etat local)."""
    if dataset == "flotte":
        rows = []
        # truck_factor_t (17/11) : tonnes par voyage de la famille, colonne
        # facultative de l'onglet flotte de Serveur_OT. Vide = repli sur le
        # facteur global cote serveurs. Excavatrices : toujours vide.
        facteurs = facteurs_familles(conn)
        # source : engins (liste operationnelle) UNION sync_flotte (machines
        # ajoutees via Settings, presentes ici meme avant le prochain pull).
        # Dedup par famille+numero : engins gagne (il porte le statut live).
        vues = set()
        for src in ("engins", "sync_flotte"):
            # statut n'existe que dans `engins` ; sync_flotte n'a que actif
            col_statut = ", statut" if src == "engins" else ""
            for r in conn.execute("SELECT code, famille, actif" + col_statut + " FROM " + src):
                famille = _txt(r["famille"])
                numero = ""
                for part in _txt(r["code"]).replace("_", "-").split("-"):
                    if part.isdigit():
                        numero = str(int(part))
                        break
                if not famille or not numero:
                    continue
                cle = (famille.lower(), numero)
                if cle in vues:
                    continue
                vues.add(cle)
                statut = r["statut"] if src == "engins" else None
                actif = bool(_bool(r["actif"]) and (statut is None or _txt(statut) != "inactif"))
                role = role_famille(famille)
                rows.append({"role": role,
                             "famille": famille, "numero": int(numero),
                             "actif": actif,
                             "truck_factor_t": (facteurs.get(famille.upper(), "")
                                                if role == "DT" else "")})
        rows.sort(key=lambda x: x["numero"])
        return rows
    if dataset == "profils":
        return [{"code": r["code"], "libelle": r["libelle"], "titre": r["titre"],
                 "role_cdc": r["role_cdc"], "icone": r["icone"],
                 "ordre": int(r["ordre"] or 0), "actif": bool(r["actif"])}
                for r in conn.execute("SELECT * FROM sync_profils ORDER BY code")]
    if dataset == "identifiants":
        return [{"profil": r["profil"], "id": r["identifiant"], "nom": r["nom"],
                 "role": r["role"], "equipe": r["equipe"], "actif": bool(r["actif"])}
                for r in conn.execute("SELECT * FROM sync_identifiants ORDER BY profil, identifiant")]
    if dataset == "positions":
        return [{"code": r["code"], "easting": r["easting"], "northing": r["northing"]}
                for r in conn.execute("SELECT * FROM sync_positions ORDER BY code")]
    if dataset == "affectation":
        rows = [{"cle": r["cle"], "valeur": r["valeur"]}
                for r in conn.execute("SELECT * FROM sync_affectation ORDER BY cle")]
        if not any(r["cle"] == "valide_le" for r in rows):
            rows.append({"cle": "valide_le",
                         "valeur": time.strftime("%Y-%m-%d %H:%M:%S")})
        return rows
    return []


def gas_push(conn, gas_url, gas_token, datasets=None):
    """Pousse l'etat local vers le Sheet (action=donnees).

    Garantie anti-effacement : on n'envoie JAMAIS un lot vide — pousser
    avant une recuperation, ou pousser un dataset sans source locale
    (blasts, notifications: generes par le serveur) viderait l'onglet
    du Spreadsheet. Ces datasets sont ecartes du push.
    """
    cibles = list(datasets) if datasets else list(GAS_PUSH_DATASETS)
    resultats = {}
    erreurs = []
    for dataset in cibles:
        if dataset not in GAS_DATASETS:
            continue
        if dataset in ("blasts", "notifications"):
            resultats[dataset] = {"ecrites": 0, "erreur": "",
                                  "note": "genere par le serveur : lecture seule"}
            continue
        lignes = gas_push_rows(dataset, conn)
        if not lignes:
            resultats[dataset] = {
                "ecrites": 0,
                "erreur": "aucune donnee locale : lancez d'abord une "
                          "recuperation depuis Google Sheets"}
            erreurs.append(dataset + ": aucune donnee locale")
            continue
        rep = gas_call("donnees", gas_url, gas_token, method="POST",
                       payload={"onglet": dataset, "lignes": lignes})
        if rep["ok"]:
            resultats[dataset] = {"ecrites": len(lignes), "erreur": ""}
            _set_state(conn, dataset, "poussee", len(lignes), len(lignes),
                       "", pushed=True)
            conn.execute("UPDATE sync_state SET dirty=0 WHERE dataset=?", (dataset,))
        else:
            msg = rep["error"] or "echec de l'ecriture"
            if "action POST inconnue" in msg or "action inconnue" in msg:
                msg = GAS_WRITE_UNSUPPORTED
            resultats[dataset] = {"ecrites": 0, "erreur": msg}
            erreurs.append(dataset + ": " + msg)
    return resultats, erreurs


# ------------------------------------------------------------ HTTP layer ----

ALLOWED_TABLES = {"zones", "trails", "engins", "voyages", "alertes", "comptes",
                  "equipements",
                  # miroirs des onglets Google Sheets (lecture + edition locale)
                  "sync_profils", "sync_identifiants", "sync_flotte",
                  "sync_positions", "sync_affectation", "sync_blasts",
                  "sync_notifications"}





class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OreTrackingServer/2.0"

    def _json(self, code, payload):
        body = json.dumps(payload, default=str, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return {}

    def _qs(self):
        return parse_qs(urlparse(self.path).query)

    def _token(self):
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:].strip()
        return ""

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Max-Age", "86400")
        self.end_headers()

    def log_message(self, fmt, *args):
        sys.stdout.write("[srv] %s\n" % (fmt % args))

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        try:
            if path.startswith("/api/"):
                return self.route_api(path, "GET")
            return self.serve_static(path)
        except Exception as e:
            return self._json(500, {"error": str(e)})

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        try:
            return self.route_api(path, "POST")
        except Exception as e:
            return self._json(500, {"error": str(e)})

    def do_PUT(self):
        path = unquote(urlparse(self.path).path)
        try:
            return self.route_api(path, "PUT")
        except Exception as e:
            return self._json(500, {"error": str(e)})

    def do_DELETE(self):
        path = unquote(urlparse(self.path).path)
        try:
            return self.route_api(path, "DELETE")
        except Exception as e:
            return self._json(500, {"error": str(e)})

    def route_api(self, path, method):
        # ---- garde global : tout est prive sauf la liste blanche ci-dessous
        # (dashboard sans login + endpoints d'authentification + sante)
        PUBLIC = {
            ("GET", "/api/health"),
            ("GET", "/api/config"),
            ("GET", "/api/stats"),
            ("GET", "/api/alerts"),
            ("GET", "/api/auth/me"),
            ("POST", "/api/auth/login"),
            ("POST", "/api/android/detect"),
            ("POST", "/api/tablettes/detect"),
        }
        if (method, path) not in PUBLIC and auth_context(self) is None:
            # consommer le corps de la requete avant de refuser, sinon les
            # octets restants corrompent la prochaine requete keep-alive
            try:
                cl = int(self.headers.get("Content-Length", 0) or 0)
                if cl > 0:
                    self.rfile.read(cl)
            except Exception:
                pass
            return self._json(401, {"error": "authentification requise"})

        conn = get_conn()
        try:
            if path == "/api/health" and method == "GET":
                ok = conn.execute("SELECT COUNT(*) FROM params").fetchone()[0] > 0
                return self._json(200, {
                    "status": "ok" if ok else "degraded",
                    "server": "OreTrackingServer/2.0",
                    "db": DB_PATH,
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                })

            if path == "/api/android" and method == "GET":
                rows = [dict(r) for r in conn.execute(
                    "SELECT id, nom, modele, android_id, serial, actif, dernier_seen "
                    "FROM android_devices ORDER BY dernier_seen DESC")]
                return self._json(200, {"android": rows})

            if path == "/api/android/detect" and method == "POST":
                body = self._body()
                nom = (body.get("nom") or "").strip()
                modele = (body.get("modele") or "").strip()
                android_id = (body.get("android_id") or "").strip()
                serial = (body.get("serial") or "").strip()
                if not android_id:
                    return self._json(400, {"error": "android_id requis"})
                maintenant = time.strftime("%Y-%m-%d %H:%M:%S")
                cur = conn.execute(
                    "INSERT INTO android_devices (nom, modele, android_id, serial, actif, dernier_seen) "
                    "VALUES (?, ?, ?, ?, 1, ?) "
                    "ON CONFLICT(android_id) DO UPDATE SET "
                    "nom=excluded.nom, modele=excluded.modele, serial=excluded.serial, "
                    "actif=1, dernier_seen=excluded.dernier_seen",
                    (nom, modele, android_id, serial, maintenant))
                conn.commit()
                new_id = cur.lastrowid
                if not new_id:
                    r = conn.execute("SELECT id FROM android_devices WHERE android_id=?",
                                     (android_id,)).fetchone()
                    new_id = r["id"] if r else 0
                return self._json(201, {"ok": True, "id": new_id,
                    "android": {"id": new_id, "nom": nom, "modele": modele,
                                "android_id": android_id, "serial": serial}})

            if path == "/api/android" and method == "DELETE":
                row_id = self._qs().get("id", [None])[0]
                if row_id is None:
                    return self._json(400, {"error": "id requis"})
                conn.execute("DELETE FROM android_devices WHERE id=?", (row_id,))
                conn.commit()
                return self._json(200, {"ok": True, "supprime": row_id})

            if path == "/api/tablettes" and method == "GET":
                rows = [dict(r) for r in conn.execute(
                    "SELECT id, nom, modele, device_id, serial, type, actif, dernier_seen "
                    "FROM tablettes ORDER BY dernier_seen DESC")]
                return self._json(200, {"tablettes": rows})

            if path == "/api/tablettes/detect" and method == "POST":
                body = self._body()
                nom = (body.get("nom") or "").strip()
                modele = (body.get("modele") or "").strip()
                device_id = (body.get("device_id") or body.get("android_id") or "").strip()
                serial = (body.get("serial") or "").strip()
                type_dev = (body.get("type") or "autre").strip()
                if not device_id:
                    return self._json(400, {"error": "device_id requis"})
                maintenant = time.strftime("%Y-%m-%d %H:%M:%S")
                cur = conn.execute(
                    "INSERT INTO tablettes (nom, modele, device_id, serial, type, actif, dernier_seen) "
                    "VALUES (?, ?, ?, ?, ?, 1, ?) "
                    "ON CONFLICT(device_id) DO UPDATE SET "
                    "nom=excluded.nom, modele=excluded.modele, serial=excluded.serial, "
                    "type=excluded.type, actif=1, dernier_seen=excluded.dernier_seen",
                    (nom, modele, device_id, serial, type_dev, maintenant))
                conn.commit()
                new_id = cur.lastrowid
                if not new_id:
                    r = conn.execute("SELECT id FROM tablettes WHERE device_id=?",
                                     (device_id,)).fetchone()
                    new_id = r["id"] if r else 0
                return self._json(201, {"ok": True, "id": new_id,
                    "tablette": {"id": new_id, "nom": nom, "modele": modele,
                                 "device_id": device_id, "serial": serial, "type": type_dev}})

            if path == "/api/tablettes" and method == "DELETE":
                row_id = self._qs().get("id", [None])[0]
                if row_id is None:
                    return self._json(400, {"error": "id requis"})
                conn.execute("DELETE FROM tablettes WHERE id=?", (row_id,))
                conn.commit()
                return self._json(200, {"ok": True, "supprime": row_id})

            if path == "/api/tables" and method == "GET":
                tables = [r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' "
                    "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
                counts = {t: conn.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]
                          for t in tables}
                return self._json(200, {"tables": counts})

            if path == "/api/rows" and method == "GET":
                table = self._qs().get("table", [""])[0]
                if table not in ALLOWED_TABLES:
                    return self._json(400, {"error": "table inconnue: " + table})
                rows = [dict(r) for r in conn.execute("SELECT * FROM " + table)]
                return self._json(200, {"table": table, "rows": rows})

            if path == "/api/rows" and method == "POST":
                body = self._body()
                table = body.get("table")
                row = body.get("row", {})
                if table not in ALLOWED_TABLES:
                    return self._json(400, {"error": "table inconnue: " + str(table)})
                if not isinstance(row, dict) or not row:
                    return self._json(400, {"error": "row vide"})
                cols = [c for c in row.keys() if c != "id"]
                if not cols:
                    return self._json(400, {"error": "aucune colonne valide"})
                if table == "equipements":
                    # upsert : protege contre les doubles soumissions
                    # (double-clic / retry reseau) sur UNIQUE(type, equip_id)
                    updates = [c for c in cols if c not in ("type", "equip_id")]
                    sql = ("INSERT INTO equipements (%s) VALUES (%s) "
                           "ON CONFLICT(type, equip_id) DO UPDATE SET %s" if updates else
                           "INSERT INTO equipements (%s) VALUES (%s) "
                           "ON CONFLICT(type, equip_id) DO NOTHING") % (
                        ", ".join(cols), ", ".join(["?"] * len(cols)),
                        ", ".join(c + "=excluded." + c for c in updates))
                    params = [row[c] for c in cols]
                else:
                    sql = "INSERT INTO %s (%s) VALUES (%s)" % (
                        table, ", ".join(cols), ", ".join(["?"] * len(cols)))
                    params = [row[c] for c in cols]
                cur = conn.execute(sql, params)
                if table in GAS_DIRTY_TABLES:
                    _mark_dirty(conn, GAS_DIRTY_TABLES[table])
                conn.commit()
                new_id = cur.lastrowid
                if table == "equipements" and not new_id:
                    r = conn.execute(
                        "SELECT id FROM equipements WHERE type=? AND equip_id=?",
                        (row.get("type"), row.get("equip_id"))).fetchone()
                    new_id = r["id"] if r else 0
                return self._json(201, {"ok": True, "id": new_id})


            if path == "/api/rows" and method == "PUT":
                body = self._body()
                table = body.get("table")
                row_id = body.get("id")
                row = body.get("row", {})
                if table not in ALLOWED_TABLES:
                    return self._json(400, {"error": "table inconnue: " + str(table)})
                if row_id is None or not isinstance(row, dict) or not row:
                    return self._json(400, {"error": "id ou row manquant"})
                cols = [c for c in row.keys() if c != "id"]
                sql = "UPDATE %s SET %s WHERE id=?" % (
                    table, ", ".join(c + "=?" for c in cols))
                conn.execute(sql, [row[c] for c in cols] + [row_id])
                if table in GAS_DIRTY_TABLES:
                    _mark_dirty(conn, GAS_DIRTY_TABLES[table])
                conn.commit()
                return self._json(200, {"ok": True})


            if path == "/api/rows" and method == "DELETE":
                table = self._qs().get("table", [""])[0]
                row_id = self._qs().get("id", [""])[0]
                if table not in ALLOWED_TABLES:
                    return self._json(400, {"error": "table inconnue: " + table})
                if not row_id:
                    return self._json(400, {"error": "id manquant"})
                conn.execute("DELETE FROM " + table + " WHERE id=?", (row_id,))
                if table in GAS_DIRTY_TABLES:
                    _mark_dirty(conn, GAS_DIRTY_TABLES[table])
                conn.commit()
                return self._json(200, {"ok": True})

            # NOTE : /api/query (SQL libre) supprime — trou de securite,
            # aucun usage dans l'application.

            # ---- AUTHENTIFICATION ----
            if path == "/api/auth/login" and method == "POST":
                body = self._body()
                ident = (body.get("identifiant") or "").strip()
                pwd = body.get("password") or ""
                if not ident or not pwd:
                    return self._json(400, {"error": "identifiant et mot de passe requis"})
                ip = self.client_address[0]
                blocked, wait = _login_blocked(ident, ip)
                if blocked:
                    return self._json(429, {
                        "error": "trop de tentatives, reessayez dans %d s" % wait,
                        "retry_after": wait})
                row = conn.execute(
                    "SELECT * FROM comptes WHERE email=? OR nom=?",
                    (ident, ident)).fetchone()
                if not row:
                    _login_fail(ident, ip)
                    return self._json(401, {"error": "compte inconnu"})
                _, h = hash_password(pwd, row["salt"])
                if not hmac.compare_digest(h, row["hash"] or ""):
                    _login_fail(ident, ip)
                    return self._json(401, {"error": "mot de passe incorrect"})
                _login_ok(ident, ip)
                token = session_create(dict(row))
                return self._json(200, {
                    "token": token,
                    "user": {"id": row["id"], "nom": row["nom"],
                             "email": row["email"], "role": row["role"]},
                })

            if path == "/api/auth/logout" and method == "POST":
                session_drop(self._token())
                return self._json(200, {"ok": True})

            if path == "/api/auth/me" and method == "GET":
                s = session_get(self._token())
                if not s:
                    return self._json(401, {"error": "non connecte"})
                return self._json(200, {"user": {
                    "id": s["compte_id"], "nom": s["nom"],
                    "email": s["email"], "role": s["role"]}})

            # Profil de l'appelant (jeton Firebase ou session) : lu par la passerelle pour autoriser ses ecritures
            if path == "/api/auth/role" and method == "GET":
                ctx = auth_context(self)
                if ctx["via"] == "firebase":
                    role = role_firestore(self._token(), ctx["uid"])
                    if role is None:
                        return self._json(403, {"error": "profil illisible ou compte desactive"})
                    return self._json(200, {"role": role, "email": ctx.get("email", ""), "uid": ctx["uid"]})
                return self._json(200, {"role": normaliser_role(ctx.get("role")), "email": ctx.get("email", "")})

            # ---- COMPTES (panneau Securite) ----
            if path == "/api/comptes":
                s = session_get(self._token())
                if not s:
                    return self._json(401, {"error": "authentification requise"})
                if method == "GET":
                    rows = [dict(r) for r in conn.execute(
                        "SELECT id, nom, email, role FROM comptes ORDER BY nom")]
                    return self._json(200, {"comptes": rows})
                if method == "POST":
                    b = self._body()
                    nom = (b.get("nom") or "").strip()
                    email = (b.get("email") or "").strip()
                    role = b.get("role") or "viewer"
                    pwd = b.get("password") or ""
                    if not nom or not pwd:
                        return self._json(400, {"error": "nom et mot de passe requis"})
                    salt, h = hash_password(pwd)
                    try:
                        cur = conn.execute(
                            "INSERT INTO comptes (nom, email, role, salt, hash) "
                            "VALUES (?,?,?,?,?)", (nom, email, role, salt, h))
                        conn.commit()
                    except sqlite3.IntegrityError:
                        return self._json(409, {"error": "compte deja existant"})
                    return self._json(201, {"ok": True, "id": cur.lastrowid})
                if method == "PUT":
                    b = self._body()
                    row_id = b.get("id")
                    if not row_id:
                        return self._json(400, {"error": "id manquant"})
                    target = conn.execute(
                        "SELECT * FROM comptes WHERE id=?", (row_id,)).fetchone()
                    if not target:
                        return self._json(404, {"error": "compte introuvable"})
                    if s["role"] != "admin":
                        return self._json(403, {"error": "role admin requis"})
                    sets, args = [], []
                    if b.get("role"):
                        sets.append("role=?"); args.append(b["role"])
                    if b.get("password"):
                        salt, h = hash_password(b["password"])
                        sets.append("salt=?"); args.append(salt)
                        sets.append("hash=?"); args.append(h)
                    if not sets:
                        return self._json(400, {"error": "rien a modifier"})
                    conn.execute("UPDATE comptes SET %s WHERE id=?" % ", ".join(sets),
                                 args + [row_id])
                    conn.commit()
                    return self._json(200, {"ok": True})
                if method == "DELETE":
                    row_id = self._qs().get("id", [""])[0]
                    if not row_id:
                        return self._json(400, {"error": "id manquant"})
                    if s["role"] != "admin":
                        return self._json(403, {"error": "role admin requis"})
                    if str(s["compte_id"]) == str(row_id):
                        return self._json(400, {"error": "impossible de supprimer son propre compte"})
                    conn.execute("DELETE FROM comptes WHERE id=?", (row_id,))
                    conn.commit()
                    return self._json(200, {"ok": True})
                return self._json(405, {"error": "methode non supportee"})

            if path == "/api/config":
                if method == "GET":
                    params = {r["cle"]: r["valeur"] for r in conn.execute("SELECT * FROM params")}
                    # ne jamais exposer le token GAS en clair
                    if "gas_token" in params:
                        params["gas_token_set"] = "true"
                        del params["gas_token"]
                    return self._json(200, {"params": params})
                body = self._body()
                params = body.get("params", {})
                for k, v in params.items():
                    conn.execute(
                        "INSERT INTO params (cle, valeur) VALUES (?, ?) "
                        "ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur", (k, str(v)))
                conn.commit()
                return self._json(200, {"ok": True})

            if path == "/api/alerts" and method == "GET":
                rows = [dict(r) for r in conn.execute(
                    "SELECT * FROM alertes ORDER BY id DESC LIMIT 100")]
                return self._json(200, {"alerts": rows})

            if path == "/api/alerts" and method == "POST":
                a = self._body()
                cur = conn.execute(
                    "INSERT INTO alertes (text, type, cat, engin) VALUES (?,?,?,?)",
                    (a.get("text", ""), a.get("type", "warn"),
                     a.get("cat", "chemin"), a.get("engin", "")))
                conn.commit()
                return self._json(201, {"ok": True, "id": cur.lastrowid})

            if path == "/api/alerts" and method == "DELETE":
                row_id = self._qs().get("id", [""])[0]
                if row_id:
                    conn.execute("DELETE FROM alertes WHERE id=?", (row_id,))
                else:
                    conn.execute("DELETE FROM alertes")
                conn.commit()
                return self._json(200, {"ok": True})

            if path == "/api/stats" and method == "GET":
                stats = {
                    "engins_actifs": conn.execute(
                        "SELECT COUNT(*) FROM engins WHERE statut='actif'").fetchone()[0],
                    "alertes": conn.execute(
                        "SELECT COUNT(*) FROM alertes WHERE lu=0").fetchone()[0],
                    "minerais_t": conn.execute(
                        "SELECT ROUND(COALESCE(SUM(tonnes),0),1) FROM voyages "
                        "WHERE materiel='ore' AND date(horodatage)=date('now')").fetchone()[0],
                    "waste_t": conn.execute(
                        "SELECT ROUND(COALESCE(SUM(tonnes),0),1) FROM voyages "
                        "WHERE materiel='waste' AND date(horodatage)=date('now')").fetchone()[0],
                    "voyages_today": conn.execute(
                        "SELECT COUNT(*) FROM voyages WHERE date(horodatage)=date('now')").fetchone()[0],
                    "grades": {},
                }
                for mat in ("high", "medium", "low", "misc"):
                    r = conn.execute(
                        "SELECT AVG(grade) g, AVG(tonnes) t FROM voyages WHERE materiel=?",
                        (mat,)).fetchone()
                    stats["grades"][mat] = {
                        "actuel": round(r["g"] or 0, 2),
                        "planned": round(r["t"] or 0, 2),
                    }
                return self._json(200, stats)

            if path == "/api/sync" and method == "GET":
                row = conn.execute("SELECT valeur FROM meta WHERE cle='last_sync'").fetchone()
                conn.execute(
                    "INSERT INTO meta (cle, valeur) VALUES ('last_sync', ?) "
                    "ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur",
                    (time.strftime("%Y-%m-%d %H:%M:%S"),))
                conn.commit()
                return self._json(200, {"last_sync": row["valeur"] if row else None,
                                        "synced_at": time.strftime("%Y-%m-%d %H:%M:%S")})

            # ---- TEST CONNEXION APP SCRIPT ----
            if path == "/api/gas/test" and method == "POST":
                body = self._body()
                gas_url = (body.get("gas_url") or "").strip()
                if not gas_url:
                    gas_url, gas_token = gas_config(conn)
                else:
                    _, gas_token = gas_config(conn)
                if not gas_url:
                    return self._json(400, {"error": "URL App Script manquante (champ ou Settings)"})

                # `action` + `token` en query string : c'est le contrat Apps
                # Script. Le test utilise `sante` (diagnostic de deploiement).
                rep = gas_call("sante", gas_url, gas_token)
                data = rep["data"] or {}
                # La lecture generique (`donnees`) permet de savoir si le
                # deploiement en ligne sait ecrire des referentiels.
                rep2 = gas_call("donnees", gas_url, gas_token,
                                params={"onglet": "flotte"})
                ecriture = rep2["ok"]
                if not ecriture:
                    rep2 = gas_call("donnees", gas_url, gas_token,
                                    params={"onglet": "positions"})
                    ecriture = rep2["ok"]

                return self._json(200, {
                    "ok": rep["ok"],
                    "url": gas_url,
                    "http_status": rep["http_status"],
                    "latency_ms": rep["latency_ms"],
                    "service": data.get("service", ""),
                    "protocole": data.get("protocole", ""),
                    "onglets": data.get("onglets", {}),
                    "pistes": data.get("pistes", ""),
                    "plans": data.get("plans", ""),
                    "base_url": data.get("base_url", ""),
                    "ecriture_supportee": ecriture,
                    "ecriture_message": "" if ecriture else GAS_WRITE_UNSUPPORTED,
                    "erreur": rep["error"],
                    "gas": data,
                })

            # ---- MIGRATION DES ONGLETS (App Script) ----
            # Lance la migration de structuration cote Sheet (identifiants <->
            # equipements, flotte <- dts, creation des onglets manquants).
            # C'est la seule voie pour corriger un Sheet incoherent sans
            # ouvrir l'editeur Apps Script.
            if path == "/api/gas/migrate" and method == "POST":
                gas_url, gas_token = gas_config(conn)
                if not gas_url:
                    return self._json(400, {"error": "gas_url non configure (Settings > Connection)"})
                rep = gas_call("migrer", gas_url, gas_token)
                data = rep["data"] or {}
                return self._json(200 if rep["ok"] else 502, {
                    "ok": rep["ok"],
                    "rapport": data.get("rapport", ""),
                    "onglets": data.get("onglets", {}),
                    "http_status": rep["http_status"],
                    "latency_ms": rep["latency_ms"],
                    "erreur": rep["error"],
                })

            # ---- DIAGNOSTIC DES REFERENTIELS (App Script) ----
            # Detaille les anomalies (doublons, profil sans machine, profil
            # sans membre actif) au lieu du seul message generique.
            if path == "/api/gas/diagnostic" and method == "GET":
                gas_url, gas_token = gas_config(conn)
                if not gas_url:
                    return self._json(400, {"error": "gas_url non configure (Settings > Connection)"})
                rep = gas_call("diagnostic", gas_url, gas_token)
                data = rep["data"] or {}
                return self._json(200 if rep["ok"] else 502, {
                    "ok": rep["ok"],
                    "onglets": data.get("onglets", {}),
                    "profils": data.get("profils", []),
                    "identifiants": data.get("identifiants", []),
                    "flotte": data.get("flotte", []),
                    "anomalies": data.get("anomalies", {}),
                    "referentiels_ok": data.get("referentiels_ok", False),
                    "referentiels_erreur": data.get("referentiels_erreur", ""),
                    "http_status": rep["http_status"],
                    "latency_ms": rep["latency_ms"],
                    "erreur": rep["error"],
                })

            # ---- PULL DEPUIS GOOGLE SHEETS (App Script) ----
            if path == "/api/gas/pull" and method == "POST":
                body = self._body()
                gas_url, gas_token = gas_config(conn)
                if not gas_url:
                    return self._json(400, {"error": "gas_url non configure (Settings > Connection)"})

                resultats, erreurs = gas_pull(conn, gas_url, gas_token)
                now_str = time.strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    "INSERT INTO meta (cle, valeur) VALUES ('last_gas_pull', ?) "
                    "ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur",
                    (now_str,))
                conn.commit()

                counts = {k: v.get("lignes", 0) for k, v in resultats.items()}
                etats = {r["dataset"]: r["etat"] for r in conn.execute(
                    "SELECT dataset, etat FROM sync_state")}
                return self._json(200, {
                    "ok": True,
                    "counts": counts,
                    "total": sum(counts.values()),
                    "errors": erreurs[:20],
                    "resultats": resultats,
                    "etats": etats,
                    "pulled_at": now_str,
                })

            # ---- PUSH VERS GOOGLE SHEETS (App Script) ----
            if path == "/api/gas/push" and method == "POST":
                body = self._body()
                gas_url, gas_token = gas_config(conn)
                if not gas_url:
                    return self._json(400, {"error": "gas_url non configure (Settings > Connection)"})
                datasets = body.get("datasets") or None
                resultats, erreurs = gas_push(conn, gas_url, gas_token, datasets)
                conn.execute(
                    "INSERT INTO meta (cle, valeur) VALUES ('last_gas_push', ?) "
                    "ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur",
                    (time.strftime("%Y-%m-%d %H:%M:%S"),))
                conn.commit()
                return self._json(200, {
                    "ok": not erreurs,
                    "resultats": resultats,
                    "errors": erreurs[:20],
                    "pushed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                })

            # ---- ETAT DE SYNCHRONISATION (comparaison local / en ligne) ----
            if path == "/api/gas/status" and method == "GET":
                gas_url, gas_token = gas_config(conn)
                datasets = []
                for ds in GAS_DATASETS:
                    ligne = {"dataset": ds, "etat": "inconnu", "local_count": 0,
                             "remote_count": 0, "dernier_pull": "",
                             "dernier_push": "", "message": "", "dirty": 0,
                             "local_seul": [], "online_seul": [], "differents": []}
                    row = conn.execute(
                        "SELECT * FROM sync_state WHERE dataset=?", (ds,)).fetchone()
                    if row is not None:
                        ligne.update({
                            "etat": row["etat"], "local_count": row["local_count"],
                            "remote_count": row["remote_count"],
                            "dernier_pull": row["dernier_pull"],
                            "dernier_push": row["dernier_push"],
                            "message": row["message"], "dirty": row["dirty"]})
                    datasets.append(ligne)

                if not gas_url:
                    for ligne in datasets:
                        ligne["etat"] = "hors_ligne"
                        ligne["message"] = "gas_url non configure"
                    return self._json(200, {"ok": False, "datasets": datasets,
                                            "erreur": "gas_url non configure"})

                if self._qs().get("live", ["1"])[0] != "0":
                    # Un seul lot d'appels GAS en parallele, puis comparaison
                    # locale : AUCUNE ecriture pendant la boucle (une
                    # transaction tenue pendant 50 s de GAS met les autres
                    # requetes en « database is locked »). On ecrit tout a la
                    # fin, en une transaction courte.
                    reps = gas_fetch_all(gas_url, gas_token)
                    ref = reps["referentiels"]

                    maj = []
                    for ligne in datasets:
                        ds = ligne["dataset"]
                        local = local_canon(conn, ds)
                        ligne["local_count"] = len(local)

                        def source(ds=ds):
                            if ds in ("profils", "identifiants", "flotte"):
                                if not ref["ok"]:
                                    return None, ref["error"] or "referentiels indisponibles"
                                data = ref["data"] or {}
                                if ds == "profils":
                                    return canon_profils(data.get("profils")), ""
                                if ds == "identifiants":
                                    return canon_identifiants(data.get("equipements")), ""
                                return canon_flotte(data.get("dts")), ""
                            if ds == "affectation":
                                if not reps["affectation"]["ok"]:
                                    return None, reps["affectation"]["error"] or "affectation indisponible"
                                return canon_affectation((reps["affectation"]["data"] or {}).get("affectation")), ""
                            if ds == "blasts":
                                if not reps["blasts"]["ok"]:
                                    return None, reps["blasts"]["error"] or "blasts indisponibles"
                                return canon_blasts((reps["blasts"]["data"] or {}).get("blasts")), ""
                            if ds == "notifications":
                                if not reps["notifications"]["ok"]:
                                    return None, reps["notifications"]["error"] or "notifications indisponibles"
                                return canon_notifications((reps["notifications"]["data"] or {}).get("notifications")), ""
                            if ds == "positions":
                                if not reps["positions"]["ok"]:
                                    return None, reps["positions"]["error"] or "positions indisponibles"
                                return canon_positions((reps["positions"]["data"] or {}).get("lignes")), ""
                            if ds == "dig_plan":
                                # Dataset particulier : les blasts Dig Plan ne
                                # transitent pas par un onglet lu ici, mais par
                                # la passerelle Senor Geology (blast_upload ->
                                # onglet dig_plan). Le diff reel se fait la-bas.
                                try:
                                    passerelle = "http://localhost:8101"
                                    with urllib.request.urlopen(
                                            passerelle + "/api/ot_diff", timeout=20) as rp:
                                        diff = json.loads(rp.read().decode("utf-8"))
                                except Exception as e:
                                    return None, "passerelle injoignable : %s" % e
                                blasts = diff.get("blasts") or {}
                                nl = len(blasts.get("seulement_local") or [])
                                nol = len(blasts.get("seulement_en_ligne") or [])
                                ndv = len(blasts.get("divergents") or [])
                                local = len(local)  # compteur local_canon
                                remote = local - nl + nol  # equivalent cote online
                                if diff.get("aligne"):
                                    return local, ""
                                return None, ("%d local(s) seul(s), %d en ligne seul(s), "
                                              "%d divergent(s)" % (nl, nol, ndv))
                            return None, "dataset inconnu"

                        remote, err = source()
                        if remote is None:
                            ligne["etat"] = "erreur"
                            ligne["message"] = err
                            maj.append((ds, "erreur", len(local), 0, err))
                            continue
                        cmp = comparer(local, remote)
                        ligne["remote_count"] = cmp["remote_count"]
                        ligne["local_seul"] = cmp["local_seul"]
                        ligne["online_seul"] = cmp["online_seul"]
                        ligne["differents"] = cmp["differents"]
                        ligne["etat"] = "identique" if cmp["identique"] else "different"
                        ligne["message"] = "" if cmp["identique"] else (
                            "%d different(s), %d local(s) seul(s), %d en ligne seule(s)"
                            % (len(cmp["differents"]), len(cmp["local_seul"]),
                               len(cmp["online_seul"])))
                        maj.append((ds, ligne["etat"], cmp["local_count"],
                                    cmp["remote_count"], ligne["message"]))
                    now = time.strftime("%Y-%m-%d %H:%M:%S")
                    for ds, etat, lc, rc, msg in maj:
                        row2 = conn.execute(
                            "SELECT dataset FROM sync_state WHERE dataset=?", (ds,)).fetchone()
                        if row2 is None:
                            conn.execute(
                                "INSERT INTO sync_state (dataset, etat, local_count, "
                                "remote_count, dernier_pull, dernier_push, message, dirty) "
                                "VALUES (?,?,?,?,?,?,?,0)",
                                (ds, etat, lc, rc, now, "", msg))
                        else:
                            conn.execute(
                                "UPDATE sync_state SET etat=?, local_count=?, "
                                "remote_count=?, message=? WHERE dataset=?",
                                (etat, lc, rc, msg, ds))
                conn.commit()
                # ---- DETAIL PAR TYPE D'ENGIN (derive, non persiste) ----
                # La ligne `flotte` melange camions et excavatrices : la page
                # Sync ne « voyait » pas les excavatrices. On decompose la
                # flotte (locale ET en ligne) en deux lignes derivees.
                ref_flotte = (reps["referentiels"]["data"] or {}).get("dts") \
                    if reps["referentiels"]["ok"] else None
                datasets.extend(_flotte_detail(
                    conn, canon_flotte(ref_flotte) if ref_flotte is not None else None))
                return self._json(200, {"ok": True, "datasets": datasets})

            # ---- NOTIFICATION VERS LE SERVEUR (action=notification) ----
            if path == "/api/gas/notification" and method == "POST":
                body = self._body()
                gas_url, gas_token = gas_config(conn)
                if not gas_url:
                    return self._json(400, {"error": "gas_url non configure (Settings > Connection)"})
                message = str(body.get("message") or "").strip()
                if not message:
                    return self._json(400, {"error": "message vide"})
                profil = str(body.get("profil") or "").strip()
                rep = gas_call("notification", gas_url, gas_token, method="POST",
                               payload={"profil": profil, "message": message})
                if not rep["ok"]:
                    return self._json(502, {"ok": False,
                                            "error": rep["error"] or "envoi refuse"})
                return self._json(200, {"ok": True, "gas": rep["data"]})

            # ---- SYNC EQUIPEMENTS (identifiants) VERS GOOGLE SHEETS ----
            # Endpoint historique : il poussait une action GAS fantaisiste
            # (`upsertEquipements`) qui n'existe pas. Il pousse desormais le
            # dataset `identifiants` (onglet de login) via `action=donnees`.
            if path == "/api/sync/equipements" and method == "POST":
                gas_url, gas_token = gas_config(conn)
                if not gas_url:
                    return self._json(400, {"error": "gas_url non configure (Settings > Connection)"})

                lignes = gas_push_rows("identifiants", conn)
                if not lignes:
                    return self._json(400, {
                        "error": "aucun identifiant local : lancez d'abord une "
                                 "recuperation depuis Google Sheets"})
                rep = gas_call("donnees", gas_url, gas_token, method="POST",
                               payload={"onglet": "identifiants", "lignes": lignes})
                if not rep["ok"]:
                    msg = rep["error"] or "ecriture refusee"
                    if "action POST inconnue" in msg or "action inconnue" in msg:
                        msg = GAS_WRITE_UNSUPPORTED
                    return self._json(502, {"ok": False, "error": msg})

                conn.execute(
                    "INSERT INTO meta (cle, valeur) VALUES ('last_equip_sync', ?) "
                    "ON CONFLICT(cle) DO UPDATE SET valeur=excluded.valeur",
                    (time.strftime("%Y-%m-%d %H:%M:%S"),))
                conn.commit()
                return self._json(200, {
                    "ok": True,
                    "count": len(lignes),
                    "synced_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "gas": rep["data"],
                })

            return self._json(404, {"error": "route inconnue: " + path})
        finally:
            conn.close()

    STATIC_EXT = {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".json": "application/json",
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".svg": "image/svg+xml", ".ico": "image/x-icon",
        ".woff": "font/woff", ".woff2": "font/woff2",
        ".geojson": "application/geo+json", ".csv": "text/csv; charset=utf-8",
        ".md": "text/plain; charset=utf-8",
    }

    def serve_static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(BASE_DIR, rel))
        if not full.startswith(BASE_DIR):
            return self._json(403, {"error": "acces refuse"})
        if not os.path.isfile(full):
            return self._json(404, {"error": "fichier introuvable"})
        ext = os.path.splitext(full)[1].lower()
        ctype = self.STATIC_EXT.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


# ------------------------------------------------------------- HTTPS --------
# Certificat auto-signe genere au 1er lancement (data/tls/) et servi en
# parallele du HTTP : https://localhost:8443 — le login et l'API transitent
# chiffrables ; le cert etant auto-signe, le navigateur affiche un avertissement
# a accepter une seule fois (ou deployer un cert d'autorite en production).
import datetime
import ipaddress
import ssl
import threading

TLS_DIR = os.path.join(BASE_DIR, "data", "tls")
TLS_CERT = os.path.join(TLS_DIR, "cert.pem")
TLS_KEY = os.path.join(TLS_DIR, "key.pem")
TLS_PORT = int(os.environ.get("OT_TLS_PORT", 8443))


def ensure_tls_cert():
    """Genere (au besoin) un certificat auto-signe RSA 2048, valable 2 ans."""
    os.makedirs(TLS_DIR, exist_ok=True)
    if os.path.isfile(TLS_CERT) and os.path.isfile(TLS_KEY):
        return
    from cryptography import x509
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, NoEncryption)
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "OreTracking"),
    ])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=730))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.DNSName("localhost"),
                x509.DNSName("*.localhost"),
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    with open(TLS_CERT, "wb") as f:
        f.write(cert.public_bytes(Encoding.PEM))
    with open(TLS_KEY, "wb") as f:
        f.write(key.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    print("  TLS: certificat auto-signe genere -> %s" % TLS_DIR)


def start_tls_server():
    """Sert la meme application en HTTPS sur TLS_PORT (thread daemon)."""
    try:
        ensure_tls_cert()
        httpd = ThreadingHTTPServer(("0.0.0.0", TLS_PORT), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(TLS_CERT, TLS_KEY)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        print("  https://localhost:%d             (application TLS)" % TLS_PORT)
        return httpd
    except Exception as e:
        print("  TLS indisponible: %s" % e)
        return None


def auto_push_dirty(conn):
    """Mailon manquant de la synchro : pousse automatiquement vers le Sheet
    les datasets modifies localement (dirty=1). Appelle periodiquement par
    la boucle de fond ; remplace l'ancien push manuel de la page Sync.
    Silencieux si le GAS n'est pas configure ou si le push echoue (retente
    au prochain cycle)."""
    rows = [dict(r) for r in conn.execute(
        "SELECT dataset FROM sync_state WHERE dirty=1")]
    if not rows:
        return
    gas_url, gas_token = gas_config(conn)
    if not gas_url:
        return
    datasets = [r["dataset"] for r in rows if r["dataset"] in GAS_PUSH_DATASETS]
    if not datasets:
        return
    try:
        resultats, erreurs = gas_push(conn, gas_url, gas_token, datasets=datasets)
        ok = [d for d, r in resultats.items() if not r.get("erreur")]
        if ok:
            conn.commit()
            print("  [sync] push auto: %s" % ", ".join(ok))
    except Exception as e:
        print("  [sync] push auto echec: %s" % e)


def boucle_auto_push():
    """Thread de fond : toutes les 10 s, tente le push des datasets dirty.
    (Le pull 5 s reste cote front ; le push doit etre serveur-side pour
    fonctionner meme si aucune page n'est ouverte.)"""
    while True:
        time.sleep(10)
        try:
            conn = get_conn()
            try:
                auto_push_dirty(conn)
            finally:
                conn.close()
        except Exception as e:
            print("  [sync] boucle push: %s" % e)


def main():
    init_db()
    cfg = load_server_config()
    save_server_config(dict(cfg) | {"port": PORT})
    threading.Thread(target=boucle_auto_push, daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("OreTracking server v2.0")
    print("  http://localhost:%d                  (application)" % PORT)
    print("  http://localhost:%d/html/dashboard.html   (dashboard live)" % PORT)
    print("  http://localhost:%d/api/health       (API)" % PORT)
    start_tls_server()
    print("  DB: %s" % DB_PATH)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nArret du serveur.")


if __name__ == "__main__":
    main()

