"""AgentCore Platform v1.0 — CMN-C2-655 CorporateGroupTripPlannerAgent server.

Standalone HTTP entry point. Entry points are adapters only — no business
logic here. docs/02_design.md §API / I-O 設計: 3 routes (/ingest /invoke
/feedback) all invoke the SAME outer graph with a `mode`-tagged
`input_context` (AgentCore single-entry design).

HITL resume (docs/02_design.md §HITL設計 resume経路): /invoke checks whether
`session_id` has a suspended checkpoint (via `agent.get_pending_thread`-style
lookup — implemented here as a local thread-id registry keyed by session_id,
since BaseGraph does not expose a public "is this thread suspended" query).
When suspended, calls `agent.resume(thread_id, feedback=...)` instead of
`agent.invoke(...)`.

Entry authentication is implemented by `entry_adapter.auth`:
  - a verified middleware trust (request.state.trust_level) is used as-is;
  - otherwise INVOKE_AUTH_TOKEN Bearer auth promotes ANONYMOUS →
    VERIFIED_EXTERNAL (_resolve_trust raises 401 on token mismatch);
  - an unauthenticated caller (no verified middleware trust AND no token
    promotion) is rejected with 401. The old fallback that defaulted such
    callers to VERIFIED_EXTERNAL was fail-open (S-1 bypass) and is removed.

Authentication runs at the TOP of every business route, BEFORE _get_agent().
Two reasons: (1) an unauthenticated caller must not be able to trigger
TRIP_DB_URL resolution / service construction, and (2) /invoke's HITL resume
branch does not build an InvocationContext at all, so placing the check inside
_build_ctx() would leave resume fail-open. /health stays unauthenticated.

compile(checkpointer=...) is called EXPLICITLY (not the no-arg `compile()`)
because hitl.enabled: true requires a checkpointer attached at compile time
(docs/02_design.md — 1.0.0 wheel does not auto-generate one from config).

IMPORTANT — _wire_real_services() BEFORE compile() (review finding, this
session): `TripPlanWorkflowGraphNode.get_subgraph()` snapshots
`self._config` (a copy of the outer graph's config dict) into the cached
inner-graph instance the FIRST time it is called, and `register_nodes()` /
`compile()` construct that node once, up front. If real DB/LLM/web-search
clients were injected into `agent.config` AFTER `compile()`, the already-built
`TripPlanWorkflowGraphNode` would never see them and the agent would run with
the injection-seam "not wired" fallbacks forever. So this module builds the
full config dict (including any real backends from `_wire_real_services()`)
BEFORE constructing `CorporateGroupTripPlannerGraph`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from threading import Lock
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from framework.schemas.invocation_context import InvocationContext
from framework.schemas.trust_level import TrustLevel
from framework.secrets.base import SecretProvider
from framework.secrets.context import bound_secrets
from shared.secrets import factory as secrets_factory
from shared.services.embedding.azure_embedding import AzureEmbedding
from shared.services.runtime import AsyncServiceRuntime
from shared.services.vector_store.qdrant_vector_store import QdrantVectorStore
from src.entry_adapter.auth import _require_verified_trust, _resolve_trust
from src.graph.graph import CorporateGroupTripPlannerGraph
from src.services.trip_kb_qdrant_adapter import TripKBQdrantAdapter
from src.services.trip_repository_factory import create_trip_repositories


_VECTOR_STORE_CONFIG_KEY = "vector_store"
_EMBEDDING_CONFIG_KEY = "embedding"
_QDRANT_URL_SECRET = "TRIP_KB_QDRANT_URL"
_EMBEDDING_API_KEY_SECRET = "TRIP_KB_EMBEDDING_API_KEY"
_EMBEDDING_ENDPOINT_SECRET = "TRIP_KB_EMBEDDING_ENDPOINT"
_EMBEDDING_DEPLOYMENT_SECRET = "TRIP_KB_EMBEDDING_DEPLOYMENT"
_QDRANT_CONFIG_KEYS = frozenset(
    {
        "collection_name",
        "namespace",
        "agent_name",
        "vector_size",
        "distance",
        "timeout_s",
        "write_timeout_s",
        "max_batch_documents",
        "max_top_k",
    }
)
_EMBEDDING_CONFIG_KEYS = frozenset(
    {
        "api_version",
        "dimensions",
        "provider",
        "timeout_s",
        "batch_size",
        "max_retries",
        "base_delay",
        "max_delay",
        "max_cache_size",
    }
)
_service_runtime: Any | None = None


@asynccontextmanager
async def _lifespan(_application: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await _shutdown_services()


app = FastAPI(title="CMN-C2-655 CorporateGroupTripPlannerAgent", lifespan=_lifespan)

# Suspended-session TTL: entries older than this are evicted on access even if
# the organizer never resumes (review finding — the registry has no
# eviction otherwise and grows unbounded for abandoned sessions).
_SUSPENDED_SESSION_TTL_SECONDS = 24 * 60 * 60


def build_agent_config(secrets: SecretProvider | None = None) -> dict:
    """Builds the base agent config, then applies _wire_real_services() BEFORE the
    graph is constructed (see module docstring "IMPORTANT").

    When a SecretProvider is supplied, the working persistence repositories
    are selected before graph construction. Keeping ``secrets`` optional lets
    unit tests build the deterministic degradation config without provisioning
    deployment secrets.
    """
    config: dict = {
        "max_retry": 3,
        "timeout_seconds": 60,
        "memory_enabled": True,
        "hitl": {"enabled": True, "max_hitl": 24},
        "plans_count": 3,
        "llm": {"model": "claude-sonnet", "temperature": 0.2, "max_tokens": 4000},
        "web_search": {"max_retries": 2, "timeout_seconds": 10},
        "ingest": {"max_rows_per_call": 500},
        "vector_store": {
            "enabled": False,
            "collection_name": "cmn_c2_655_trip_kb",
            "namespace": "cmn",
            "agent_name": "cmn_c2_655",
            "vector_size": 1536,
            "distance": "cosine",
            "timeout_s": 30,
            "write_timeout_s": 60,
            "max_batch_documents": 1000,
            "max_top_k": 100,
        },
        "embedding": {
            "api_version": "2024-02-15-preview",
            "dimensions": 1536,
            "provider": "azure",
            "timeout_s": 30,
            "batch_size": 16,
            "max_retries": 5,
            "base_delay": 1.0,
            "max_delay": 60.0,
            "max_cache_size": 10000,
        },
    }
    if secrets is not None:
        _wire_real_services(config, secrets)
    return config


def _wire_real_services(config: dict, secrets: SecretProvider) -> None:
    """Select persistence backends and mutate ``config`` before compilation."""
    global _service_runtime

    repositories = create_trip_repositories(secrets.require("TRIP_DB_URL"))
    config["policy_db"] = repositories.policy_database
    config["trip_records_db"] = repositories.trip_records_database
    kb_dependencies = _build_trip_kb_dependencies(config, secrets)
    if kb_dependencies is not None:
        vector_store, embedding, runtime = kb_dependencies
        config["trip_kb_vector_store"] = vector_store
        config["trip_kb_embedding"] = embedding
        _service_runtime = runtime


def _build_trip_kb_dependencies(
    config: Mapping[str, Any], secrets: SecretProvider
) -> tuple[TripKBQdrantAdapter, Any, Any] | None:
    """Build the optional SDK Qdrant/Azure pair at the sole KB swap point."""
    raw_store = config.get(_VECTOR_STORE_CONFIG_KEY)
    if raw_store is None:
        return None
    if not isinstance(raw_store, Mapping):
        raise ValueError(f"config.{_VECTOR_STORE_CONFIG_KEY} must be a mapping")
    enabled = raw_store.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"config.{_VECTOR_STORE_CONFIG_KEY}.enabled must be a boolean")
    if not enabled:
        return None

    store_config = _validate_config_section(
        raw_store,
        section=_VECTOR_STORE_CONFIG_KEY,
        allowed=_QDRANT_CONFIG_KEYS | {"enabled"},
        required=_QDRANT_CONFIG_KEYS,
    )
    raw_embedding = config.get(_EMBEDDING_CONFIG_KEY)
    if not isinstance(raw_embedding, Mapping):
        raise ValueError(f"config.{_EMBEDDING_CONFIG_KEY} must be a mapping")
    embedding_config = _validate_config_section(
        raw_embedding,
        section=_EMBEDDING_CONFIG_KEY,
        allowed=_EMBEDDING_CONFIG_KEYS,
        required=_EMBEDDING_CONFIG_KEYS,
    )
    if int(store_config["vector_size"]) != int(embedding_config["dimensions"]):
        raise ValueError("config.vector_store.vector_size must match config.embedding.dimensions")

    qdrant_url = _require_clean(secrets, _QDRANT_URL_SECRET)
    embedding_api_key = _require_clean(secrets, _EMBEDDING_API_KEY_SECRET)
    embedding_endpoint = _require_clean(secrets, _EMBEDDING_ENDPOINT_SECRET)
    embedding_deployment = _require_clean(secrets, _EMBEDDING_DEPLOYMENT_SECRET)
    embedding_config.update(
        {
            "base_url": embedding_endpoint,
            "api_key": embedding_api_key,
            "model": embedding_deployment,
        }
    )
    store_config.update(
        {
            "url": qdrant_url,
            "embedding_base_url": embedding_endpoint,
            "embedding_api_key": embedding_api_key,
            "embedding_model": embedding_deployment,
            "embedding_api_version": embedding_config["api_version"],
            "embedding_provider": embedding_config["provider"],
        }
    )

    runtime = AsyncServiceRuntime()
    try:
        qdrant = QdrantVectorStore(store_config, runtime=runtime)
        embedding = AzureEmbedding(embedding_config, runtime=runtime)
    except BaseException:
        try:
            _close_runtime_after_build_failure(runtime)
        except BaseException:
            # Cleanup must not hide the constructor exception that explains
            # why an explicitly enabled KB could not start.
            pass
        raise
    return TripKBQdrantAdapter(qdrant), embedding, runtime


def _validate_config_section(
    raw: Mapping[str, Any],
    *,
    section: str,
    allowed: frozenset[str],
    required: frozenset[str],
) -> dict[str, Any]:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ValueError(f"config.{section} has unknown keys: {unknown}")
    missing = sorted(
        key for key in required if raw.get(key) is None or (isinstance(raw.get(key), str) and not raw.get(key).strip())
    )
    if missing:
        raise ValueError(f"config.{section} is missing required keys: {missing}")
    return {key: raw[key] for key in required}


def _require_clean(secrets: SecretProvider, key: str) -> str:
    try:
        value = secrets.require(key)
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{key} must be a non-empty value") from exc
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty value")
    return value.strip()


def _close_runtime_after_build_failure(runtime: Any) -> None:
    """Close an SDK runtime from either sync or already-async construction."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(runtime.aclose(timeout=5.0))
        return

    def close_in_thread() -> None:
        try:
            asyncio.run(runtime.aclose(timeout=5.0))
        except BaseException:
            # The constructor exception remains the actionable startup error.
            pass

    from threading import Thread

    thread = Thread(target=close_in_thread)
    thread.start()
    thread.join()


