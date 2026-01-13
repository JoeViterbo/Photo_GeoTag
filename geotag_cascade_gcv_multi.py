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

import argparse
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import time
from collections import Counter
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import exifread
from geopy.distance import geodesic
from geopy.geocoders import Nominatim
from tqdm import tqdm

# --- Fix Python 3.8: importlib.metadata.packages_distributions ---
try:
    import importlib.metadata as _ilm
    if not hasattr(_ilm, "packages_distributions"):
        import importlib_metadata as _ilm_backport
        _ilm.packages_distributions = _ilm_backport.packages_distributions
except Exception:
    pass

def set_google_credentials(override: bool = True) -> None:
    """
    Establece GOOGLE_APPLICATION_CREDENTIALS desde 'google_geo_tag.json' ubicado en el directorio del script.
    Si override=False y ya existe la variable de entorno, no se modifica.
    Solo se establece si el fichero existe y es un JSON válido.
    """
    script_dir = os.path.dirname(os.path.abspath(__file__))
    _google_cred_file = os.path.join(script_dir, "google_geo_tag.json")
    try:
        if not os.path.exists(_google_cred_file):
            logger.warning(f"GOOGLE_APPLICATION_CREDENTIALS no establecido: no se encontró {_google_cred_file}")
            return
        # Validar JSON (evita usar un fichero corrupto)
        try:
            with open(_google_cred_file, "r", encoding="utf-8") as fh:
                json.load(fh)
        except Exception as e:
            logger.warning(f"No se estableció GOOGLE_APPLICATION_CREDENTIALS: {_google_cred_file} no es un JSON válido: {e}")
            return
        prev = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if prev and prev != _google_cred_file:
            if override:
                logger.info(f"SOBREESCRIBIENDO GOOGLE_APPLICATION_CREDENTIALS: {prev} -> {_google_cred_file}")
            else:
                logger.info(f"GOOGLE_APPLICATION_CREDENTIALS existente ({prev}); no se sobrescribe (override=False)")
                return
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _google_cred_file
        logger.info(f"GOOGLE_APPLICATION_CREDENTIALS establecido en: {_google_cred_file}")
    except Exception as e:
        logger.warning(f"Error al intentar establecer GOOGLE_APPLICATION_CREDENTIALS: {e}")

# Vision SDK (REST transport)
# warnings BS4 (si en algún momento se usa HTML)
import warnings

import imagehash

# Wikipedia resolver
import wikipedia
from bs4 import GuessedAtParserWarning
from google.cloud import vision

# Para pHash
from PIL import Image

warnings.filterwarnings("ignore", category=GuessedAtParserWarning)

