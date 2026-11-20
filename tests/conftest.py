"""Shared test fixtures.

Isolation is set up **at import time**, not inside a fixture, because pytest
imports test modules during collection — before any fixture runs. A test file's
``from aegis import config`` would otherwise capture the real repository path
and quietly write into the checkout.

The environment is therefore redirected once per session to a temp directory;
the ``sandbox`` fixture then resets per-test *state* (files, recorded config
problem, pending approvals). The package is imported exactly once, so a normal
module-level import in a test file is always correct.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile

import pytest

_SANDBOX = tempfile.mkdtemp(prefix="aegis-tests-")
_HOME = os.path.join(_SANDBOX, "home")
os.makedirs(_HOME, exist_ok=True)

_ENV_OVERRIDES = {
    "HOME": _HOME,
    "USERPROFILE": _HOME,  # Windows
    "AEGIS_CONFIG": os.path.join(_SANDBOX, "aegis_config.json"),
    "AEGIS_TOKEN_FILE": os.path.join(_SANDBOX, ".aegis_token"),
    "AEGIS_HOST": "127.0.0.1",
}
_ENV_CLEARED = (
    "AEGIS_TOKEN",
    "AEGIS_ALLOWED_HOSTS",
    "AEGIS_ALLOW_READ_OUTSIDE_HOME",
    "AEGIS_ALLOW_WRITE_OUTSIDE_HOME",
    "AEGIS_EMBED_TOKEN",
    "AEGIS_PREVIEW_HOST_SUFFIXES",
)

for _key, _value in _ENV_OVERRIDES.items():
    os.environ[_key] = _value
for _key in _ENV_CLEARED:
    os.environ.pop(_key, None)

atexit.register(shutil.rmtree, _SANDBOX, True)


@pytest.fixture()
def sandbox():
    """A clean per-test state inside the session sandbox."""
    from aegis import approvals, config

    for name in ("aegis_config.json", ".aegis_token"):
        target = os.path.join(_SANDBOX, name)
        if os.path.exists(target):
            os.unlink(target)
    for leftover in os.listdir(_SANDBOX):
        if ".corrupt-" in leftover:
            os.unlink(os.path.join(_SANDBOX, leftover))
    for child in os.listdir(_HOME):
        path = os.path.join(_HOME, child)
        shutil.rmtree(path) if os.path.isdir(path) else os.unlink(path)

    config.LAST_PROBLEM = None
    approvals.forget_all()
    # A real (fake) provider, so the agent takes the model path and not the
    # demo brain. Tests that care about the demo path override this explicitly.
    config.save_config({
        "safe_mode": True,
        "llm": {"base_url": "https://api.example/v1", "api_key": "sk-test-key-0001", "model": "m"},
    })
    return _SANDBOX


@pytest.fixture()
def client(sandbox):
    """An authenticated TestClient (loopback Host + the generated token)."""
    from fastapi.testclient import TestClient

    from aegis.security import get_token
    from aegis.server import app

    return TestClient(app, raise_server_exceptions=False), {
        "Host": "localhost:8000",
        "X-Aegis-Token": get_token(),
    }


@pytest.fixture()
def auth(client):
    return client[1]


@pytest.fixture()
def anon(sandbox):
    """An unauthenticated client with its own cookie jar."""
    from fastapi.testclient import TestClient

    from aegis.server import app

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def paths(sandbox):
    from pathlib import Path

    class Paths:
        home = Path(_HOME)
        config = Path(_SANDBOX) / "aegis_config.json"
        token = Path(_SANDBOX) / ".aegis_token"

    return Paths