async def _shutdown_services() -> None:
    global _service_runtime

    runtime, _service_runtime = _service_runtime, None
    if runtime is not None:
        await runtime.aclose(timeout=5.0)


# Service construction resolves TRIP_DB_URL, so defer it until the first
# business request. Importing the ASGI app and calling /health do not require a
# provisioned deployment secret; /invoke, /ingest, and /feedback fail closed if
# the required secret is missing.
_agent: CorporateGroupTripPlannerGraph | None = None
_agent_lock = Lock()


def _get_agent() -> CorporateGroupTripPlannerGraph:
    global _agent
    if _agent is None:
        with _agent_lock:
            if _agent is None:
                secrets = secrets_factory(namespace="cmn", agent_name="cmn-c2-655")
                candidate = CorporateGroupTripPlannerGraph(config=build_agent_config(secrets))
                candidate.compile(checkpointer=InMemorySaver())
                candidate.provision_secrets(secrets)
                _agent = candidate
    return _agent


# session_id -> (thread_id, suspended_at_epoch_seconds) registry for suspended
# (awaiting_human) sessions. InvocationContext.thread_id defaults to a fresh
# uuid4 per call; to resume a suspended session we must reuse the ORIGINAL
# thread_id, so we record the mapping the first time a session suspends.
# Guarded by _sessions_lock — FastAPI may run request handlers across
# multiple worker threads (review finding: unguarded dict mutation
# risks lost updates / KeyErrors under concurrent resume attempts).
_suspended_sessions: dict[str, tuple[str, float]] = {}
_sessions_lock = Lock()


