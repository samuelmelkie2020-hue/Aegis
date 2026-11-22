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

STATIC = Path(__file__).resolve().parent.parent / "aegis" / "static"


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
    assert "%%AEGIS_TOKEN%%" in markup  # the source has it...
    # ...and the server replaces it (checked through the real response below)


def test_served_page_has_the_token_substituted(html):
    assert "%%AEGIS_TOKEN%%" not in html


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




# ---------------------------------------------------------------------------
# Live view (the screen panel's recording mode)
# ---------------------------------------------------------------------------

def test_live_toggle_is_a_real_toggle_for_assistive_tech(html):
    tag = re.search(r'<button[^>]*id="liveToggle"[^>]*>', html).group(0)
    assert 'aria-pressed="false"' in tag, "off must be the default, and say so"
    assert "aria-label=" in tag, "an icon-only button needs a name"


def test_live_view_starts_off_and_the_badge_is_hidden(html):
    """The badge is the only signal that the screen is being read."""
    tag = re.search(r'<div[^>]*id="liveBadge"[^>]*>', html).group(0)
    assert "hidden" in tag


def test_the_panel_says_live_even_while_off(html):
    """Nobody should have to guess what the toggle turns on."""
    assert 'id="liveToggle"' in html
    assert "Watch my screen" in html


def test_live_loop_cannot_run_twice(html):
    """Overlapping polls would queue stale screenshots."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    body = js[js.index("function startLive"):]
    body = body[: body.index(chr(10) + "}")]
    assert "if (state.live) return;" in body


def test_live_view_stops_when_the_tab_is_hidden(html):
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "visibilitychange" in js
    assert "document.hidden" in js


def test_live_frames_revoke_their_object_urls(html):
    """A leak here holds every frame of a session in memory."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    create = js.count("URL.createObjectURL")
    revoke = js.count("URL.revokeObjectURL")
    assert create >= 1
    assert revoke >= create, "every object URL must be released"


def test_hud_telemetry_elements_exist():
    """The gauges and bars are driven by ids; a typo shows as a blank dial."""
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for element in (
        "gCpu", "vCpu", "gMem", "vMem", "gTmp", "vTmp",
        "bBat", "tBat", "bDisk", "tDisk", "bLoad", "tLoad",
        "uptime", "clock", "stateLabel",
    ):
        assert f'id="{element}"' in markup, f"{element} is missing from the markup"
        assert f'"{element}"' in js, f"{element} is never updated by the script"


def test_gauges_are_labelled_and_decorative_svgs_hidden():
    """A dial with no visible label is just a coloured ring."""
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    # Split on the gauge wrapper: each block runs to the next one.
    blocks = re.split(r'<div class="gauge">', markup)[1:]
    assert len(blocks) >= 3
    for block in blocks:
        assert "<label>" in block.split("</div>\n")[0] or "<label>" in block[:900]
        assert 'aria-hidden="true"' in block[:400], "decorative dial is exposed to screen readers"

def test_every_class_the_ui_needs_is_styled():
    """A restyle must not quietly drop a class the UI depends on.

    When the visual theme was replaced wholesale, three classes the script and
    server use survived in the markup but lost their rules — invisible in a
    browser, and the kind of thing that only shows up as an unstyled panel.
    """
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    markup = (STATIC / "index.html").read_text(encoding="utf-8")
    server = (STATIC.parent / "server.py").read_text(encoding="utf-8")

    defined = set(re.findall(r"\.([a-zA-Z][\w-]*)", css))
    used: set[str] = set()
    for source in (markup, server):
        for attr in re.findall(r'class="([^"]+)"', source):
            used.update(attr.split())
    for attr in re.findall(r'class="([^"]+)"', js):
        used.update(attr.split())
    used.update(re.findall(r'classList\.(?:add|remove|toggle)\("([\w-]+)"', js))

    # Toggled-only hooks that carry no appearance of their own.
    allowed_unstyled = {"hidden", "desktop"}
    missing = sorted(used - defined - allowed_unstyled)
    assert not missing, f"classes used but not styled: {missing}"

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

    # The only localhost literals allowed are local provider presets. Those are
    # handed to the backend as a base URL; the browser never fetches them itself.
    localhosts = re.findall(r'https?://(?:localhost|127\.0\.0\.1)[^"\']*', js)
    assert all("11434" in url or "1234" in url for url in localhosts), (
        f"unexpected localhost URL: {localhosts}"
    )


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


# ---------------------------------------------------------------------------
# Updates
# ---------------------------------------------------------------------------

def test_the_update_card_is_hidden_until_there_is_an_update(html):
    tag = re.search(r'<div[^>]*id="updateCard"[^>]*>', html).group(0)
    assert "hidden" in tag, "an update banner must not sit there permanently"


def test_a_hidden_element_stays_hidden_even_when_it_has_a_display_rule():
    """`hidden` is a weak UA rule; a `display:` on the element beats it.

    The update banner is a grid and starts `hidden`, so without an explicit rule
    it was visible on load with nothing in it.
    """
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".update-card[hidden]" in css
    assert ".hidden { display: none !important; }" in css


