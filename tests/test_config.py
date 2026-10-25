"""Tests for configuration constants and the island window shell."""

from __future__ import annotations

from PySide6 import QtCore

from music_player import config
from music_player.island import IslandWindow


def test_supported_extensions_cover_common_formats():
    for ext in (".mp3", ".wav", ".flac", ".m4a", ".aac"):
        assert ext in config.SUPPORTED_EXTENSIONS


def test_settings_dir_uses_localappdata():
    path = config.settings_dir()
    assert config.APP_ID in str(path)


def test_island_window_shell(qapp):
    window = IslandWindow()
    window.show()
    assert window.objectName() == "islandWindow"
    assert window._frame.objectName() == "island"  # styled pill surface
    flags = window.windowFlags()
    assert flags & QtCore.Qt.WindowType.WindowStaysOnTopHint
    assert flags & QtCore.Qt.WindowType.FramelessWindowHint
    window.close()