def _evict_expired_sessions() -> None:
    cutoff = time.time() - _SUSPENDED_SESSION_TTL_SECONDS
    expired = [sid for sid, (_tid, ts) in _suspended_sessions.items() if ts < cutoff]
    for sid in expired:
        del _suspended_sessions[sid]


def _pop_suspended_thread(session_id: str) -> str | None:
    with _sessions_lock:
        _evict_expired_sessions()
        entry = _suspended_sessions.pop(session_id, None)
        return entry[0] if entry else None


def _record_suspended_thread(session_id: str, thread_id: str) -> None:
    with _sessions_lock:
        _suspended_sessions[session_id] = (thread_id, time.time())


class InvokeRequest(BaseModel):
    input: str = ""
    session_id: str = ""
    request_params: dict = {}
    care_notes: str = ""


class IngestRequest(BaseModel):
    session_id: str = ""
    ingest_kind: str  # "policy" | "trip_records"
    rows: list[dict] = []
    org_unit: str = "default"
    version: str = ""
    effective_date: str = ""


class FeedbackRequest(BaseModel):
    session_id: str = ""
    plan_id: str
    actual_cost: float | None = None
    satisfaction_score: float | None = None
    complaint_text: str = ""
    organizer_edited: bool = False


def _authenticate(request: Request) -> TrustLevel:
    """Authenticate the entry caller, or reject with 401/403.

    Scope note: the entry_adapter part also exposes a tenant/org scope contract,
    but this template has NO scope model — a trip plan is carried in-request via
    input_context and the KB is not partitioned per caller, so no code path
    reads request tenant/org scope. Enforcing authenticate_scope() would gate on
    a scope nothing consumes (a "default" fallback would silently match
    everything), so only the auth core is wired.

    Returns the resolved trust so callers do NOT re-read request.state — the
    fail-open getattr default is exactly the defect this replaces.
    """
    trust, token_authenticated = _resolve_trust(request)
    # Unauthenticated = trust is still ANONYMOUS after resolution and no token
    # promotion. Test the trust VALUE, not attribute presence — a middleware
    # that explicitly stamps ANONYMOUS must still be treated as unauthenticated
    # (→ 401), not silently accepted as "verified because the attr exists".
    if trust is TrustLevel.ANONYMOUS and not token_authenticated:
        raise HTTPException(status_code=401, detail="Authentication is required.")
    _require_verified_trust(trust)
    return trust


