"""AgentCore Platform v1.0 — CMN-C2-655 past-trip KB (vector) service.

One realized trip = one chunk (docs/02_design.md §Services層). Chunk metadata
carries only non-personal outcome fields (destination_area, season, party
size band, budget band, satisfaction_score, complaint_flags) — never
participant names or a name<->care-need linkage (C-01).
"""

from __future__ import annotations

from typing import Any

from shared.services.embedding.base_embedding import BaseEmbedding
from shared.services.vector_store.base_vector_store import BaseVectorStore


class TripKBService:
    def __init__(
        self,
        vector_store: BaseVectorStore,
        embedding: BaseEmbedding | None = None,
    ) -> None:
        self._vs = vector_store
        # QdrantVectorStore currently performs document/query embedding
        # internally. Keep the independently constructed SDK AzureEmbedding on
        # the injected service boundary without calling it a second time.
        self._embedding = embedding

    def upsert_trip_record(
        self,
        *,
        plan_id: str,
        destination_area: str,
        season: str,
        party_size: int,
        budget_band: str,
        summary_text: str,
        satisfaction_score: float | None = None,
        complaint_flags: list[str] | None = None,
    ) -> None:
        self._vs.upsert(
            [
                {
                    "id": plan_id,
                    "text": summary_text,
                    "metadata": {
                        "source_id": plan_id,
                        "destination_area": destination_area,
                        "season": season,
                        "party_size": party_size,
                        "budget_band": budget_band,
                        "satisfaction_score": satisfaction_score,
                        "complaint_flags": complaint_flags or [],
                    },
                }
            ]
        )

    def find_similar_trips(
        self, *, destination_hint: str, season: str, party_size: int, budget_band: str, top_k: int = 5
    ) -> list[dict[str, Any]]:
        """Similarity search by destination-area/season/scale/budget-band; returns
        candidates annotated with satisfaction_score / complaint_flags for
        candidate_rank to accept/penalize/exclude."""
        query = f"{destination_hint} {season} party_size={party_size} budget={budget_band}"
        return self._vs.search(query, top_k=top_k)
