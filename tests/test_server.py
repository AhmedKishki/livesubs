"""Server tests: routes, the WebSocket contract, and what each role is allowed to read.

These use a ticking test publisher rather than the sim at speed=0, so delivery can
be observed *after* a client connects. The sim's flat-out mode finishes before a
socket is even open, which would only ever exercise the snapshot path.
"""

from __future__ import annotations

import asyncio
import io

import pytest
import qrcode
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from livesubs.config import Settings
from livesubs.events import SegmentState, SubtitleSegment, Timing
from livesubs.hub import Hub
from livesubs.server import create_app, routes
from livesubs.server import ws as websocket_routes

TICK_S = 0.05


#: ``(speaker, default language, optional language)`` for each mode.
MODES = [("en", "de", "ar"), ("de", "en", "ar")]
MODE_IDS = ["english-speaker", "german-speaker"]


class TickingPublisher:
    """Publishes one committed segment per language every 50 ms until stopped."""

    def __init__(self, *languages: str) -> None:
        self.supported_languages = languages or ("de", "ar")
        self.seq = 0

    async def run(self, hub: Hub, *, session_id: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            for language in self.supported_languages:
                hub.publish_segment(
                    SubtitleSegment(
                        seq=self.seq,
                        rev=1,
                        state=SegmentState.FINAL,
                        language=language,
                        text=f"{language} line {self.seq}.",
                        src=f"Spoken line {self.seq}.",
                        timing=Timing(audio_ms=self.seq * 2000, emit_ms=self.seq * 2000 + 400),
                    )
                )
            self.seq += 1
            try:
                await asyncio.wait_for(stop.wait(), timeout=TICK_S)
            except TimeoutError:
                continue


def make_client(publisher=None, speaker: str = "en") -> TestClient:
    # _env_file=None keeps a developer's real .env out of the test run.
    settings = Settings(_env_file=None, session={"speaker": speaker})
    return TestClient(create_app(settings, publisher or TickingPublisher()))


def next_message_of_type(ws, wanted: str, limit: int = 8) -> dict:
    """Read until a message of `wanted` arrives, so tests stay order-tolerant."""

    seen: list[str] = []
    for _ in range(limit):
        message = ws.receive_json()
        seen.append(message.get("type", "?"))
        if message.get("type") == wanted:
            return message
    raise AssertionError(f"no {wanted!r} within {limit} messages; saw {seen}")


# --- pages and assets ---------------------------------------------------------


def test_landing_page_loads() -> None:
    with make_client() as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "/static/landing.js" in response.text


def test_viewer_page_loads_for_the_configured_session() -> None:
    with make_client() as client:
        response = client.get("/s/demo")
        assert response.status_code == 200
        assert "/static/viewer.js" in response.text


def test_unknown_session_is_a_404_not_silently_served() -> None:
    with make_client() as client:
        assert client.get("/s/nope").status_code == 404
        assert client.get("/api/session/nope").status_code == 404


def test_static_assets_are_served() -> None:
    with make_client() as client:
        for asset in (
            "language.js",
            "viewer.js",
            "overlay.js",
            "pacing.js",
            "client.js",
            "landing.js",
            "admin.js",
            "styles.css",
        ):
            response = client.get(f"/static/{asset}")
            assert response.status_code == 200, asset


def test_admin_page_loads() -> None:
    with make_client() as client:
        assert client.get("/admin").status_code == 200


# --- session API --------------------------------------------------------------


@pytest.mark.parametrize(("speaker", "default", "optional"), MODES, ids=MODE_IDS)
def test_session_metadata_matches_the_wire_contract(
    speaker: str, default: str, optional: str
) -> None:
    with make_client(TickingPublisher(default, optional), speaker) as client:
        payload = client.get("/api/session").json()
        assert payload["id"] == "demo"
        assert payload["source"] == speaker
        assert payload["default"] == default
        assert payload["languages"] == [default, optional]
        assert payload["version"] == 2
        assert payload["ws_url"] == "/ws/demo"


def test_session_metadata_describes_each_language_for_the_picker() -> None:
    with make_client() as client:
        targets = {t["code"]: t for t in client.get("/api/session").json()["targets"]}

    assert targets["de"]["native"] == "Deutsch" and targets["de"]["dir"] == "ltr"
    assert targets["ar"]["native"] == "العربية" and targets["ar"]["dir"] == "rtl"


def test_the_session_says_it_records_only_when_the_publisher_does() -> None:
    """The landing page repeats this to the audience, so it cannot come from a setting alone."""

    class Recording(TickingPublisher):
        records_audio = True

    with make_client(Recording()) as client:
        assert client.get("/api/session").json()["recording"] is True

    with make_client() as client:
        assert client.get("/api/session").json()["recording"] is False


def test_a_publisher_that_declares_nothing_offers_only_the_default_language() -> None:
    class Plain:
        async def run(self, hub, *, session_id, stop):
            await stop.wait()

    with make_client(Plain(), "de") as client:
        payload = client.get("/api/session").json()

    assert payload["languages"] == ["en"]


def test_qr_url_points_at_the_viewer_page_over_plain_http() -> None:
    """Plain HTTP on purpose: a certificate warning would break the scan."""

    with make_client() as client:
        url = client.get("/api/qr-url").json()["url"]
        assert url.startswith("http://")
        assert url.endswith("/s/demo")


def test_qr_url_follows_the_host_the_request_arrived_on() -> None:
    """Otherwise the QR always encodes localhost and a phone scans itself.

    Deriving from the request means scanning works whatever address the attendee
    used to reach the landing page, with no configuration.
    """

    with make_client() as client:
        # A documentation address (RFC 5737 TEST-NET-1), the same convention the
        # pre-flight check uses for its own probe. A real private address here would
        # be machine-specific and would publish one deployment's topology.
        url = client.get(
            "/api/qr-url", headers={"host": "192.0.2.10:8000"}
        ).json()["url"]
        assert url == "http://192.0.2.10:8000/s/demo"


def test_explicit_public_url_overrides_the_request_host() -> None:
    # Construction uses the config-file groups; reading may use the flat
    # accessors. The flat names are read-only properties, so `public_url=` as a
    # keyword would silently do nothing (extra="ignore").
    settings = Settings(
        _env_file=None,
        server={"public_url": "http://subs.example.test:9000"},
    )
    with TestClient(create_app(settings, TickingPublisher())) as client:
        url = client.get("/api/qr-url", headers={"host": "127.0.0.1:8000"}).json()["url"]
        assert url == "http://subs.example.test:9000/s/demo"


def test_qr_code_is_a_real_png() -> None:
    with make_client() as client:
        response = client.get("/qr.png")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
        assert len(response.content) > 200          # not a degenerate image
        assert response.headers["cache-control"] == "no-store"


def test_the_session_id_in_the_settings_is_the_one_served() -> None:
    """``--session-id`` must not be a flag that half the application ignores.

    Overriding the settings once, before the app is built, is what makes ``/healthz``,
    ``/api/session`` and the viewer page agree with the artefacts a run writes.
    """

    settings = Settings(_env_file=None).for_session(
        session_id="evalslice", session_name="Eval slice"
    )

    with TestClient(create_app(settings, TickingPublisher())) as client:
        assert client.get("/healthz").json()["session"] == "evalslice"

        payload = client.get("/api/session").json()
        assert payload["id"] == "evalslice"
        assert payload["name"] == "Eval slice"

        assert client.get("/s/evalslice").status_code == 200


def test_log_config_keeps_this_packages_loggers_alive() -> None:
    """uvicorn's own config disables loggers created before it starts.

    That silenced the pipeline's INFO lines - including the per-stage latency the
    evaluation document asks to be logged - in every served run.
    """

    from uvicorn.config import LOGGING_CONFIG

    from livesubs.server.app import log_config

    config = log_config("debug")

    assert config["loggers"]["livesubs"]["level"] == "DEBUG"
    assert config["loggers"]["livesubs"]["propagate"] is False
    # A copy, because uvicorn's dict is module state shared with other callers.
    assert "livesubs" not in LOGGING_CONFIG["loggers"]


def test_stats_describe_the_audience_and_the_host_without_any_lecture_text() -> None:
    with make_client() as client, client.websocket_connect("/ws/demo") as ws:
        next_message_of_type(ws, "segment")
        payload = client.get("/api/stats").json()

    assert payload["session"]["id"] == "demo" and payload["session"]["running"] is True
    assert payload["session"]["languages"] == ["de", "ar"]
    assert payload["audience"]["viewers"] == 1
    assert payload["audience"]["viewers_by_language"] == {"de": 1, "ar": 0}
    assert payload["audience"]["messages_sent"] >= 1 and payload["audience"]["bytes_sent"] > 0
    assert payload["server"]["open_files"] >= 3 and payload["server"]["loop_lag_ms"]["count"] >= 0
    # A publisher that cannot describe itself contributes nothing rather than an error.
    assert payload["pipeline"] is None
    # The publisher's lines are "Spoken line N." and "<language> line N.".
    assert "Spoken line" not in str(payload) and "de line" not in str(payload), (
        "no lecture text belongs in the figures"
    )


def test_stats_include_what_a_publisher_says_about_itself() -> None:
    class Describing(TickingPublisher):
        def snapshot(self) -> dict:
            return {"kind": "test", "committed": 7}

    with make_client(Describing()) as client:
        assert client.get("/api/stats").json()["pipeline"] == {"kind": "test", "committed": 7}


def test_stats_count_an_operator_apart_from_viewers() -> None:
    with (
        make_client() as client,
        client.websocket_connect("/ws/demo?role=admin") as ws,
    ):
        next_message_of_type(ws, "segment")
        audience = client.get("/api/stats").json()["audience"]

    assert (audience["viewers"], audience["operators"]) == (0, 1)


def test_healthz_reports_the_session_running() -> None:
    with make_client() as client:
        payload = client.get("/healthz").json()
        assert payload["ok"] is True
        assert payload["session"] == "demo"


# --- WebSocket contract -------------------------------------------------------


@pytest.mark.parametrize(("speaker", "default", "optional"), MODES, ids=MODE_IDS)
def test_viewer_gets_identity_health_then_subtitles_without_the_source(
    speaker: str, default: str, optional: str
) -> None:
    with (
        make_client(TickingPublisher(default, optional), speaker) as client,
        client.websocket_connect("/ws/demo") as ws,
    ):
        hello = ws.receive_json()
        assert hello["type"] == "hello"
        assert hello["data"]["session_id"] == "demo"
        assert hello["data"]["source"] == speaker
        assert hello["data"]["targets"] == [default, optional]
        assert hello["data"]["language"] == default

        assert ws.receive_json()["type"] == "status"

        # Several lines, so a leak of the other language would have had time to show.
        for _ in range(4):
            segment = next_message_of_type(ws, "segment")
            assert segment["data"]["language"] == default
            assert segment["data"]["text"]
            # The default-language guarantee is enforced server-side, not trusted to
            # the viewer page.
            assert segment["data"]["src"] is None


@pytest.mark.parametrize(("speaker", "default", "optional"), MODES, ids=MODE_IDS)
def test_the_optional_language_needs_its_explicit_role(
    speaker: str, default: str, optional: str
) -> None:
    with (
        make_client(TickingPublisher(default, optional), speaker) as client,
        client.websocket_connect(f"/ws/demo?role=viewer_{optional}") as ws,
    ):
        assert ws.receive_json()["data"]["language"] == optional
        for _ in range(4):
            segment = next_message_of_type(ws, "segment")
            assert segment["data"]["language"] == optional
            assert segment["data"]["src"] is None


@pytest.mark.parametrize(("speaker", "default", "optional"), MODES, ids=MODE_IDS)
def test_admin_does_receive_the_source_and_every_language(
    speaker: str, default: str, optional: str
) -> None:
    """The console cannot diagnose a bad translation without the speaker's own words."""

    with (
        make_client(TickingPublisher(default, optional), speaker) as client,
        client.websocket_connect("/ws/demo?role=admin") as ws,
    ):
        seen = [next_message_of_type(ws, "segment")["data"] for _ in range(4)]

    assert all(segment["src"] is not None for segment in seen)
    assert {segment["language"] for segment in seen} == {default, optional}


@pytest.mark.parametrize("role", ["superuser", "viewer_AR", "viewer_", "Admin", "viewer_xx"])
def test_an_unknown_role_is_treated_as_a_plain_viewer(role: str) -> None:
    with make_client() as client, client.websocket_connect(f"/ws/demo?role={role}") as ws:
        for _ in range(3):
            segment = next_message_of_type(ws, "segment")
            assert segment["data"]["language"] == "de"
            assert segment["data"]["src"] is None


def test_a_language_outside_the_mode_is_refused_with_4403() -> None:
    """An English speaker has no English subtitles to offer."""

    with make_client() as client:
        with (
            pytest.raises(WebSocketDisconnect) as refused,
            client.websocket_connect("/ws/demo?role=viewer_en") as ws,
        ):
            ws.receive_json()
        assert refused.value.code == 4403
        assert client.get("/healthz").json()["viewers"] == 0


def test_unknown_session_is_refused_at_the_socket() -> None:
    with make_client() as client:
        try:
            with client.websocket_connect("/ws/nope") as ws:
                ws.receive_json()
        except WebSocketDisconnect as exc:
            assert exc.code == 4404
        else:  # pragma: no cover
            raise AssertionError("expected the socket to be refused")


def test_late_joiner_is_immediately_oriented() -> None:
    """No attendee should ever be staring at a blank screen mid-lecture.

    This is what the hub's hello/status/segment snapshot exists for, and it also
    guards a real bug: hello was originally broadcast once at startup and never
    cached, so a mid-lecture joiner never learned the session identity.
    """

    with make_client() as client:
        with client.websocket_connect("/ws/demo") as first:
            next_message_of_type(first, "segment")

        # A second viewer connects while the lecture is already running.
        with client.websocket_connect("/ws/demo") as second:
            hello = next_message_of_type(second, "hello")
            assert hello["data"]["session_name"]
            assert next_message_of_type(second, "segment")["data"]["text"]


def test_several_viewers_are_served_independently() -> None:
    with (
        make_client() as client, client.websocket_connect("/ws/demo") as viewer_a,
        client.websocket_connect("/ws/demo") as viewer_b,
    ):
        assert next_message_of_type(viewer_a, "segment")["data"]["text"]
        assert next_message_of_type(viewer_b, "segment")["data"]["text"]


def test_viewer_count_reflects_connected_sockets() -> None:
    with make_client() as client:
        assert client.get("/healthz").json()["viewers"] == 0

        with client.websocket_connect("/ws/demo") as ws:
            next_message_of_type(ws, "segment")
            assert client.get("/healthz").json()["viewers"] == 1

        # After the context manager exits the socket is closed and unsubscribed.
        assert client.get("/healthz").json()["viewers"] == 0


def test_admin_connection_is_not_counted_as_an_audience() -> None:
    """An operator console must not look like a viewing device.

    The gate that pauses paid transcription and translation keys off the viewer
    count, so an admin-only connection must leave it at zero.
    """

    with make_client() as client, client.websocket_connect("/ws/demo?role=admin") as ws:
        next_message_of_type(ws, "segment")
        payload = client.get("/healthz").json()
        assert payload["viewers"] == 0
        assert payload["connections"] == 1


class IdlePublisher:
    async def run(self, hub, *, session_id, stop):
        await stop.wait()


def test_idle_client_answers_heartbeats_and_remains_connected(monkeypatch) -> None:
    monkeypatch.setattr(websocket_routes, "HEARTBEAT_INTERVAL_S", 0.01)
    monkeypatch.setattr(websocket_routes, "CLIENT_IDLE_TIMEOUT_S", 0.1)
    with make_client(IdlePublisher()) as client, client.websocket_connect("/ws/demo") as ws:
        next_message_of_type(ws, "hello")
        for _ in range(5):
            next_message_of_type(ws, "ping")
            ws.send_json({"type": "pong"})
        assert client.get("/healthz").json()["viewers"] == 1


def test_unresponsive_client_is_disconnected_and_removed(monkeypatch) -> None:
    monkeypatch.setattr(websocket_routes, "HEARTBEAT_INTERVAL_S", 0.01)
    monkeypatch.setattr(websocket_routes, "CLIENT_IDLE_TIMEOUT_S", 0.05)
    with make_client(IdlePublisher()) as client:
        with client.websocket_connect("/ws/demo") as ws:
            next_message_of_type(ws, "hello")
            with pytest.raises(WebSocketDisconnect) as error:
                while True:
                    ws.receive_json()
            assert error.value.code == 1001
        assert client.get("/healthz").json()["viewers"] == 0


# --- the QR URL, and the flow that could not work ---------------------------------


def test_opening_the_page_at_localhost_still_produces_a_scannable_qr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The documented primary flow, which was the one flow that was broken.

    The README tells the operator to open the landing page on the machine running the
    server and scan the code from a phone. Opening it at ``localhost`` is the obvious
    thing to type there, and deriving the URL blindly from the request produced a QR
    encoding ``localhost`` — which no phone can resolve, because ``localhost`` on a
    phone is the phone. So a loopback host is replaced by this machine's LAN address.
    """

    monkeypatch.setattr(routes, "lan_ip", lambda: "192.0.2.10")

    with make_client() as client:
        url = client.get(
            "/api/qr-url", headers={"host": "localhost:8000"}
        ).json()["url"]

    assert url == "http://192.0.2.10:8000/s/demo"


@pytest.mark.parametrize(
    "host",
    [
        "localhost:8000",
        "127.0.0.1:8000",
        "[::1]:8000",
        # A bind address. This is what the operations doc tells the operator to use, so
        # it is what they type into the browser next: it opens on this machine and is
        # refused on every phone, which is how it got past the loopback-only check.
        "0.0.0.0:8000",
        "[::]:8000",
        # An address DHCP never replaced, reachable only across one wire segment.
        "169.254.7.7:8000",
    ],
)
def test_every_spelling_of_this_machine_is_replaced(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setattr(routes, "lan_ip", lambda: "192.0.2.10")

    with make_client() as client:
        url = client.get("/api/qr-url", headers={"host": host}).json()["url"]

    assert url == "http://192.0.2.10:8000/s/demo"


def test_without_a_lan_address_the_request_host_is_still_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A URL that needs the request host beats no URL at all."""

    monkeypatch.setattr(routes, "lan_ip", lambda: None)

    with make_client() as client:
        url = client.get(
            "/api/qr-url", headers={"host": "localhost:8000"}
        ).json()["url"]

    assert url == "http://localhost:8000/s/demo"


def test_the_qr_image_encodes_the_same_url_as_the_printed_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The image and the fallback text must agree, or one of them lies.

    Compared byte for byte against an image generated for the expected URL, because
    nothing in this environment can decode a QR code back to its contents. Generating
    the reference proves what the served bytes contain just as well.
    """

    monkeypatch.setattr(routes, "lan_ip", lambda: "192.0.2.10")

    with make_client() as client:
        served = client.get("/qr.png", headers={"host": "localhost:8000"}).content
        printed = client.get(
            "/api/qr-url", headers={"host": "localhost:8000"}
        ).json()["url"]

    reference = io.BytesIO()
    qrcode.make(printed).save(reference, format="PNG")

    assert served == reference.getvalue()
    assert printed == "http://192.0.2.10:8000/s/demo"


@pytest.mark.parametrize(
    "host,expected",
    [
        ("localhost", True),
        ("LOCALHOST", True),
        ("127.0.0.1", True),
        ("127.9.9.9", True),
        ("::1", True),
        ("[::1]", True),
        # A bind address is not a destination. This is the case that got past a
        # loopback-only check and put 0.0.0.0 into a QR code.
        ("0.0.0.0", True),
        ("::", True),
        ("[::]", True),
        ("169.254.7.7", True),
        ("fe80::1", True),
        ("192.0.2.10", False),
        ("subs.example.test", False),
        ("", False),
    ],
)
def test_local_only_detection(host: str, expected: bool) -> None:
    """`localhost` is a name, not an address, so both spellings have to be handled -
    and a bind address is not an address anybody else can use at all."""

    assert routes._is_local_only(host) is expected


# --- where the frontend comes from -------------------------------------------------------


def test_an_installed_copy_serves_its_own_frontend_not_the_working_directorys(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: with no checkout to find, the root fell back to the working directory.

    Run from a directory that happened to hold a ``web`` folder, an installed copy served
    that folder to the whole network instead of its own pages.
    """

    (tmp_path / "web" / "static").mkdir(parents=True)
    (tmp_path / "web" / "index.html").write_text("someone else's page")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(routes, "checkout_root", lambda: None)

    chosen = routes.web_dir(Settings(_env_file=None))

    assert chosen != tmp_path / "web"
    assert chosen == routes.Path(routes.__file__).resolve().parent.parent / "web"


def test_a_checkout_serves_the_web_folder_beside_its_pyproject() -> None:
    from livesubs.config import checkout_root

    root = checkout_root()
    assert root is not None
    assert routes.web_dir(Settings(_env_file=None)) == root / "web"


def test_healthz_answers_503_when_the_pipeline_has_died() -> None:
    """A probe that says 200 either way cannot be used to restart a dead pipeline."""

    class Dies:
        async def run(self, hub, *, session_id, stop):
            raise RuntimeError("capture exploded")

    with make_client(Dies()) as client:
        deadline = 50
        response = client.get("/healthz")
        while response.status_code == 200 and deadline:
            deadline -= 1
            import time

            time.sleep(0.02)
            response = client.get("/healthz")

        assert response.status_code == 503
        assert response.json()["ok"] is False


def test_a_failing_publisher_calls_the_failure_hook_once() -> None:
    called = []

    class Dies:
        async def run(self, hub, *, session_id, stop):
            raise RuntimeError("capture exploded")

    app = create_app(Settings(_env_file=None), Dies(), on_failure=lambda: called.append(1))
    with TestClient(app):
        import time

        for _ in range(50):
            if called:
                break
            time.sleep(0.02)

    assert called == [1]
    assert app.state.sessions.require("demo").failed is True
