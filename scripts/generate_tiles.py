"""Genere un fichier MBTiles multi-zooms a partir d'un GeoTIFF.

Usage:
    python generate_tiles.py input.tif output.mbtiles [--minzoom 10] [--maxzoom 16] [--tilesize 256]

Genere un pyramid de tuiles PNG optimise pour le serveur local de tuiles.
"""
import argparse
import math
import sqlite3
import struct
import zlib
import sys
import time
import numpy as np

try:
    import tifffile
except ImportError:
    print("ERREUR: pip install tifffile")
    sys.exit(1)


def read_geotiff_info(path):
    """Lit les infos geographiques d'un GeoTIFF."""
    with tifffile.TiffFile(path) as tif:
        page = tif.pages[0]
        h, w = page.shape[:2]

        tags = {}
        for tag in page.tags.values():
            tags[tag.name] = tag.value

        # Bounds from ModelPixelScaleTag + ModelTiepointTag or GDAL_METADATA
        # Try GeoKeys first
        geo_keys = tags.get('GeoKeyDirectoryTag', None)
        model_tiepoint = tags.get('ModelTiepointTag', None)
        model_scale = tags.get('ModelPixelScaleTag', None)

        # Try GDAL metadata for bounds
        gdal_md = tags.get('GDALMetadata', None)
        bounds = None

        if gdal_md:
            import re
            ulx = re.search(r'upper.left.x\s*=\s*([-\d.]+)', gdal_md)
            uly = re.search(r'upper.left.y\s*=\s*([-\d.]+)', gdal_md)
            lrx = re.search(r'lower.right.x\s*=\s*([-\d.]+)', gdal_md)
            lry = re.search(r'lower.right.y\s*=\s*([-\d.]+)', gdal_md)
            if ulx and uly and lrx and lry:
                bounds = [float(ulx.group(1)), float(uly.group(1)),
                          float(lrx.group(1)), float(lry.group(1))]

        if not bounds and model_tiepoint and model_scale:
            # tiepoint: (x=0, y=0) -> geographic
            tx, ty = model_tiepoint[3], model_tiepoint[4]
            sx, sy = model_scale[0], model_scale[1]
            bounds = [tx, ty, tx + w * sx, ty - h * sy]

        if not bounds:
            # Fallback: try to read from tif.tags
            for tag in tif.pages[0].tags.values():
                if 'Bounds' in tag.name or 'BBOX' in tag.name:
                    pass

            # Last resort: assume WGS84 and scan image
            print("Attention: pas de metadonnees geographiques detectees.")
            print("On suppose WGS84. Ajoutez manuellement les bounds si necessaire.")
            bounds = [-180, 90, 180, -90]

        print(f"Image: {w} x {h} pixels")
        print(f"Bounds: {bounds}")
        print(f"[xmin={bounds[0]}, ymin={bounds[3]}, xmax={bounds[2]}, ymax={bounds[1]}]")

        return w, h, bounds


