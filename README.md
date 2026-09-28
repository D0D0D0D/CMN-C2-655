# CMN-C2-655 — CorporateGroupTripPlannerAgent

> **Category**: Cat 2 (domain workflow)
> **Industry**: CMN

## Overview

CorporateGroupTripPlannerAgent drafts a corporate group trip — a company outing, an offsite, a
training retreat — from four inputs the organizer usually has early: party size, budget band,
season, and departure point. It checks the organization's travel policy, ranks destinations and
lodging against past trip records, optionally supplements those with current public information,
and synthesizes three itinerary options with cost estimates, policy findings, anonymized care
requirements and dated organizer tasks. The organizer then refines the draft in conversation —
"lower the budget", "shift the dates", "go with option 2" — until a plan is settled, and the
result is a Markdown proposal fit to attach to an internal approval request, plus the same
content as JSON.

The back-and-forth is not an afterthought; it is the main way the template is used. The first
call comes back as `awaiting_human` with three candidates, and the organizer's replies on the
same `session_id` steer the plan. Accumulated expense records, satisfaction surveys and
completed-trip outcomes feed back in, so a team's later plans are informed by its earlier ones.

**What this template refuses to hold is as deliberate as what it produces.** Participant care
requirements — dietary restrictions, accessibility needs — are carried only as *category ×
count*, never linked to a person. Seating and rooming are handled as role slots, not names. The
design point is that the planning state has nowhere to put a participant identity, so matching
individuals to arrangements stays outside the agent, done by a person against the roster.

It does not book or pay for anything. It produces the proposal; execution stays with the
organizer.

This is an agent template built with the **AGENTIC STAR** development platform and the
**AgentCore Framework**. It is intended to be taken as a starting point: fork it, adapt it to
your own data and policies, and run it inside your own AGENTIC STAR deployment.

## Integration

Provide:

- an AgentCore runtime with a checkpointer — `memory_enabled` and `hitl.enabled` are both true,
  and the organizer dialogue suspends and resumes across calls;
- **authentication in front of the HTTP entry point** — see *Security and Limitations*, this is
  not optional for this template;
- a database holding travel policy and past trip records, reached through `TRIP_DB_URL`;
- optionally, a vector store and embedding endpoint for similarity search over past trips
  (`vector_store.enabled` is `false` by default, and the agent runs without it);
- optionally, a web-search backend. None is wired: the framework's `shared.tools.web_search` is
  a placeholder that returns a fixed string, so market-rate figures fall back to past records.

Secrets declared in `config/agent.yaml`: `TRIP_DB_URL`, `TRIP_KB_QDRANT_URL`,
`TRIP_KB_EMBEDDING_API_KEY`, `TRIP_KB_EMBEDDING_ENDPOINT`, `TRIP_KB_EMBEDDING_DEPLOYMENT`.

Runtime parameters are recorded in `config/config.yaml`:

| Key | Default | Meaning |
|---|---|---|
| `hitl.max_hitl` | `24` | Ceiling on organizer dialogue rounds — a runaway guard, not a target |
| `plans_count` | `3` | Candidate itineraries synthesized per round |
| `web_search.max_retries` | `2` | Retries before falling back to past-record estimates |
| `ingest.max_rows_per_call` | `500` | Row ceiling per ingest request |
| `vector_store.enabled` | `false` | Past-trip similarity search; off by default |
| `embedding.dimensions` | `1536` | Must match `vector_store.vector_size` |

Note that the standalone server in `src/api/server.py` does not read that file: it builds the
same values in `build_agent_config()`. The two are kept in step by a test that compares them, so
change both together.

## Requirements

**This template does not run standalone.** It requires:

