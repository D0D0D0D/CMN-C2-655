"""実 LLM 1回転チェック用の repo 固有 fixture。"""

import pytest


@pytest.fixture
def agent_config() -> dict:
    # tests/integration/test_hitl_flow.py:33-36 の plan happy path と同じ追加 config（無し）。
    return {}


@pytest.fixture
def case_payload() -> str:
    # tests/integration/test_hitl_flow.py:33-36 の plan happy path と同じ第1引数。
    return ""


@pytest.fixture
def live_input_context() -> dict:
    # tests/integration/test_hitl_flow.py:33-36 の plan happy path と同じ input_context。
    return {
        "mode": "plan",
        "request_params": {
            "party_size": 10,
            "destination_hint": "Hakone",
        },
        "care_notes": "vegetarian",
    }


@pytest.fixture
def live_llm_injector():
    # tests/integration/test_hitl_flow.py:33-36 の plan happy path に実 LLM wrapper を配線する。
    def _inject(agent, llm):
        agent.config["llm"] = {"instance": llm}
        return agent

    return _inject
