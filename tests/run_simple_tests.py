import json
import os
import sys
import tempfile
from pathlib import Path

# In tests we avoid requiring all optional dependencies by adding minimal dummies
from types import ModuleType

dummy = ModuleType("dummy")
sys.modules.setdefault("exifread", dummy)
# geopy submodules
sys.modules.setdefault("geopy", ModuleType("geopy"))
sys.modules.setdefault("geopy.geocoders", ModuleType("geopy.geocoders"))
sys.modules["geopy.geocoders"].Nominatim = lambda user_agent=None: None
sys.modules.setdefault("geopy.distance", ModuleType("geopy.distance"))
sys.modules["geopy.distance"].geodesic = lambda a, b: type("G", (), {"km": 0})
# tqdm
sys.modules.setdefault("tqdm", ModuleType("tqdm"))
sys.modules["tqdm"].tqdm = lambda x, desc=None: x
# PIL / imagehash / wikipedia / bs4 minimal
sys.modules.setdefault("PIL", ModuleType("PIL"))
sys.modules.setdefault("PIL.Image", ModuleType("PIL.Image"))
sys.modules.setdefault("imagehash", ModuleType("imagehash"))
sys.modules.setdefault("wikipedia", ModuleType("wikipedia"))
bs4_mod = ModuleType("bs4")
bs4_mod.GuessedAtParserWarning = Warning
sys.modules.setdefault("bs4", bs4_mod)
# google cloud vision minimal dummy (proper ModuleType)
google_mod = ModuleType("google")
gcloud_mod = ModuleType("google.cloud")
vision_mod = ModuleType("google.cloud.vision")
vision_mod.ImageAnnotatorClient = lambda transport="rest": None
vision_mod.Image = ModuleType("vision.Image")
sys.modules.setdefault("google", google_mod)
sys.modules.setdefault("google.cloud", gcloud_mod)
sys.modules.setdefault("google.cloud.vision", vision_mod)
# expose attribute
sys.modules["google.cloud"].vision = sys.modules["google.cloud.vision"]

from geotag_cascade_gcv_multi import load_multi_plan, resolve_folder_path


def run():
    # test_resolve_folder_relative_with_base
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "base"
        base.mkdir()
        sub = base / "photos"
        sub.mkdir()

        resolved = resolve_folder_path("photos", base_path=str(base), path_maps=None)
        assert os.path.abspath(resolved) == os.path.abspath(str(sub))

    # test_resolve_folder_with_path_map
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        real = td / "volume1" / "homes" / "user" / "photos"
        real.mkdir(parents=True)
        fake_old = "/var/services/homes/user/photos"
        path_maps = [("/var/services/homes", str(td / "volume1" / "homes"))]

        resolved = resolve_folder_path(fake_old, base_path=None, path_maps=path_maps)
        assert os.path.abspath(resolved) == os.path.abspath(str(real))

    # test_load_multi_plan
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        good = td / "good"
        good.mkdir()

        plan = [
            {
                "name": "Good",
                "path": str(good),
                "tags": [{"range": [1, 2], "hint": "Madrid"}],
            },
            {"name": "Bad", "path": "", "tags": []},
        ]

        plan_file = td / "plan.json"
        with open(plan_file, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, ensure_ascii=False)

        folders = load_multi_plan(str(plan_file), base_path=None, path_maps=None)
        assert len(folders) == 1
        assert folders[0]["name"] == "Good"
        assert folders[0]["path"] == str(good)
        assert folders[0]["orig_path"] == str(good)

    print("All simple tests passed")


if __name__ == "__main__":
    run()
