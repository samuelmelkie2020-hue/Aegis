"""The live screen view.

A live view is the most invasive thing Aegis does: it reads the whole display
several times a second while it is on. So these tests are less about "does the
picture arrive" and more about the limits that keep it from becoming a
surveillance feed — the token gate, the rate clamp, the idle stop, the time
limit, and never leaving a frame behind in a cache.

The display is faked throughout: CI has no X server, and a test suite must never
depend on someone's actual screen.
"""

from __future__ import annotations

import base64
import io

import pytest

from aegis import tools


@pytest.fixture()
def fake_display(monkeypatch):
    """A 2560x1440 display that counts how many times it was grabbed."""
    state = {"grabs": 0, "monitors": 1}

    class Shot:
        size = (2560, 1440)
        rgb = bytes(2560 * 1440 * 3)

    class Sct:
        @property
        def monitors(self):
            return [{}] * state["monitors"]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def grab(self, monitor):
            state["grabs"] += 1
            return Shot()

    class Mss:
        @staticmethod
        def mss():
            return Sct()

    monkeypatch.setitem(__import__("sys").modules, "mss", Mss)
    return state


@pytest.fixture()
def tiny_capture(monkeypatch):
    """Replace the grab itself: 1800 real captures would take minutes."""
    from PIL import Image

    calls = {"n": 0}

    def fake_capture():
        calls["n"] += 1
        model = Image.new("RGB", (32, 18), (10, 20, 30))
        return model, model, 1.0, (32, 18)

    monkeypatch.setattr(tools, "capture_screen", fake_capture)
    return calls


@pytest.fixture(autouse=True)
def fresh_live_state(monkeypatch):
    """Each test starts a new live run, and no test inherits a cached frame."""
    monkeypatch.setattr(tools, "_live_requests", 0.0, raising=False)
    monkeypatch.setattr(tools, "_live_opened", 0.0, raising=False)
    monkeypatch.setattr(tools, "_live_frame", "", raising=False)
    monkeypatch.setattr(tools, "_live_frame_info", {}, raising=False)
    monkeypatch.setattr(tools, "_live_tick", 0, raising=False)
    yield


# ---------------------------------------------------------------------------
# The frame itself
# ---------------------------------------------------------------------------

def test_frame_is_jpeg_and_decodes_to_a_real_image(fake_display):
    from PIL import Image

    frame = tools.live_frame(2.0)
    assert frame["ok"] is True
    assert frame["format"] == "jpeg"

    image = Image.open(io.BytesIO(base64.b64decode(frame["image"])))
    assert image.format == "JPEG"
    # The panel is a few hundred pixels wide; a 4K frame is wasted bandwidth.
    assert image.width <= tools.LIVE_MAX_WIDTH
    assert frame["screen_width"] == 2560


def test_frame_reports_the_display_size_not_the_scaled_size(fake_display):
    frame = tools.live_frame(2.0)
    assert (frame["screen_width"], frame["screen_height"]) == (2560, 1440)
    assert frame["width"] < frame["screen_width"], "the frame was not downscaled"


# ---------------------------------------------------------------------------
# Limits: the server does not trust the client
# ---------------------------------------------------------------------------

def test_rate_is_clamped_however_fast_the_client_asks(tiny_capture):
    """A caller asking for 1000 fps still gets LIVE_MAX_FPS worth of captures."""
    tools.live_frame(1000)
    assert tools.live_frame(1000)["reused"] is True
    assert tiny_capture["n"] == 1, "the second call grabbed the screen again"


def test_a_fast_poll_re_sends_instead_of_capturing(tiny_capture):
    first = tools.live_frame(2.0)
    second = tools.live_frame(2.0)
    assert second["reused"] is True
    assert second["image"] == first["image"]
    assert second["tick"] == first["tick"]
    assert tiny_capture["n"] == 1


