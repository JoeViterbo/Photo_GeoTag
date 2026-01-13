import runpy
import sys
import os
import json
import tempfile
import shutil
import pytest

from pathlib import Path


def _write_remove_json(tmp_path, folders):
    p = tmp_path / "remove_loc.json"
    p.write_text(json.dumps(folders, ensure_ascii=False))
    return str(p)


def test_remove_loc_detects_gps_via_exiftool_and_calls_remove(monkeypatch, tmp_path):
    # Preparar carpeta con una imagen
    folder = tmp_path / "c"
    folder.mkdir()
    img = folder / "img.jpg"
    img.write_bytes(b"jpg")

    rem = _write_remove_json(tmp_path, [str(folder)])

    # Simular argv
    monkeypatch.setattr(sys, "argv", ["geotag_cascade_gcv_multi.py", "--remove-loc-file", rem, "--yes"])

    # Forzar find_exiftool y que get_gps_from_exiftool detecte GPS aunque has_gps diga que no
    monkeypatch.setattr('geotag_cascade_gcv_multi.find_exiftool', lambda x=None: "/fake/exiftool")
    monkeypatch.setattr('geotag_cascade_gcv_multi.has_gps', lambda p: False)
    monkeypatch.setattr('geotag_cascade_gcv_multi.get_gps_from_exiftool', lambda p, e: (1.0, 2.0))

    calls = []
    monkeypatch.setattr('geotag_cascade_gcv_multi.remove_gps_exiftool', lambda p, exiftool_path=None: calls.append(p))

    with pytest.raises(SystemExit) as se:
        runpy.run_module('geotag_cascade_gcv_multi', run_name='__main__')
    assert se.value.code == 0

    assert len(calls) == 1
    assert str(img) in calls[0]
