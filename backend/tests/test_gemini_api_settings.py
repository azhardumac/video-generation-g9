import json
import asyncio

from fastapi import HTTPException
from starlette.requests import Request
import pytest

from app.gemini_secrets import GeminiSecretStore


class SettingsDB:
    def __init__(self):
        self.values = {}

    def get_setting(self, key):
        return self.values.get(key)

    def put_setting(self, key, value):
        self.values[key] = value


class FakeGeminiProbe:
    def __init__(self, *_args):
        pass

    def test_connection(self):
        return [{"id": "gemini-3.8-flash", "display_name": "Gemini Flash"}]

    def test_generation_endpoint(self):
        return {"success": True, "http_status": 200, "model": "gemini-3.8-flash",
                "api_version": "v1beta", "endpoint_host": "generativelanguage.googleapis.com",
                "endpoint_path": "/v1beta/models/gemini-3.8-flash:generateContent",
                "request_shape": "text_only_no_schema_no_media", "response_text": "ok"}


def key_request(value):
    body = json.dumps({"api_key": value}).encode()
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request({"type": "http", "method": "POST", "path": "/api/story-planner/gemini/key",
                    "query_string": b"", "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())]}, receive)


def test_api_key_backend_storage_connection_test_and_model_selection_never_return_key(tmp_path, monkeypatch):
    from app import main as api
    db = SettingsDB()
    secret_path = tmp_path / "config" / "gemini.json"
    monkeypatch.setattr(api, "db", db)
    monkeypatch.setattr(api, "gemini_secret_store", GeminiSecretStore(secret_path, env={}))
    fake_key = "fake-secret-for-api-test"

    saved = asyncio.run(api.save_gemini_key(key_request(fake_key)))
    assert saved["saved"] is True
    assert fake_key not in json.dumps(saved)
    assert secret_path.stat().st_mode & 0o777 == 0o600
    settings = api.story_planner_config()
    assert fake_key not in json.dumps(settings)
    assert settings["gemini"]["status"] == "configured_not_tested"

    monkeypatch.setattr(api, "GeminiStoryPlanner", FakeGeminiProbe)
    tested = api.test_gemini_connection()
    assert tested["connected"] is True
    assert tested["model"] == "gemini-3.8-flash"
    assert fake_key not in json.dumps(tested)
    selected = api.select_gemini_model(api.GeminiModelRequest(model="gemini-3.8-flash"))
    assert selected["model"] == "gemini-3.8-flash"
    endpoint_test = api.test_gemini_generation_endpoint(
        api.GeminiGenerationEndpointProbeRequest(model="gemini-3.8-flash", confirmed=True))
    assert endpoint_test["success"] is True
    assert endpoint_test["endpoint_path"] == "/v1beta/models/gemini-3.8-flash:generateContent"
    assert fake_key not in json.dumps(endpoint_test)
    assert set(db.values) == {"story_planner_gemini"}  # A probe creates no run or attempt history.
    assert fake_key not in json.dumps(api.story_planner_config())
    assert api.remove_gemini_key()["removed"] is True
    assert api.story_planner_config()["gemini"]["status"] == "missing_key"

    oversized_secret = "do-not-echo-this" * 50
    with pytest.raises(HTTPException) as invalid:
        asyncio.run(api.save_gemini_key(key_request(oversized_secret)))
    assert invalid.value.status_code == 400
    assert oversized_secret not in str(invalid.value.detail)


def test_test_connection_requires_key_and_model_cannot_be_selected_before_account_verification(tmp_path, monkeypatch):
    from app import main as api
    monkeypatch.setattr(api, "db", SettingsDB())
    monkeypatch.setattr(api, "gemini_secret_store", GeminiSecretStore(tmp_path / "secret.json", env={}))
    with pytest.raises(HTTPException) as missing:
        api.test_gemini_connection()
    assert missing.value.status_code == 409
    with pytest.raises(HTTPException) as model:
        api.select_gemini_model(api.GeminiModelRequest(model="gemini-3.8-flash"))
    assert model.value.status_code == 409
    with pytest.raises(HTTPException) as unconfirmed:
        api.test_gemini_generation_endpoint(
            api.GeminiGenerationEndpointProbeRequest(model="gemini-3.8-flash", confirmed=False))
    assert unconfirmed.value.status_code == 400


def test_generation_endpoint_probe_requires_a_recently_account_listed_model(tmp_path, monkeypatch):
    from app import main as api
    db = SettingsDB()
    monkeypatch.setattr(api, "db", db)
    monkeypatch.setattr(api, "gemini_secret_store", GeminiSecretStore(tmp_path / "secret.json", env={"GEMINI_API_KEY": "probe-secret"}))
    with pytest.raises(HTTPException) as untested:
        api.test_gemini_generation_endpoint(
            api.GeminiGenerationEndpointProbeRequest(model="gemini-2.5-flash", confirmed=True))
    assert untested.value.status_code == 409

    db.put_setting("story_planner_gemini", {"status": "connected", "models": [
        {"id": "gemini-2.5-flash", "display_name": "Gemini 2.5 Flash"}], "model": "gemini-2.5-flash"})
    with pytest.raises(HTTPException) as arbitrary:
        api.test_gemini_generation_endpoint(
            api.GeminiGenerationEndpointProbeRequest(model="gemini-3.8-flash", confirmed=True))
    assert arbitrary.value.status_code == 400


def test_generation_endpoint_probe_returns_safe_route_error_without_persisting_it(tmp_path, monkeypatch):
    from app import main as api
    db = SettingsDB()
    monkeypatch.setattr(api, "db", db)
    monkeypatch.setattr(api, "gemini_secret_store", GeminiSecretStore(tmp_path / "secret.json", env={"GEMINI_API_KEY": "probe-private-key"}))
    db.put_setting("story_planner_gemini", {"status": "connected", "models": [
        {"id": "gemini-2.5-flash", "display_name": "Gemini Flash"}], "model": "gemini-2.5-flash"})

    class MissingEndpoint:
        def __init__(self, *_args):
            pass

        def test_generation_endpoint(self):
            from app.gemini_planning import GeminiPlanningError
            raise GeminiPlanningError("model_or_endpoint_incompatible", "Basic endpoint returned 404.",
                diagnostics={"http_status": 404, "provider_status": "NOT_FOUND", "api_version": "v1beta",
                             "endpoint_host": "generativelanguage.googleapis.com",
                             "endpoint_path": "/v1beta/models/gemini-2.5-flash:generateContent"})

    monkeypatch.setattr(api, "GeminiStoryPlanner", MissingEndpoint)
    with pytest.raises(HTTPException) as missing:
        api.test_gemini_generation_endpoint(
            api.GeminiGenerationEndpointProbeRequest(model="gemini-2.5-flash", confirmed=True))
    assert missing.value.status_code == 400
    assert missing.value.detail["diagnostics"]["endpoint_path"] == "/v1beta/models/gemini-2.5-flash:generateContent"
    assert "probe-private-key" not in json.dumps(missing.value.detail)
    assert set(db.values) == {"story_planner_gemini"}


def test_gemini_provider_requires_per_run_remote_consent(monkeypatch):
    from app import main as api
    monkeypatch.setattr(api, "_project", lambda _project_id: {"id": "isolated-project"})
    request = api.CreateFilmRunRequest(prompt="A general story for consent testing.",
        idempotency_key="consent-test", initial_confirmation=True, planner_provider="gemini",
        remote_processing_consent=False)
    try:
        api.create_film_run("isolated-project", request)
        assert False, "Gemini runs must require remote-processing consent"
    except HTTPException as error:
        assert error.status_code == 400
        assert "explicit opt-in" in error.detail

    legacy = api.CreateFilmRunRequest(prompt="A local story.", idempotency_key="legacy-local")
    assert legacy.planner_provider == "local"
    assert legacy.remote_processing_consent is False