def test_tick_advances_only_on_a_fresh_frame(tiny_capture, monkeypatch):
    """The UI shows 'Live' on a new tick and 'Stalled' when it stops moving."""
    clock = [1000.0]
    monkeypatch.setattr(tools.time, "monotonic", lambda: clock[0])

    first = tools.live_frame(2.0)
    assert first["reused"] is False and first["tick"] == 1

    clock[0] += 0.1                       # inside one frame interval
    assert tools.live_frame(2.0)["tick"] == 1, "a re-send looked like a new frame"

    clock[0] += 1.0                       # a full interval later
    fresh = tools.live_frame(2.0)
    assert fresh["reused"] is False and fresh["tick"] == 2


def test_a_run_ends_after_the_time_limit(tiny_capture, monkeypatch):
    """Otherwise a forgotten tab watches the screen all afternoon.

    Polled continuously, as a real client would: the idle stop must not rescue a
    view that has been open too long (that is the difference between "nobody is
    looking" and "it has been running for half an hour").
    """
    clock = [1000.0]
    monkeypatch.setattr(tools.time, "monotonic", lambda: clock[0])

    stopped = None
    for _ in range(int(tools.LIVE_MAX_SECONDS) + 2):
        clock[0] += 1.0
        frame = tools.live_frame(2.0)
        if not frame["ok"]:
            stopped = frame
            break

    assert stopped is not None, "the view never ended"
    assert stopped["reason"] == "time limit"
    assert "minutes" in stopped["detail"]
    assert clock[0] - 1000.0 >= tools.LIVE_MAX_SECONDS


def test_an_idle_view_starts_a_new_run_rather_than_staying_dead(tiny_capture, monkeypatch):
    """After the idle stop the view is off; asking again must work."""
    clock = [1000.0]
    monkeypatch.setattr(tools.time, "monotonic", lambda: clock[0])
    tools.live_frame(2.0)

    clock[0] += tools.LIVE_IDLE_STOP + 1          # nobody watched for two minutes
    again = tools.live_frame(2.0)
    assert again["ok"] is True
    assert again["reused"] is False


def test_limits_are_conservative_by_default():
    assert tools.LIVE_MAX_FPS <= 5, "a live view is not a screen recorder"
    assert tools.LIVE_IDLE_STOP <= 300
    assert tools.LIVE_MAX_SECONDS <= 3600


# ---------------------------------------------------------------------------
# Failure: no display must not take the app down
# ---------------------------------------------------------------------------

def test_no_display_returns_a_reason_instead_of_raising(monkeypatch):
    class BrokenMss:
        @staticmethod
        def mss():
            raise OSError("no display")

    monkeypatch.setitem(__import__("sys").modules, "mss", BrokenMss)
    frame = tools.live_frame(2.0)
    assert frame["ok"] is False
    assert frame["reason"] == "capture"
    assert "no display" in frame["detail"]


# ---------------------------------------------------------------------------
# HTTP surface
# ---------------------------------------------------------------------------

def test_live_frame_requires_the_token(anon):
    response = anon.get("/api/live/frame", headers={"Host": "localhost:8000"})
    assert response.status_code == 401
    # and it must not leak a frame in the error path
    assert response.headers.get("content-type", "").startswith("application/json")


def test_live_frame_is_never_cached(client, fake_display):
    """A screen recording must not survive in a browser cache."""
    http, auth = client
    response = http.get("/api/live/frame", headers=auth)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/jpeg"
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["X-Aegis-Tick"] == "1"


def test_live_frame_reports_a_missing_display_as_unavailable(client, monkeypatch):
    class BrokenMss:
        @staticmethod
        def mss():
            raise OSError("no display")

    monkeypatch.setitem(__import__("sys").modules, "mss", BrokenMss)
    http, auth = client
    response = http.get("/api/live/frame", headers=auth)
    assert response.status_code == 503
    assert response.json()["reason"] == "capture"
    assert "no-store" in response.headers["cache-control"]


def test_fps_is_bounded_by_the_query_model(client, fake_display):
    """Pydantic rejects a silly fps before the capture code ever runs."""
    http, auth = client
    assert http.get("/api/live/frame?fps=0", headers=auth).status_code == 422
    assert http.get("/api/live/frame?fps=9999", headers=auth).status_code == 422
