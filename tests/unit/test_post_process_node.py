# CMN-C2-655 — Unit Tests: PostProcessNode

from framework.schemas.agent_status import AgentStatus
from src.nodes.post_process_node import PostProcessNode


def _base_state(**overrides) -> dict:
    state = {
        "caller_trust_level": "VERIFIED_EXTERNAL",
        "status": "success",
        "node_history": [],
        "error_log": [],
        "mode": "plan",
        "result": {
            "plans": [{"destination_area": "Hakone", "itinerary": ["Day 1"],
                       "lodging": "Ryokan", "transport": "Bus",
                       "estimated_cost_per_person": 30000}],
            "selected_plan": {"destination_area": "Hakone", "itinerary": ["Day 1"],
                               "lodging": "Ryokan", "transport": "Bus",
                               "estimated_cost_per_person": 30000},
            "policy_status": "verified",
            "policy_citations": [{"policy_version": "v1", "clause_no": "3", "excerpt": "上限5万円"}],
            "care_requirements": [{"category": "vegetarian", "count": 2}],
            "assumption_notes": [],
            "quality_notes": [],
        },
        "plan_id": "p-1",
    }
    state.update(overrides)
    return state


class TestPostProcessNodePlanMode:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_formats_markdown_and_json(self):
        result = self.node.execute(_base_state())
        assert result["status"] == AgentStatus.SUCCESS.value
        formatted = result["formatted_output"]
        assert "markdown" in formatted and "json" in formatted
        assert "Hakone" in formatted["markdown"]

    def test_fixed_care_task_line_present(self):
        result = self.node.execute(_base_state())
        markdown = result["formatted_output"]["markdown"]
        assert "配慮対象者の個別対応は名簿と突き合わせて実施" in markdown
        assert "AG は個人紐付けを保持しない" in markdown

    def test_care_survey_template_included(self):
        result = self.node.execute(_base_state())
        markdown = result["formatted_output"]["markdown"]
        assert "配布用配慮アンケート" in markdown

    def test_unverified_policy_shows_warning(self):
        state = _base_state()
        state["result"]["policy_status"] = "unverified"
        state["result"]["policy_citations"] = []
        result = self.node.execute(state)
        markdown = result["formatted_output"]["markdown"]
        assert "要確認" in markdown or "規程が未整備" in markdown

    def test_upstream_error_passthrough(self):
        state = _base_state(status=AgentStatus.ERROR.value)
        result = self.node.execute(state)
        assert result["status"] == AgentStatus.ERROR.value
        assert "formatted_output" not in result


class TestPostProcessNodeS3Gate:
    def setup_method(self):
        self.node = PostProcessNode()

    def test_residual_email_blocked(self):
        state = _base_state()
        state["result"]["plans"][0]["rationale"] = "contact organizer@example.com"
        self.node.execute(state)
        # emulate the @final S-3 pipeline call directly on the produced result
        import pytest
        with pytest.raises(RuntimeError):
            self.node._extra_security_gate_output({
                "formatted_output": {"markdown": "residual email organizer@example.com in output"},
            })


class TestPostProcessNodeSelectionPending:
    """Selection-pending output surface.

    When hitl_allowed=false suppresses the wall-bounce dialogue, hitl_refine
    leaves selected_plan empty. post_process must present the candidates as
    unchosen — and must not widen the output surface while doing so.
    """

    def setup_method(self):
        self.node = PostProcessNode()

    def _pending_state(self, plans: list) -> dict:
        state = _base_state()
        state["result"]["plans"] = plans
        state["result"]["selected_plan"] = {}
        return state

    def test_pending_selection_lists_candidates_without_deciding(self):
        state = self._pending_state([
            {"destination_area": "Hakone", "itinerary": ["Day 1"], "lodging": "Ryokan",
             "transport": "Bus", "estimated_cost_per_person": 30000},
            {"destination_area": "Kyoto", "itinerary": ["Day 1"], "lodging": "Hotel",
             "transport": "Rail", "estimated_cost_per_person": 42000},
        ])

        payload = self.node.execute(state)["formatted_output"]

        assert payload["json"]["selection_status"] == "pending_organizer"
        assert payload["json"]["plan"] == {}
        assert len(payload["json"]["candidate_plans"]) == 2
        assert "幹事による選定が未了" in payload["markdown"]
        assert "## 行程表" not in payload["markdown"]

    def test_candidate_projection_drops_rationale_and_care_detail(self):
        """The JSON must not carry fields the Markdown never renders.

        `rationale` is unreviewed LLM prose and `care_accommodations` is
        per-plan care detail — emitting either verbatim widens the C-01
        surface. Both are injected here and must be absent downstream.
        """
        state = self._pending_state([
            {"destination_area": "Hakone", "itinerary": ["Day 1"], "lodging": "Ryokan",
             "transport": "Bus", "estimated_cost_per_person": 30000,
             "rationale": "幹事の田中さんの希望に沿った案",
             "care_accommodations": ["Alice: nut allergy"]},
        ])

        payload = self.node.execute(state)["formatted_output"]
        candidate = payload["json"]["candidate_plans"][0]

        assert "rationale" not in candidate
        assert "care_accommodations" not in candidate
        assert set(candidate) <= {"destination_area", "itinerary", "lodging",
                                  "transport", "estimated_cost_per_person"}
        assert "Alice" not in str(payload["json"])
        assert "田中" not in str(payload["json"])

    def test_selected_plan_is_projected_too(self):
        """The same projection applies on the normal approve path — the JSON
        `plan` previously carried rationale/care detail as well."""
        state = _base_state()
        state["result"]["selected_plan"]["rationale"] = "contact organizer@example.com"

        payload = self.node.execute(state)["formatted_output"]

        assert "rationale" not in payload["json"]["plan"]
        assert "organizer@example.com" not in str(payload["json"])

    def test_nested_json_identifier_is_blocked_by_s3_backstop(self):
        """Allowlisted fields are still scanned: the framework's @final S-3 only
        walks top-level strings, so the template must walk the nested payload."""
        import pytest

        with pytest.raises(RuntimeError, match="S-3 output gate"):
            self.node._extra_security_gate_output({
                "formatted_output": {
                    "markdown": "clean markdown",
                    "json": {"candidate_plans": [{"lodging": "連絡先 organizer@example.com"}]},
                },
            })

    def test_no_candidates_is_not_reported_as_selected(self):
        """Empty synthesis must not emit an 行程表 heading over an empty plan."""
        payload = self.node.execute(self._pending_state([]))["formatted_output"]

        assert payload["json"]["selection_status"] == "no_candidates"
        assert payload["json"]["plan"] == {}
        assert payload["json"]["candidate_plans"] == []
        assert "## 行程表" not in payload["markdown"]
        assert "生成できませんでした" in payload["markdown"]


