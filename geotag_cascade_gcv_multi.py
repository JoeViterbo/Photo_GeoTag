#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Geotag con Google Cloud Vision (REST SDK) + Web Detection + OCR + pHash,
con plan por rangos (plan.json) como bias principal, herencia por última conocida,
y límite de radio respecto al hint del plan.

Escritura EXIF+XMP lossless con exiftool. Fuerza "touch" tras escribir para que
Synology Photos reindexe.

Requiere:
  pip install exifread geopy tqdm requests google-cloud-vision
  pip install pillow imagehash wikipedia charset-normalizer

NOTAS:
- Usa el SDK oficial de Vision pero con transport="rest" (evita gRPC en NAS).
- No recorre subdirectorios; procesa solo ficheros del path dado.
- Orden de procesamiento por fecha de toma ascendente (EXIF DateTimeOriginal → Image DateTime → mtime).
- plan.json tiene preferencia sobre herencia (si existe).
"""

import os
import re
import csv
import json
import time
import argparse
import subprocess
from typing import Optional, Tuple, Dict, List
from collections import Counter
from datetime import datetime

import exifread
from geopy.geocoders import Nominatim
from geopy.distance import geodesic
from tqdm import tqdm
import requests

# Vision SDK (REST transport)
from google.cloud import vision

# Para pHash
from PIL import Image
import imagehash

# Wikipedia resolver
import wikipedia

# warnings BS4 (si en algún momento se usa HTML)
import warnings
from bs4 import BeautifulSoup  # requiere beautifulsoup4 si se usa
from bs4 import GuessedAtParserWarning
warnings.filterwarnings("ignore", category=GuessedAtParserWarning)

GENERIC_LABELS = {
    "summit","viewpoint","overlook","entrance","exit","ticket","gate","temple",
    "pagoda","church","cathedral","museum","station","bridge","castle","palace",
    "plaza","square","park","garden","street","city","town","village","market",
    "waterfall","beach","mountain","river","lake","island","tower","monument",
    "memorial","statue","building","university","campus","airport","bus station",
    "train station","harbor","port"
}

def is_generic_label(name: str) -> bool:
    s = name.strip().lower()
    if len(s) < 4:
        return True
    # si contiene únicamente palabras genéricas
    words = [w for w in re.split(r"[^a-zA-ZÀ-ÿ']+", s) if w]
    if not words:
        return True
    generic_hits = sum(1 for w in words if w in GENERIC_LABELS)
    return generic_hits >= max(1, len(words))  # todas o casi todas genéricas

def hint_tokens(hint_name: Optional[str]) -> List[str]:
    if not hint_name:
        return []
    toks = [t.lower() for t in re.split(r"[^a-zA-ZÀ-ÿ']+", hint_name) if t.strip()]
    # elimina palabras muy comunes
    stop = {"the","of","de","la","el","los","las","y","and","en","do","da"}
    return [t for t in toks if t not in stop and len(t) >= 3]


# ---------------------- Utilidades EXIF / Fechas --------------------------
def get_exif_tags(path: str):
    with open(path, 'rb') as f:
        return exifread.process_file(f, details=False)

def has_gps(path: str) -> bool:
    try:
        tags = get_exif_tags(path)
        return any(k.startswith('GPS') for k in tags)
    except Exception:
        return False

def photo_timestamp(path: str) -> datetime:
    """Fecha de toma: EXIF DateTimeOriginal → Image DateTime → mtime."""
    try:
        tags = get_exif_tags(path)
        for key in ('EXIF DateTimeOriginal', 'Image DateTime'):
            if key in tags:
                try:
                    return datetime.strptime(str(tags[key]), "%Y:%m:%d %H:%M:%S")
                except Exception:
                    pass
    except Exception:
        pass
    return datetime.fromtimestamp(os.path.getmtime(path))


# ---------------------- exiftool (escritura lossless) ---------------------
def have_exiftool(exiftool_path="exiftool") -> bool:
    try:
        subprocess.run([exiftool_path, "-ver"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return True
    except Exception:
        return False

def write_gps_exiftool(path: str, lat: float, lon: float, note: Optional[str] = None, exiftool_path="exiftool"):
    """
    Escribe GPS en EXIF + XMP. Incluye Refs y VersionID para máxima compatibilidad.
    Usa -P para preservar tiempos y luego hacemos touch() para reindexado.
    """
    latref = "N" if lat >= 0 else "S"
    lonref = "E" if lon >= 0 else "W"
    alat = abs(lat)
    alon = abs(lon)

    cmd = [
        exiftool_path,
        "-overwrite_original", "-P", "-n",
        f"-GPSLatitude={alat}", f"-GPSLongitude={alon}",
        f"-GPSLatitudeRef={latref}", f"-GPSLongitudeRef={lonref}",
        "-GPSVersionID=2.3.0.0",
        f"-XMP:GPSLatitude={lat}", f"-XMP:GPSLongitude={lon}",
    ]
    if note:
        cmd.append(f"-EXIF:UserComment={note}")
    cmd.append(path)
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def touch_file(path: str):
    """Actualiza mtime/atime para que Synology Photos reindexe."""
    try:
        now = time.time()
        os.utime(path, (now, now))
    except Exception:
        pass


# ---------------------- Geocodificación y Hints ---------------------------
_geolocator = Nominatim(user_agent="geo-filler-gcv-boost")

def resolve_hints(hints: Optional[str]) -> List[Tuple[float, float, str]]:
    """Resuelve --hint global (coma separa múltiples hints). Se usa solo el primero."""
    if not hints:
        return []
    out = []
    for raw in hints.split(","):
        h = raw.strip()
        if not h:
            continue
        try:
            loc = _geolocator.geocode(h, timeout=15)
            if loc:
                out.append((loc.latitude, loc.longitude, h))
        except Exception:
            pass
    return out

def build_index_hint_map_from_data(data: List[Dict]) -> Tuple[Dict[int, Tuple[float, float, str]], List[str]]:
    """
    Construye el mapa de índices a hints desde una lista de objetos {range:[start,end], hint:"Cadena"}.
    Geocodifica cada hint tal cual (no separa por comas).
    Devuelve {idx -> (lat, lon, hint)} y lista de errores.
    """
    errors: List[str] = []
    per_index_hint: Dict[int, Tuple[float, float, str]] = {}
    
    if not isinstance(data, list):
        return {}, [f"plan_invalid_format: root must be a list"]

    names: List[str] = []
    ranges: List[Tuple[int, int, str]] = []
    for i, entry in enumerate(data):
        try:
            rng = entry["range"]
            name = str(entry["hint"]).strip()
            if not isinstance(rng, list) or len(rng) != 2:
                errors.append(f"plan_item_{i}_invalid_range")
                continue
            start, end = int(rng[0]), int(rng[1])
            if end < start:
                start, end = end, start
            ranges.append((start, end, name))
            if name not in names:
                names.append(name)
        except Exception:
            errors.append(f"plan_item_{i}_parse_error")

    name2coords: Dict[str, Optional[Tuple[float, float]]] = {}
    for name in names:
        try:
            loc = _geolocator.geocode(name, timeout=15)
            name2coords[name] = (loc.latitude, loc.longitude) if loc else None
            if loc is None:
                errors.append(f"hint_unresolved:{name}")
        except Exception as e:
            errors.append(f"hint_geocode_error:{name}:{e}")
            name2coords[name] = None

    for (start, end, name) in ranges:
        coords = name2coords.get(name)
        if not coords:
            continue
        lat, lon = coords
        for idx in range(start, end + 1):
            per_index_hint[idx] = (lat, lon, name)

    return per_index_hint, errors

def build_index_hint_map_from_file(plan_path: str) -> Tuple[Dict[int, Tuple[float, float, str]], List[str]]:
    """
    Lee plan.json (lista de objetos {range:[start,end], hint:"Cadena compuesta"}).
    Geocodifica cada hint tal cual (no separa por comas).
    Devuelve {idx -> (lat, lon, hint)} y lista de errores.
    """
    try:
        with open(plan_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception as e:
        return {}, [f"plan_read_error:{e}"]
    
    return build_index_hint_map_from_data(data)


# ---------------------- Orden por fecha (no recursivo) --------------------
def list_media_sorted_by_capture(root: str) -> List[str]:
    exts = (".jpg", ".jpeg", ".heic", ".heif", ".tif", ".tiff", ".png",
            ".dng", ".nef", ".cr2", ".arw", ".rw2", ".orf", ".raf", ".srw")
    files = []
    for fn in os.listdir(root):
        fp = os.path.join(root, fn)
        if os.path.isfile(fp) and os.path.splitext(fn)[1].lower() in exts:
            files.append(fp)

    def capture_ts(fp: str) -> datetime:
        try:
            tags = get_exif_tags(fp)
            for key in ('EXIF DateTimeOriginal', 'Image DateTime'):
                if key in tags:
                    try:
                        return datetime.strptime(str(tags[key]), "%Y:%m:%d %H:%M:%S")
                    except Exception:
                        pass
        except Exception:
            pass
        return datetime.fromtimestamp(os.path.getmtime(fp))

    files.sort(key=lambda p: (capture_ts(p), os.path.basename(p).lower()))
    return files


# ---------------------- Resolver nombres → coordenadas --------------------
def to_coords_with_bias(name: str,
                        bias: Optional[Tuple[float,float]] = None,
                        country_hint: Optional[str] = None,
                        max_km_if_bias: Optional[float] = 50.0,
                        must_match_hint_tokens: Optional[List[str]] = None):
    # 0) descarta etiquetas genéricas tipo "summit"
    if is_generic_label(name):
        return None

    query = f"{name} {country_hint}".strip() if country_hint else name

    # 1) Wikipedia: primero 'es', luego 'en'
    for lang in ("es", "en"):
        try:
            wikipedia.set_lang(lang)
            titles = wikipedia.search(query, results=3)
        except Exception:
            continue

        for t in titles:
            try:
                p = wikipedia.page(t, auto_suggest=False)

                # ACCESO SEGURO A COORDENADAS
                try:
                    coords_attr = p.coordinates
                except (KeyError, AttributeError):
                    coords_attr = None

                if not coords_attr:
                    continue

                try:
                    lat, lon = coords_attr
                except Exception:
                    continue

                # respeta bias
                if bias and max_km_if_bias is not None:
                    try:
                        if geodesic(bias, (lat, lon)).km > max_km_if_bias:
                            continue
                    except Exception:
                        pass

                if must_match_hint_tokens:
                    text = (f"{t} {p.title or ''}").lower()
                    if not any(tok in text for tok in must_match_hint_tokens):
                        try:
                            summary = (p.summary or "").lower()
                        except Exception:
                            summary = ""
                        if not any(tok in summary for tok in must_match_hint_tokens):
                            continue

                return (lat, lon, t, f"wikipedia-{lang}")

            except Exception:
                # cualquier problema con esta página → probamos la siguiente
                continue

    # 2) Nominatim (más laxo, pero con las mismas barreras)
    try:
        loc = _geolocator.geocode(query, timeout=15)
        if loc:
            if bias and max_km_if_bias is not None:
                try:
                    if geodesic(bias, (loc.latitude, loc.longitude)).km > max_km_if_bias:
                        return None
                except Exception:
                    pass
            if must_match_hint_tokens:
                text = (query or "").lower()
                if not any(tok in text for tok in must_match_hint_tokens):
                    return None
            return (loc.latitude, loc.longitude, query, "nominatim")
    except Exception:
        pass

    return None


def get_bias_from_plan_or_hint(per_index_hint: Dict[int, Tuple[float,float,str]],
                               idx: int,
                               hint_coords: List[Tuple[float,float,str]]):
    """
    Si hay plan:
      - si idx está en el plan → usa ese hint
      - si idx NO está → usa SIEMPRE el último hint del plan
    Si no hay plan pero hay --hint → usa el primer hint global.
    """
    if per_index_hint:
        if idx in per_index_hint:
            lat, lon, name = per_index_hint[idx]
            return (lat, lon), name
        # fuera de rango → última posición del JSON con hint
        last_idx = max(per_index_hint.keys())
        lat, lon, name = per_index_hint[last_idx]
        return (lat, lon), name

    if hint_coords:
        lat, lon, name = hint_coords[0]
        return (lat, lon), name

    return None, None


# ---------------------- pHash utils ---------------------------------------
_phash_cache: Dict[str, str] = {}
_result_cache: Dict[str, Tuple[float,float,str,str]] = {}  # phash -> (lat, lon, label, source)

def phash_of(path: str) -> Optional[str]:
    try:
        with Image.open(path) as im:
            im = im.convert("RGB")
            return str(imagehash.phash(im))
    except Exception:
        return None


# ---------------------- Bias helper ---------------------------------------
def within_bias(lat: float, lon: float,
                bias: Optional[Tuple[float,float]],
                max_km_bias: Optional[float]) -> bool:
    if bias is None or max_km_bias is None:
        return True
    try:
        return geodesic(bias, (lat, lon)).km <= max_km_bias
    except Exception:
        return True  # en caso de duda, no bloqueamos


# ---------------------- Vision SDK (REST transport) -----------------------
def get_vision_client():
    return vision.ImageAnnotatorClient(transport="rest")

def detect_landmark_gcv_sdk_status(path: str, min_conf: float, timeout_s: float):
    try:
        client = get_vision_client()
        with open(path, "rb") as f:
            image = vision.Image(content=f.read())
        resp = client.landmark_detection(image=image, timeout=timeout_s)
        if resp.error.message:
            return ("error", resp.error.message)
        anns = resp.landmark_annotations
        if not anns:
            return ("empty", None)
        top = anns[0]
        score = getattr(top, "score", 0.0) or 0.0
        if score < min_conf:
            return ("empty", None)
        if top.locations:
            loc = top.locations[0].lat_lng
            return ("ok", (float(loc.latitude), float(loc.longitude), str(top.description), float(score)))
        return ("empty", None)
    except Exception as e:
        return ("error", str(e))

def gcv_web_detection(image_bytes: bytes, timeout_s: float = 20.0):
    try:
        client = get_vision_client()
        image = vision.Image(content=image_bytes)
        resp = client.web_detection(image=image, timeout=timeout_s)
        if resp.error.message:
            return ("error", resp.error.message)
        wd = resp.web_detection
        if not wd:
            return ("empty", None)
        labels = []
        if wd.best_guess_labels:
            labels.extend([x.label for x in wd.best_guess_labels if x.label])
        if wd.web_entities:
            labels.extend([x.description for x in wd.web_entities if x.description])
        # Dedup ordenado
        seen = set(); cand = []
        for s in labels:
            s2 = (s or "").strip()
            if s2 and s2.lower() not in seen:
                cand.append(s2); seen.add(s2.lower())
        if not cand:
            return ("empty", None)
        return ("ok", cand)
    except Exception as e:
        return ("error", str(e))

def gcv_text_detection(image_bytes: bytes, timeout_s: float = 20.0):
    try:
        client = get_vision_client()
        image = vision.Image(content=image_bytes)
        resp = client.text_detection(image=image, timeout=timeout_s)
        if resp.error.message:
            return ("error", resp.error.message)
        anns = resp.text_annotations
        if not anns:
            return ("empty", None)
        full_text = (anns[0].description or "").strip()
        return ("ok", full_text)
    except Exception as e:
        return ("error", str(e))


# ---------------------- Detección automática de tipo JSON -----------------
def detect_json_type(data: List[Dict]) -> str:
    """
    Detecta si el JSON es formato single (range/hint) o multi (name/path/tags).
    Retorna 'single', 'multi' o 'unknown'.
    """
    if not data or not isinstance(data, list) or len(data) == 0:
        return 'unknown'
    
    first = data[0]
    if not isinstance(first, dict):
        return 'unknown'
    
    # Formato multi: tiene 'name' y 'tags'
    if 'name' in first and 'tags' in first:
        return 'multi'
    
    # Formato single: tiene 'range' y 'hint'
    if 'range' in first and 'hint' in first:
        return 'single'
    
    return 'unknown'

# ---------------------- Multi-plan loader ---------------------------------
def load_multi_plan(multi_plan_path: str, base_path: Optional[str] = None) -> List[Dict]:
    """
    Lee plan_multi.json (array de objetos con name, path, tags).
    Devuelve lista de carpetas a procesar.
    Si path está vacío, usa name como nombre de carpeta relativo a base_path.
    """
    try:
        with open(multi_plan_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        
        if not isinstance(data, list):
            raise ValueError("plan_multi.json debe ser un array")
        
        folders = []
        for entry in data:
            name = entry.get("name", "")
            path = entry.get("path", "").strip()
            tags = entry.get("tags", [])
            
            if not name:
                continue
            
            # Si path está vacío, construir desde base_path + name
            if not path:
                if base_path:
                    folder_path = os.path.join(base_path, name)
                else:
                    folder_path = name
            else:
                folder_path = path
            
            folders.append({
                "name": name,
                "path": folder_path,
                "tags": tags
            })
        
        return folders
    except Exception as e:
        raise SystemExit(f"ERROR leyendo plan_multi.json: {e}")

# ---------------------- Núcleo --------------------------------------------
def process_folder(
    root: str,
    hints_str: Optional[str],
    dry_run: bool,
    start_index: int,
    end_index: Optional[int],
    gcv_minconf: float,
    gcv_timeout: float,
    verbose: bool,
    plan_path: Optional[str],
    plan_data: Optional[List[Dict]] = None,
    exiftool_path: str = "exiftool",
    force: bool = False,
):
    # exiftool requerido para escribir (salvo dry-run)
    if not have_exiftool(exiftool_path) and not dry_run:
        raise SystemExit("ERROR: exiftool no está disponible. Instálalo o pasa --exiftool-path.")

    # Ficheros (no recursivo) orden por fecha
    files = list_media_sorted_by_capture(root)

    if verbose:
        print(f"[info] total en carpeta: {len(files)}")
        print(f"[info] start_index: {start_index}")
        print(f"[info] end_index: {end_index if end_index is not None else '(none)'}")
        print(f"[info] force overwrite GPS: {force}")

    # Inicializa known (GPS ya existente)
    known: Dict[str, Optional[Tuple[float, float]]] = {}
    for f in files:
        known[f] = get_gps_from_exif(f) if has_gps(f) else None

    # Hints de plan o global
    per_index_hint: Dict[int, Tuple[float, float, str]] = {}
    plan_errors: List[str] = []
    hint_coords: List[Tuple[float, float, str]] = []
    if plan_data is not None:
        per_index_hint, plan_errors = build_index_hint_map_from_data(plan_data)
    elif plan_path:
        per_index_hint, plan_errors = build_index_hint_map_from_file(plan_path)
    else:
        hint_coords = resolve_hints(hints_str)

    # Semilla de última conocida (si ya hay alguna con GPS)
    last_known: Optional[Tuple[float, float, str]] = None
    for f in files:
        if known[f]:
            lat, lon = known[f]
            last_known = (lat, lon, f"[seed:{os.path.basename(f)}]")
            break
    has_plan = (plan_data is not None) or (plan_path is not None)
    if not has_plan and last_known is None and hint_coords:
        lat, lon, name = hint_coords[0]
        last_known = (lat, lon, f"[seed-hint:{name}]")

    # Logging
    rows = []
    counts = Counter()
    def log(action, fpath, lat=None, lon=None, source=None):
        rows.append({
            "file": fpath,
            "action": action,
            "lat": f"{lat:.6f}" if lat is not None else "",
            "lon": f"{lon:.6f}" if lon is not None else "",
            "source": source or ""
        })
        counts[action] += 1
        if verbose:
            base = os.path.basename(fpath)
            if lat is not None and lon is not None:
                print(f"[{action}] {base} -> {lat:.6f},{lon:.6f} {source or ''}")
            else:
                print(f"[{action}] {base} {source or ''}")

    for msg in plan_errors:
        log("plan_error", "(plan)", source=msg)

    # radio máximo en km alrededor del hint del plan
    DEFAULT_MAX_KM_BIAS = 20.0

    # ---- Recorrido principal ----
    for idx, f in enumerate(tqdm(files, desc="Geotagging"), start=1):
        if idx < start_index:
            log("skip_start_index", f)
            continue
        if end_index is not None and idx > end_index:
            log("skip_end_index", f)
            continue

        # si ya tiene GPS
        if known[f]:
            lat, lon = known[f]
            last_known = (lat, lon, f"[exif:{os.path.basename(f)}]")
            if not force:
                # comportamiento antiguo: se salta la foto
                log("skip_has_gps", f, lat, lon, last_known[2])
                continue
            else:
                # nuevo comportamiento: se fuerza a recalcular, pero dejamos constancia
                log("force_overwrite_has_gps", f, lat, lon, last_known[2])
                # no hacemos continue: dejamos que siga el pipeline y reescriba coords

        # Bias / hint para ESTE índice (plan por rango o --hint global)
        bias, country_hint = get_bias_from_plan_or_hint(per_index_hint, idx, hint_coords)
        hint_toks = hint_tokens(country_hint)
        max_km_bias = DEFAULT_MAX_KM_BIAS if bias is not None else None

        # 1) GCV LANDMARK (REST SDK)
        status, payload = detect_landmark_gcv_sdk_status(f, gcv_minconf, gcv_timeout)
        if status == "ok" and payload:
            lat, lon, name, score = payload

            if not within_bias(lat, lon, bias, max_km_bias):
                log("gcv_too_far_plan", f, lat, lon,
                    f"[gcv:{name}:out_of_range]")
            else:
                note = f"detected:gcv:{name}:{score:.2f}"
                if not dry_run:
                    try:
                        write_gps_exiftool(f, lat, lon, note=note, exiftool_path=exiftool_path)
                        touch_file(f)
                    except Exception as e:
                        log("error_write", f, source=f"exiftool:{e}")
                    else:
                        known[f] = (lat, lon)
                        last_known = (lat, lon, f"[gcv:{name}]")
                        log("write_gcv", f, lat, lon, f"{note} [writer:exiftool]")
                        continue
                else:
                    known[f] = (lat, lon)
                    last_known = (lat, lon, f"[gcv:{name}]")
                    log("write_gcv", f, lat, lon, f"{note} [writer:exiftool]")
                    continue

        elif status == "empty":
            log("gcv_empty", f)
        elif status == "error":
            log("gcv_error", f, source=str(payload))

        # 2) BOOSTERS: pHash → Web Detection → OCR (antes del plan fijo)

        # pHash reuse (pero respetando el bias del plan)
        h = phash_of(f)
        if h and h in _result_cache:
            lat, lon, lab, src = _result_cache[h]

            if not within_bias(lat, lon, bias, max_km_bias):
                log("skip_phash_too_far", f, lat, lon,
                    f"[phash_out_of_range:{lab}]")
            else:
                note = f"reused_from_phash:{lab}:{src}"
                if not dry_run:
                    try:
                        write_gps_exiftool(f, lat, lon, note=note, exiftool_path=exiftool_path)
                        touch_file(f)
                    except Exception as e:
                        log("error_write", f, source=f"exiftool:{e}")
                    else:
                        known[f] = (lat, lon)
                        last_known = (lat, lon, "[phash]")
                        log("write_phash", f, lat, lon, f"{note} [writer:exiftool]")
                        continue
                else:
                    known[f] = (lat, lon)
                    last_known = (lat, lon, "[phash]")
                    log("write_phash", f, lat, lon, f"{note} [writer:exiftool]")
                    continue

        # Web Detection + resolver nombres a coords
        coords = None
        img_bytes = None
        try:
            with open(f, "rb") as fh:
                img_bytes = fh.read()
            wd_status, labels = gcv_web_detection(img_bytes, timeout_s=gcv_timeout)
        except Exception as e:
            wd_status, labels = ("error", str(e))

        if wd_status == "ok" and labels:
            for name in labels[:8]:
                coords = to_coords_with_bias(
                    name,
                    bias=bias,
                    country_hint=country_hint,
                    max_km_if_bias=max_km_bias if bias is not None else None,
                    must_match_hint_tokens=hint_toks if bias is not None else None
                )
                if coords:
                    break

        if coords:
            lat, lon, label, src = coords
            note = f"derived_from_web:{label}:{src}"
            if not dry_run:
                try:
                    write_gps_exiftool(f, lat, lon, note=note, exiftool_path=exiftool_path)
                    touch_file(f)
                except Exception as e:
                    log("error_write", f, source=f"exiftool:{e}")
                else:
                    known[f] = (lat, lon)
                    last_known = (lat, lon, f"[web:{label}]")
                    log("write_web", f, lat, lon, f"{note} [writer:exiftool]")
                    if h:
                        _result_cache[h] = (lat, lon, label, src)
                    continue
            else:
                known[f] = (lat, lon)
                last_known = (lat, lon, f"[web:{label}]")
                log("write_web", f, lat, lon, f"{note} [writer:exiftool]")
                if h:
                    _result_cache[h] = (lat, lon, label, src)
                continue

        # OCR → resolver líneas a coords
        if img_bytes is None:
            ocr_status, text = ("error", "no_image_bytes")
        else:
            try:
                ocr_status, text = gcv_text_detection(img_bytes, timeout_s=gcv_timeout)
            except Exception as e:
                ocr_status, text = ("error", str(e))

        if ocr_status == "ok" and text:
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            lines.sort(key=len, reverse=True)
            for q in lines[:5]:
                q2 = re.sub(r"[^A-Za-zÀ-ÿ0-9 '’&\-,\.]", " ", q)
                coords = to_coords_with_bias(
                    q2,
                    bias=bias,
                    country_hint=country_hint,
                    max_km_if_bias=max_km_bias if bias is not None else None,
                    must_match_hint_tokens=hint_toks if bias is not None else None
                )
                if coords:
                    break

            if coords:
                lat, lon, label, src = coords
                note = f"derived_from_ocr:{label}:{src}"
                if not dry_run:
                    try:
                        write_gps_exiftool(f, lat, lon, note=note, exiftool_path=exiftool_path)
                        touch_file(f)
                    except Exception as e:
                        log("error_write", f, source=f"exiftool:{e}")
                    else:
                        known[f] = (lat, lon)
                        last_known = (lat, lon, f"[ocr:{label}]")
                        log("write_ocr", f, lat, lon, f"{note} [writer:exiftool]")
                        if h:
                            _result_cache[h] = (lat, lon, label, src)
                        continue
                else:
                    known[f] = (lat, lon)
                    last_known = (lat, lon, f"[ocr:{label}]")
                    log("write_ocr", f, lat, lon, f"{note} [writer:exiftool]")
                    if h:
                        _result_cache[h] = (lat, lon, label, src)
                    continue

        # 3) PLAN por rangos (preferente respecto a herencia)
        if has_plan and per_index_hint:
            bias_coords, name = get_bias_from_plan_or_hint(per_index_hint, idx, hint_coords)
            if bias_coords:
                lat, lon = bias_coords
                note = f"assigned_hint_seed_file:{name}"
                if not dry_run:
                    try:
                        write_gps_exiftool(f, lat, lon, note=note, exiftool_path=exiftool_path)
                        touch_file(f)
                    except Exception as e:
                        log("error_write", f, source=f"exiftool:{e}")
                    else:
                        known[f] = (lat, lon)
                        last_known = (lat, lon, f"[seed-hint-file:{name}]")
                        log("write_hint_seed_file", f, lat, lon, f"{note} [writer:exiftool]")
                        continue
                else:
                    known[f] = (lat, lon)
                    last_known = (lat, lon, f"[seed-hint-file:{name}]")
                    log("write_hint_seed_file", f, lat, lon, f"{note} [writer:exiftool]")
                    continue

        # 4) Última conocida (respetando bias)
        if last_known is not None:
            lat, lon, src = last_known

            if not within_bias(lat, lon, bias, max_km_bias):
                log("skip_last_known_too_far", f, lat, lon,
                    f"[last_known_out_of_range:{src}]")
            else:
                note = f"assigned_last_known:{src}"
                if not dry_run:
                    try:
                        write_gps_exiftool(f, lat, lon, note=note, exiftool_path=exiftool_path)
                        touch_file(f)
                    except Exception as e:
                        log("error_write", f, source=f"exiftool:{e}")
                    else:
                        known[f] = (lat, lon)
                        log("write_last_known", f, lat, lon, f"{note} [writer:exiftool]")
                        continue
                else:
                    known[f] = (lat, lon)
                    log("write_last_known", f, lat, lon, f"{note} [writer:exiftool]")
                    continue

        # 5) Semilla tardía con --hint global (solo si NO hay plan)
        if not has_plan and hint_coords:
            lat, lon, name = hint_coords[0]
            note = f"assigned_hint_seed:{name}"
            if not dry_run:
                try:
                    write_gps_exiftool(f, lat, lon, note=note, exiftool_path=exiftool_path)
                    touch_file(f)
                except Exception as e:
                    log("error_write", f, source=f"exiftool:{e}")
                else:
                    known[f] = (lat, lon)
                    last_known = (lat, lon, f"[seed-hint:{name}]")
                    log("write_hint_seed", f, lat, lon, f"{note} [writer:exiftool]")
                    continue
            else:
                known[f] = (lat, lon)
                last_known = (lat, lon, f"[seed-hint:{name}]")
                log("write_hint_seed", f, lat, lon, f"{note} [writer:exiftool]")
                continue

        # 6) Nada aplicable
        log("skip_no_source", f)

    # Guardar CSV: en modo multi-plan, usar nombre único por carpeta
    if plan_data is not None:
        # Modo multi-plan: usar nombre de carpeta en el CSV
        folder_name = os.path.basename(root.rstrip(os.sep))
        safe_name = re.sub(r'[^\w\-_\.]', '_', folder_name)
        out_csv = f"result_{safe_name}.csv"
    else:
        # Modo single: usar result.csv
        out_csv = "result.csv"
    
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["file","action","lat","lon","source"])
        w.writeheader()
        w.writerows(rows)

    # Resumen
    total = sum(counts.values())
    print("\nResumen:")
    print(f"  Archivos en carpeta: {len(files)}")
    print(f"  Eventos registrados: {total}")
    for k in sorted(counts):
        print(f"    {counts[k]:5d} {k}")
    print(f"\nLog guardado en {out_csv}")


# ---------------------- Lectura GPS (util) --------------------------------
def get_gps_from_exif(path: str) -> Optional[Tuple[float, float]]:
    try:
        tags = get_exif_tags(path)
        lat_vals = tags.get('GPS GPSLatitude')
        lon_vals = tags.get('GPS GPSLongitude')
        lat_ref  = tags.get('GPS GPSLatitudeRef')
        lon_ref  = tags.get('GPS GPSLongitudeRef')
        if not (lat_vals and lon_vals):
            return None
        def to_deg(v):
            parts = [float(str(x)) for x in v.values]
            d, m, s = parts
            return d + m/60.0 + s/3600.0
        lat = to_deg(lat_vals); lon = to_deg(lon_vals)
        if lat_ref and str(lat_ref).strip().upper() == 'S': lat = -lat
        if lon_ref and str(lon_ref).strip().upper() == 'W': lon = -lon
        return (lat, lon)
    except Exception:
        return None


# ---------------------- CLI -----------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", default=None, help="Carpeta con fotos (no recursivo). Requerido si no se usa --multi-plan")
    ap.add_argument("--hint", default=None, help="Ubicaciones separadas por coma (se usa el PRIMER hint si no hay plan)")
    ap.add_argument("--file", dest="plan_path", default=None, help="Ruta JSON de plan. Detecta automáticamente formato single (range/hint) o multi (name/path/tags)")
    ap.add_argument("--multi-plan", default=None, help="Ruta JSON multi-carpeta (plan_multi.json). Procesa todas las carpetas listadas. Equivalente a --file con formato multi")
    ap.add_argument("--base-path", default=None, help="Ruta base para construir paths cuando 'path' está vacío en multi-plan")
    ap.add_argument("--dry-run", action="store_true", help="No escribe EXIF (simulación)")
    ap.add_argument("--start-index", type=int, default=1,
                    help="Índice global de foto (ordenada) desde el que empezar a procesar")
    ap.add_argument("--end-index", type=int, default=None,
                    help="Índice global de foto (ordenada) hasta el que procesar (inclusive)")
    ap.add_argument("--gcv-minconf", type=float, default=0.60, help="Confianza mínima para aceptar GCV landmark")
    ap.add_argument("--gcv-timeout", type=float, default=20.0, help="Timeout por foto para Vision (segundos)")
    ap.add_argument("--verbose", action="store_true", help="Imprimir acción por cada foto")
    ap.add_argument("--exiftool-path", default="exiftool", help="Ruta al binario exiftool (p. ej. /opt/bin/exiftool)")
    ap.add_argument("--force", action="store_true",
                    help="Forzar escritura de localización aunque la foto ya tenga GPS")

    args = ap.parse_args()
    
    # Si se especifica --file, detectar automáticamente el tipo de JSON
    plan_file = args.multi_plan or args.plan_path
    json_type = None
    json_data = None
    
    if plan_file:
        if not os.path.exists(plan_file):
            raise SystemExit(f"ERROR: Archivo JSON '{plan_file}' no existe")
        
        try:
            with open(plan_file, "r", encoding="utf-8") as fh:
                json_data = json.load(fh)
            
            if not isinstance(json_data, list):
                raise SystemExit(f"ERROR: El JSON debe ser un array")
            
            json_type = detect_json_type(json_data)
            
            if json_type == 'unknown':
                raise SystemExit(f"ERROR: No se pudo detectar el tipo de JSON. Debe ser formato single (range/hint) o multi (name/path/tags)")
            
            if args.verbose:
                print(f"[info] JSON detectado como formato: {json_type}")
        
        except json.JSONDecodeError as e:
            raise SystemExit(f"ERROR: JSON inválido: {e}")
        except Exception as e:
            raise SystemExit(f"ERROR leyendo JSON: {e}")
    
    # Modo multi-plan: procesar múltiples carpetas
    if json_type == 'multi' or args.multi_plan:
        if json_type != 'multi':
            # Si se usó --multi-plan pero no se detectó como multi, usar load_multi_plan
            folders = load_multi_plan(args.multi_plan, base_path=args.base_path)
        else:
            # Si se detectó automáticamente como multi desde --file
            folders = []
            for entry in json_data:
                name = entry.get("name", "")
                path = entry.get("path", "").strip()
                tags = entry.get("tags", [])
                
                if not name:
                    continue
                
                # Si path está vacío, construir desde base_path + name
                if not path:
                    if args.base_path:
                        folder_path = os.path.join(args.base_path, name)
                    else:
                        folder_path = name
                else:
                    folder_path = path
                
                folders.append({
                    "name": name,
                    "path": folder_path,
                    "tags": tags
                })
        
        if args.verbose:
            print(f"[info] Modo multi-plan: {len(folders)} carpetas a procesar")
        
        total_folders = len(folders)
        for folder_idx, folder in enumerate(folders, start=1):
            folder_path = folder["path"]
            folder_name = folder["name"]
            tags = folder["tags"]
            
            if args.verbose:
                print(f"\n[{folder_idx}/{total_folders}] Procesando: {folder_name}")
                print(f"  Ruta: {folder_path}")
            
            if not os.path.isdir(folder_path):
                print(f"  [WARNING] Carpeta no existe: {folder_path}, saltando...")
                continue
            
            # Procesar esta carpeta con sus tags como plan
            process_folder(
                root=folder_path,
                hints_str=None,  # No usar hint global en modo multi-plan
                dry_run=args.dry_run,
                start_index=args.start_index,
                end_index=args.end_index,
                gcv_minconf=args.gcv_minconf,
                gcv_timeout=args.gcv_timeout,
                verbose=args.verbose,
                plan_path=None,
                plan_data=tags,  # Pasar los tags directamente
                exiftool_path=args.exiftool_path,
                force=args.force,
            )
        
        if args.verbose:
            print(f"\n[info] Procesamiento multi-plan completado")
    
    # Modo single: procesar una sola carpeta
    else:
        if not args.path:
            raise SystemExit("ERROR: Se requiere 'path' o un archivo JSON (--file o --multi-plan)")
        
        if not os.path.isdir(args.path):
            raise SystemExit(f"ERROR: '{args.path}' no es un directorio válido")
        
        # Si hay plan_path o se detectó como single, usar ese plan
        plan_data_for_single = None
        plan_path_for_single = None
        
        if json_type == 'single':
            # Usar los datos del JSON detectado
            plan_data_for_single = json_data
            if args.verbose:
                print(f"[info] Usando plan desde JSON (formato single)")
        elif args.plan_path:
            # Usar el plan_path tradicional
            plan_path_for_single = args.plan_path
        
        process_folder(
            root=args.path,
            hints_str=args.hint,
            dry_run=args.dry_run,
            start_index=args.start_index,
            end_index=args.end_index,
            gcv_minconf=args.gcv_minconf,
            gcv_timeout=args.gcv_timeout,
            verbose=args.verbose,
            plan_path=plan_path_for_single,
            plan_data=plan_data_for_single,
            exiftool_path=args.exiftool_path,
            force=args.force,
        )
