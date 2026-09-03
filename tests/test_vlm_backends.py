"""The OpenAI-compatible VLM client, and the three backends that share it.

Phase 2.9. `ollama`, `openrouter` and `local_http` are one client behind three
names, so the thing worth protecting is that the NAME still changes the right
knobs: the default URL, whether a key is demanded up front, and which attribution
headers go out. Get that wrong and the failure is silent -- frames go to the wrong
endpoint, or a key meant for one provider is Bearer'd to another.

Nothing here touches the network. `urllib.request.urlopen` is monkeypatched, so
these run in CI with no key and no cost -- which matters, because the whole point
of Phase 2.9 is that the real thing had never been measured.
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from app.detection.vlm.local_http_v1 import DEFAULT_MODEL, LocalHttpVerifier
from app.detection.vlm.registry import OPENAI_COMPATIBLE, VlmProfile, get_verifier

REPLY = '{"is_pothole": true, "confidence": 0.8, "severity": "deep", "rationale": "cavity"}'


class FakeResponse(io.BytesIO):
    """Just enough of an http.client.HTTPResponse for a `with urlopen(...)` block."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


@pytest.fixture
def captured(monkeypatch):
    """Capture the outgoing Request instead of sending it."""
    sent: dict = {}

    def fake_urlopen(req, timeout=None):
        sent["url"] = req.full_url
        sent["headers"] = dict(req.headers)
        sent["body"] = json.loads(req.data.decode("utf-8"))
        sent["timeout"] = timeout
        return FakeResponse(
            json.dumps({"choices": [{"message": {"content": REPLY}}]}).encode("utf-8")
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return sent


class TestRequestShape:
    def test_the_image_goes_out_as_a_data_uri_block(self, captured):
        LocalHttpVerifier(url="http://x/v1/chat/completions").verify(b"\xff\xd8jpeg", {})
        content = captured["body"]["messages"][0]["content"]
        image = next(c for c in content if c["type"] == "image_url")
        assert image["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert any(c["type"] == "text" for c in content)

    def test_temperature_is_zero_so_a_rerun_is_a_rerun(self, captured):
        """A verifier that answers differently on the same frame cannot be measured."""
        LocalHttpVerifier(url="http://x/v1/chat/completions").verify(b"j", {})
        assert captured["body"]["temperature"] == 0

    def test_the_verdict_is_parsed_back(self, captured):
        v = LocalHttpVerifier(url="http://x/v1/chat/completions", model_id="m").verify(b"j", {})
        assert (v.is_pothole, v.confidence, v.severity, v.model_id) == (True, 0.8, "deep", "m")

    def test_model_id_defaults_when_empty(self, captured):
        LocalHttpVerifier(url="http://x/v1/chat/completions").verify(b"j", {})
        assert captured["body"]["model"] == DEFAULT_MODEL

    def test_a_missing_url_is_refused_at_construction(self):
        """Not at the first call, which would be several hundred frames later."""
        with pytest.raises(ValueError, match="vlm_http_url"):
            LocalHttpVerifier(url="")


class TestAuthAndHeaders:
    def test_no_key_means_no_authorization_header(self, captured):
        """Ollama is keyless. Sending an empty Bearer makes some servers 401."""
        LocalHttpVerifier(url="http://x/v1/chat/completions").verify(b"j", {})
        assert not any(k.lower() == "authorization" for k in captured["headers"])

    def test_a_key_is_sent_as_a_bearer(self, captured):
        LocalHttpVerifier(url="http://x/v1/chat/completions", api_key="sk-1").verify(b"j", {})
        assert captured["headers"]["Authorization"] == "Bearer sk-1"

    def test_extra_headers_are_merged(self, captured):
        LocalHttpVerifier(
            url="http://x/v1/chat/completions", extra_headers={"X-Title": "RoadWatch"}
        ).verify(b"j", {})
        assert captured["headers"]["X-title"] == "RoadWatch"


class TestJsonMode:
    def test_on_by_default(self, captured):
        LocalHttpVerifier(url="http://x/v1/chat/completions").verify(b"j", {})
        assert captured["body"]["response_format"] == {"type": "json_object"}

    def test_can_be_turned_off_for_models_that_reject_it(self, captured):
        """Some vision models 400 on response_format. The field must then be absent,
        not sent as null -- parse_verdict's regex is the fallback."""
        LocalHttpVerifier(url="http://x/v1/chat/completions", json_mode=False).verify(b"j", {})
        assert "response_format" not in captured["body"]


def test_an_http_error_carries_its_body(monkeypatch):
    """The status line alone never says WHICH cause fired -- bad key, unknown model,
    a text-only model, no credit. hybrid_v1 logs only str(e), so the body has to be
    folded into the message or the operator is left guessing."""
    def boom(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, 400, "Bad Request", {},
            io.BytesIO(b'{"error":{"message":"model does not support images"}}'),
        )

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError, match="does not support images"):
        LocalHttpVerifier(url="http://x/v1/chat/completions", model_id="mini").verify(b"j", {})


def test_an_unreadable_error_body_does_not_mask_the_error(monkeypatch):
    class Unreadable(io.BytesIO):
        def read(self, *a):
            raise OSError("connection reset")

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 500, "Server Error", {}, Unreadable())

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError, match="500"):
        LocalHttpVerifier(url="http://x/v1/chat/completions").verify(b"j", {})