def test_the_update_card_has_both_steps_and_a_dismiss(html):
    for button in ("updateGet", "updateInstall", "updateLater"):
        assert f'id="{button}"' in html, f"{button} is missing"


def test_install_is_not_offered_until_a_download_finishes(html):
    """Install must be the second step, never the first."""
    tag = re.search(r'<button[^>]*id="updateInstall"[^>]*>', html).group(0)
    assert "hidden" in tag


def test_updates_can_be_checked_by_hand(html):
    """A user who dismissed a banner must be able to ask again."""
    assert 'id="checkUpdates"' in html
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "checkUpdates.addEventListener" in js


def test_installing_asks_for_confirmation_in_the_page(html):
    """The app is about to close itself; that cannot be a single stray click."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    install = js[js.index("async function installUpdate"):]
    install = install[: install.index("updateGet.addEventListener")]
    assert "window.confirm" in install
    assert "/api/update/install" in install
    assert "/api/approve" in install


def test_the_update_dot_respects_reduced_motion(html):
    """The pulse is decoration; the media query already neutralises it."""
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    reduced = css[css.index("prefers-reduced-motion"):]
    assert "animation-duration: 0.01ms" in reduced


def test_a_failed_update_is_shown_not_swallowed(html):
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'showUpdate("failed"' in js


# ---------------------------------------------------------------------------
# The local-model panel: the default brain, with no key to type
# ---------------------------------------------------------------------------

def test_settings_shows_a_local_model_panel_first(html):
    """The first thing in Settings is the thing that needs no account."""
    assert 'id="localPanel"' in html
    assert html.index('id="localPanel"') < html.index('id="setProvider"')
    for element in ("localHint", "localCpu", "localRam", "localGpu", "localModels",
                    "localSuggest", "localUse", "localRefresh", "localPull"):
        assert f'id="{element}"' in html, f"missing {element}"


def test_cloud_providers_are_collapsed_behind_a_summary(html):
    """Cloud is optional now; it must not read like required setup."""
    detail = html[html.index('id="cloudPanel"'):]
    summary = detail[: detail.index("</summary>")]
    assert "optional" in summary.lower()
    assert "api key" in summary.lower()


def test_the_local_provider_is_the_default_option(html):
    """A new install must land on a working local setup, not a cloud one."""
    select = re.search(r'<select id="setProvider">(.*?)</select>', html, re.S).group(1)
    first = re.search(r'<option value="([^"]+)"', select).group(1)
    assert first in ("ollama", "lmstudio")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    provider = js[js.index("const PROVIDERS"):]
    provider = provider[: provider.index("};")]
    ollama = provider[provider.index("ollama:"):]
    assert "local: true" in ollama[:120]
    assert "api.openai.com" not in ollama[: ollama.index("}")]


def test_the_api_key_field_is_hidden_for_a_local_model(html):
    """No key exists for a local model; showing the field invents a missing step."""
    assert 'id="keyField"' in html
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "keyField.hidden = local" in js


def test_the_panel_renders_progress_and_can_cancel(html):
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "/api/local/status" in js
    assert "/api/local/pull" in js
    assert "/api/local/pull/cancel" in js
    assert 'id="pullFill"' in html
    assert "renderPull" in js
    status_fn = js[js.index("function renderPull"):]
    status_fn = status_fn[: status_fn.index("async function loadLocalStatus")]
    assert "pull.error" in status_fn          # an error is shown, not swallowed
    assert '"cancelled"' in status_fn         # a cancelled download says so


def test_a_missing_model_is_explained_on_first_run(html):
    """The demo brain must not look like a finished install."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "demo brain" in js
    assert "💻 This computer" in js
    assert "No API key, no account" in js


def test_the_status_line_says_where_the_model_runs(html):
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    fn = js[js.index("function updateModelStatus"):]
    fn = fn[: fn.index("// ---------------------------------------------------------------------------\n// Chat rendering")]
    assert "on this PC" in fn
    # a local endpoint with no model is still the demo brain, not "connected"
    assert "local ? Boolean(llm.model)" in fn


def test_the_local_panel_has_no_inline_styles(html):
    """Styling belongs in style.css, which the reduced-motion rules also cover."""
    panel = html[html.index('id="localPanel"'): html.index('id="cloudPanel"')]
    assert "style=" not in panel
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".local-panel" in css
    assert ".pull-fill" in css
    assert ".model-chip" in css


# ---------------------------------------------------------------------------
# Stop button: the control must exist, be wired, and be reachable by keyboard
# ---------------------------------------------------------------------------

def test_the_stop_button_exists_and_starts_hidden(html):
    tag = re.search(r'<button[^>]*id="stopBtn"[^>]*>', html).group(0)
    assert "hidden" in tag, "Stop must not sit next to Send doing nothing"
    assert 'aria-label="Stop this answer"' in tag


