"""Runtime session controls acknowledge intent, not recogniser activation."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from livesubs.config import Settings
from livesubs.server import create_app


class ControlledPublisher:
    supported_languages = ("en", "de", "ar")
    speaker_options = tuple(SimpleNamespace(code=code, name=name) for code, name in (
        ("auto", "Auto"), ("en", "English"), ("de", "German"), ("ar", "Arabic")))

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
        assert data["source"] == "multilingual"
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


@pytest.mark.parametrize("body", [{}, [], None, {"speaker": "xx"},
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


def test_speaker_options_are_source_metadata_not_viewer_languages_or_polling_data():
    publisher = ControlledPublisher()
    publisher.speaker_options += (
        SimpleNamespace(code="fr", name="French"),
        SimpleNamespace(code="zh-HK", name="Chinese (Hong Kong)"),
    )
    expected = [{"code": option.code, "name": option.name} for option in publisher.speaker_options]
    with client_for(publisher) as client:
        for path in ("/api/session", "/api/session/demo"):
            metadata = client.get(path).json()
            assert metadata["speaker_options"] == expected
            assert metadata["languages"] == ["en", "de", "ar"]
            assert [target["code"] for target in metadata["targets"]] == ["en", "de", "ar"]
        assert "speaker_options" not in client.get("/api/stats").json()["session"]
        with client.websocket_connect("/ws/demo?role=viewer_ar") as ws:
            hello = ws.receive_json()
            assert hello["v"] == 2
            assert hello["data"]["source"] == "multilingual"
            assert hello["data"]["targets"] == ["en", "de", "ar"]
        for speaker in ("ar", "fr", "zh-HK"):
            result = client.post("/api/session/demo/settings", json={"speaker": speaker})
            assert result.status_code == 200
            assert result.json()["speaker"] == speaker
            assert "speaker_options" not in result.json()


def test_regional_speaker_is_canonicalized_before_validation_and_noop_detection():
    publisher = ControlledPublisher()
    publisher.speaker_options += (SimpleNamespace(code="fr-CA", name="French (Canada)"),)
    with client_for(publisher) as client:
        first = client.post("/api/session/demo/settings", json={"speaker": "FR-ca"})
        assert first.status_code == 200
        assert first.json()["speaker"] == "fr-CA"
        same = client.post("/api/session/demo/settings", json={"speaker": "fr-ca"})
        assert same.json() == first.json()
        assert publisher.revision == 1


@pytest.mark.parametrize(("speaker", "fallback"), [
    ("ar", "de"), ("fr", "ar"), ("en", "fr"), ("fr_CA", "de"),
])
def test_unsupported_model_choice_rejects_entire_update_before_publisher_invalidation(speaker, fallback):
    publisher = ControlledPublisher()
    publisher.speaker_options = publisher.speaker_options[:3]
    with client_for(publisher) as client:
        before = client.get("/api/session").json()
        rejected = client.post("/api/session/demo/settings", json={"speaker": speaker, "default_language": fallback})
        assert rejected.status_code == 422
        after = client.get("/api/session").json()
        for key in ("speaker", "default_language", "revision", "recognition"):
            assert after[key] == before[key]
        assert publisher.revision == 0


def test_simulation_controls_accept_only_advertised_source_choices():
    from livesubs.sim import SimPublisher

    publisher = SimPublisher()
    with client_for(publisher) as client:
        metadata = client.get("/api/session").json()
        assert {option["code"] for option in metadata["speaker_options"]} == {"auto", "en", "de", "ar"}
        assert client.post("/api/session/demo/settings", json={"speaker": "ar"}).status_code == 200
        before = client.get("/api/session").json()
        assert client.post("/api/session/demo/settings", json={"speaker": "fr", "default_language": "de"}).status_code == 422
        after = client.get("/api/session").json()
        assert after["recognition"] == before["recognition"]
        assert after["default_language"] == "en"