def lat_to_y_tile(lat, zoom):
    """Convertit latitude en Y tile (XYZ, pas TMS)."""
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    return int((1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * n)


def lon_to_x_tile(lon, zoom):
    """Convertit longitude en X tile."""
    n = 2 ** zoom
    return int((lon + 180.0) / 360.0 * n)


def write_png_256(img_array):
    """Encode un tableau numpy uint8 (H, W, 3 ou 4) en PNG 256x256, retourne bytes."""
    from PIL import Image
    if img_array.ndim == 2:
        img = Image.fromarray(img_array, 'L').convert('RGBA')
    elif img_array.shape[2] == 3:
        img = Image.fromarray(img_array, 'RGB').convert('RGBA')
    elif img_array.shape[2] == 4:
        img = Image.fromarray(img_array, 'RGBA')
    else:
        img = Image.fromarray(img_array[:, :, :3], 'RGB').convert('RGBA')

    img = img.resize((256, 256), Image.LANCZOS)
    import io
    buf = io.BytesIO()
    img.save(buf, format='PNG', optimize=True)
    return buf.getvalue()


def generate_tiles(input_path, output_path, min_zoom, max_zoom, tile_size=256):
    """Genere les tuiles a partir du GeoTIFF."""
    print(f"\nLecture de {input_path}...")
    t0 = time.time()

    w, h, bounds = read_geotiff_info(input_path)

    # bounds format: [xmin, ymax, xmax, ymin] (GIS convention: top-left, bottom-right)
    # = [lon_west, lat_north, lon_east, lat_south]
    xmin, ymax, xmax, ymin = bounds[0], bounds[1], bounds[2], bounds[3]
    if xmin > xmax:
        xmin, xmax = xmax, xmin
    if ymin > ymax:
        ymin, ymax = ymax, ymin

    print(f"Zone: lon [{xmin:.6f}, {xmax:.6f}], lat [{ymin:.6f}, {ymax:.6f}]")

    # Lire l'image source
    print("Chargement de l'image source (peut prendre un moment)...")
    with tifffile.TiffFile(input_path) as tif:
        src = tif.pages[0].asarray()

    print(f"Image chargee: {src.shape}, type={src.dtype} ({time.time()-t0:.1f}s)")

    # Convertir en RGB uint8 si necessaire
    if src.dtype == np.float32 or src.dtype == np.float64:
        lo, hi = np.nanpercentile(src, [2, 98])
        if hi <= lo:
            hi = lo + 1
        src = np.clip((src.astype(np.float32) - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)

    if src.ndim == 2:
        src_rgb = np.stack([src, src, src], axis=-1)
    elif src.shape[2] >= 4:
        src_rgb = src[:, :, :3].copy()
    elif src.shape[2] == 3:
        src_rgb = src.copy()
    else:
        src_rgb = np.stack([src[:,:,0], src[:,:,0], src[:,:,0]], axis=-1)

    del src  # liberer memoire

    # Bounds en pixels source
    src_w, src_h = src_rgb.shape[1], src_rgb.shape[0]

    # Ouvrir la base MBTiles
    print(f"\nCreation de {output_path}...")
    db = sqlite3.connect(output_path)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")
    db.execute("CREATE TABLE IF NOT EXISTS tiles ("
               "zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, "
               "tile_data BLOB)")
    db.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT)")
    db.execute("CREATE INDEX IF NOT EXISTS idx_tiles ON tiles(zoom_level, tile_column, tile_row)")

    total_tiles = 0

    for zoom in range(min_zoom, max_zoom + 1):
        t1 = time.time()
        n = 2 ** zoom

        # Tiles couvrant la zone
        x_min_tile = max(0, lon_to_x_tile(xmin, zoom))
        x_max_tile = min(n - 1, lon_to_x_tile(xmax, zoom))

        # Y tiles (XYZ: 0 = nord)
        y_min_tile = max(0, lat_to_y_tile(ymax, zoom))  # nord (valeur petit)
        y_max_tile = min(n - 1, lat_to_y_tile(ymin, zoom))  # sud (valeur grand)

        # Conversion tile -> lon/lat -> pixel source
        def tile_lon(tx):
            return tx / n * 360.0 - 180.0

        def tile_lat_north(ty):
            return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * ty / n))))

        def tile_lat_south(ty):
            return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (ty + 1) / n))))

        zoom_tiles = 0
        zoom_skipped = 0

        for tx in range(x_min_tile, x_max_tile + 1):
            for ty in range(y_min_tile, y_max_tile + 1):
                # Lon/lat de la tuile
                t_lon_w = tile_lon(tx)
                t_lon_e = tile_lon(tx + 1)
                t_lat_n = tile_lat_north(ty)
                t_lat_s = tile_lat_south(ty)

                # Pixel source correspondant
                px_w = int((t_lon_w - xmin) / (xmax - xmin) * src_w)
                px_e = int((t_lon_e - xmin) / (xmax - xmin) * src_w)
                px_n = int((ymax - t_lat_n) / (ymax - ymin) * src_h)
                px_s = int((ymax - t_lat_s) / (ymax - ymin) * src_h)

                # Clamp
                px_w = max(0, min(src_w, px_w))
                px_e = max(0, min(src_w, px_e))
                px_n = max(0, min(src_h, px_n))
                px_s = max(0, min(src_h, px_s))

                if px_e <= px_w or px_s <= px_n:
                    zoom_skipped += 1
                    continue

                # Extraire la zone source et redimensionner
                patch = src_rgb[px_n:px_s, px_w:px_e]
                if patch.size == 0:
                    zoom_skipped += 1
                    continue

                # Creer image 256x256
                from PIL import Image as PILImage
                pil_img = PILImage.fromarray(patch, 'RGB')
                pil_img = pil_img.resize((tile_size, tile_size), PILImage.LANCZOS)

                # Encoder en PNG
                import io
                buf = io.BytesIO()
                pil_img.save(buf, format='PNG', optimize=True)
                png_data = buf.getvalue()

                # TMS y (inverser)
                tms_y = n - 1 - ty
                db.execute("INSERT INTO tiles VALUES (?, ?, ?, ?)",
                           (zoom, tx, tms_y, png_data))
                zoom_tiles += 1

        db.commit()
        total_tiles += zoom_tiles
        elapsed = time.time() - t1
        print(f"  Zoom {zoom}: {zoom_tiles} tuiles ({x_max_tile-x_min_tile+1}x{y_max_tile-y_min_tile+1}), {elapsed:.1f}s")

    # Metadata
    meta = {
        'name': 'Fond de carte',
        'type': 'overlay',
        'description': 'Fond de carte genere depuis GeoTIFF',
        'version': '1.1',
        'format': 'png',
        'minzoom': str(min_zoom),
        'maxzoom': str(max_zoom),
        'bounds': f'{xmin},{ymin},{xmax},{ymax}'
    }
    for k, v in meta.items():
        db.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", (k, v))

    db.commit()
    db.close()

    elapsed = time.time() - t0
    print(f"\nTermine: {total_tiles} tuiles generees en {elapsed:.1f}s")
    print(f"Fichier: {output_path}")
    print(f"Zoom: {min_zoom}-{max_zoom}")
    print(f"Utilise: python serve_tiles.py 8080")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Genere MBTiles depuis GeoTIFF')
    parser.add_argument('input', help='Fichier GeoTIFF entree')
    parser.add_argument('output', help='Fichier MBTiles sortie')
    parser.add_argument('--minzoom', type=int, default=10, help='Zoom minimum (defaut: 10)')
    parser.add_argument('--maxzoom', type=int, default=16, help='Zoom maximum (defaut: 16)')
    parser.add_argument('--tilesize', type=int, default=256, help='Taille des tuiles (defaut: 256)')
    args = parser.parse_args()
    generate_tiles(args.input, args.output, args.minzoom, args.maxzoom, args.tilesize)
