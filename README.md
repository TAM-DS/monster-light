# Monster Light

Monster Light is a governed AI-assisted paper trading system: AI proposes, a human authorizes, deterministic controls verify current reality, and only then may execution occur.

## Why it exists

AI can reason and recommend, but consequential systems need explicit authority boundaries, deterministic validation, and evidence. A recommendation is not authority to change a portfolio, and human approval cannot substitute for checking whether a trade is valid now.

The v0.1 governed execution core implements this boundary through application services, SQLite transactions, and immutable trade audit records.

## Authority model

```text
AI     -> PROPOSE
Human  -> AUTHORIZE
System -> VERIFY / EXECUTE
Audit  -> RECORD EVIDENCE
```

**Approval permits execution to be attempted. It does not override current deterministic validation.** AI cannot authorize its own proposal or mutate authoritative portfolio state.

## Governed workflow

```text
Trusted grounding evidence
  -> AI proposal -> PENDING
  -> human approval -> APPROVED
  -> fresh execution evidence
  -> deterministic validation
  -> EXECUTED or REJECTED attempt
  -> immutable audit evidence
```

Successful execution marks the proposal `EXECUTED` and records an `ACCEPTED` trade audit. A deterministic portfolio rejection records a `REJECTED` audit while leaving the proposal `APPROVED`. The attempt's outcome and the proposal's status are distinct.

## The important failure case

The deterministic execution demo attempts an approved purchase of one AAPL share at a synthetic price of $210 with only $100 in cash:

- The proposal has a recorded human approval.
- Execution is attempted through the governed service.
- Deterministic validation rejects it with `InsufficientCash`.
- Cash and positions remain unchanged; the proposal remains `APPROVED`.
- A `REJECTED` audit preserves the reason, approval linkage, execution evidence, and unchanged portfolio values.

This is the strongest governance proof: even an approved recommendation cannot override authoritative portfolio state. The history truthfully records both the authorization and the failed attempt.

## Evidence provenance

| Evidence | Question answered | Linkage |
| --- | --- | --- |
| Grounding evidence | What informed the recommendation? | The proposal retains `grounding_evidence_id`. |
| Execution evidence | What did the system verify when action was attempted? | The trade audit retains `market_evidence_id`. |

Evidence carries source, price, observation time, and retrieval time. Execution checks evidence against the approved symbol and price and a configured freshness limit, then validates current portfolio state. Grounding provenance does not establish that execution is valid later. The execution demos use separate grounding and execution evidence records.

## At-most-once successful execution

**Retries may repeat the request. They must not repeat the consequence.**

[Concurrency tests](tests/test_approved_proposal_execution.py) race execution of the same approved proposal using independent SQLite connections. Across DELETE and WAL journal modes, with and without caller-owned transactions, they prove:

- At most one successful execution.
- Exactly one portfolio mutation.
- Exactly one `ACCEPTED` audit.

The competing attempt encounters SQLite contention; a later attempt against an `EXECUTED` proposal is refused. This proves at-most-once successful consequence, not automatic retry handling. No production changes were required for the concurrency proof.

Failed deterministic attempts remain retryable while the proposal is `APPROVED`. Tests show that a later valid attempt can succeed after prerequisites change, preserving the earlier rejection audit.

## Architecture and components

| Component | Responsibility |
| --- | --- |
| `ModelProposalAdapter` / `AIProposalService` | Convert model output grounded in supplied evidence into a persisted, non-authoritative `PENDING` proposal. |
| `ApprovalService` | Record explicit human approval of the proposal. |
| `ApprovedProposalExecutionService` | Require approval evidence, verify execution evidence, and coordinate execution and proposal status atomically. |
| `TradeService` | Apply deterministic portfolio rules and record accepted or rejected trade outcomes. |
| SQLite repositories | Persist portfolios, proposals, approvals, evidence, and immutable trade audit records. |

Successful portfolio mutation, audit insertion, and proposal transition share a transaction boundary. Trade audit records are append-only, with SQLite triggers preventing update, deletion, and replacement. These are logical boundaries within one application.

[ARCHITECTURE.md](ARCHITECTURE.md) records the original architectural requirements; the implementation and tests establish the capabilities described here.

## Demos

All demos use synthetic market evidence and disposable in-memory SQLite databases. The three live demos require `OPENAI_API_KEY` in the process environment; keep credentials out of source files.

```sh
# Live OpenAI call: create a PENDING proposal.
PYTHONPATH=src uv run python -m monster_light.demo.live_model_proposal

# Live OpenAI call: review a proposal and type approve; no execution.
PYTHONPATH=src uv run python -m monster_light.demo.human_approval

# Offline: synthetic approval fixtures; prove success and insufficient-cash rejection.
PYTHONPATH=src uv run python -m monster_light.demo.deterministic_execution

# Live OpenAI call: proposal, interactive approval, execution, and linked evidence.
PYTHONPATH=src uv run python -m monster_light.demo.governed_workflow
```

## Tests

Verified current result: **528 tests passed, 58 subtests passed**.

```sh
uv run pytest -q
```

The suite verifies authority boundaries, evidence validation, portfolio invariants, audit preservation, and concurrent execution. Demo tests inject model clients and operator input to run offline.

## Scope

Paper trading only: no broker integration, no real money, and no market API in v0.1 demos. Human approver metadata is caller-supplied and is not authenticated identity.
