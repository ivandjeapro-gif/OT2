"""Prototype du paquet geo DT (sera integre a Server_Senor_Geology.py)."""
import json, re, hashlib, math, sqlite3, sys

def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None

def flitch_de(numero):
    m = re.match(r"^\s*(\d+(?:[.,]\d+)?)", str(numero or ""))
    return float(m.group(1).replace(",", ".")) if m else None

def type_zone(nom):
    n = str(nom or "")
    if re.search(r"waste", n, re.I): return "waste"
    if re.search(r"rom\s*a?\s*pad", n, re.I): return "rompad"
    return ""

def poly_propre(brut):
    try:
        pts = json.loads(brut or "[]")
    except (TypeError, ValueError):
        return None
    res = []
    for p in pts if isinstance(pts, list) else []:
        if not isinstance(p, (list, tuple)) or len(p) < 2: return None
        x, y = _f(p[0]), _f(p[1])
        if x is None or y is None: return None
        q = [round(x, 3), round(y, 3)]
        if not res or res[-1] != q: res.append(q)
    if len(res) >= 2 and res[0] == res[-1]: res.pop()
    return res if len(res) >= 3 else None

def aire(poly):
    s = 0.0
    for i in range(len(poly)):
        x1, y1 = poly[i]; x2, y2 = poly[(i + 1) % len(poly)]
        s += x1 * y2 - x2 * y1
    return abs(s) / 2

def _orient(a, b, c):
    v = (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])
    return 0 if abs(v) < 1e-9 else (1 if v > 0 else -1)

def auto_intersecte(poly):
    n = len(poly)
    for i in range(n):
        a, b = poly[i], poly[(i+1) % n]
        for j in range(i+1, n):
            if j == i or (j+1) % n == i or (i+1) % n == j: continue
            c, d = poly[j], poly[(j+1) % n]
            if _orient(a,b,c) != _orient(a,b,d) and _orient(c,d,a) != _orient(c,d,b) and 0 not in (_orient(a,b,c),_orient(a,b,d),_orient(c,d,a),_orient(c,d,b)):
                return True
    return False