def _build_ctx(
    request: Request,
    session_id: str,
    thread_id: str | None = None,
    trust: TrustLevel | None = None,
) -> InvocationContext:
    kwargs = {
        "session_id": session_id or str(uuid4()),
        # Trust comes from _authenticate(), never from a fail-open getattr
        # default. The parameter is required in practice; the None branch only
        # keeps the signature usable for direct unit construction.
        "caller_trust_level": trust if trust is not None else _authenticate(request),
        "caller_id": getattr(request.state, "caller_id", ""),
        # HITL permission comes from the authenticated request state that the
        # platform's auth middleware populates (it is the component that reads
        # and validates the Gateway's X-HITL-Allowed header). The raw client
        # header is deliberately NOT read here — any caller could set it, and
        # forcing a HITL-free run is exactly what would turn the organizer's
        # wall-bounce review into an unattended one. Default True matches the
        # framework's fail-open default for interactive callers.
        # NOTE: this covers the first invoke only. See docs/02_design.md
        # §HITL 設計「既知の制約」— BaseGraph.resume(thread_id, feedback) takes
        # no InvocationContext, so a permission change made while a session is
        # suspended cannot be applied on resume from the template layer.
        "hitl_allowed": getattr(request.state, "hitl_allowed", True),
    }
    if thread_id:
        kwargs["thread_id"] = thread_id
    return InvocationContext(**kwargs)


