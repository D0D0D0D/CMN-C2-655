# CMN-C2-655 — Integration Test: real-service config plumbing
# review finding, this session: TripPlanWorkflowGraphNode.get_subgraph()
# snapshots the config dict into the cached inner-graph instance the FIRST
# time it is called (at compile() time, via register_nodes()). If real
# backends were injected into agent.config AFTER compile(), the already-built
# inner graph would never see them. Fixed in src/api/server.py by building
# the full config (wire_services()) BEFORE constructing the outer graph.
# This test proves the FIX generically: config keys present at construction
# time DO reach the inner graph's services.

from langgraph.checkpoint.memory import InMemorySaver

from src.graph.graph import CorporateGroupTripPlannerGraph
from src.services.policy_service import PolicyService


class _FakeDB:
    def __init__(self):
        self.rows = {}

    def fetch_one(self, query, params=None):
        key = (params or {}).get("org_unit")
        matches = [v for (org, _version), v in self.rows.items() if org == key]
        return matches[0] if matches else None

    def fetch_many(self, query, params=None):
        return []

    def execute(self, query, params=None):
        params = params or {}
        if "org_unit" in params and "version" in params and "clauses" in params:
            self.rows[(params["org_unit"], params["version"])] = {
                "version": params["version"], "clauses": params["clauses"],
            }


class TestServiceWiringReachesInnerGraph:
    def test_policy_db_configured_before_construction_is_used_by_inner_graph(self):
        db = _FakeDB()
        # Pre-populate as if an operator had already /ingest'd a policy.
        PolicyService(db).upsert_policy(org_unit="default", version="v1",
                                         effective_date="2026-01-01",
                                         clauses=[{"clause_no": "1", "text": "上限5万円",
                                                   "constraint_type": "per_person_cap",
                                                   "limit_value": 50000}])

        # Config built BEFORE construction (the fixed pattern) — this is what
        # server.py's build_agent_config() -> CorporateGroupTripPlannerGraph(config=...)
        # sequence does.
        config = {"memory_enabled": True, "hitl": {"enabled": True, "max_hitl": 24},
                   "policy_db": db}
        agent = CorporateGroupTripPlannerGraph(config=config)
        agent.compile(checkpointer=InMemorySaver())

        from framework.schemas.invocation_context import InvocationContext
        from framework.schemas.trust_level import TrustLevel

        ctx = InvocationContext(caller_trust_level=TrustLevel.VERIFIED_EXTERNAL)
        input_context = {"mode": "plan", "request_params": {"party_size": 5, "destination_hint": "Kyoto",
                                                              "org_unit": "default"}}
        result = agent.invoke("", ctx=ctx, input_context=input_context)
        assert result["status"] == "awaiting_human"

        resumed = agent.resume(thread_id=result["thread_id"], feedback="承認します")
        assert resumed["output"]["json"]["policy_status"] == "verified"
