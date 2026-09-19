"""Serveur de tuiles MBTiles simple pour OreTracking.

Usage: python serve_tiles.py [port]
Sert toutes les fichiers *.mbtiles presents dans ce dossier.

Routes:
    /{z}/{x}/{y}.png          -> 1er .mbtiles (ordre alphabetique)
    /{nom}/{z}/{x}/{y}.png    -> fichier {nom}.mbtiles du dossier
    /list                     -> liste JSON des .mbtiles disponibles
"""
import sys
import json
import glob
import os
import sqlite3
from urllib.parse import unquote
from http.server import HTTPServer, BaseHTTPRequestHandler

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080

_conns = {}


def list_mbtiles():
    return sorted(os.path.basename(f) for f in glob.glob(os.path.join(BASE_DIR, "*.mbtiles")))


def find_mbtiles(name):
    if not name:
        files = list_mbtiles()
        return os.path.join(BASE_DIR, files[0]) if files else None
    candidates = [os.path.join(BASE_DIR, name), os.path.join(BASE_DIR, name + ".mbtiles")]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None


def get_conn(path):
    if path not in _conns:
        _conns[path] = sqlite3.connect(path)
    return _conns[path]


class TileHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        parts = unquote(self.path).split("?", 1)[0].strip("/").split("/")

        if parts and parts[0] == "list":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(json.dumps(list_mbtiles()).encode("utf-8"))
            return

        name = ""
        if len(parts) == 4:
            name, z, x, y = parts[0], parts[1], parts[2], parts[3]
        elif len(parts) == 3:
            z, x, y = parts[0], parts[1], parts[2]
        else:
            self.send_error(404)
            return

        y = y.split(".")[0]
        try:
            z, x, y = int(z), int(x), int(y)
        except ValueError:
            self.send_error(404)
            return

        path = find_mbtiles(name)
        if not path:
            self.send_response(404)
            self.end_headers()
            return

        try:
            conn = get_conn(path)
            row = conn.execute(
                "SELECT tile_data FROM tiles WHERE zoom_level=? AND tile_column=? AND tile_row=?",
                (z, x, (1 << z) - 1 - y)
            ).fetchone()
        except sqlite3.Error:
            row = None

        if row:
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "max-age=3600")
            self.end_headers()
            self.wfile.write(row[0])
        else:
            self.send_error(404)

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    files = list_mbtiles()
    print(f"MBTiles trouves: {files or 'aucun'}")
    print(f"Serving on http://localhost:{PORT}")
    HTTPServer(("", PORT), TileHandler).serve_forever()