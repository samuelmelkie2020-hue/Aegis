"""Front-end contract tests.

The UI has no build step and no framework, so the cheapest way to keep it
honest is to assert on the served HTML: element ids the script needs, and the
accessibility contract (#21). Broken ids are silent failures in a browser, and
accessibility regressions are invisible until someone cannot use the app.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "jarvis" / "static"


@pytest.fixture()
def html(client):
    http, auth = client
    return http.get("/", headers=auth).text


# ---------------------------------------------------------------------------
# Wiring: the script and the markup must agree
# ---------------------------------------------------------------------------

def test_every_id_the_script_uses_exists_in_the_markup():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    markup = (STATIC / "index.html").read_text(encoding="utf-8")

    used = set(re.findall(r'getElementById\("([^"]+)"\)', js))
    present = set(re.findall(r'id="([^"]+)"', markup))
    # created at runtime by the script itself
    dynamic = {"toast", "screenFrame", "welcomeHero"}

    missing = sorted(used - present - dynamic)
    assert not missing, f"app.js looks for ids that do not exist: {missing}"


def test_no_token_placeholder_is_served():
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "%%JARVIS_TOKEN%%" in markup  # the source has it...
    # ...and the server replaces it (checked through the real response below)


def test_served_page_has_the_token_substituted(html):
    assert "%%JARVIS_TOKEN%%" not in html


def test_script_is_loaded_after_config_js(html):
    """The token bootstrap must run before app.js reads it."""
    assert html.index("/config.js") < html.index("/static/app.js")


# ---------------------------------------------------------------------------
# Accessibility (#21)
# ---------------------------------------------------------------------------

def test_chat_log_is_a_polite_live_region(html):
    tag = re.search(r'<div[^>]*id="chatLog"[^>]*>', html).group(0)
    assert 'aria-live="polite"' in tag, "screen readers would not announce replies"
    assert 'role="log"' in tag
    assert "aria-label" in tag


def test_settings_dialog_has_modal_semantics(html):
    tag = re.search(r'<div[^>]*role="dialog"[^>]*>', html).group(0)
    assert 'aria-modal="true"' in tag
    assert "aria-labelledby" in tag


def test_dialog_label_points_at_a_real_heading(html):
    label_id = re.search(r'aria-labelledby="([^"]+)"', html).group(1)
    assert f'id="{label_id}"' in html


def test_composer_has_a_programmatic_label(html):
    assert 'for="userInput"' in html
    assert 'id="userInput"' in html


def test_icon_only_buttons_have_accessible_names(html):
    """Every emoji-only button needs a name a screen reader can read."""
    for element in re.findall(r"<button[^>]*>[^<]*</button>", html, flags=re.S):
        text = re.sub(r"<[^>]+>", "", element).strip()
        has_name = "aria-label=" in element or "title=" in element
        if text and re.fullmatch(r"[\W_]+", text, flags=re.UNICODE) and len(text) <= 4:
            assert has_name, f"button {element[:60]!r} has no accessible name"


def test_decorative_canvas_is_hidden_from_assistive_tech(html):
    tag = re.search(r"<canvas[^>]*>", html).group(0)
    assert 'aria-hidden="true"' in tag


def test_status_message_region_exists(html):
    assert 'id="orbLabel"' in html
    assert 'role="status"' in html


def test_toast_is_announced(html):
    tag = re.search(r'<div[^>]*id="toast"[^>]*>', html).group(0)
    assert 'role="status"' in tag or 'aria-live' in tag


def test_screen_image_has_meaningful_alt_text(html):
    tag = re.search(r'<img[^>]*id="screenImg"[^>]*>', html).group(0)
    alt = re.search(r'alt="([^"]*)"', tag).group(1)
    assert alt and alt.lower() not in {"image", "img", "screenshot"}


def test_focus_is_never_suppressed_without_a_replacement():
    """`outline: none` with nothing in its place hides keyboard focus."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    suppressions = [
        line for line in css.splitlines()
        if "outline: none" in line and "focus-visible" not in line
    ]
    assert len(suppressions) <= 1, f"unexplained outline suppression: {suppressions}"
    assert ":focus-visible" in css, "no visible focus style at all"
    assert ".sr-only" in css


def test_reduced_motion_is_respected():
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "prefers-reduced-motion" in css
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "prefers-reduced-motion: reduce" in js


# ---------------------------------------------------------------------------
# No inline secrets or absolute localhost calls in the front-end
# ---------------------------------------------------------------------------

def test_frontend_never_hardcodes_localhost_api_calls():
    """Calls to *this* server must be relative, or the preview proxy breaks them.

    Absolute localhost URLs are only acceptable as a provider preset (Ollama),
    which is a URL we hand to the backend, never one the browser fetches.
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for call in re.findall(r'api\(\s*[`"]([^`"]+)', js) + re.findall(r'fetch\(\s*[`"]([^`"]+)', js):
        assert not call.startswith(("http://", "https://")), f"absolute API call: {call}"
        assert call.startswith("/"), f"non-relative API call: {call}"

    # the only localhost literals allowed are provider presets
    localhosts = re.findall(r'https?://(?:localhost|127\.0\.0\.1)[^"\']*', js)
    assert all("11434" in url for url in localhosts), f"unexpected localhost URL: {localhosts}"


def test_no_api_key_placeholder_is_prefilled():
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    key_input = re.search(r"<input[^>]*id=\"setApiKey\"[^>]*>", markup).group(0)
    assert "value=" not in key_input  # never render a key into the page
    assert 'autocomplete="off"' in key_input


def test_script_uses_the_authenticated_fetch_wrapper():
    """A bare fetch() to /api would 401 and silently break the UI."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    bare = re.findall(r'fetch\("/api[^"]*"', js)
    assert not bare, f"unauthenticated API call(s): {bare}"