class TestRegistry:
    """The backend NAME is the whole user-facing API here, so it has to pick right."""

    def _select(self, monkeypatch, **overrides):
        from app.config import settings

        base = {
            "vlm_backend": "none", "vlm_api_key": "", "vlm_http_url": "",
            "vlm_model_id": "", "vlm_timeout": 30.0, "vlm_json_mode": True,
            "vlm_http_referer": "", "vlm_http_title": "",
        }
        for k, v in {**base, **overrides}.items():
            monkeypatch.setattr(settings, k, v)
        return get_verifier()

    def test_none_disables_verification(self, monkeypatch):
        assert self._select(monkeypatch, vlm_backend="none") is None

    def test_ollama_needs_no_key_and_knows_its_own_url(self, monkeypatch):
        v = self._select(monkeypatch, vlm_backend="ollama")
        assert v.url == "http://localhost:11434/v1/chat/completions"
        assert v.api_key == ""

    def test_openrouter_refuses_to_start_without_a_key(self, monkeypatch):
        """Caught here rather than as a 401 on frame 1 of a long sweep."""
        with pytest.raises(ValueError, match="VLM_API_KEY"):
            self._select(monkeypatch, vlm_backend="openrouter")

    def test_openrouter_url_and_key(self, monkeypatch):
        v = self._select(monkeypatch, vlm_backend="openrouter", vlm_api_key="sk-or")
        assert v.url == "https://openrouter.ai/api/v1/chat/completions"
        assert v.api_key == "sk-or"

    def test_attribution_headers_are_openrouter_only(self, monkeypatch):
        """A local server has no use for them, and they would leak the deployment
        URL to whatever endpoint VLM_HTTP_URL happens to point at."""
        router = self._select(
            monkeypatch, vlm_backend="openrouter", vlm_api_key="k",
            vlm_http_referer="https://roadwatch.example", vlm_http_title="RoadWatch",
        )
        assert router.extra_headers == {
            "HTTP-Referer": "https://roadwatch.example", "X-Title": "RoadWatch",
        }
        local = self._select(
            monkeypatch, vlm_backend="ollama",
            vlm_http_referer="https://roadwatch.example", vlm_http_title="RoadWatch",
        )
        assert local.extra_headers == {}

    def test_explicit_url_overrides_the_backend_default(self, monkeypatch):
        """So ollama on another host, or an OpenRouter-compatible proxy, still works."""
        v = self._select(
            monkeypatch, vlm_backend="ollama", vlm_http_url="http://gpu-box:11434/v1/chat/completions"
        )
        assert v.url == "http://gpu-box:11434/v1/chat/completions"

    def test_local_http_still_demands_an_explicit_url(self, monkeypatch):
        """It is the "anything else" backend -- there is no sane default to guess."""
        with pytest.raises(ValueError, match="vlm_http_url"):
            self._select(monkeypatch, vlm_backend="local_http")

    def test_json_mode_reaches_the_client(self, monkeypatch):
        v = self._select(monkeypatch, vlm_backend="ollama", vlm_json_mode=False)
        assert v.json_mode is False

    @pytest.mark.parametrize("backend", sorted(OPENAI_COMPATIBLE))
    def test_every_openai_compatible_backend_builds_the_same_client(self, monkeypatch, backend):
        v = self._select(
            monkeypatch, vlm_backend=backend, vlm_api_key="k",
            vlm_http_url="http://x/v1/chat/completions",
        )
        assert isinstance(v, LocalHttpVerifier)


