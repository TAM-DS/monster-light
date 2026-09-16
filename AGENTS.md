# Monster Light Engineering Contract

Monster Light is a governed AI-assisted trading system.

## Core Trust Boundary

AI may:
- interpret user intent
- analyze information
- propose actions
- explain reasoning

AI may not:
- mutate portfolio state directly
- bypass deterministic validation
- fabricate market evidence
- authorize its own actions

All consequential state changes must pass through
deterministic application services and explicit validation.

## Engineering Rules

- Prefer clear domain boundaries over convenience.
- Write tests for business invariants.
- Do not weaken controls to make implementation easier.
- Do not make large unrelated changes.
- Inspect existing code before modifying it.
- Explain architectural trade-offs when they matter.
- Never commit secrets or credentials.
- Treat Git history as engineering evidence.