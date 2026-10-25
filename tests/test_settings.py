"""Tests for settings persistence and corruption recovery."""

from __future__ import annotations

import json

from music_player import settings


def test_load_missing_file_returns_defaults(tmp_path):
    result = settings.load(tmp_path / "settings.json")
    assert result == settings.DEFAULTS


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    data = {"song": "Karma Police", "duration_minutes": 45, "expanded": True}
    settings.save(data, path)

    loaded = settings.load(path)
    assert loaded["song"] == "Karma Police"
    assert loaded["duration_minutes"] == 45
    assert loaded["expanded"] is True


def test_save_is_atomic_and_backup_kept(tmp_path):
    path = tmp_path / "settings.json"
    settings.save({"song": "first"}, path)
    settings.save({"song": "second"}, path)

    backup = tmp_path / "settings.json.bak"
    assert backup.exists()
    assert json.loads(backup.read_text())["song"] == "first"
    assert json.loads(path.read_text())["song"] == "second"
    # No leftover temp files.
    assert list(tmp_path.iterdir()) == sorted([path, backup]) or {p.name for p in tmp_path.iterdir()} == {
        "settings.json",
        "settings.json.bak",
    }


def test_corrupt_file_falls_back_to_backup(tmp_path):
    path = tmp_path / "settings.json"
    settings.save({"song": "good"}, path)
    settings.save({"song": "better"}, path)  # the first save moves into .bak

    # Corrupt the primary file; the backup still holds the previous good save.
    path.write_text("{ this is not json !!!")

    loaded = settings.load(path)
    assert loaded["song"] == "good"


def test_corrupt_file_and_backup_fall_back_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("not json at all")
    backup = tmp_path / "settings.json.bak"
    backup.write_text("[1, 2, 3]")  # valid JSON but not an object

    assert settings.load(path) == settings.DEFAULTS


def test_non_dict_root_recovers(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(["a", "list"]))
    assert settings.load(path) == settings.DEFAULTS


def test_unknown_keys_dropped_missing_keys_defaulted(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"song": "x", "junk": 123}))

    loaded = settings.load(path)
    assert "junk" not in loaded
    assert loaded["duration_minutes"] == settings.DEFAULTS["duration_minutes"]


def test_wrong_types_coerced_or_defaulted(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"duration_minutes": "12", "expanded": True, "song": 99}))

    loaded = settings.load(path)
    assert loaded["duration_minutes"] == 12.0  # numeric string coerced
    assert loaded["expanded"] is True
    assert isinstance(loaded["song"], str)  # int coerced to string


def test_save_creates_parent_directory(tmp_path):
    path = tmp_path / "nested" / "dir" / "settings.json"
    settings.save({"song": "x"}, path)
    assert path.exists()