# ── VlmProfile: a per-caller choice, not a global mutation ────────────────────


def test_a_profile_does_not_touch_the_settings_singleton(monkeypatch):
    """THE PROPERTY THIS TYPE EXISTS FOR.

    get_verifier() read settings.vlm_backend, so a per-request choice was not
    expressible. The only precedent for overriding it is scripts/vlm_eval.py, which
    assigns to `settings` directly -- safe in a single-threaded CLI, and actively
    dangerous in a request handler: under `uvicorn --workers 2` one request's mutation
    changes the backend for every other request in flight, which for this feature
    means roadway imagery sent to a provider the operator did not choose.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "vlm_backend", "none")
    verifier = get_verifier(VlmProfile(backend="ollama", model_id="m"))
    assert verifier is not None                 # the profile was honoured...
    assert settings.vlm_backend == "none"       # ...and the singleton is untouched


def test_no_profile_reproduces_todays_behaviour(monkeypatch):
    """Additive by construction: every existing call site passes nothing."""
    from app.config import settings

    monkeypatch.setattr(settings, "vlm_backend", "none")
    assert get_verifier() is None
    monkeypatch.setattr(settings, "vlm_backend", "ollama")
    assert get_verifier() is not None


def test_the_api_key_is_an_env_var_name_not_a_value(monkeypatch):
    """So a profile can be logged or echoed without leaking a credential."""
    monkeypatch.setenv("TEST_VLM_KEY", "sk-secret-value")
    profile = VlmProfile(backend="openrouter", model_id="m", api_key_env="TEST_VLM_KEY")

    # The secret appears nowhere in the profile itself.
    assert "sk-secret-value" not in repr(profile)
    assert profile.api_key_env == "TEST_VLM_KEY"
    # But it resolves when actually needed.
    assert profile.api_key() == "sk-secret-value"


def test_an_unset_key_env_resolves_empty_rather_than_raising():
    profile = VlmProfile(backend="ollama", api_key_env="NOT_SET_ANYWHERE_12345")
    assert profile.api_key() == ""
    assert VlmProfile(backend="ollama").api_key() == ""


def test_openrouter_still_refuses_without_a_key(monkeypatch):
    """The existing guard must survive the refactor -- it is what stops an
    unauthenticated call to a paid endpoint."""
    monkeypatch.delenv("NOT_SET_ANYWHERE_12345", raising=False)
    with pytest.raises(ValueError, match="VLM_API_KEY"):
        get_verifier(VlmProfile(backend="openrouter", api_key_env="NOT_SET_ANYWHERE_12345"))


def test_a_profile_is_frozen():
    """Immutable, so passing one around cannot become the mutation it replaced."""
    profile = VlmProfile(backend="ollama")
    with pytest.raises(Exception):
        profile.backend = "openrouter"  # type: ignore[misc]