# Logging
logging.basicConfig(level=logging.INFO, format='[%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

GENERIC_LABELS = {
    "summit","viewpoint","overlook","entrance","exit","ticket","gate","temple",
    "pagoda","church","cathedral","museum","station","bridge","castle","palace",
    "plaza","square","park","garden","street","city","town","village","market",
    "waterfall","beach","mountain","river","lake","island","tower","monument",
    "memorial","statue","building","university","campus","airport","bus station",
    "train station","harbor","port"
}

# ---------------------- Configuración desde config.json --------------------
DEFAULT_CONFIG = {
    "gcv": {
        "minconf": 0.60,
        "timeout": 20.0
    },
    "geocoding": {
        "timeout": 15.0,
        "max_km_bias": 20.0,
        "max_km_if_bias": 50.0
    },
    "exiftool": {
        "path": "exiftool"
    },
    "output": {
        "csv_prefix": "result"
    }
}

# Default path mappings useful on Synology NAS (source_prefix -> target_prefix)
DEFAULT_PATH_MAPS: List[Tuple[str, str]] = [
    ("/var/services/homes", "/volume1/homes")
]

_config_cache: Optional[Dict] = None

def load_config(config_path: Optional[str] = None) -> Dict:
    """
    Carga configuración desde config.json.
    Si no existe o hay error, usa valores por defecto.
    Si config_path es None, busca config.json en el directorio del script.
    """
    global _config_cache

    if _config_cache is not None:
        return _config_cache

    if config_path is None:
        # Buscar config.json en el mismo directorio que el script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        config_path = os.path.join(script_dir, "config.json")

    config = DEFAULT_CONFIG.copy()

    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as fh:
                user_config = json.load(fh)

            # Merge recursivo de configuración
            def merge_dict(base: Dict, override: Dict) -> Dict:
                result = base.copy()
                for key, value in override.items():
                    if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                        result[key] = merge_dict(result[key], value)
                    else:
                        result[key] = value
                return result

            config = merge_dict(config, user_config)

        except Exception as e:
            logger.warning(f"Error leyendo config.json: {e}. Usando valores por defecto.")

    _config_cache = config
    return config

def get_config_value(section: str, key: str, default=None):
    """Obtiene un valor de configuración de forma segura."""
    config = load_config()
    return config.get(section, {}).get(key, default)

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
def have_exiftool(exiftool_path: Optional[str] = None) -> bool:
    """Detecta si existe exiftool usable (usa `find_exiftool`)."""
    exe = find_exiftool(exiftool_path or get_config_value("exiftool", "path", "exiftool"))
    if not exe:
        return False
    try:
        subprocess.run([exe, "-ver"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        return True
    except Exception:
        return False

def find_exiftool(explicit_path: Optional[str] = None) -> Optional[str]:
    """Encuentra el ejecutable `exiftool`.

    Comprueba `explicit_path`, luego intenta localizar en PATH y en rutas comunes.
    """
    candidates = []
    if explicit_path:
        candidates.append(explicit_path)
    candidates.extend(["exiftool", "/opt/bin/exiftool", "/usr/bin/exiftool", "/usr/local/bin/exiftool"])

    for cand in candidates:
        try:
            if os.path.isabs(cand) and os.path.isfile(cand) and os.access(cand, os.X_OK):
                logger.debug(f"exiftool resolved to absolute path: {cand}")
                return cand
            found = shutil.which(cand)
            if found:
                logger.debug(f"exiftool resolved to: {found}")
                return found
        except Exception:
            continue
    return None

def run_exiftool_cmd(cmd: List[str]) -> Tuple[int, str, str]:
    try:
        cp = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, text=True)
        return cp.returncode, (cp.stdout or "").strip(), (cp.stderr or "").strip()
    except Exception as e:
        return 1, "", str(e)


def _parse_coord_pair_from_string(s: str) -> Optional[Tuple[float, float]]:
    try:
        # puede ser 'lat lon' o 'lat, lon' o con grados/decimales
        nums = re.findall(r"[-+]?[0-9]*\.?[0-9]+", s)
        if len(nums) >= 2:
            return float(nums[0]), float(nums[1])
    except Exception:
        pass
    return None


def get_gps_from_exiftool(path: str, exiftool_exe: str) -> Optional[Tuple[float, float]]:
    """Lee campos GPS desde exiftool de forma robusta buscando varias etiquetas.

    Usa `exiftool -j -n -GPS*` y acepta las diferentes variantes:
      - GPSLatitude / GPSLongitude (numéricas)
      - XMP:GPSLatitude / XMP:GPSLongitude
      - GPSPosition o Composite:GPSPosition (cadena que contiene dos números)
    Devuelve (lat, lon) si se encuentra, o None en caso contrario.
    """
    try:
        cmd = [exiftool_exe, "-j", "-n", "-GPS*", path]
        rc, out, err = run_exiftool_cmd(cmd)
        if rc != 0:
            logger.debug(f"exiftool -j returned rc={rc}, err={err}")
            return None
        if not out:
            return None
        try:
            arr = json.loads(out)
            if not arr or not isinstance(arr, list):
                return None
            rec = arr[0]

            # 1) Campos numéricos directos
            for k in ("GPSLatitude", "GPSLongitude"):
                pass
            lat = rec.get("GPSLatitude")
            lon = rec.get("GPSLongitude")
            if lat is not None and lon is not None:
                try:
                    return float(lat), float(lon)
                except Exception:
                    pass

            # 2) XMP explícito
            lat = rec.get("XMP:GPSLatitude") or rec.get("XMP:GPSLatitudeRef")
            lon = rec.get("XMP:GPSLongitude") or rec.get("XMP:GPSLongitudeRef")
            # Intentar pares XMP
            if lat is not None and lon is not None:
                try:
                    return float(lat), float(lon)
                except Exception:
                    pass

            # 3) GPSPosition / Composite:GPSPosition (cadena)
            for key in ("GPSPosition", "Composite:GPSPosition"):
                v = rec.get(key)
                if v:
                    parsed = _parse_coord_pair_from_string(str(v))
                    if parsed:
                        return parsed

            # 4) Buscar cualquier campo con 'GPSPosition' o similar
            for k, v in rec.items():
                if isinstance(k, str) and 'gps' in k.lower() and isinstance(v, str):
                    parsed = _parse_coord_pair_from_string(v)
                    if parsed:
                        return parsed

            logger.debug(f"exiftool GPS read returned no coords for {path}: out={out!r}")
            return None
        except Exception as e:
            logger.debug(f"Error parsing exiftool JSON output: {e}; out={out}")
            return None
    except Exception as e:
        logger.debug(f"Error running exiftool for GPS read: {e}")
        return None


def write_gps_exiftool(path: str, lat: float, lon: float, note: Optional[str] = None, exiftool_path: Optional[str] = None, retries: int = 2):
    """
    Escribe GPS en EXIF + XMP. Incluye Refs y VersionID para máxima compatibilidad.
    Hace hasta `retries` intentos y valida la escritura leyendo los tags tras cada intento.
    """
    exiftool_exe = find_exiftool(exiftool_path or get_config_value("exiftool", "path", "exiftool"))
    if not exiftool_exe:
        raise RuntimeError("exiftool not found on PATH or common locations")
    logger.debug(f"Using exiftool executable: {exiftool_exe}")

    latref = "N" if lat >= 0 else "S"
    lonref = "E" if lon >= 0 else "W"
    alat = abs(lat)
    alon = abs(lon)

    base_cmd = [
        exiftool_exe,
        "-overwrite_original",
        "-P",
        "-n",
        f"-GPSLatitude={alat}",
        f"-GPSLongitude={alon}",
        f"-GPSLatitudeRef={latref}",
        f"-GPSLongitudeRef={lonref}",
        "-GPSVersionID=2.3.0.0",
        f"-XMP:GPSLatitude={lat}",
        f"-XMP:GPSLongitude={lon}",
    ]
    if note:
        base_cmd.append(f"-EXIF:UserComment={note}")

    cmd = base_cmd + [path]

    attempt = 0
    delay = 0.5
    while attempt <= retries:
        attempt += 1
        rc, out, err = run_exiftool_cmd(cmd)
        if rc != 0:
            # include exiftool stderr for easier debugging
            logger.error(f"exiftool write failed (rc={rc}) stdout={out!r} stderr={err!r}")
            raise RuntimeError(f"exiftool failed(rc={rc}): {err or out}")
        # touch and verify
        touch_file(path)

        # Prefer exiftool-based verification (more reliable than exifread)
        try:
            gps = get_gps_from_exiftool(path, exiftool_exe)
            if gps:
                g_lat, g_lon = gps
                try:
                    if geodesic((lat, lon), (g_lat, g_lon)).km <= 0.1:
                        return True
                except Exception:
                    if abs(lat - g_lat) < 1e-4 and abs(lon - g_lon) < 1e-4:
                        return True
        except Exception as e:
            logger.debug(f"exiftool-based verification error: {e}")

        # Fallback to exifread based verification
        try:
            gps2 = get_gps_from_exif(path)
            if gps2:
                g_lat, g_lon = gps2
                try:
                    if geodesic((lat, lon), (g_lat, g_lon)).km <= 0.1:
                        return True
                except Exception:
                    if abs(lat - g_lat) < 1e-4 and abs(lon - g_lon) < 1e-4:
                        return True
        except Exception:
            pass

        if attempt <= retries:
            logger.debug(f"Verification failed; retrying in {delay}s (attempt {attempt}/{retries}) stdout={out!r} stderr={err!r}")
            time.sleep(delay)
            delay *= 2
            continue
        # if we reach here, verification failed after retries
        logger.error(f"exiftool write verification failed for {path}; last stdout={out!r} stderr={err!r}")
        raise RuntimeError(f"exiftool write verification failed: stdout={out!r} stderr={err!r}")


def remove_gps_exiftool(path: str, exiftool_path: Optional[str] = None, retries: int = 2):
    """Elimina los campos GPS (EXIF + XMP) usando exiftool.

    Intenta hasta `retries` veces y verifica que no quedan tags GPS. Si la verificación falla,
    hace una consulta diagnóstica con exiftool para listar tags GPS y lo registra para depuración.
    """
    exiftool_exe = find_exiftool(exiftool_path or get_config_value("exiftool", "path", "exiftool"))
    if not exiftool_exe:
        raise RuntimeError("exiftool not found on PATH or common locations")

    # Intentar borrar todas las variantes conocidas de campos GPS
    base_cmd = [
        exiftool_exe,
        "-overwrite_original",
        "-P",
        "-n",
        # borra grupo GPS completo y tags comunes en EXIF/XMP
        "-GPS:all=",
        "-GPSLatitude=",
        "-GPSLongitude=",
        "-GPSLatitudeRef=",
        "-GPSLongitudeRef=",
        "-GPSVersionID=",
        "-XMP:GPSLatitude=",
        "-XMP:GPSLongitude=",
        "-XMP:GPSLatitudeRef=",
        "-XMP:GPSLongitudeRef=",
        "-XMP:GPSPosition=",
    ]
    cmd = base_cmd + [path]

    attempt = 0
    delay = 0.5
    last_out = ""
    last_err = ""
    while attempt <= retries:
        attempt += 1
        rc, out, err = run_exiftool_cmd(cmd)
        last_out, last_err = out, err
        if rc != 0:
            logger.error(f"exiftool remove failed (rc={rc}) stdout={out!r} stderr={err!r}")
            raise RuntimeError(f"exiftool failed(rc={rc}): {err or out}")

        # touch and verify
        touch_file(path)

        # Prefer exiftool-based verification (más fiable que exifread)
        try:
            gps = get_gps_from_exiftool(path, exiftool_exe)
            if gps is None:
                try:
                    if not has_gps(path):
                        return True
                except Exception:
                    return True
        except Exception as e:
            logger.debug(f"exiftool-based verification error: {e}")

        # Fallback a exifread
        try:
            if not has_gps(path):
                return True
        except Exception:
            pass

        if attempt <= retries:
            logger.debug(f"Verification failed; retrying in {delay}s (attempt {attempt}/{retries}) stdout={out!r} stderr={err!r}")
            time.sleep(delay)
            delay *= 2
            continue

        # Preparar diagnóstico: listar cualquier tag GPS que aún exista
        try:
            diag_cmd = [exiftool_exe, "-j", "-n", "-GPS*", path]
            rc2, out2, err2 = run_exiftool_cmd(diag_cmd)
            logger.error(f"exiftool remove verification failed for {path}; last stdout={last_out!r} stderr={last_err!r}")
            logger.error(f"Diagnostic exiftool -GPS*: rc={rc2} stdout={out2!r} stderr={err2!r}")
        except Exception as e:
            logger.error(f"Error running diagnostic exiftool query: {e}")

        raise RuntimeError(f"exiftool remove verification failed: stdout={last_out!r} stderr={last_err!r}")


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
    geocode_timeout = get_config_value("geocoding", "timeout", 15.0)
    out = []
    for raw in hints.split(","):
        h = raw.strip()
        if not h:
            continue
        try:
            loc = _geolocator.geocode(h, timeout=geocode_timeout)
            if loc:
                out.append((loc.latitude, loc.longitude, h))
        except Exception:
            pass
    return out

def extract_fallback_from_hint(hint: str) -> Optional[str]:
    """Extrae la parte "country" final tras la última coma del hint (p. ej. "Mostoles, Spain" -> "Spain").
    Retorna None si no hay coma o la parte final está vacía.
    """
    try:
        parts = [p.strip() for p in str(hint).split(",") if p.strip()]
        if not parts:
            return None
        return parts[-1]
    except Exception:
        return None


def build_index_hint_map_from_data(data: List[Dict]) -> Tuple[Dict[int, Tuple[Optional[float], Optional[float], str]], List[str]]:
    """
    Construye el mapa de índices a hints desde una lista de objetos que pueden ser:
      - {'range': [start, end], 'hint': '...'} (compatibilidad backwards)
      - {'last': N, 'hint': '...'}
    Para el caso 'last' las entradas marcan el índice final (inclusive) del bloque; los bloques
    se ordenan por 'last' ascendente y el primer bloque comienza en 1, cada bloque empieza en
    previous_last+1.

    Para cada hint intentamos geocodificar el hint completo; si falla, intentamos geocodificar
    solo la parte "país" (última porción tras la última coma). Si eso tampoco funciona, se
    asigna el hint textual como fallback (sin coordenadas numéricas).

    Devuelve per_index_hint: {idx -> (lat|None, lon|None, used_hint_string)} y lista de errores.
    """
    errors: List[str] = []
    per_index_hint: Dict[int, Tuple[Optional[float], Optional[float], str]] = {}

    items: List[Tuple[int, Optional[int], str]] = []  # (pos, last, hint) pos unused for last-mode

    # 1) Normalizar entradas: aceptar 'range' (backwards) o 'last'
    for i, entry in enumerate(data):
        try:
            name = str(entry.get("hint", "")).strip()
            if not name:
                errors.append(f"plan_item_{i}_missing_hint")
                continue
            if "range" in entry:
                rng = entry["range"]
                if not isinstance(rng, list) or len(rng) != 2:
                    errors.append(f"plan_item_{i}_invalid_range")
                    continue
                start, end = int(rng[0]), int(rng[1])
                if end < start:
                    start, end = end, start
                items.append((start, end, name))  # store as explicit range
            elif "last" in entry:
                last = int(entry["last"])
                items.append((None, last, name))
            else:
                errors.append(f"plan_item_{i}_no_range_or_last")
        except Exception:
            errors.append(f"plan_item_{i}_parse_error")

    # If items contain explicit ranges (start != None), use them directly; otherwise convert 'last' list
    explicit_ranges: List[Tuple[int, int, str]] = [ (s,e,n) for (s,e,n) in items if s is not None ]
    last_only: List[Tuple[int, str]] = [ (e,n) for (s,e,n) in items if s is None ]

    ranges: List[Tuple[int, int, str]] = []

    if explicit_ranges:
        # Just add normalized explicit ranges
        ranges.extend(explicit_ranges)
    elif last_only:
        # Sort by last ascending and build start as previous_last+1 (first starts at 1)
        last_only.sort(key=lambda x: x[0])
        prev_last = 0
        for last, name in last_only:
            start = prev_last + 1
            end = last
            if end < start:
                # malformed; skip with error
                errors.append(f"plan_item_invalid_last:{name}:{last}")
                continue
            ranges.append((start, end, name))
            prev_last = end

    # 2) Geocodificar nombres (primero intentamos el nombre completo, si falla, intentamos el 'country' final)
    name2coords: Dict[str, Optional[Tuple[float, float]]] = {}
    name2_used_name: Dict[str, str] = {}
    geocode_timeout = get_config_value("geocoding", "timeout", 15.0)

    unique_names = []
    for _, _, name in ranges:
        if name not in unique_names:
            unique_names.append(name)

    for name in unique_names:
        try:
            loc = _geolocator.geocode(name, timeout=geocode_timeout)
            if loc:
                name2coords[name] = (loc.latitude, loc.longitude)
                name2_used_name[name] = name
                continue
            # intento fallback con la parte 'country'
            country = extract_fallback_from_hint(name)
            if country and country != name:
                try:
                    loc2 = _geolocator.geocode(country, timeout=geocode_timeout)
                except Exception as e:
                    loc2 = None
                if loc2:
                    name2coords[name] = (loc2.latitude, loc2.longitude)
                    name2_used_name[name] = country
                    errors.append(f"hint_fallback_used:{name}->{country}")
                    continue
            # si llegamos aquí, sin coords
            name2coords[name] = None
            name2_used_name[name] = country or name
            errors.append(f"hint_unresolved:{name}")
        except Exception as e:
            errors.append(f"hint_geocode_error:{name}:{e}")
            name2coords[name] = None
            name2_used_name[name] = extract_fallback_from_hint(name) or name

    # 3) Rellenar per_index_hint; incluso si coords es None, guardamos la cadena usada para fallback
    for (start, end, name) in ranges:
        coords = name2coords.get(name)
        used_name = name2_used_name.get(name, name)
        if coords:
            lat, lon = coords
            for idx in range(start, end + 1):
                per_index_hint[idx] = (float(lat), float(lon), used_name)
        else:
            # No coords: store (None, None, used_name) so que el pipeline sepa que solo tiene hint textual
            for idx in range(start, end + 1):
                per_index_hint[idx] = (None, None, used_name)

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
                        max_km_if_bias: Optional[float] = None,
                        must_match_hint_tokens: Optional[List[str]] = None):
    # Usar valor de configuración si no se especifica
    if max_km_if_bias is None:
        max_km_if_bias = get_config_value("geocoding", "max_km_if_bias", 50.0)
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
    geocode_timeout = get_config_value("geocoding", "timeout", 15.0)
    try:
        loc = _geolocator.geocode(query, timeout=geocode_timeout)
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


def get_bias_from_plan_or_hint(
    per_index_hint: Dict[int, Tuple[Optional[float], Optional[float], str]],
    idx: int,
    hint_coords: List[Tuple[float, float, str]],
):
    """
    Si hay plan (per_index_hint construido a partir de ranges):
      - si idx está en el mapa → usa ese hint (si tiene coords numéricas devuelve bias, si no devuelve None y devuelve solo el nombre para tokens)
      - si idx está después del último índice mapeado → usa el último hint
      - si idx está entre bloques, usa el hint del bloque anterior (el índice mapeado más cercano por la izquierda)
      - si idx es anterior al primer bloque mapeado → NO hace fallback (devuelve None)
    Si no hay plan pero hay --hint → usa el primer hint global.
    """
    if per_index_hint:
        if idx in per_index_hint:
            lat, lon, name = per_index_hint[idx]
            if lat is None or lon is None:
                return None, name
            return (lat, lon), name
        keys = sorted(per_index_hint.keys())
        if not keys:
            return None, None
        # Si el índice está después del último mapeado, usar el último
        if idx > keys[-1]:
            lat, lon, name = per_index_hint[keys[-1]]
            if lat is None or lon is None:
                return None, name
            return (lat, lon), name
        # Buscar el bloque previo más cercano (key < idx)
        left_keys = [k for k in keys if k < idx]
        if left_keys:
            k = max(left_keys)
            lat, lon, name = per_index_hint[k]
            if lat is None or lon is None:
                return None, name
            return (lat, lon), name
        # índice anterior al primer bloque mapeado: no fallback
        return None, None

    if hint_coords:
        lat, lon, name = hint_coords[0]
        return (lat, lon), name

    return None, None


# ---------------------- pHash utils ---------------------------------------
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

    # Formato single: tiene 'range' o 'last' y 'hint'
    if ('range' in first or 'last' in first) and 'hint' in first:
        return 'single'

    return 'unknown'

# ---------------------- Multi-plan loader ---------------------------------
def load_multi_plan(multi_plan_path: str, base_path: Optional[str] = None, path_maps: Optional[List[Tuple[str,str]]] = None) -> List[Dict]:
    """
    Lee plan_multi.json (array de objetos con name, path, tags).
    Devuelve lista de carpetas a procesar.
    Si path está vacío, la entrada se ignora (se requiere 'path' en el JSON).
    path_maps: lista opcional de tuplas (old_prefix, new_prefix) para intentar mapear rutas no existentes (útil en NAS)
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

            # Si path está vacío, ignorar la entrada (se debe usar siempre la propiedad 'path')
            if not path:
                logger.warning(f"Entrada '{name}' no tiene 'path' definido, se ignora")
                continue
            else:
                folder_path = path

            # Resolver ~/$VARS y aplicar path mapping si procede
            folder_path = resolve_folder_path(folder_path, base_path, path_maps)

            folders.append({
                "name": name,
                "path": folder_path,
                "orig_path": entry.get("path", ""),
                "tags": tags
            })

        return folders
    except Exception as e:
        raise SystemExit(f"ERROR leyendo plan_multi.json: {e}")


# ---------------------- Helper: resolución de paths ---------------------


def resolve_folder_path(folder_path: str, base_path: Optional[str], path_maps: Optional[List[Tuple[str,str]]] = None) -> str:
    """Expande variables (~ y $VARS) y, si es relativo, prueba unirlo con base_path.
    Si la ruta no existe y se proporcionan path_maps, intenta aplicar cada reemplazo (old_prefix->new_prefix)
    para localizar la ruta en otro prefijo (útil en NAS donde el path puede cambiar de montaje).
    Devuelve la ruta expandida (puede no existir) para que el flujo principal la valide.
    """
    # Expandir user y variables de entorno
    try:
        p = os.path.expanduser(os.path.expandvars(str(folder_path or "")))
    except Exception:
        p = str(folder_path or "")

    # Si ya existe tal cual (absoluta o relativa), devolverla
    if os.path.isdir(p):
        return p

    # Si parece una ruta relativa y tenemos base_path, probar base_path/join
    if base_path and not os.path.isabs(p):
        cand = os.path.join(base_path, p)
        if os.path.isdir(cand):
            return cand

    # Intentar aplicar path mappings
    if path_maps:
        for old, new in path_maps:
            try:
                if p.startswith(old):
                    cand = p.replace(old, new, 1)
                    if os.path.isdir(cand):
                        return cand
            except Exception:
                continue

    # No más intentos: devolver la ruta expandida
    return p

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
        logger.info(f"total en carpeta: {len(files)}")
        logger.info(f"start_index: {start_index}")
        logger.info(f"end_index: {end_index if end_index is not None else '(none)'}")
        logger.info(f"force overwrite GPS: {force}")

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
                logger.info(f"[{action}] {base} -> {lat:.6f},{lon:.6f} {source or ''}")
            else:
                logger.info(f"[{action}] {base} {source or ''}")

    for msg in plan_errors:
        log("plan_error", "(plan)", source=msg)

    # radio máximo en km alrededor del hint del plan (desde configuración)
    DEFAULT_MAX_KM_BIAS = get_config_value("geocoding", "max_km_bias", 20.0)

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
    csv_prefix = get_config_value("output", "csv_prefix", "result")
    if plan_data is not None:
        # Modo multi-plan: usar nombre de carpeta en el CSV
        folder_name = os.path.basename(root.rstrip(os.sep))
        safe_name = re.sub(r'[^\w\-_\.]', '_', folder_name)
        out_csv = f"{csv_prefix}_{safe_name}.csv"
    else:
        # Modo single: usar result.csv
        out_csv = f"{csv_prefix}.csv"

    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["file","action","lat","lon","source"])
        w.writeheader()
        w.writerows(rows)

    # Resumen
    total = sum(counts.values())
    logger.info("Resumen:")
    logger.info(f"  Archivos en carpeta: {len(files)}")
    logger.info(f"  Eventos registrados: {total}")
    for k in sorted(counts):
        logger.info(f"    {counts[k]:5d} {k}")
    logger.info(f"Log guardado en {out_csv}")


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
    # Cargar configuración para valores por defecto
    config = load_config()
    default_gcv_minconf = get_config_value("gcv", "minconf", 0.60)
    default_gcv_timeout = get_config_value("gcv", "timeout", 20.0)
    default_exiftool_path = get_config_value("exiftool", "path", "exiftool")

    ap.add_argument("--gcv-minconf", type=float, default=default_gcv_minconf, help=f"Confianza mínima para aceptar GCV landmark (default: {default_gcv_minconf})")
    ap.add_argument("--gcv-timeout", type=float, default=default_gcv_timeout, help=f"Timeout por foto para Vision en segundos (default: {default_gcv_timeout})")
    ap.add_argument("--verbose", action="store_true", help="Imprimir acción por cada foto")
    ap.add_argument("--exiftool-path", default=default_exiftool_path, help=f"Ruta al binario exiftool (default: {default_exiftool_path})")
    ap.add_argument("--force", action="store_true",
                    help="Forzar escritura de localización aunque la foto ya tenga GPS")
    ap.add_argument("--path-map", dest="path_map", action="append", default=[],
                    help="Mapear prefijos de ruta en formato OLD:NEW. Repetible. Ej: /var/services/homes:/volume1/homes")
    ap.add_argument("--fail-on-missing", action="store_true", help="Si hay carpetas listadas en plan_multi.json que no existen, salir con error antes de procesar")
    ap.add_argument("--apply-path-maps", action="store_true", help="Aplicar los --path-map (y defaults) directamente en el archivo JSON (se hace backup antes de sobrescribir)")
    ap.add_argument("--no-override-creds", action="store_true", help="No sobrescribir GOOGLE_APPLICATION_CREDENTIALS desde google_geo_tag.json en el script root")
    ap.add_argument("--remove-loc-file", default=None, help="Ruta JSON (array) con carpetas para eliminar geolocalización. Si se pasa se ejecuta y el script termina.")
    ap.add_argument("--yes", action="store_true", help="No pedir confirmación interactiva (útil para ejecuciones no interactivas)")

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
                raise SystemExit("ERROR: El JSON debe ser un array")

            json_type = detect_json_type(json_data)

            if json_type == 'unknown':
                raise SystemExit("ERROR: No se pudo detectar el tipo de JSON. Debe ser formato single (range/hint) o multi (name/path/tags)")

            if args.verbose:
                logger.info(f"JSON detectado como formato: {json_type}")

        except json.JSONDecodeError as e:
            raise SystemExit(f"ERROR: JSON inválido: {e}")
        except Exception as e:
            raise SystemExit(f"ERROR leyendo JSON: {e}")

    # Construir lista de path maps: defaults + user-provided
    path_maps: List[Tuple[str,str]] = []
    # Añadir default mappings útiles en Synology (siempre presentes)
    path_maps.extend(DEFAULT_PATH_MAPS)
    for pm in args.path_map:
        if ":" in pm:
            old, new = pm.split(":", 1)
            path_maps.append((old, new))
        else:
            logger.warning(f"--path-map '{pm}' no tiene formato OLD:NEW y se ignora")

    # Si el usuario solicitó aplicar mappings al archivo JSON en disco, hacerlo ahora y guardar backup
    if args.apply_path_maps and plan_file:
        if not json_data:
            raise SystemExit("ERROR: No hay datos JSON cargados para aplicar mappings")
        changed = False
        for entry in json_data:
            p = entry.get("path")
            if not p or not str(p).strip():
                continue
            for old, new in path_maps:
                if p.startswith(old):
                    np = p.replace(old, new, 1)
                    if np != p:
                        entry["path"] = np
                        changed = True
                        if args.verbose:
                            logger.info(f"Patch JSON: {p} -> {np} (entry: {entry.get('name','(no-name)')})")
        if changed:
            # Backup
            bak = f"{plan_file}.bak.{datetime.now().strftime('%Y%m%dT%H%M%S')}"
            shutil.copy2(plan_file, bak)
            with open(plan_file, "w", encoding="utf-8") as fh:
                json.dump(json_data, fh, ensure_ascii=False, indent=2)
            logger.info(f"Mappings aplicados. Backup guardado en {bak} y JSON actualizado: {plan_file}")
            # Recargar json_data desde archivo actualizado
            with open(plan_file, "r", encoding="utf-8") as fh:
                json_data = json.load(fh)
        else:
            logger.info("No se detectaron cambios al aplicar path_maps sobre el JSON")

    # Establecer credenciales (si el usuario no solicitó lo contrario)
    set_google_credentials(override=not getattr(args, "no_override_creds", False))

    # Modo: eliminación de geolocalización según JSON (remove_loc.json)
    if args.remove_loc_file:
        rem_path = args.remove_loc_file or "remove_loc.json"
        if not os.path.exists(rem_path):
            raise SystemExit(f"ERROR: Archivo JSON '{rem_path}' no existe")
        try:
            with open(rem_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, list):
                raise SystemExit("ERROR: El JSON debe ser un array de rutas de carpetas")
        except json.JSONDecodeError as e:
            raise SystemExit(f"ERROR: JSON inválido en '{rem_path}': {e}")
        except Exception as e:
            raise SystemExit(f"ERROR leyendo '{rem_path}': {e}")

        total_folders = len(data)
        if args.verbose:
            logger.info(f"Remove-loc: {total_folders} carpetas listadas en {rem_path}")

        # Resolver todas las rutas primero y mostrarlas para confirmación
        resolved: List[Tuple[str, str, bool]] = []  # (orig, resolved, exists)
        for idx, folder in enumerate(data, start=1):
            if not isinstance(folder, str) or not folder.strip():
                resolved.append((str(folder), "<invalid>", False))
                continue
            folder_path = resolve_folder_path(folder, args.base_path, path_maps)
            exists = os.path.isdir(folder_path)
            resolved.append((folder, folder_path, exists))

        # Mostrar resumen al usuario
        logger.info("Se van a procesar las siguientes rutas (remove-loc):")
        for i, (orig, rpath, exists) in enumerate(resolved, start=1):
            status = "EXISTS" if exists else "MISSING"
            logger.info(f"  {i}. {rpath}    ({status})  <- orig: {orig}")

        # Confirmación interactiva (si no se pasa --yes)
        if not args.yes:
            try:
                import sys as _sys
                if not _sys.stdin or not _sys.stdin.isatty():
                    raise SystemExit("ERROR: Entorno no interactivo: pasa --yes para confirmar automáticamente")
            except SystemExit:
                raise
            except Exception:
                # si no podemos determinar si es TTY, seguir y preguntar (input puede bloquear)
                pass

            ans = input("¿Continuar y eliminar geolocalización en las carpetas listadas? [y/N]: ").strip().lower()
            if ans not in ("y", "yes"):
                logger.info("Operación cancelada por el usuario")
                raise SystemExit(0)

        # Ejecutar la eliminación en las rutas existentes (con contadores y resumen)
        total_files_checked = 0
        total_with_gps = 0
        total_removed = 0
        total_skipped_no_gps = 0
        total_errors = 0
        missing_folders = 0

        # Resolver exiftool una sola vez para poder usarlo como fallback de detección
        exiftool_exe = find_exiftool(args.exiftool_path or get_config_value("exiftool", "path", "exiftool"))
        if not exiftool_exe and not args.dry_run:
            raise SystemExit("ERROR: exiftool no está disponible. Instálalo o pasa --exiftool-path.")
        if args.verbose:
            logger.info(f"exiftool resolved to: {exiftool_exe}")

        # Comportamiento: forzar eliminación por defecto cuando se usa --remove-loc-file
        force_remove_loc = True

        for idx, (orig, folder_path, exists) in enumerate(resolved, start=1):
            if not exists:
                logger.warning(f"Carpeta no existe: {folder_path}, saltando")
                missing_folders += 1
                continue

            files = list_media_sorted_by_capture(folder_path)
            n_files = len(files)
            total_files_checked += n_files

            if args.verbose:
                logger.info(f"[{idx}/{total_folders}] Procesando carpeta: {folder_path} ({n_files} ficheros)")
            else:
                logger.info(f"Procesando carpeta: {folder_path} ({n_files} ficheros)")

            removed_this_folder = 0
            skipped_this_folder = 0
            errors_this_folder = 0
            checked_this_folder = n_files

            for f in tqdm(files, desc=f"Remove GPS {os.path.basename(folder_path)}"):
                try:
                    # Detectar GPS: preferir exifread (has_gps) y si no hay, usar exiftool como fallback
                    gps_present = False
                    try:
                        if has_gps(f):
                            gps_present = True
                        elif exiftool_exe:
                            try:
                                if get_gps_from_exiftool(f, exiftool_exe):
                                    gps_present = True
                                    if args.verbose:
                                        logger.debug(f"gps detected via exiftool for: {f}")
                            except Exception:
                                # ignore errors from exiftool read
                                pass
                    except Exception:
                        # cualquier error en detección → asumimos no-GPS para no romper el flujo
                        gps_present = False

                    if not gps_present and not force_remove_loc:
                        # Diagnóstico: si verbose y exiftool disponible, muestre salida -GPS* para investigar
                        if args.verbose and exiftool_exe:
                            try:
                                rc2, out2, err2 = run_exiftool_cmd([exiftool_exe, "-j", "-n", "-GPS*", f])
                                logger.info(f"Diagnostic exiftool -GPS* for skipped file {f}: rc={rc2} out={out2!r} err={err2!r}")
                            except Exception as e:
                                logger.debug(f"Error running exiftool diagnostic for {f}: {e}")

                        skipped_this_folder += 1
                        total_skipped_no_gps += 1
                        if args.verbose:
                            logger.info(f"skip no-gps: {f}")
                        continue

                    # Si se solicitó forzar la eliminación, proceder aunque no se detecte GPS (eliminará los mismos tags que el script escribe)
                    if force_remove_loc and not gps_present:
                        if args.verbose:
                            logger.info(f"FORCE remove GPS (no-detect) for: {f}")
                        # seguir adelante y tratar como si tuviera GPS

                    total_with_gps += 1

                    if args.dry_run:
                        logger.info(f"DRY RUN: remover GPS de {f}")
                        continue

                    # Llamada real de eliminación
                    remove_gps_exiftool(f, exiftool_path=args.exiftool_path)
                    removed_this_folder += 1
                    total_removed += 1
                    logger.info(f"removed GPS: {f}")
                except Exception as e:
                    errors_this_folder += 1
                    total_errors += 1
                    logger.error(f"error removing GPS for {f}: {e}")

            # Resumen por carpeta
            logger.info(f"Resumen carpeta [{idx}/{total_folders}]: checked={checked_this_folder} with_gps={removed_this_folder + errors_this_folder} removed={removed_this_folder} skipped_no_gps={skipped_this_folder} errors={errors_this_folder}")

        # Resumen global
        logger.info("--- Remove-loc summary ---")
        logger.info(f"folders_listed={total_folders} missing_folders={missing_folders}")
        logger.info(f"files_checked={total_files_checked} files_with_gps_estimated={total_with_gps} removed={total_removed} skipped_no_gps={total_skipped_no_gps} errors={total_errors}")
        logger.info("Eliminación de geolocalización completada")
        raise SystemExit(0)

    # Modo multi-plan: procesar múltiples carpetas
    if json_type == 'multi' or args.multi_plan:
        mp_path = args.multi_plan or plan_file
        folders = load_multi_plan(mp_path, base_path=args.base_path, path_maps=path_maps)
        if args.verbose:
            logger.info(f"Modo multi-plan: {len(folders)} carpetas a procesar")

        total_folders = len(folders)

        # Preflight: comprobar existencia de carpetas listadas
        missing_folders = [f for f in folders if not os.path.isdir(os.path.expanduser(os.path.expandvars(f.get('path',''))))]
        if missing_folders:
            logger.warning("Carpetas listadas que no existen:")
            for m in missing_folders:
                logger.warning(f"  - {m.get('name','(no-name)')} -> {m.get('path','')}")
            if args.fail_on_missing:
                raise SystemExit("ERROR: Hay carpetas listadas en el plan que no existen (use --path-map o corrija el JSON)")
            else:
                logger.info("Continuando: se saltarán las carpetas que no existan. Usa --fail-on-missing para forzar error")

        for folder_idx, folder in enumerate(folders, start=1):
            folder_path = folder["path"]
            folder_name = folder["name"]
            tags = folder["tags"]

            if args.verbose:
                logger.info(f"[{folder_idx}/{total_folders}] Procesando: {folder_name}")
                logger.info(f"  Ruta: {folder_path}")
                origp = folder.get("orig_path", "")
                if origp and origp != folder_path:
                    logger.info(f"  Resuelto desde: {origp} -> {folder_path}")

            if not os.path.isdir(folder_path):
                logger.warning(f"Carpeta no existe: {folder_path} (entry name: {folder_name}), saltando...")
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
            logger.info("Procesamiento multi-plan completado")

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
                logger.info("Usando plan desde JSON (formato single)")
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
