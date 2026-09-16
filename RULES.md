# Frontline Architecture and Engineering Rules

This document records the current system boundaries and the conventions that
new code, tests, and evaluations must follow. It is descriptive: it does not
change the product contract.

## Product boundary

Frontline is an inbound freight-negotiation voice agent. It verifies a motor
carrier, finds the caller's load, negotiates a rate, records an agreement or
above-max bid, and may hand the call to a human broker.

The intended architecture is a prompt-driven agent with tool-backed effects.
Keep that architecture intact. Do not introduce intent routing, autonomous
background decisions, or a second agent to replace the existing flow unless a
separate product decision explicitly authorizes it.

## Runtime architecture

```text
Daily PSTN webhook
  -> voice-agent/server.py (room allocation and bot start)
  -> voice-agent/bot.py (Pipecat pipeline)
  -> Deepgram STT -> OpenAI LLM -> Cartesia TTS
  -> voice-agent/call_helpers.py / transfer_tool_handler.py (tool effects)
  -> shared/src (carrier, load, database, quote, and routing services)
  -> Supabase and approved development integrations
```

There are two active prompt stages:

1. `get_initial_system_prompt` in `voice-agent/voice_prompt.py` manages
   carrier identification and reference-number collection.
2. `build_full_negotiation_prompt` replaces the system message after
   `get_load_context` succeeds and contains load-specific negotiation policy.

The LLM may use only the registered tool contracts in
`voice-agent/tool_definitions.py`:

- `verify_carrier`
- `get_load_context`
- `record_agreement`
- `end_call`
- `transfer_to_human`
- `transfer_human_to_carrier` (broker-transfer room only)

Keep prompt instructions, registered tools, and tool schemas consistent. A
prompt must never tell the model to call a tool that is not registered in that
stage.

## Business and safety invariants

- Verify a carrier before requesting a load reference in the normal flow.
- Do not state a carrier or load lookup outcome without a corresponding tool
  result for that exact value.
- A new MC number or new reference number requires a new lookup.
- Reject implausible load references rather than allowing fabricated or
  stale-turn text to reach the database.
- Do not reveal internal goal, ceiling, margin, budget, or equivalent
  confidential pricing to the caller.
- Do not counter a below-goal carrier bid with a higher price.
- Record normal agreements only after the load is present; above-max bids also
  require usable contact information.
- Do not transfer before successful load lookup and valid broker routing.
- Once transfer has started, do not end the call from the carrier-side agent.
- Treat a newly verified MC as a new identity after the caller denies a
  phone-preverified identity; require fresh verbal confirmation.

## Repository layout and ownership

| Area | Purpose |
| --- | --- |
| `voice-agent/` | Webhook server, Pipecat pipeline, prompts, tools, and transfer flow. |
| `shared/src/` | Shared Supabase access, carrier lookup, quote client, load normalization, and transfer routing. |
| `shared/tests/` | Mocked unit/functional tests for shared services. |
| `voice-agent/tests/` | Mocked tests for voice-agent handlers and prompt guardrails. |
| `docs/` | Setup, database, integration, operational, and evaluation documentation. |
| `web/` | Separate Next.js dashboard; do not couple voice-agent evaluations to it. |

The current voice service uses Python 3.11. The dashboard has separate Node.js
22 and private-package requirements; it is outside the default voice-agent
evaluation scope.

## Coding conventions

- Use Python for voice-agent, shared-service, and evaluation work.
- Preserve existing module naming and use `snake_case` for Python symbols.
- Keep side effects behind existing service/helper boundaries. Prefer injected
  collaborators and mocks in tests over network interception.
- Match existing async style: async handlers accept the Pipecat callback
  signature and return structured JSON through `result_callback`.
- Validate input at tool boundaries and return structured, actionable errors.
- Keep tenant/org context when reading or writing load and call data.
- Do not log credentials, access tokens, unredacted audio, or unnecessary PII.
- Do not silently broaden a live integration from a sandbox/development
  endpoint to a production endpoint.
- Use narrowly scoped changes. Prompt edits require regression coverage for
  the behavior they intentionally change and for nearby safety rules.

## Test and evaluation rules

Existing tests use mocks and placeholder Supabase values. Run from
`voice-agent/` after installing the declared dependencies:

```bash
SUPABASE_URL=http://test-suite.local SUPABASE_SERVICE_ROLE_KEY=test-key \
  python -m pytest ../shared/tests -v --tb=short
python -m pytest tests -v --tb=short
```

New evaluations must:

- run with synthetic data and mocked service boundaries by default;
- make no Daily, OpenAI, Deepgram, Cartesia, Supabase, carrier, quote, Slack,
  or phone-network request;
- identify each scenario, the risk it covers, expected behavior, pass/fail
  result, and useful failure diagnostics;
- distinguish deterministic prompt/tool-contract checks from true LLM,
  speech, provider, or PSTN end-to-end validation;
- document prerequisites, commands, success thresholds, assumptions, and
  known limitations;
- include evidence from a completed run before being presented as complete.

Provider-backed development calls are a separate qualification activity. They
require approved development credentials, isolated Supabase data, a Daily test
number, and sandbox-only quote/transfer configuration. HTTP health checks and
mocked tests do not prove those integrations work.

## Configuration and data rules

- Use `voice-agent/.env.example` only as a template; never commit a populated
  `.env` file.
- Keep `ROOM_POOL_SIZE=0` for local health-only checks unless approved Daily
  development credentials are configured.
- Use the documented `HIGHWAY_API_BASE_URL` development endpoint and
  `KCH_QUOTE_USE_SANDBOX=true` for development qualification.
- Service-role credentials remain server-side; never expose them to the web
  client, fixtures, output, or documentation.
- Use synthetic MCs, phone numbers, carrier names, load IDs, and prices in
  tests and evaluation fixtures.

## Change checklist

Before completing a change:

1. Confirm the prompt, registered functions, and handler contracts agree.
2. Add or update focused tests for altered behavior and high-risk regressions.
3. Run the applicable mocked tests and record the exact outcome.
4. State any tests that could not run and why.
5. Document assumptions and unverified external behavior.
6. Do not claim real-call or provider coverage without approved staging
   evidence.
