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


def test_remove_loc_prompt_decline(monkeypatch, tmp_path, capsys):
    # Preparar carpeta con una imagen
    folder = tmp_path / "a"
    folder.mkdir()
    img = folder / "img.jpg"
    img.write_bytes(b"jpg")

    rem = _write_remove_json(tmp_path, [str(folder)])

    # Simular argv
    monkeypatch.setattr(sys, "argv", ["geotag_cascade_gcv_multi.py", "--remove-loc-file", rem, "--dry-run"])

    # Simular que el usuario responde 'n'
    monkeypatch.setattr('builtins.input', lambda prompt='': 'n')

    with pytest.raises(SystemExit) as se:
        runpy.run_module('geotag_cascade_gcv_multi', run_name='__main__')
    assert se.value.code == 0


def test_remove_loc_prompt_accept(monkeypatch, tmp_path):
    # Preparar carpeta con una imagen
    folder = tmp_path / "b"
    folder.mkdir()
    img = folder / "img.jpg"
    img.write_bytes(b"jpg")

    rem = _write_remove_json(tmp_path, [str(folder)])

    # Simular argv
    monkeypatch.setattr(sys, "argv", ["geotag_cascade_gcv_multi.py", "--remove-loc-file", rem, "--dry-run"])

    # Simular que el usuario responde 'y'
    monkeypatch.setattr('builtins.input', lambda prompt='': 'y')

    with pytest.raises(SystemExit) as se:
        runpy.run_module('geotag_cascade_gcv_multi', run_name='__main__')
    # Exited normally with code 0
    assert se.value.code == 0