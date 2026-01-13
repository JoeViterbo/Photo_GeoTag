import sys
import types
import pytest

# Inject minimal stubs for optional heavy deps so tests import cleanly
sys.modules.setdefault("exifread", types.SimpleNamespace(process_file=lambda f, details=False: {}))
# geopy.distance.geodesic used in verification; stub to return an object with km attribute
class _DummyDist:
    def __init__(self, a, b):
        self.km = 0.0

def _dummy_geodesic(a, b):
    return _DummyDist(a, b)
sys.modules.setdefault("geopy", types.SimpleNamespace(distance=types.SimpleNamespace(geodesic=_dummy_geodesic), geocoders=types.SimpleNamespace(Nominatim=lambda **k: None)))
# wikipedia stub
sys.modules.setdefault("wikipedia", types.SimpleNamespace(set_lang=lambda l: None, search=lambda q, results=3: [], page=lambda t, auto_suggest=False: types.SimpleNamespace(coordinates=None)))

import geotag_cascade_gcv_multi as g


def test_write_gps_exiftool_success(monkeypatch, tmp_path):
    called = {}

    monkeypatch.setattr(g, "find_exiftool", lambda x=None: "/fake/exiftool")

    def fake_run(cmd):
        called["run"] = called.get("run", 0) + 1
        return (0, "", "")

    monkeypatch.setattr(g, "run_exiftool_cmd", fake_run)
    monkeypatch.setattr(g, "get_gps_from_exif", lambda p: (41.108333, 29.022222))

    p = tmp_path / "img.jpg"
    p.write_bytes(b"jpg")

    assert g.write_gps_exiftool(str(p), 41.108333, 29.022222, note="test") is True
    assert called.get("run", 0) == 1


def test_write_gps_exiftool_retry_then_success(monkeypatch, tmp_path):
    calls = {"run": 0, "verify": 0}

    monkeypatch.setattr(g, "find_exiftool", lambda x=None: "/fake/exiftool")

    def fake_run(cmd):
        calls["run"] += 1
        return (0, "", "")

    def fake_getgps(p):
        calls["verify"] += 1
        # first call returns None, second returns the coords
        return (None if calls["verify"] == 1 else (41.01, 29.02))

    monkeypatch.setattr(g, "run_exiftool_cmd", fake_run)
    monkeypatch.setattr(g, "get_gps_from_exif", fake_getgps)

    p = tmp_path / "img.jpg"
    p.write_bytes(b"jpg")

    assert g.write_gps_exiftool(str(p), 41.01, 29.02, note=None, retries=2) is True
    assert calls["run"] >= 1
    assert calls["verify"] >= 2


def test_write_gps_exiftool_exiftool_json_verify(monkeypatch, tmp_path):
    # exiftool returns JSON with GPS but exifread returns None; should still succeed
    monkeypatch.setattr(g, "find_exiftool", lambda x=None: "/fake/exiftool")

    def fake_run(cmd):
        # if '-j' in cmd, simulate JSON output
        if '-j' in cmd or '-GPS*' in cmd:
            return (0, '[{"GPSLatitude": 41.5, "GPSLongitude": -3.5}]', '')
        return (0, '', '')

    monkeypatch.setattr(g, "run_exiftool_cmd", fake_run)
    monkeypatch.setattr(g, "get_gps_from_exif", lambda p: None)

    p = tmp_path / "img.jpg"
    p.write_bytes(b"jpg")

    assert g.write_gps_exiftool(str(p), 41.5, -3.5, note=None, retries=1) is True


def test_get_gps_from_exiftool_gpsposition(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "find_exiftool", lambda x=None: "/fake/exiftool")

    def fake_run(cmd):
        # simulate exiftool -j -n -GPS*
        if '-j' in cmd and any('-GPS*' in a for a in cmd):
            return (0, '[{"GPSPosition": "41.123 2.345"}]', '')
        return (0, '', '')

    monkeypatch.setattr(g, "run_exiftool_cmd", fake_run)

    p = tmp_path / "img.jpg"
    p.write_bytes(b"jpg")

    assert g.get_gps_from_exiftool(str(p), "/fake/exiftool") == (41.123, 2.345)


def test_write_gps_exiftool_verify_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "find_exiftool", lambda x=None: "/fake/exiftool")
    monkeypatch.setattr(g, "run_exiftool_cmd", lambda cmd: (0, "", ""))
    monkeypatch.setattr(g, "get_gps_from_exif", lambda p: None)

    p = tmp_path / "img.jpg"
    p.write_bytes(b"jpg")

    with pytest.raises(RuntimeError):
        g.write_gps_exiftool(str(p), 41.01, 29.02, note=None, retries=1)


def test_write_gps_exiftool_no_exiftool(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "find_exiftool", lambda x=None: None)

    p = tmp_path / "img.jpg"
    p.write_bytes(b"jpg")

    with pytest.raises(RuntimeError):
        g.write_gps_exiftool(str(p), 41.01, 29.02, note=None)


def test_remove_gps_exiftool_success(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "find_exiftool", lambda x=None: "/fake/exiftool")
    monkeypatch.setattr(g, "run_exiftool_cmd", lambda cmd: (0, "", ""))
    monkeypatch.setattr(g, "get_gps_from_exif", lambda p: None)
    monkeypatch.setattr(g, "get_gps_from_exiftool", lambda p, e: None)

    p = tmp_path / "img.jpg"
    p.write_bytes(b"jpg")

    assert g.remove_gps_exiftool(str(p)) is True


def test_remove_gps_exiftool_verify_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "find_exiftool", lambda x=None: "/fake/exiftool")

    calls = []
    def fake_run(cmd):
        # record calls for diagnostics
        calls.append(cmd)
        # Diagnostic query includes literal '-GPS*'
        if any(arg == '-GPS*' for arg in cmd):
            # simulate exiftool returning JSON showing GPS still present
            return (0, '[{"GPSLatitude": 41.0, "GPSLongitude": 2.0}]', '')
        # otherwise simulate successful command
        return (0, "", "")

    monkeypatch.setattr(g, "run_exiftool_cmd", fake_run)
    monkeypatch.setattr(g, "get_gps_from_exif", lambda p: (41.0, 2.0))

    p = tmp_path / "img.jpg"
    p.write_bytes(b"jpg")

    with pytest.raises(RuntimeError):
        g.remove_gps_exiftool(str(p), retries=1)

    # ensure we did call the diagnostic '-GPS*' query
    assert any(any(arg == '-GPS*' for arg in cmd) for cmd in calls)
