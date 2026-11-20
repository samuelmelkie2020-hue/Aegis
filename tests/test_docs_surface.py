"""The public surface of the server.

The API needs no documentation endpoint: it is described in the README and its
only client is the bundled UI. Leaving FastAPI's automatic docs on publishes a
complete map of every endpoint — including request bodies — to anything that can
reach the port, without a token. That is a free reconnaissance step for an
attacker, and it was live until this was added.
"""

from __future__ import annotations

import pytest

DOC_ROUTES = ["/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"]


@pytest.mark.parametrize("path", DOC_ROUTES)
def test_auto_docs_are_not_served(client, path):
    http, auth = client
    assert http.get(path, headers=auth).status_code == 404, f"{path} is reachable"


@pytest.mark.parametrize("path", DOC_ROUTES)
def test_auto_docs_are_not_served_to_anonymous_callers(anon, path):
    assert anon.get(path, headers={"Host": "localhost:8000"}).status_code == 404


def test_the_app_itself_still_works(client):
    """Guarding the docs must not take the real routes with it."""
    http, auth = client
    assert http.get("/api/health", headers=auth).status_code == 200
