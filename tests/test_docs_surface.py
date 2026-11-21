"""The public surface of the server.

The API needs no documentation endpoint: it is described in the README and its
only client is the bundled UI. Leaving FastAPI's automatic docs on publishes a
complete map of every endpoint — including request bodies — to anything that can
reach the port, without a token. That is a free reconnaissance step for an
attacker, and it was live until this was added.
"""

from __future__ import annotations

from pathlib import Path

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


# ---------------------------------------------------------------------------
# The security page
# ---------------------------------------------------------------------------

def test_a_security_page_exists_and_is_linked_from_the_readme():
    """A project that can run shell commands needs a place to report a problem."""
    root = Path(__file__).resolve().parent.parent
    security = root / "SECURITY.md"
    assert security.exists(), "SECURITY.md is missing"
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "SECURITY.md" in readme, "nothing points at the security page"


def test_the_security_page_states_what_is_not_protected():
    """Promising only the good half of the model is how trust gets lost."""
    root = Path(__file__).resolve().parent.parent
    text = (root / "SECURITY.md").read_text(encoding="utf-8")
    assert "not protected" in text
    for claim in ("safe mode asks; it does not sandbox", "not a filter"):
        assert claim in text.lower(), f"SECURITY.md no longer says: {claim}"


def test_the_security_page_names_the_real_token_file():
    """A rotation recipe pointing at a path that does not exist is worse than none."""
    from aegis.config import CONFIG_PATH

    text = (Path(__file__).resolve().parent.parent / "SECURITY.md").read_text(encoding="utf-8")
    assert ".aegis_token" in text
    # The installed locations it documents must match where the app actually puts it.
    assert CONFIG_PATH.parent.name == "Aegis" or "from source" in text
