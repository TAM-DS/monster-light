# Monster Light Architecture

## 1. Purpose

Monster Light is a governed AI-assisted trading system. This document defines
the intended architecture for v0.1: deterministic paper trading with an explicit
boundary between AI-generated suggestions and authoritative portfolio state.
It describes requirements, not capabilities already implemented.

Keep the architecture lightweight. Begin with a single application and SQLite;
introduce modules, interfaces, or separate services only when implementation
requires them. Logical responsibilities do not require separate deployments.

## 2. Trust boundary

AI may interpret user intent, analyze supplied information, explain reasoning,
and produce structured trade proposals. A proposal is an untrusted input to the
application, not an instruction that can directly change a portfolio.

AI may not mutate authoritative portfolio state, bypass deterministic
validation, fabricate market evidence, or authorize its own actions. In v0.1,
every AI-originated trade requires explicit human approval of the specific
proposal. A material change to that proposal requires renewed approval.

Approval permits the application to consider execution; it does not override
validation. All consequential mutations must pass through deterministic
application services and explicit validation, regardless of their origin.

## 3. v0.1 scope

v0.1 supports paper trading only. Execution simulates trades and updates a
simulated portfolio; it must not place live orders or move real funds.

SQLite is the initial authoritative store for portfolio state. Deterministic
portfolio behavior comes first. AI proposal generation and its human approval
workflow may follow within v0.1, but cannot weaken the same execution controls.

The supported instruments, order forms, and simulation rules should be limited
to what the implemented vertical slice needs and defined explicitly before use.
Unsupported requests must be rejected rather than silently approximated.

## 4. Conceptual flow

The intended AI-assisted flow is:

1. Capture user intent and its origin.
2. Obtain market evidence with provenance and timestamps.
3. Produce a structured proposal linked to that intent and evidence.
4. Record explicit human approval of the specific AI-originated proposal.
5. Have a deterministic application service validate the approved request
   against current authoritative state, evidence, and configured policies.
6. Apply defined paper execution rules.
7. Persist the resulting portfolio changes and the audit records that explain
   them in a consistent transaction.
8. Return the recorded outcome and resulting state to the user.

Early checks may provide feedback before approval, but authoritative validation
must occur before execution. Rejections and failures are recorded without
applying a trade. The first slice uses direct structured inputs instead of AI
while exercising the deterministic portion of this flow.

## 5. Architectural boundaries

These are logical responsibilities to preserve as implementation grows:

| Responsibility | Boundary |
| --- | --- |
| User interaction and AI assistance | Capture intent, present evidence and proposals, and collect human decisions. Cannot write authoritative portfolio state. |
| Application services | Coordinate approval checks, deterministic validation, paper execution, transactions, and audit recording. Own consequential mutation entry points. |
| Portfolio rules | Define deterministic calculations and business invariants without depending on AI output or presentation behavior. |
| Market evidence handling | Preserve source information and timestamps and supply evidence for validation and simulation. Cannot authorize trades. |
| SQLite persistence | Store authoritative portfolio state and associated durable records under application-controlled transactions. |

Initially, these responsibilities may live in a few modules in one process.
Avoid speculative abstraction layers, generic execution frameworks, or separate
services. Add a boundary when it protects a concrete invariant or isolates an
implemented dependency.

## 6. State ownership

SQLite owns the authoritative persisted portfolio state, including the cash,
positions, and execution records needed by the supported simulation. The
deterministic application services are its only consequential write path,
including portfolio initialization and any supported adjustments.

AI context, proposals, UI views, and caches are not authoritative portfolio
state. Proposals and approvals are workflow records; neither constitutes an
executed trade or a portfolio balance.

State-dependent validation and mutation must operate against a consistent
database state. Transaction handling must prevent concurrent requests from
invalidating checked assumptions. Portfolio changes and their durable audit
linkage must commit together or leave the portfolio unchanged.

## 7. Validation principles

Validation must be explicit, deterministic, and testable. The same inputs,
authoritative state, evidence, policy configuration, and evaluation time must
produce the same decision.

