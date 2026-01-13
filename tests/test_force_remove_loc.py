import runpy
import sys
import os
import json
import pytest

from pathlib import Path


def _write_remove_json(tmp_path, folders):
    p = tmp_path / "remove_loc.json"
    p.write_text(json.dumps(folders, ensure_ascii=False))
    return str(p)


def test_force_remove_loc_calls_remove_for_all(monkeypatch, tmp_path):
    folder = tmp_path / "d"
    folder.mkdir()
    img = folder / "img.jpg"
    img.write_bytes(b"jpg")

    rem = _write_remove_json(tmp_path, [str(folder)])

    monkeypatch.setattr(sys, "argv", ["geotag_cascade_gcv_multi.py", "--remove-loc-file", rem, "--yes"])

    # has_gps returns False, but default force_remove_loc should call remove_gps_exiftool anyway
    monkeypatch.setattr('geotag_cascade_gcv_multi.has_gps', lambda p: False)

    calls = []
    monkeypatch.setattr('geotag_cascade_gcv_multi.remove_gps_exiftool', lambda p, exiftool_path=None: calls.append(p))
    monkeypatch.setattr('geotag_cascade_gcv_multi.find_exiftool', lambda x=None: '/fake/exiftool')

    with pytest.raises(SystemExit) as se:
        runpy.run_module('geotag_cascade_gcv_multi', run_name='__main__')
    assert se.value.code == 0
    assert len(calls) == 1
    assert str(img) in calls[0]
