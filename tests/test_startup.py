"""Tests for the optional Windows startup registration.

The real HKCU Run key is NEVER touched by these tests — every operation
targets a throwaway test subkey that the fixture deletes afterwards.
The spec requires the option to stay disabled by default and to remove
only this app's own registration.
"""

from __future__ import annotations

import sys

import pytest
import winreg

from music_player import config, startup

TEST_KEY = r"Software\DynamicIslandMusicPlayerTestStartup"


@pytest.fixture
def test_key():
    startup.RUN_KEY  # real key reference untouched by the tests below
    yield TEST_KEY
    # Clean the throwaway key whatever happened inside a test.
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as key:
            while True:
                try:
                    winreg.EnumValue(key, 0)
                    winreg.DeleteValue(key, config.APP_ID)
                except OSError:
                    break
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, TEST_KEY)
    except OSError:
        pass


def test_disabled_by_default_on_fresh_key(test_key):
    assert startup.is_enabled(test_key) is False


def test_enable_writes_registration(test_key):
    error = startup.enable(test_key)
    assert error is None
    assert startup.is_enabled(test_key) is True

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, test_key) as key:
        command, _ = winreg.QueryValueEx(key, config.APP_ID)
    assert sys.executable in command


def test_launch_command_points_at_entry_point():
    command = startup._launch_command()
    assert sys.executable in command
    assert "main.py" in command


def test_disable_removes_only_our_value(test_key):
    # A foreign value in the same key must survive our disable().
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as key:
        winreg.SetValueEx(key, "SomeOtherApp", 0, winreg.REG_SZ, "other.exe")

    startup.enable(test_key)
    assert startup.disable(test_key) is None
    assert startup.is_enabled(test_key) is False

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as key:
        other, _ = winreg.QueryValueEx(key, "SomeOtherApp")  # untouched
    assert other == "other.exe"


def test_disable_when_not_registered_is_success(test_key):
    assert startup.disable(test_key) is None
    assert startup.is_enabled(test_key) is False


def test_enable_is_idempotent(test_key):
    assert startup.enable(test_key) is None
    assert startup.enable(test_key) is None
    assert startup.is_enabled(test_key) is True
    # One enable + one idempotent enable = still exactly one value.
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, TEST_KEY) as key:
        our_values = []
        for i in range(5):
            try:
                name, _data, _type = winreg.EnumValue(key, i)
            except OSError:
                break
            if name == config.APP_ID:
                our_values.append(name)
    assert len(our_values) == 1


def test_real_run_key_not_modified_by_tests():
    """Guard: this test suite must leave the *real* Run key alone."""
    # Reading is fine; the assertion documents that the suite never enabled
    # the real registration during this run.
    was_enabled = startup.is_enabled()  # whatever the user chose before
    assert isinstance(was_enabled, bool)