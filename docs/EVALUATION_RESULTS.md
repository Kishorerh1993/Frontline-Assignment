# Offline evaluation run evidence

Date: 2026-09-16

## Commands attempted

From `voice-agent/`:

```bash
PYTHONPATH=.:../shared python3 -m evals.runner
```

Result: the runner handled initialization failure and exited with code `2`.
The base Python installation has no installed `pipecat` dependency, required
by `tool_definitions.py`. No evaluation scenario started, no provider was
contacted, and no report artifact was written.

The runner's source files did pass the available syntax check:

```bash
python3 -m py_compile \
  voice-agent/evals/__init__.py \
  voice-agent/evals/fixtures.py \
  voice-agent/evals/scenarios.py \
  voice-agent/evals/runner.py \
  voice-agent/tests/test_evaluation_suite.py
```

Result: pass (exit code 0). `git diff --check` also passed.

The hermetic production-contract validation completed successfully:

```bash
cd voice-agent
PYTHONPATH=.:../shared python3 -m evals.hermetic_validation
```

Result: pass (exit code 0). This executed the repository's real prompt
builders, `tool_definitions.py`, static handler-binding inspection, and
scripted turn trajectories using a minimal import-time Pipecat schema stub.
It made no external requests and did not start the voice pipeline.

The complete 8-scenario, 103-check JSON report is retained at
`artifacts/hermetic-production-contract-validation.json`. It records every
scenario/check outcome and confirms `passed: true`.

A hermetic evaluation run also completed successfully. It supplied a synthetic
passing prompt scenario and a scenario whose renderer raises, using the async
runner API with synthetic schema and handler registries. The result correctly
reported the passing scenario and a failed `scenario_execution` check for the
render error. This is completed evidence for runner error isolation and report
semantics only; it is not evidence that the default catalog or real handlers
passed.

The dependency-free turn-level evaluator smoke check also passed. It exercised
two sequential synthetic caller/tool turns and verified the expected prompt
stage, governing contract, action, and turn order. As above, this validates
the evaluator mechanics only; the production-prompt trajectory catalog still
requires the declared dependencies.

Hidden-case diagnostics also passed with empty, null-like, malformed, repeated,
and concurrently invoked synthetic inputs. These checks confirm deterministic
failure reporting and the preserved positional scenario API; they do not
replace the dependency-backed catalog run.

## Required follow-up run

In a Python 3.11 environment with the repository's declared dependencies
installed, run:

```bash
cd voice-agent
python -m evals.runner
python -m pytest tests/test_evaluation_suite.py -v
```

Replace this record with the resulting scenario output before treating the
offline evaluation suite as fully qualified. This limitation is environmental;
the repository's documented test setup requires `pip`, virtual-environment
support, and the dependencies in `requirements.txt`, none of which are
available in this hosted workspace.
