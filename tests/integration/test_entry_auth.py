# CMN-C2-655 — Entry authentication on the standalone HTTP adapter.
#
# The adapter used to default an unstamped caller to VERIFIED_EXTERNAL
# (`getattr(request.state, "trust_level", TrustLevel.VERIFIED_EXTERNAL)`), so an
# unauthenticated call passed the S-1 trust gate — including the write routes
# /ingest and /feedback. These tests pin the replacement contract:
#
#   verified middleware trust -> used as-is
#   no middleware trust + valid INVOKE_AUTH_TOKEN Bearer -> VERIFIED_EXTERNAL
#   neither -> 401
#
# _get_agent() is monkeypatched throughout: it resolves TRIP_DB_URL and builds
# real repositories, which is not what these tests are about. A rejected caller
# must never reach it at all, which is asserted directly below.

import pytest
from fastapi.testclient import TestClient
from framework.schemas.trust_level import TrustLevel

from src.api import server


_TOKEN = "entry-token-for-testing"
_PLAN_BODY = {"input": "10 名で箱根に社員旅行", "request_params": {"party_size": 10}}
_INGEST_BODY = {"ingest_kind": "policy", "rows": [{"k": "v"}]}
_FEEDBACK_BODY = {"plan_id": "plan-1"}

# (route, body) for every authenticated business route.
_ROUTES = [
    ("/invoke", _PLAN_BODY),
    ("/ingest", _INGEST_BODY),
    ("/feedback", _FEEDBACK_BODY),
]


class _RecordingAgent:
    """Stand-in for the compiled graph; records whether it was reached."""

    def __init__(self):
        self.invoked = 0
        self.resumed = 0
        self._secrets_provider = None
        self.last_ctx = None

    def invoke(self, user_input="", ctx=None, input_context=None, **kwargs):
        self.invoked += 1
        self.last_ctx = ctx
        return {"status": "success", "output": {"markdown": "ok", "json": {}}}

    def resume(self, thread_id="", feedback=""):
        self.resumed += 1
        return {"status": "success", "output": {"markdown": "ok", "json": {}}}


@pytest.fixture
def agent(monkeypatch):
    """Install a recording agent and neutralise bound_secrets."""
    recorder = _RecordingAgent()
    monkeypatch.setattr(server, "_get_agent", lambda: recorder)

    class _NullBinder:
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(server, "bound_secrets", lambda _provider: _NullBinder())
    return recorder


@pytest.fixture
def client():
    return TestClient(server.app)


class _StampTrust:
    """Raw ASGI wrapper standing in for the platform's auth middleware.

    `app.middleware("http")` cannot be used: `server.app` is a module-level
    application that TestClient has already started, and Starlette refuses to
    add middleware after startup. Starlette backs `request.state` with
    `scope["state"]`, so seeding that key is the equivalent stamp one layer
    lower, and it exercises the real HTTP path rather than calling the auth
    helper directly.
    """

    def __init__(self, app, **state):
        self._app = app
        self._state = state

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            scope["state"] = {**scope.get("state", {}), **self._state}
        await self._app(scope, receive, send)


class TestUnauthenticatedIsRejected:
    """No middleware trust and no token -> 401, on every business route."""

    @pytest.mark.parametrize("route,body", _ROUTES)
    def test_no_credentials_returns_401(self, client, agent, monkeypatch, route, body):
        monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)

        response = client.post(route, json=body)

        assert response.status_code == 401
        # The rejected caller must not reach the graph at all — this is the
        # reason auth runs before _get_agent(), not inside _build_ctx().
        assert agent.invoked == 0
        assert agent.resumed == 0

    @pytest.mark.parametrize("route,body", _ROUTES)
    def test_wrong_token_returns_401(self, client, agent, monkeypatch, route, body):
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)

        response = client.post(route, json=body, headers={"Authorization": "Bearer wrong-token"})

        assert response.status_code == 401
        assert agent.invoked == 0

    @pytest.mark.parametrize("route,body", _ROUTES)
    def test_token_configured_but_absent_from_request_returns_401(
        self, client, agent, monkeypatch, route, body
    ):
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)

        response = client.post(route, json=body)

        assert response.status_code == 401
        assert agent.invoked == 0

    def test_middleware_stamped_anonymous_string_is_still_rejected(self, agent, monkeypatch):
        """A middleware that explicitly stamps ANONYMOUS (as the raw string) is
        unauthenticated, not "verified because the attribute exists"."""
        monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
        stamped = TestClient(_StampTrust(server.app, trust_level="ANONYMOUS"))

        response = stamped.post("/invoke", json=_PLAN_BODY)

        assert response.status_code == 401
        assert agent.invoked == 0


class TestAuthenticatedIsAccepted:
    @pytest.mark.parametrize("route,body", _ROUTES)
    def test_valid_token_is_accepted(self, client, agent, monkeypatch, route, body):
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)

        response = client.post(route, json=body, headers={"Authorization": f"Bearer {_TOKEN}"})

        assert response.status_code == 200
        assert agent.invoked == 1

    def test_token_promotion_reaches_the_graph_as_verified_external(
        self, client, agent, monkeypatch
    ):
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)

        client.post("/invoke", json=_PLAN_BODY, headers={"Authorization": f"Bearer {_TOKEN}"})

        assert agent.last_ctx.caller_trust_level is TrustLevel.VERIFIED_EXTERNAL

    def test_verified_middleware_trust_is_used_without_a_token(self, agent, monkeypatch):
        monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
        stamped = TestClient(_StampTrust(server.app, trust_level=TrustLevel.INTERNAL))

        response = stamped.post("/invoke", json=_PLAN_BODY)

        assert response.status_code == 200
        # Middleware trust is used as-is, not downgraded to VERIFIED_EXTERNAL.
        assert agent.last_ctx.caller_trust_level is TrustLevel.INTERNAL


class TestResumePathIsAuthenticated:
    """The /invoke HITL resume branch never builds an InvocationContext, so a
    check placed inside _build_ctx() would leave it fail-open. Auth runs at the
    top of the handler instead; this pins that."""

    def test_unauthenticated_resume_is_rejected(self, client, agent, monkeypatch):
        monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)
        session_id = "suspended-session-1"
        server._record_suspended_thread(session_id, "thread-abc")
        try:
            response = client.post("/invoke", json={"input": "承認します", "session_id": session_id})

            assert response.status_code == 401
            assert agent.resumed == 0
            # The suspended entry must survive a rejected call — a 401 must not
            # consume the session's pending thread.
            assert session_id in server._suspended_sessions
        finally:
            server._suspended_sessions.pop(session_id, None)

    def test_authenticated_resume_is_accepted(self, client, agent, monkeypatch):
        monkeypatch.setenv("INVOKE_AUTH_TOKEN", _TOKEN)
        session_id = "suspended-session-2"
        server._record_suspended_thread(session_id, "thread-def")
        try:
            response = client.post(
                "/invoke",
                json={"input": "承認します", "session_id": session_id},
                headers={"Authorization": f"Bearer {_TOKEN}"},
            )

            assert response.status_code == 200
            assert agent.resumed == 1
        finally:
            server._suspended_sessions.pop(session_id, None)


class TestHealthStaysOpen:
    def test_health_needs_no_credentials(self, client, monkeypatch):
        monkeypatch.delenv("INVOKE_AUTH_TOKEN", raising=False)

        response = client.get("/health")

        assert response.status_code == 200
        assert response.json()["status"] == "ok"
