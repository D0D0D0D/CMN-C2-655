"""AgentCore Platform v1.0 — CMN-C2-655 travel_policy DB service.

Version-managed UPSERT of company travel policy documents and constraint
lookup with citation. Injected `BaseDatabase` — tests can supply any contract
implementation; server.py selects this agent's SQLite backend through the
repository factory (see docs/07_operation_guide.md).

Schema (docs/02_design.md §Services層):
  travel_policy(policy_id PK, version, org_unit, effective_date, clauses JSON)
  clauses = [{"clause_no": str, "text": str, "constraint_type": str, "limit_value": Any}, ...]
"""

from __future__ import annotations

from typing import Any

from shared.services.database.base_db import BaseDatabase


class PolicyService:
    def __init__(self, db: BaseDatabase) -> None:
        self._db = db

    def upsert_policy(self, *, org_unit: str, version: str, effective_date: str,
                       clauses: list[dict]) -> dict:
        """Version-managed UPSERT — a new version does not overwrite prior versions."""
        existing = self._db.fetch_one(
            "SELECT policy_id FROM travel_policy WHERE org_unit = :org_unit AND version = :version",
            {"org_unit": org_unit, "version": version},
        )
        if existing:
            self._db.execute(
                "UPDATE travel_policy SET effective_date = :effective_date, clauses = :clauses "
                "WHERE org_unit = :org_unit AND version = :version",
                {"org_unit": org_unit, "version": version, "effective_date": effective_date,
                 "clauses": clauses},
            )
            return {"action": "updated", "org_unit": org_unit, "version": version,
                     "clause_count": len(clauses)}
        self._db.execute(
            "INSERT INTO travel_policy (org_unit, version, effective_date, clauses) "
            "VALUES (:org_unit, :version, :effective_date, :clauses)",
            {"org_unit": org_unit, "version": version, "effective_date": effective_date,
             "clauses": clauses},
        )
        return {"action": "inserted", "org_unit": org_unit, "version": version,
                 "clause_count": len(clauses)}

    def check_constraints(self, *, org_unit: str, party_size: int,
                           budget_band: str) -> dict:
        """Returns {"status": "verified"|"unverified", "constraints": [...], "citations": [...]}.

        Degrades to "unverified" (design decision — do not block the plan flow)
        when no policy row exists for org_unit.
        """
        row = self._db.fetch_one(
            "SELECT version, clauses FROM travel_policy WHERE org_unit = :org_unit "
            "ORDER BY effective_date DESC LIMIT 1",
            {"org_unit": org_unit},
        )
        if not row:
            return {"status": "unverified", "constraints": [], "citations": []}

        clauses: list[dict[str, Any]] = row.get("clauses", []) or []
        constraints = []
        citations = []
        for clause in clauses:
            constraints.append({
                "constraint_type": clause.get("constraint_type"),
                "limit_value": clause.get("limit_value"),
            })
            citations.append({
                "policy_version": row.get("version"),
                "clause_no": clause.get("clause_no"),
                "excerpt": clause.get("text", "")[:280],
            })
        return {"status": "verified", "constraints": constraints, "citations": citations}