Before execution, validate:

- The structured request is complete and uses supported instruments, actions,
  quantities, and numeric representations.
- An AI-originated request has explicit human approval bound to the proposal
  being executed, and its origin cannot bypass that requirement.
- Portfolio business invariants hold, including applicable cash and position
  constraints under the defined paper trading rules.
- Required market evidence is present, traceable, and fresh enough for the
  operation under the configured freshness policy.
- The proposed execution and resulting state conform to the configured
  simulation rules.

Freshness thresholds must be configurable rather than embedded as unexplained
constants. Missing or unusable evidence must produce an explicit rejection
where that evidence is required. Human approval and AI explanations cannot
substitute for any required check.

Execution must eventually support idempotency and duplicate prevention. Retain
stable request and execution identifiers from the outset so that a later
implementation can bind an idempotency key to a request, return an existing
outcome for a retry, and reject conflicting reuse. Do not claim retry safety
until duplicate prevention is implemented and tested.

## 8. Audit and evidence requirements

Preserve a traceable chain from intent through proposal, evidence, approval,
validation, paper execution, and resulting portfolio state. Use durable linked
identifiers rather than relying on conversational history or free-text logs.

Records should retain the relevant inputs, actor or origin, event timestamps,
human approval or rejection, validation decision and reasons, effective policy
configuration, simulation assumptions, execution outcome, and the state changes
needed to explain the result. Record failed and rejected attempts as well as
successful executions. For direct deterministic inputs, record that origin
explicitly; do not invent an AI proposal or approval event.

Market evidence must retain its source, source observation timestamp when
available, retrieval timestamp, and the values used in the decision. Distinguish
observation time from retrieval time; retrieving old data does not make it new.
Missing source timestamps must remain explicit and be handled by the freshness
policy. Preserve the evidence used, or a durable reference to an immutable
snapshot, so later changes at the source cannot erase the decision's basis.

Auditability does not require an event-sourced architecture. Ordinary SQLite
records and transactions are sufficient initially if they preserve the chain
and prevent silent replacement of historical decision evidence.

## 9. First vertical slice

Implement deterministic portfolio behavior before AI integration:

1. Initialize a paper portfolio through an application service and persist it
   in SQLite.
2. Accept a narrowly defined structured paper trade request with explicit
   execution inputs and clearly labeled test or market evidence.
3. Validate request fields, evidence requirements, and portfolio invariants.
4. Apply deterministic simulation rules and atomically persist the execution,
   portfolio changes, and audit linkage.
5. Read the resulting portfolio from SQLite and expose accepted or rejected
   outcomes with reasons.

Acceptance behaviors:

- A valid BUY changes portfolio state correctly.
- An invalid SELL is rejected and leaves portfolio state unchanged.

Test business invariants: correct cash and position changes, rejection of
unsupported or invalid requests, configured evidence freshness, persistence
across reloads, and absence of partial portfolio changes when an operation
fails. Fixed evidence and an explicit evaluation time should make tests
repeatable. Test approval enforcement when AI-originated inputs are introduced.

This slice establishes the trusted execution path that later AI integration
must use. It does not need an AI model, conversational interface, or live market
feed to prove deterministic portfolio behavior.

## 10. Explicitly deferred capabilities

- Live trading, broker order routing, and movement of real funds are outside
  v0.1.
- Autonomous approval or execution of AI-originated trades is outside v0.1;
  human approval remains mandatory.
- AI integration and its approval interface are deferred until deterministic
  portfolio behavior is established.
- Complete idempotency and duplicate prevention may follow the first slice,
  but remain required before enabling automatic execution retries or repeated
  delivery paths.
- Advanced order types, broader asset coverage, sophisticated fill models,
  streaming feeds, and automated strategies wait for concrete requirements.
- Distributed services, queues, event sourcing, additional databases, and
  generalized plugin frameworks are deferred until implementation needs justify
  their complexity.

Deferral does not relax the trust boundary, deterministic validation, evidence
requirements, or auditability of any capability that is implemented.
