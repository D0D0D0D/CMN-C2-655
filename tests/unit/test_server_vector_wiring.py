"""Construction policy tests for the optional SDK Qdrant trip KB."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from src.api import server


class _Secrets:
    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        self.values = {
            "TRIP_KB_QDRANT_URL": "http://qdrant:6333",
            "TRIP_KB_EMBEDDING_API_KEY": "embedding-secret",
            "TRIP_KB_EMBEDDING_ENDPOINT": "https://embedding.example.azure.com",
            "TRIP_KB_EMBEDDING_DEPLOYMENT": "text-embedding-3-small",
        }
        self.values.update(overrides or {})

    def require(self, key: str) -> str:
        if key not in self.values:
            raise KeyError(key)
        return self.values[key]


def _enabled_config() -> dict:
    config = deepcopy(server.build_agent_config())
    config["vector_store"]["enabled"] = True
    return config


def test_checked_in_configuration_defaults_to_disabled_without_connection_values() -> None:
    root = Path(__file__).resolve().parents[2]
    runtime_config = yaml.safe_load((root / "config/config.yaml").read_text())
    manifest = yaml.safe_load((root / "config/agent.yaml").read_text())

    assert runtime_config["vector_store"]["enabled"] is False
    assert "url" not in runtime_config["vector_store"]
    assert "base_url" not in runtime_config["embedding"]
    assert "model" not in runtime_config["embedding"]
    assert runtime_config["vector_store"]["vector_size"] == 1536
    assert runtime_config["embedding"]["dimensions"] == 1536
    assert "platform-rag" not in manifest["requires"]["extras"]
    assert {
        "TRIP_DB_URL",
        "TRIP_KB_QDRANT_URL",
        "TRIP_KB_EMBEDDING_API_KEY",
        "TRIP_KB_EMBEDDING_ENDPOINT",
        "TRIP_KB_EMBEDDING_DEPLOYMENT",
    } <= set(manifest["requires"]["secrets"])



def test_disabled_vector_store_preserves_kb_free_degradation(monkeypatch) -> None:
    monkeypatch.setattr(server, "QdrantVectorStore", pytest.fail)
    monkeypatch.setattr(server, "AzureEmbedding", pytest.fail)
    monkeypatch.setattr(server, "AsyncServiceRuntime", pytest.fail)

    assert server._build_trip_kb_dependencies(server.build_agent_config(), _Secrets()) is None


@pytest.mark.parametrize(
    ("secret_name", "value"),
    [
        ("TRIP_KB_QDRANT_URL", ""),
        ("TRIP_KB_EMBEDDING_API_KEY", "   "),
        ("TRIP_KB_EMBEDDING_ENDPOINT", "\t"),
        ("TRIP_KB_EMBEDDING_DEPLOYMENT", "\n"),
    ],
)
def test_enabled_vector_store_rejects_empty_or_blank_connection_secrets(
    secret_name: str, value: str, monkeypatch
) -> None:
    class _Runtime:
        async def aclose(self, *, timeout=None) -> None:
            pass

    class _Service:
        def __init__(self, config, *, runtime) -> None:
            pass

    monkeypatch.setattr(server, "QdrantVectorStore", _Service)
    monkeypatch.setattr(server, "AzureEmbedding", _Service)
    monkeypatch.setattr(server, "AsyncServiceRuntime", _Runtime)
    with pytest.raises(ValueError, match=secret_name):
        server._build_trip_kb_dependencies(_enabled_config(), _Secrets({secret_name: value}))


def test_enabled_vector_store_rejects_missing_connection_secret() -> None:
    secrets = _Secrets()
    del secrets.values["TRIP_KB_EMBEDDING_ENDPOINT"]

    with pytest.raises(ValueError, match="TRIP_KB_EMBEDDING_ENDPOINT"):
        server._build_trip_kb_dependencies(_enabled_config(), secrets)


def test_vector_and_embedding_dimensions_must_match(monkeypatch) -> None:
    class _Runtime:
        async def aclose(self, *, timeout=None) -> None:
            pass

    class _Service:
        def __init__(self, config, *, runtime) -> None:
            pass

    monkeypatch.setattr(server, "QdrantVectorStore", _Service)
    monkeypatch.setattr(server, "AzureEmbedding", _Service)
    monkeypatch.setattr(server, "AsyncServiceRuntime", _Runtime)
    config = _enabled_config()
    config["embedding"]["dimensions"] = 3072

    with pytest.raises(ValueError, match="vector_size must match"):
        server._build_trip_kb_dependencies(config, _Secrets())


def test_enabled_vector_store_constructs_and_injects_both_sdk_services(
    monkeypatch,
) -> None:
    created: dict[str, object] = {}

    class _Runtime:
        async def aclose(self, *, timeout=None) -> None:
            created["closed_with"] = timeout

    class _Qdrant:
        def __init__(self, config, *, runtime) -> None:
            created["store_config"] = config
            created["store_runtime"] = runtime

        def search(self, query, top_k=10):
            return []

        def upsert(self, documents):
            return None

        def delete(self, ids):
            return None

    class _Embedding:
        def __init__(self, config, *, runtime) -> None:
            created["embedding_config"] = config
            created["embedding_runtime"] = runtime

    monkeypatch.setattr(server, "QdrantVectorStore", _Qdrant)
    monkeypatch.setattr(server, "AzureEmbedding", _Embedding)
    monkeypatch.setattr(server, "AsyncServiceRuntime", _Runtime)

    dependencies = server._build_trip_kb_dependencies(_enabled_config(), _Secrets())

    assert dependencies is not None
    vector_store, embedding, runtime = dependencies
    assert isinstance(vector_store, server.TripKBQdrantAdapter)
    assert isinstance(embedding, _Embedding)
    assert created["store_runtime"] is runtime
    assert created["embedding_runtime"] is runtime
    assert created["store_config"]["url"] == "http://qdrant:6333"
    assert created["store_config"]["embedding_base_url"] == "https://embedding.example.azure.com"
    assert created["store_config"]["embedding_model"] == "text-embedding-3-small"
    assert created["embedding_config"]["base_url"] == "https://embedding.example.azure.com"
    assert created["embedding_config"]["model"] == "text-embedding-3-small"
