"""Backend-independent contract for the trip KB's Qdrant boundary."""

from __future__ import annotations

from uuid import UUID

from src.services.trip_kb_qdrant_adapter import TripKBQdrantAdapter
from src.services.trip_kb_service import TripKBService


class _FakeQdrantVectorStore:
    def __init__(self) -> None:
        self.documents: list[dict] = []

    def upsert(self, documents: list[dict]) -> None:
        self.documents.extend(documents)

    def search(self, query: str, top_k: int = 10) -> list[dict]:
        del query
        return [
            {
                "id": document["id"],
                "payload": document["metadata"],
                "score": 0.91,
                "similarity": 0.91,
            }
            for document in self.documents[:top_k]
        ]

    def delete(self, ids: list[str]) -> None:
        self.documents = [document for document in self.documents if document["id"] not in ids]


def test_trip_kb_contract_translates_sdk_document_and_hit_shapes() -> None:
    qdrant = _FakeQdrantVectorStore()
    service = TripKBService(TripKBQdrantAdapter(qdrant))

    service.upsert_trip_record(
        plan_id="plan-business-42",
        destination_area="Hakone",
        season="spring",
        party_size=12,
        budget_band="standard",
        summary_text="温泉と研修を組み合わせた一泊旅行",
        satisfaction_score=4.7,
        complaint_flags=["transport_delay"],
    )

    stored = qdrant.documents[0]
    assert str(UUID(stored["id"])) == stored["id"]
    assert stored["id"] != "plan-business-42"
    assert stored["content"] == "温泉と研修を組み合わせた一泊旅行"
    assert stored["metadata"]["source_id"] == "plan-business-42"

    results = service.find_similar_trips(
        destination_hint="Hakone",
        season="spring",
        party_size=12,
        budget_band="standard",
    )
    assert results == [
        {
            "id": "plan-business-42",
            "metadata": stored["metadata"],
            "score": 0.91,
            "similarity": 0.91,
        }
    ]


def test_adapter_delete_uses_the_same_deterministic_sdk_id() -> None:
    qdrant = _FakeQdrantVectorStore()
    adapter = TripKBQdrantAdapter(qdrant)
    adapter.upsert([{"text": "trip", "metadata": {"source_id": "plan-business-42"}}])

    adapter.delete(["plan-business-42"])

    assert qdrant.documents == []