| Requirement | Notes |
|---|---|
| **AGENTIC STAR platform** | The agent is designed to run on the platform, which supplies the caller identity, the HITL permission and the secret store. Deployment guides and API documentation: [AGENTIC STAR Developers](https://developers.fd.agenticstar.tm.softbank.jp/) |
| **AgentCore Framework** (`agenticstar-agentcore`) | Supplied by the platform's own package registry at build time. It is not published on public PyPI, so `pip install -e .` alone does not fetch it. |
| Python | >=3.11 |

```bash
pip install -e .
```

### Behaviour without the platform

The framework is designed to run on AGENTIC STAR, and the agent does not fall back to a degraded
mode when it is absent. The standalone server in `src/api/server.py` starts and serves the
endpoints, but several things it depends on come from the platform: the authenticated caller
identity, the HITL permission flag, and the secret provider that selects the real persistence
backends. Without them it runs with in-code defaults and assumes an authenticated caller — which
is why it must sit behind authentication (see *Security and Limitations*). The test suite runs
without a platform connection.

## Input

Four endpoints:

**`POST /invoke`** — plan, then refine. Takes `input`, `session_id`, `request_params`
(`party_size`, `budget_band`, `season`, `departure_point`, optional `destination_hint`) and
`care_notes`. The first call for a `session_id` starts a plan; a later call on the same
`session_id` is routed as the organizer's reply into the suspended dialogue, with `input`
carrying that reply.

```bash
curl -X POST http://localhost:8000/invoke \
  -H "Content-Type: application/json" \
  -d '{
    "input": "",
    "session_id": "demo-001",
    "request_params": {"party_size": 20, "budget_band": "standard",
                       "season": "autumn", "departure_point": "Tokyo"},
    "care_notes": "vegetarian x2, wheelchair x1"
  }'
```

**`POST /ingest`** — load reference data. Takes `ingest_kind` (`"policy"` or `"trip_records"`),
`rows`, `org_unit`, `version` and `effective_date`. Participant identifier columns are discarded
at this step, before anything is stored.

**`POST /feedback`** — record a completed trip. Takes `plan_id`, `actual_cost`,
`satisfaction_score`, `complaint_text` and `organizer_edited`, which improve later rankings.
`plan_id` is the identifier the planning response returned — an opaque `plan-` string of
letters. Pass it back unchanged; it is the only key that links the outcome to the plan.

**`GET /health`** — liveness.

## Output

The first planning call returns `status: "awaiting_human"` with a `thread_id` and
`hitl_metadata` carrying the prompt shown to the organizer, alongside the candidate plans:

```json
{
  "status": "awaiting_human",
  "thread_id": "…",
  "hitl_metadata": {
    "prompt": "3案から選択、または条件変更（予算/日程/宿泊タイプ等）をご記入ください。"
  },
  "session_id": "demo-001"
}
```

Once the dialogue settles (`status: "success"`), the output carries a Markdown proposal and the
same content as JSON. The proposal has fixed sections: itinerary, cost estimate, policy-check
result with clause citations, anonymized care requirements as category and headcount, and dated
organizer tasks. When the policy for an organizational unit has not been loaded, the policy
section reports `unverified` and says so, rather than implying the plan was cleared.

Plan fields that reach the caller are restricted to a fixed allowlist — destination area,
itinerary, lodging, transport and per-person cost estimate. Anything else a plan carries
internally does not leave the agent.

Values above are illustrative; real output depends on the request and the accumulated records.

## Security and Limitations

**Authenticate this template upstream.** The nodes require verified-external trust, but the
standalone server has no entry-level authentication of its own: it reads the caller's trust from
request state populated by upstream middleware and, absent that middleware, assumes
`VERIFIED_EXTERNAL`. That default is fail-open, and it applies to the writing endpoints
(`/ingest`, `/feedback`) as well as to `/invoke`. Deploy it behind the platform's auth
middleware, or put equivalent authentication in front of it before exposing it. Likewise, the
HITL permission flag is read from authenticated request state and never from a raw client
header — a caller must not be able to turn the organizer's review off by asserting it.

Care information is protected primarily by structure, not by scanning. The caller-facing output
is a closed allowlist of plan fields, which decides mechanically what can leave; a
direct-identifier scan over the whole formatted payload (Markdown and every nested JSON string)
runs as a second layer. The order matters — the allowlist is the defence, the scan is the backup.

> **Do not write personal names into free text.** The identifier scan removes email addresses,
> phone numbers and honorific-suffixed names (`◯◯様`) from free-text fields, but it is a
> defence-in-depth layer, **not a guarantee**. It does not remove a bare name without an
> honorific ("佐藤と田中で調整します"), a name with a job title ("田中部長"), or a foreign or
> katakana name. It can also over-redact a legitimate place or venue name, because it matches
> patterns rather than understanding context. The primary control is the operating rule — do not
> enter names — supported by a design that has no field to store one.

Further limits worth knowing:

- **No web-search backend is wired.** The framework's `shared.tools.web_search` returns a fixed
  placeholder string at this SDK version, so market-rate figures come from past records. The
  output states this in its quality note.
- **The past-trip vector store is off by default.** With `vector_store.enabled: false` the agent
  still plans from the database; similarity search over past trips simply does not run.
- **A permission change mid-dialogue does not take effect.** `hitl_allowed` is captured on the
  first call. The framework's resume path takes no invocation context, so a suspended session
  continues under the permission it started with.
- **Policy checking is only as good as the loaded policy.** An organizational unit with no policy
  loaded gets `unverified`, which is a prompt to ask a human — not a pass.

Output is machine-generated and assistive. It does not substitute for expert review, and any
decision that matters should be verified by a person against the policy and the primary records.

## Release

- Version: 1.0.0
- Category: fixed multi-step domain workflow (Cat 2) with an inner workflow subgraph
- Generation mode: `llm`
- HITL: enabled — `hitl.max_hitl: 24`; `memory_enabled: true` (checkpointer required)
- Persistence: relational store for policy and trip records; optional vector store for past-trip
  similarity search
- External API clients: deployment-supplied; none bundled

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m pytest tests/ -v
```

Tests run without a platform connection, without secrets and without a vector store.

## Project Structure

```
src/          agent implementation (nodes, graphs, services, schemas)
tests/        unit, integration and boundary tests
config/       agent manifest and runtime configuration
docs/         design and test documentation
```

## Customising

1. Load your own travel policy and past trip records through `/ingest`.
2. Adjust `config/config.yaml` — and the matching defaults in `build_agent_config()` — for your
   plan count, dialogue ceiling and ingest limits.
3. Wire a real web-search backend if current market rates matter to your estimates.
4. Enable the vector store and embedding endpoint if you want similarity search over past trips.
5. Re-run the test suite.

## Documentation

`docs/02_design.md` carries the node flow, the state schema, the HITL design and the care-data
protection model. `docs/03_test_spec.md` records the test specification and the verification
results.

## License

MIT — see [LICENSE](LICENSE).

## Status of this repository

This template is published **as is**, by its individual author, under the MIT license. It carries
**no warranty and no support commitment**, and no organisation stands behind its behaviour or
fitness for any purpose. Issues and pull requests may or may not receive a response; that is at
the sole discretion of the repository owner.
