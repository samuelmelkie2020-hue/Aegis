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


def test_every_request_body_model_is_defined_before_its_route():
    """FastAPI resolves annotations from *module globals at decoration time*.

    A request model defined below its route is silently not recognised: the body
    is dropped, the handler gets its default, and the endpoint behaves as if the
    request were ignored. This bit /api/local/adopt once — the model was defined
    next to /api/chat/stop, 250 lines after the route.
    """
    import ast
    import re

    source = (Path(__file__).resolve().parent.parent / "aegis" / "server.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    model_lines: dict[str, int] = {}
    route_lines: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name.endswith(("Request", "Body")):
            model_lines[node.name] = node.lineno
        elif isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                if isinstance(decorator, ast.Call) and "app" in ast.unparse(decorator.func):
                    route_lines.append((node.lineno, ast.unparse(node.args)))

    assert model_lines and route_lines
    for line, signature in route_lines:
        for name, defined in model_lines.items():
            if re.search(rf"\b{name}\b", signature):
                assert defined < line, (
                    f"{signature.splitlines()[0][:60]} uses {name}, defined later at line "
                    f"{defined} — FastAPI will ignore the request body"
                )


# ---------------------------------------------------------------------------
# The landing page (site/index.html, deployed to Vercel)
# ---------------------------------------------------------------------------

SITE = Path(__file__).resolve().parent.parent / "site" / "index.html"


def test_the_landing_page_exists_and_is_self_contained():
    """It must work offline and in a sandbox: no CDN fonts, no external scripts.

    The app's own stylesheet already depends on Google Fonts as a nicety; a
    marketing page that cannot render without a third party is a page that
    sometimes renders as unstyled text.
    """
    import re

    html = SITE.read_text(encoding="utf-8")
    assert "<title>" in html and "Aegis" in html
    urls = re.findall(r'(?:src|href)="(https?://[^"]+)"', html)
    offsite = [u for u in urls if not u.startswith("https://github.com/")]
    assert not offsite, f"the page loads something from somewhere else: {offsite}"
    assert "<script src" not in html, "no external script tags"


def test_the_landing_page_links_to_real_releases():
    """A download button that 404s is worse than no button."""
    import re

    html = SITE.read_text(encoding="utf-8")
    version = (Path(__file__).resolve().parent.parent / "aegis" / "__init__.py").read_text(
        encoding="utf-8"
    ).split('__version__ = "')[1].split('"')[0]
    downloads = re.findall(r'releases/download/([^/"]+)/([^"]+\.zip)', html)
    assert downloads, "no download links"
    for tag, asset in downloads:
        assert tag == f"v{version}", f"page points at {tag}, the app is v{version}"
        assert asset.startswith("Aegis-"), asset


def test_the_landing_page_does_not_overpromise_privacy():
    """The honest half has to be on the page, not only in SECURITY.md."""
    html = SITE.read_text(encoding="utf-8")
    assert "does not sandbox" in html
    assert "cloud provider" in html.lower()
    assert "not protected" in html.lower() or "What you should know" in html


def test_the_landing_page_has_no_dangling_anchors():
    import re

    html = SITE.read_text(encoding="utf-8")
    ids = set(re.findall(r'id="([^"]+)"', html))
    anchors = set(re.findall(r'href="#([^"]+)"', html))
    assert not (anchors - ids), f"anchors with no target: {anchors - ids}"


def test_vercel_is_told_this_is_a_static_site():
    """Deploying the repo root would serve Python source as the site."""
    config = Path(__file__).resolve().parent.parent / "vercel.json"
    assert config.exists()
    import json

    data = json.loads(config.read_text(encoding="utf-8"))
    assert data.get("outputDirectory") == "site"
    assert "buildCommand" not in data or data["buildCommand"] is None


def test_the_landing_page_says_where_the_wake_word_is_heard():
    """A feature card and the honest line have to live on the same page."""
    html = SITE.read_text(encoding="utf-8")
    assert "Hey Aegis" in html
    assert "matched on your own machine" in html or "matched locally" in html
    assert "speech engine" in html, "the browser's engine has to be named"


def test_the_landing_page_test_count_is_not_a_stale_number():
    """It said 515 while the suite had 530 — a number nobody rechecks."""
    import re

    html = SITE.read_text(encoding="utf-8")
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
    site_count = re.search(r"(\d+)\s+tests", html)
    readme_count = re.search(r"# (\d+) tests", readme)
    assert site_count and readme_count
    assert site_count.group(1) == readme_count.group(1), (
        f"the site says {site_count.group(1)} tests, the README says {readme_count.group(1)}"
    )


PAGES = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "pages.yml"


def test_the_website_has_a_second_home_that_needs_no_token():
    """Vercel is blocked on an account permission; Pages is not.

    The site is one self-contained file, so the fallback is a copy — but a
    workflow that publishes the wrong folder, or one with no permissions,
    fails silently on a push that nobody is watching.
    """
    text = PAGES.read_text(encoding="utf-8")
    assert "upload-pages-artifact" in text and "deploy-pages" in text
    assert "path: site" in text, "it must publish site/, not the repository root"
    assert "pages: write" in text and "id-token: write" in text
    # Publishing only when the page changes keeps unrelated merges quiet.
    assert "site/**" in text
    # Pages was not enabled on the repository, and configure-pages fails with
    # "Get Pages site failed" unless it is told it may turn it on.
    assert "enablement: true" in text