class TestPostProcessNodeS3OutputWalk:
    """S-3 backstop over the nested JSON payload (review 2026-07-29, High).

    The framework's @final S-3 only inspects top-level result strings. These
    exercise the node through __call__() — the real gate path — so a walker
    that silently skipped a branch would show up as a passing invocation.
    """

    def setup_method(self):
        self.node = PostProcessNode()

    def _state_with_plan_field(self, **plan_overrides) -> dict:
        plan = {"destination_area": "Hakone", "itinerary": ["Day 1"], "lodging": "Ryokan",
                "transport": "Bus", "estimated_cost_per_person": 30000}
        plan.update(plan_overrides)
        state = _base_state()
        state["result"]["plans"] = [plan]
        state["result"]["selected_plan"] = {}
        return state

    def test_japanese_name_in_allowlisted_value_is_blocked(self):
        """The allowlist bounds which KEYS leave; values are LLM-authored, so the
        walk must still catch an identifier inside one."""
        result = self.node(self._state_with_plan_field(lodging="田中太郎さん手配の旅館"))

        assert result["status"] == AgentStatus.ERROR.value
        assert any("name_jp_honorific" in entry for entry in result["error_log"])

    def test_email_followed_by_japanese_text_is_blocked(self):
        """detect_pii's trailing-\\b patterns miss this shape; the supplementary
        pass in identifier_sanitizer is what closes it."""
        # No space before the Japanese text — that is the shape detect_pii's
        # trailing-\b pattern fails on. With a space the ordinary email pattern
        # would match and the test would not exercise the supplementary pass.
        result = self.node(self._state_with_plan_field(transport="手配は organizer@example.comにご連絡"))

        assert result["status"] == AgentStatus.ERROR.value
        assert any("email" in entry for entry in result["error_log"])

    def test_clean_output_still_passes(self):
        """Regression guard: the walk must not reject legitimate plan text."""
        result = self.node(self._state_with_plan_field())

        assert result["status"] == AgentStatus.SUCCESS.value
        assert not result.get("error_log")


class TestPostProcessNodeS3WalkFailsClosed:
    """Anything the walker cannot prove it understands must raise, not be skipped.

    A `set` is the concrete case: it is not JSON-serialisable, but FastAPI's
    jsonable_encoder turns it into an array — so skipping it would ship an
    unscanned value to the caller.
    """

    def setup_method(self):
        self.node = PostProcessNode()

    def _gate(self, payload):
        return self.node._extra_security_gate_output({"formatted_output": payload})

    def test_nested_set_is_rejected(self):
        import pytest

        with pytest.raises(RuntimeError, match="non-JSON-serialisable"):
            self._gate({"json": {"candidate_plans": [{"lodging": {"organizer@example.com"}}]}})

    def test_non_string_key_is_rejected(self):
        import pytest

        with pytest.raises(RuntimeError, match="non-string key"):
            self._gate({"json": {1: "value"}})

    def test_cycle_is_rejected(self):
        import pytest

        payload: dict = {"json": {}}
        payload["json"]["self"] = payload

        with pytest.raises(RuntimeError, match="cyclic"):
            self._gate(payload)

    def test_excessive_depth_is_rejected(self):
        import pytest

        payload: dict = {"leaf": "ok"}
        for _ in range(25):
            payload = {"nested": payload}

        with pytest.raises(RuntimeError, match="deeper than"):
            self._gate(payload)

    def test_custom_object_is_rejected(self):
        import pytest

        class _Opaque:
            pass

        with pytest.raises(RuntimeError, match="non-JSON-serialisable"):
            self._gate({"json": {"plan": _Opaque()}})

    def test_scalars_and_none_are_walked_without_error(self):
        assert self._gate({"json": {"cost": 30000, "ok": True, "missing": None}}) is not None

    def test_shared_reference_is_not_mistaken_for_a_cycle(self):
        """A DAG (the same dict referenced from two places) is legitimate —
        only a self-referential path is a cycle. Rejecting DAGs would block
        valid output, so the walker tracks the ancestor chain, not every node
        it has ever seen."""
        shared = {"note": "共通の注記"}

        assert self._gate({"json": {"a": shared, "b": shared}}) is not None

    def test_frozenset_and_bytes_are_rejected_like_set(self):
        import pytest

        for payload in ({"json": frozenset({"x"})}, {"json": b"organizer@example.com"}):
            with pytest.raises(RuntimeError, match="non-JSON-serialisable"):
                self._gate(payload)