def construire(conn, tolerance_m=0.0, depart_m=30.0):
    conn.row_factory = sqlite3.Row
    anomalies = []
    sources = {r["id"]: r["nom"] for r in conn.execute("SELECT id, nom FROM sources WHERE actif=1")}
    src_par_nom = {str(v).strip().lower(): v for v in sources.values()}
    blasts = []
    for b in conn.execute("SELECT * FROM blasts WHERE actif=1 ORDER BY numero, id"):
        if str(b["clos_le"] or "").strip():
            continue                                   # blast termine : plus de chargement
        src = src_par_nom.get(str(b["source_nom"] or "").strip().lower(), str(b["source_nom"] or "").strip())
        if not src: anomalies.append({"code": "blast_sans_source", "blast": b["numero"]})
        blocs = []
        for o in conn.execute("SELECT nom, sommets, minerai, destination FROM oreblocs WHERE blast_id=? AND actif=1 ORDER BY nom", (b["id"],)):
            poly = poly_propre(o["sommets"])
            if not poly:
                anomalies.append({"code": "bloc_polygone_invalide", "blast": b["numero"], "bloc": o["nom"]}); continue
            if auto_intersecte(poly):
                anomalies.append({"code": "bloc_auto_intersecte", "blast": b["numero"], "bloc": o["nom"]})
            minerai = 1 if o["minerai"] == 1 else 0
            dest = str(o["destination"] or "").strip()
            if minerai and not dest:
                anomalies.append({"code": "bloc_minerai_sans_destination", "blast": b["numero"], "bloc": o["nom"]})
            blocs.append({"nom": o["nom"], "minerai": minerai, "destination": dest, "poly": poly})
        if not blocs:
            anomalies.append({"code": "blast_sans_polygone", "blast": b["numero"]})
        elif not any(x["minerai"] for x in blocs):
            anomalies.append({"code": "blast_sans_minerai", "blast": b["numero"]})
        blasts.append({"numero": b["numero"], "flitch": flitch_de(b["numero"]), "source": src, "blocs": blocs})
    # chevauchements : deux blasts actifs, meme flitch, polygones identiques ou recouvrants (meme bbox)
    vus = {}
    for bl in blasts:
        for o in bl["blocs"]:
            cle = (bl["flitch"], tuple(map(tuple, o["poly"])))
            if cle in vus and vus[cle] != bl["numero"]:
                anomalies.append({"code": "bloc_en_double", "blast": bl["numero"], "bloc": o["nom"], "avec": vus[cle]})
            vus.setdefault(cle, bl["numero"])
    zones = []
    for d in conn.execute("SELECT nom, sommets, x, y FROM dumping_areas WHERE actif=1 ORDER BY nom"):
        poly = poly_propre(d["sommets"])
        if not poly: anomalies.append({"code": "zone_sans_polygone", "zone": d["nom"]})
        zones.append({"nom": d["nom"], "type": type_zone(d["nom"]), "poly": poly or [], "x": _f(d["x"]), "y": _f(d["y"])})
    zones_ok = {z["nom"] for z in zones}
    consignes = []
    for t in conn.execute("SELECT t.nom AS teneur, t.source_id, d.nom AS zone FROM teneurs t LEFT JOIN dumping_areas d "
                          "ON d.id = t.dumping_area_id AND d.actif=1 WHERE t.actif=1 ORDER BY t.source_id, t.nom"):
        src = sources.get(t["source_id"])
        if not src: continue
        if not t["zone"]:
            anomalies.append({"code": "teneur_sans_zone", "source": src, "teneur": t["teneur"]}); continue
        consignes.append({"source": src, "teneur": str(t["teneur"]).strip(), "zone": t["zone"]})
    cle_cons = {(c["source"].lower(), c["teneur"].upper()) for c in consignes}
    for bl in blasts:
        for o in bl["blocs"]:
            if o["minerai"] and o["destination"] and (str(bl["source"]).lower(), o["destination"].upper()) not in cle_cons:
                anomalies.append({"code": "destination_sans_consigne", "blast": bl["numero"], "bloc": o["nom"], "destination": o["destination"]})
    checkpoints = []
    for c in conn.execute("SELECT nom, x1, y1, x2, y2, COALESCE(interdit_waste, 1) iw, COALESCE(interdit_ore, 0) io "
                          "FROM checkpoints WHERE actif=1 ORDER BY id"):
        a, b = [_f(c["x1"]), _f(c["y1"])], [_f(c["x2"]), _f(c["y2"])]
        if None in a + b or (a == b):
            anomalies.append({"code": "checkpoint_invalide", "checkpoint": c["nom"]}); continue
        checkpoints.append({"nom": c["nom"], "a": [round(a[0], 3), round(a[1], 3)], "b": [round(b[0], 3), round(b[1], 3)],
                            "interdit_waste": 1 if c["iw"] else 0, "interdit_ore": 1 if c["io"] else 0})
    corps = {"v": 1, "tolerance_m": tolerance_m, "depart_m": depart_m, "blasts": blasts, "zones": zones,
             "consignes": consignes, "checkpoints": checkpoints}
    version = hashlib.sha256(json.dumps(corps, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
    corps["version"] = version
    corps["anomalies"] = anomalies
    return corps

if __name__ == "__main__":
    c = sqlite3.connect(sys.argv[1])
    p = construire(c, 0.0, 10.0)
    json.dump(p, open(sys.argv[2], "w"), separators=(",", ":"))
    print("version", p["version"], "blasts", len(p["blasts"]), "blocs", sum(len(b["blocs"]) for b in p["blasts"]),
          "zones", len(p["zones"]), "consignes", len(p["consignes"]), "checkpoints", len(p["checkpoints"]))
    print("taille", len(json.dumps(p, separators=(",", ":"))), "octets")
    from collections import Counter
    print(Counter(a["code"] for a in p["anomalies"]))
    for a in p["anomalies"][:40]: print(a)
