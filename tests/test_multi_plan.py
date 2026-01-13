import json
import os
import sys

# Evitar fallos de importación en entornos sin dependencias instaladas creando módulos dummy
dummy = type("D", (), {})
sys.modules.setdefault("exifread", dummy)
# geopy submódulos
sys.modules.setdefault("geopy", dummy)
sys.modules.setdefault("geopy.geocoders", dummy)
sys.modules["geopy.geocoders"].Nominatim = lambda user_agent=None: None
sys.modules.setdefault("geopy.distance", dummy)
sys.modules["geopy.distance"].geodesic = lambda a, b: type("G", (), {"km": 0})
sys.modules.setdefault("tqdm", dummy)
sys.modules["tqdm"].tqdm = lambda x, desc=None: x
# PIL / imagehash / wikipedia / bs4 minimal
sys.modules.setdefault("PIL", dummy)
sys.modules.setdefault("PIL.Image", dummy)
sys.modules.setdefault("imagehash", dummy)
sys.modules.setdefault("wikipedia", dummy)
sys.modules.setdefault("bs4", dummy)

from geotag_cascade_gcv_multi import load_multi_plan, resolve_folder_path


def test_resolve_folder_relative_with_base(tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    sub = base / "photos"
    sub.mkdir()

    resolved = resolve_folder_path("photos", base_path=str(base), path_maps=None)
    assert os.path.abspath(resolved) == os.path.abspath(str(sub))


def test_resolve_folder_with_path_map(tmp_path):
    # Simula una ruta antigua que debe mapearse a una ruta real en tmp
    real = tmp_path / "volume1" / "homes" / "user" / "photos"
    real.mkdir(parents=True)
    fake_old = "/var/services/homes/user/photos"
    path_maps = [("/var/services/homes", str(tmp_path / "volume1" / "homes"))]

    resolved = resolve_folder_path(fake_old, base_path=None, path_maps=path_maps)
    assert os.path.abspath(resolved) == os.path.abspath(str(real))


def test_load_multi_plan(tmp_path):
    # Crear dos carpetas: una válida y otra inexistente
    good = tmp_path / "good"
    good.mkdir()

    plan = [
        {
            "name": "Good",
            "path": str(good),
            "tags": [{"last": 2, "hint": "Madrid"}],
        },
        {"name": "Bad", "path": "", "tags": []},
    ]

    plan_file = tmp_path / "plan.json"
    with open(plan_file, "w", encoding="utf-8") as fh:
        json.dump(plan, fh, ensure_ascii=False)

    folders = load_multi_plan(str(plan_file), base_path=None, path_maps=None)
    assert len(folders) == 1
    assert folders[0]["name"] == "Good"
    assert folders[0]["path"] == str(good)
    assert folders[0]["orig_path"] == str(good)