@app.post("/invoke")
async def invoke(req: InvokeRequest, request: Request):
    # BEFORE _get_agent(): an unauthenticated caller must not reach secret
    # resolution / service construction. Also BEFORE the resume branch below,
    # which never builds an InvocationContext and would otherwise stay
    # fail-open.
    trust = _authenticate(request)
    session_id = req.session_id or str(uuid4())
    agent = _get_agent()
    with bound_secrets(agent._secrets_provider):
        thread_id = _pop_suspended_thread(session_id)
        if thread_id is not None:
            result = agent.resume(thread_id=thread_id, feedback=req.input)
        else:
            ctx = _build_ctx(request, session_id, trust=trust)
            input_context = {
                "mode": "plan",
                "request_params": req.request_params,
                "care_notes": req.care_notes,
            }
            result = agent.invoke(req.input, ctx=ctx, input_context=input_context)

        if result.get("status") == "awaiting_human":
            _record_suspended_thread(session_id, result.get("thread_id", ""))
        result["session_id"] = session_id
        return result


@app.post("/ingest")
async def ingest(req: IngestRequest, request: Request):
    # Write route — authenticated before any secret/service resolution.
    trust = _authenticate(request)
    session_id = req.session_id or str(uuid4())
    agent = _get_agent()
    with bound_secrets(agent._secrets_provider):
        ctx = _build_ctx(request, session_id, trust=trust)
        input_context = {
            "mode": "ingest",
            "ingest_payload": {
                "ingest_kind": req.ingest_kind,
                "rows": req.rows,
                "org_unit": req.org_unit,
                "version": req.version,
                "effective_date": req.effective_date,
            },
        }
        result = agent.invoke("", ctx=ctx, input_context=input_context)
        result["session_id"] = session_id
        return result


@app.post("/feedback")
async def feedback(req: FeedbackRequest, request: Request):
    # Write route — authenticated before any secret/service resolution.
    trust = _authenticate(request)
    session_id = req.session_id or str(uuid4())
    agent = _get_agent()
    with bound_secrets(agent._secrets_provider):
        ctx = _build_ctx(request, session_id, trust=trust)
        input_context = {
            "mode": "feedback",
            "request_params": {
                "plan_id": req.plan_id,
                "actual_cost": req.actual_cost,
                "satisfaction_score": req.satisfaction_score,
                "organizer_edited": req.organizer_edited,
            },
        }
        result = agent.invoke(req.complaint_text, ctx=ctx, input_context=input_context)
        result["session_id"] = session_id
        return result


@app.get("/health")
def health():
    return {"status": "ok", "agent": "CMN-C2-655"}