def test_stop_posts_the_turn_id_the_server_announced():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    stop_fn = js[js.index("async function stopTurn"):]
    stop_fn = stop_fn[: stop_fn.index("async function sendMessage")]
    assert "/api/chat/stop" in stop_fn
    assert "turn_id" in stop_fn
    # The id comes from the stream, never from anything the page invents.
    assert 'case "turn":' in js
    assert "state.turnId = ev.turn_id" in js


def test_send_and_stop_swap_in_the_same_place():
    """One control, two jobs: a busy turn must not leave Send clickable."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    body = js[js.index("function setBusy"):]
    body = body[: body.index("async function stopTurn")]
    assert 'sendBtn.classList.toggle("hidden", busy)' in body
    assert 'stopBtn.classList.toggle("hidden", !busy)' in body
    assert "stopBtn.disabled" in body


def test_escape_stops_a_running_turn():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'if (e.key !== "Escape" || !state.busy) return;' in js
    assert "stopTurn();" in js


def test_a_stopped_reply_is_not_rendered_as_a_failure():
    """Stopping is expected; showing ⚠ for it would read as a crash."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "ev.stopped" in js
    assert "is-stopped" in js
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".is-stopped" in css


def test_a_stopped_turn_is_not_overwritten_with_no_response():
    """The `finally` fallback fired after a stop and replaced the note.

    A stopped turn writes its own ending in the `done` handler; the fallback text
    for an empty reply then overwrote it with "(no response)".
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    finally_block = js[js.index("  } finally {"):]
    finally_block = finally_block[: finally_block.index("setBusy(false)")]
    assert "is-stopped" in finally_block


def test_the_stopped_note_uses_markdown_this_app_actually_renders():
    """mdLite handles *italics*, not _italics_ — the note showed its asterisks."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "${note}" in js
    assert "_${note}_" not in js


def test_a_conversation_can_be_saved_without_asking_the_server(html):
    """Saving is a local export: the transcript never leaves the machine."""
    assert 'id="exportChat"' in html
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    export_fn = js[js.index('document.getElementById("exportChat")'):]
    export_fn = export_fn[: export_fn.index("// new conversation")]
    assert "text/markdown" in export_fn
    assert "download =" in export_fn
    # It must not post the transcript anywhere.
    assert "/api/" not in export_fn
    # And the object URL must be released, or the transcript leaks per export.
    assert "revokeObjectURL" in export_fn


# ---------------------------------------------------------------------------
# "I downloaded a model and I cannot find it"
# ---------------------------------------------------------------------------

def test_there_is_a_button_that_finds_a_model_on_this_pc(html):
    assert 'id="localFind"' in html
    assert "Find the model I downloaded" in html
    # It must be in the settings panel, next to the hardware it runs on.
    panel = html[html.index('id="localPanel"'):]
    assert 'id="localFind"' in panel[: panel.index("</section>")]


def test_the_find_button_calls_the_adopt_endpoint():
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    handler = js[js.index('localFind.addEventListener("click"'):]
    handler = handler[: handler.index("});\n}")]
    assert "/api/local/adopt" in handler
    # Busy state, so a slow disk scan does not look like a dead button.
    assert "localFind.disabled = true" in handler
    assert "localFind.disabled = false" in handler


def test_what_is_on_disk_is_listed_with_its_size(html):
    for element in ("localDisk", "localDiskHead", "localDiskModels"):
        assert f'id="{element}"' in html, f"{element} is missing"
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function renderDisk(" in js
    assert "entry.size_gb" in js
    # A model on disk must be clickable — clicking it selects it.
    assert "useFoundModel(entry.name)" in js


def test_clicking_a_model_on_disk_selects_it_without_a_second_press():
    """The chip posts the name, so the user never has to type a model name."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    fn = js[js.index("async function useFoundModel"):]
    fn = fn[: fn.index("// One press: look in the folders")]
    assert 'body: JSON.stringify({ model: name })' in fn
    assert "setModel.value = data.model" in fn


def test_the_panel_shows_models_from_ollama_and_from_loose_files():
    """One list, because "what do I have?" is one question."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    fn = js[js.index("function renderDisk("):]
    fn = fn[: fn.index("async function useFoundModel")]
    assert "function renderDisk(disk, weights)" in js
    assert "(weights || [])" in fn
    # A file-based model is tagged, so "why does clicking this not work?" is
    # answered on the chip rather than by a failed request.
    assert "chip-file" in fn
    assert "entry.file" in fn


def test_sizes_never_render_as_zero_gb():
    """A 274 MB embedding model must not show as "0.0 GB"."""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function fmtSize(" in js
    body = js[js.index("function fmtSize("):]
    body = body[: body.index("function renderLocal")]
    assert "toFixed(1)} GB" in body and "MB" in body and "kB" in body
    # The chip uses the byte-precise formatter, not the GB one.
    disk = js[js.index("function renderDisk("):]
    assert "fmtSize(bytes)" in disk
