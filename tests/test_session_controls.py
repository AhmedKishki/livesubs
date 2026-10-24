"""Runtime session controls acknowledge intent, not recogniser activation."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from livesubs.config import Settings
from livesubs.server import create_app


class ControlledPublisher:
    supported_languages = ("en", "de", "ar")

    def __init__(self):
        self.requested = "auto"
        self.revision = 0

    def set_speaker(self, value):
        changed = value != self.requested
        self.requested = value
        self.revision += changed
        return changed

    def speaker_snapshot(self):
        return {"requested": self.requested, "active": None, "state": "paused",
                "revision": self.revision, "error": None}

    async def run(self, hub, *, session_id, stop: asyncio.Event):
        await stop.wait()


def client_for(publisher=None, fallback="en"):
    return TestClient(create_app(
        Settings(_env_file=None, session={"default_language": fallback}),
        publisher or ControlledPublisher(),
    ))


def test_fixed_offer_and_distinct_fallback():
    with client_for(fallback="ar") as client:
        data = client.get("/api/session").json()
        assert data["languages"] == ["en", "de", "ar"]
        assert data["source"] == "en+de"
        assert data["default"] == "ar"
        assert data["default_language"] == "ar"
        assert data["recognition"]["requested"] == "auto"


def test_settings_revision_and_noop_are_independent_of_recognition_revision():
    with client_for() as client:
        post = lambda data: client.post("/api/session/demo/settings", json=data).json()
        first = post({"speaker": "de", "default_language": "ar"})
        assert first["revision"] == 1
        assert first["speaker"] == "de"
        assert first["recognition"]["active"] is None
        assert post({"speaker": "de", "default_language": "ar"}) == first
        second = post({"default_language": "de"})
        assert second["revision"] == 2
        assert second["recognition"]["revision"] == 1
        assert client.get("/api/stats").json()["session"]["revision"] == 2


@pytest.mark.parametrize("body", [{}, [], None, {"speaker": "ar"},
    {"speaker": "de", "default_language": "xx"}, {"extra": 1}, {"speaker": 1}])
def test_invalid_settings_are_atomic(body):
    with client_for() as client:
        response = client.post("/api/session/demo/settings", content=json.dumps(body),
                               headers={"content-type": "application/json"})
        assert response.status_code == 422
        assert client.get("/api/session").json()["speaker"] == "auto"


@pytest.mark.parametrize(("origin", "status"), [
    ("http://testserver", 200), ("http://TESTSERVER:80", 200),
    ("null", 403), ("https://testserver", 403), ("http://foreign", 403),
    ("http://testserver:81", 403),
    ("http://testserver:0", 403), ("http://testserver@foreign", 403),
    ("http://testserver/path", 403),
])
def test_browser_origin_is_same_origin_only(origin, status):
    with client_for() as client:
        response = client.post("/api/session/demo/settings", json={"speaker": "en"},
                               headers={"origin": origin})
        assert response.status_code == status
        assert "access-control-allow-origin" not in response.headers


def test_content_type_json_and_unknown_session():
    with client_for() as client:
        assert client.post("/api/session/nope/settings", json={"speaker": "en"}).status_code == 404
        assert client.post("/api/session/demo/settings", content="{}").status_code == 415
        assert client.post("/api/session/demo/settings", content="{",
                           headers={"content-type": "application/json"}).status_code == 422


@pytest.mark.parametrize("languages", [("de", "ar"), (), None])
def test_incapable_publisher_rejected_instead_of_narrowed(languages):
    publisher = ControlledPublisher()
    publisher.supported_languages = languages
    with pytest.raises(ValueError, match="all.*en.*de.*ar"):
        client_for(publisher)


def test_legacy_role_resolves_current_fallback_but_existing_socket_is_pinned():
    with client_for() as client, client.websocket_connect("/ws/demo?role=viewer") as old:
        assert old.receive_json()["data"]["language"] == "en"
        client.post("/api/session/demo/settings", json={"default_language": "ar"})
        with client.websocket_connect("/ws/demo?role=viewer") as new:
            assert new.receive_json()["data"]["language"] == "ar"
        session = client.app.state.sessions.get("demo")
        assert session.hub.viewers_by_language["en"] == 1


def test_explicit_viewer_role_survives_fallback_change_and_reconnect():
    with client_for() as client:
        with client.websocket_connect("/ws/demo?role=viewer_de") as first:
            assert first.receive_json()["data"]["language"] == "de"
        assert client.post("/api/session/demo/settings", json={"default_language": "ar"}).status_code == 200
        with client.websocket_connect("/ws/demo?role=viewer_de") as reconnected:
            assert reconnected.receive_json()["data"]["language"] == "de"
