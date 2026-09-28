"""Translate the trip-KB contract to the SDK Qdrant store contract."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from shared.services.vector_store.base_vector_store import BaseVectorStore


class TripKBQdrantAdapter(BaseVectorStore):
    """Keep business IDs and Qdrant point IDs in their supported shapes."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def search(self, query: str, top_k: int = 10) -> list[dict[str, Any]]:
        adapted: list[dict[str, Any]] = []
        for hit in self._store.search(query, top_k=top_k) or []:
            payload = hit.get("payload") if isinstance(hit, Mapping) else None
            metadata = dict(payload) if isinstance(payload, Mapping) else {}
            adapted.append(
                {
                    "id": (
                        metadata.get("source_id")
                        if "source_id" in metadata
                        else hit.get("id")
                        if isinstance(hit, Mapping)
                        else None
                    ),
                    "metadata": metadata,
                    "score": hit.get("score") if isinstance(hit, Mapping) else None,
                    "similarity": (hit.get("similarity") if isinstance(hit, Mapping) else None),
                }
            )
        return adapted

    def upsert(self, documents: list[Any]) -> None:
        translated: list[dict[str, Any]] = []
        for document in documents:
            if not isinstance(document, Mapping):
                self._store.upsert(documents)
                return
            metadata = document.get("metadata", {})
            if not isinstance(metadata, Mapping):
                self._store.upsert(documents)
                return
            source_id = metadata.get("source_id", document.get("id"))
            if not isinstance(source_id, str) or not source_id.strip():
                raise ValueError("trip KB document requires a non-empty metadata.source_id")
            translated.append(
                {
                    "id": _sdk_point_id(source_id),
                    "content": document.get("text", ""),
                    "metadata": {**metadata, "source_id": source_id},
                }
            )
        self._store.upsert(translated)

    def delete(self, ids: list[Any]) -> None:
        self._store.delete([_sdk_point_id(source_id) for source_id in ids])


def _sdk_point_id(source_id: object) -> str:
    """Map a trip plan ID to the canonical UUID shape accepted by the SDK."""
    return str(uuid5(NAMESPACE_URL, f"cmn-c2-655:{source_id}"))
