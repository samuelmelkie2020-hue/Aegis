"""The HUD's telemetry endpoint.

The HUD draws gauges from these numbers, so the shape matters as much as the
values: a missing sensor must read as ``None`` (the UI shows a dash) rather
than as a fabricated 0 that looks like a cold CPU.
"""

from __future__ import annotations

import types

import pytest


@pytest.fixture()
def fake_psutil(monkeypatch):
    """A machine with known numbers, so the payload can be asserted exactly."""
    module = types.SimpleNamespace(
        cpu_percent=lambda interval=None: 42.0,
        cpu_count=lambda logical=True: 8,
        getloadavg=lambda: (1.5, 1.0, 0.5),
        virtual_memory=lambda: types.SimpleNamespace(
            percent=61.2, used=9_800_000_000, total=16_000_000_000
        ),
        disk_usage=lambda path: types.SimpleNamespace(percent=73.0, free=120_000_000_000),
        sensors_temperatures=lambda: {"k10temp": [types.SimpleNamespace(current=54.4)]},
        sensors_battery=lambda: types.SimpleNamespace(percent=88.4, power_plugged=True),
        boot_time=lambda: 1_000_000.0,
    )
    monkeypatch.setitem(__import__("sys").modules, "psutil", module)
    return module


def test_stats_reports_the_machine(client, fake_psutil):
    http, auth = client
    response = http.get("/api/stats", headers=auth)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["cpu"] == {"percent": 42.0, "cores": 8, "load1": 1.5}
    assert body["memory"]["percent"] == 61.2
    assert body["memory"]["total_gb"] == 16.0
    assert body["temperature"] == 54.4
    assert body["battery"] == {"percent": 88, "plugged": True}
    assert body["disk"]["percent"] == 73.0
    assert body["uptime_seconds"] > 0


def test_missing_sensors_are_null_not_zero(client, monkeypatch):
    """A desktop with no battery must not look like a 0%-charged laptop."""
    module = types.SimpleNamespace(
        cpu_percent=lambda interval=None: 5.0,
        cpu_count=lambda logical=True: 4,
        getloadavg=lambda: (0.1, 0.1, 0.1),
        virtual_memory=lambda: types.SimpleNamespace(percent=20.0, used=1, total=2),
        disk_usage=lambda path: types.SimpleNamespace(percent=10.0, free=1),
        boot_time=lambda: 0.0,
    )
    monkeypatch.setitem(__import__("sys").modules, "psutil", module)

    http, auth = client
    body = http.get("/api/stats", headers=auth).json()

    assert body["temperature"] is None
    assert body["battery"] is None


def test_stats_never_takes_the_server_down(client):
    """psutil is quirky per platform; an unexpected error must not 500 the HUD."""
    http, auth = client
    assert http.get("/api/stats", headers=auth).status_code == 200
