# Offline evaluation run evidence

Date: 2026-09-17

## Environment

The following completed runs used a local macOS virtual environment with
Python 3.12.3, pytest 9.1.1, and Pipecat 0.0.95. The test commands used only
synthetic fixtures, mocks, and placeholder Supabase settings; no production
credentials or provider calls were used.

## Completed evaluation gate

From `voice-agent/`:

```bash
python -m evals.runner --json-out ../artifacts/frontline-evaluation.json
```

Result: **PASS — 11 of 11 scenarios.** The machine-readable report is saved
as `artifacts/frontline-evaluation.json`. It covers prompt/tool contracts,
three handler-side rejection paths, and the scripted carrier trajectories.

The run emitted two expected warnings that demonstrate the negative paths were
blocked: an implausible fabricated load reference was rejected before a lookup,
and a transfer attempted before load lookup was blocked. Neither warning is a
test failure.

The runner reported these known enforcement gaps:

- Prompt checks cannot prove stochastic LLM compliance or tool-call ordering.
- This offline suite does not exercise Daily, PSTN, speech, LLM, Supabase,
  carrier, quote, Slack, or broker-transfer providers.

## Completed supporting tests

```bash
python -m pytest tests/test_evaluation_suite.py -v
```

Result: **PASS — 14 passed in 0.85s.** This verifies evaluation-runner
diagnostics, error isolation, schema/handler drift reporting, and async API
behavior.

```bash
SUPABASE_URL=http://test-suite.local SUPABASE_SERVICE_ROLE_KEY=test-key \
  python -m pytest ../shared/tests -v --tb=short
```

Result: **PASS — 91 passed in 0.33s.** These mocked functional tests cover
carrier lookup clients, quote submission behavior, load normalization,
negotiation database service behavior, Salesforce queries, and transfer
routing.

```bash
python -m pytest tests -v --tb=short
```

Result: **PASS — 76 passed and 17 subtests passed in 1.18s.** This suite
includes the evaluation tests as well as carrier-service, phone identity,
transfer-gating, and prompt-guardrail tests.

## Interpretation and limits

These results qualify the deterministic pre-merge evaluation gate and its
mocked unit-level boundaries. They do not establish live integration behavior:
Daily/PSTN lifecycle, speech recognition and synthesis, LLM responses and
same-turn tool ordering, Supabase persistence, carrier and quote APIs, Slack,
and completed broker transfer remain unverified. Those require an approved
staging environment with isolated data, provider credentials, a pinned model
configuration, repeated trials, and separately reported pass-rate thresholds.
