# Frontline offline evaluation suite

## Purpose

This suite is a pre-merge safety net for the prompt-driven freight voice
agent. It evaluates deterministic contracts around the prompt and registered
tools, where regressions have high operational impact:

- carrier identity and MC/reference lookup sequencing;
- lookup-result hallucination guardrails;
- replacement of a denied phone-preverified identity;
- confidential goal/ceiling price protections;
- negotiation closing and above-max-bid instructions; and
- load-context requirements before human transfer.

It also runs scripted, turn-by-turn carrier trajectories for the normal flow,
three-strike MC closure, phone-identity replacement, and transfer gating. Each
turn records its expected state, permitted tool action, and governing prompt
contract.

It complements, rather than replaces, the mocked functional tests in
`shared/tests/` and `voice-agent/tests/`.

## Safety model

The default runner uses only synthetic load, carrier, phone, and price data.
It renders local prompt builders, reads the canonical room tool collections,
and invokes three real tool-handler rejection paths while patching every
reachable database, identity-persistence, quote, and transfer boundary to
raise if it is touched. A guardrail regression therefore reports a failed
scenario instead of reaching a live effect. It does not start Pipecat, create
a Daily room, call an LLM, contact Supabase, or make any carrier, quote, Slack,
speech, or phone-network request.

No credentials are required beyond the installed Python dependencies already
declared by the voice service.

## Run the suite

From `voice-agent/`, install both the runtime and test dependencies:

```bash
python -m pip install -r requirements.txt -r tests/requirements-test.txt
python -m evals.runner
python -m pytest tests/test_evaluation_suite.py -v
```

When package installation is unavailable, the following hermetic check still
loads and validates the repository's real prompt, schema, binding, and
turn-trajectory source. It stubs only Pipecat's import-time schema class and
makes no provider calls:

```bash
PYTHONPATH=.:../shared python -m evals.hermetic_validation
```

For a reviewable evidence artifact:

```bash
PYTHONPATH=.:../shared python -m evals.hermetic_validation \
  --json-out ../artifacts/hermetic-production-contract-validation.json
```

To save a machine-readable artifact explicitly:

```bash
python -m evals.runner --json-out ../artifacts/frontline-evaluation.json
```

The runner writes no files unless `--json-out` is provided. A zero exit code
means every deterministic prompt-contract scenario passed. A non-zero exit
code identifies one or more failing checks. Exit code `2` means the runner
itself could not initialize or write the requested report.

Python callers already running an event loop should use
`await evals.runner.run_evaluations_async()`; the synchronous
`run_evaluations()` entry point is intended for the CLI and synchronous tests.

## Interpreting results

Each scenario names the product risk, room, prompt stage, and failed contract
fragment. It also checks exposed tool names, parameter names/types/enums, and
the static handler-to-function bindings in the Room 1 and Room 2 pipeline
modules. The suite currently treats the following as required gates:

| Scenario | Required confidence |
| --- | --- |
| `initial-mc-lookup-contract` | MC lookup/tool sequencing and three-strike closure rules remain in the initial prompt. |
| `phone-identity-replacement-contract` | A caller who denies a phone-preverified identity must reverify and verbally confirm a new identity. |
| `loaded-negotiation-confidentiality-contract` | Negotiation prompt preserves confidential-pricing, closure, and transfer guardrails. |
| `broker-room-tool-contract` | Broker-only handoff schema remains outside the carrier-room tool set. |
| `*-handler-contract` | Invalid reference, agreement-without-load, and transfer-without-load paths reject without the associated side effect. |
| `*-turns` | Every scripted caller/tool turn has a valid prompt state, expected action, and governing contract. |

The report also lists known enforcement gaps. These are intentionally visible
but do not change the exit status because static rendering cannot prove them.
Prompt-render and handler setup failures are reported as failures for their
individual scenario where possible, rather than hiding the remaining results.

## Scope and limitations

This suite does **not** establish that the configured LLM follows the prompt
on every stochastic response, including same-turn speech and function-call
ordering. The turn trajectories validate the expected deterministic policy
contract, not an LLM-generated transcript. It also does not validate Daily room lifecycle, SIP/PSTN behavior,
Deepgram transcription, Cartesia speech, Supabase writes, carrier lookup,
quote submission, Slack notifications, or broker transfer completion.

Those require an approved staging environment with isolated data and provider
credentials. Any future provider-backed evaluation should reuse these scenario
identifiers, pin the model/provider configuration, run repeated trials, and
report pass rate separately from this deterministic gate.

## Assumptions

- The carrier-room tool set remains the five schemas assembled in `bot.py`;
  `transfer_human_to_carrier` is registered only by the Room 2 pipeline.
- The broker-room transfer tool remains configured independently in the Room 2
  context; it must not be treated as a carrier-room tool.
- Existing handler tests remain responsible for mocked data-service behavior.
- Prompt text may evolve, but edits to the safety contracts above require a
  deliberate scenario update and review.
