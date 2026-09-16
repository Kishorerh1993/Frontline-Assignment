"""Dependency-free validation of Frontline's real prompt and tool definitions.

This intentionally replaces only import-time third-party interfaces. It loads
the repository's actual ``tool_definitions.py``, ``voice_prompt.py``, and
shared negotiation-prompt source, then evaluates their contracts without
starting a Pipecat pipeline or contacting a provider.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from dataclasses import asdict
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import Iterator


class _FunctionSchema:
    """Small compatible stand-in for the schema surface used by the evaluator."""

    def __init__(self, *, name, description, properties, required):
        self.name = name
        self.description = description
        self.properties = properties
        self.required = required

    def to_default_dict(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": self.required,
                },
            },
        }


def _module(name: str) -> ModuleType:
    module = ModuleType(name)
    module.__path__ = []
    return module


@contextmanager
def _stubbed_imports() -> Iterator[None]:
    """Temporarily install the minimal imports needed by prompt/schema code."""
    root = Path(__file__).resolve().parents[2]
    shared_prompt_path = root / "shared" / "src" / "negotiation_prompt.py"
    spec = importlib.util.spec_from_file_location("src.negotiation_prompt", shared_prompt_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load shared negotiation prompt")
    negotiation_prompt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(negotiation_prompt)

    pipecat = _module("pipecat")
    adapters = _module("pipecat.adapters")
    schemas = _module("pipecat.adapters.schemas")
    function_schema = _module("pipecat.adapters.schemas.function_schema")
    function_schema.FunctionSchema = _FunctionSchema
    src = _module("src")
    src.negotiation_prompt = negotiation_prompt

    replacements = {
        "pipecat": pipecat,
        "pipecat.adapters": adapters,
        "pipecat.adapters.schemas": schemas,
        "pipecat.adapters.schemas.function_schema": function_schema,
        "src": src,
        "src.negotiation_prompt": negotiation_prompt,
    }
    reset_names = ("tool_definitions", "voice_prompt")
    previous = {name: sys.modules.get(name) for name in (*replacements, *reset_names)}
    try:
        sys.modules.update(replacements)
        for name in reset_names:
            sys.modules.pop(name, None)
        yield
    finally:
        for name, module in previous.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def run_report() -> dict:
    """Validate real prompt rendering, schemas, and static handler bindings."""
    from evals.conversations import conversation_scenarios
    from evals.runner import (
        evaluate_conversation_scenario,
        evaluate_prompt_scenario,
        registered_handler_bindings_by_room,
        registered_tool_parameter_contracts_by_room,
        registered_tool_required_arguments_by_room,
        registered_tools_by_room,
    )
    from evals.scenarios import prompt_scenarios

    with _stubbed_imports():
        tools = registered_tools_by_room()
        required = registered_tool_required_arguments_by_room()
        parameters = registered_tool_parameter_contracts_by_room()
        bindings = registered_handler_bindings_by_room()
        prompt_results = tuple(
            evaluate_prompt_scenario(
                scenario,
                tools,
                required,
                parameters,
                bindings,
            )
            for scenario in prompt_scenarios()
        )
        # Render again through each real scenario so conversation validation
        # uses the same production prompt builders rather than copied text.
        rendered_prompts = {
            scenario.prompt_stage: scenario.render()
            for scenario in prompt_scenarios()
            if scenario.prompt_stage != "broker_handoff"
        }
        conversation_results = tuple(
            evaluate_conversation_scenario(scenario, rendered_prompts)
            for scenario in conversation_scenarios()
        )

    results = (*prompt_results, *conversation_results)
    return {
        "passed": all(result.passed for result in results),
        "scenario_count": len(results),
        "scenarios": [asdict(result) for result in results],
    }


def run() -> tuple[bool, tuple[str, ...]]:
    """Compatibility wrapper returning pass/fail and concise failure diagnostics."""
    report = run_report()
    failures = tuple(
        f"{result['identifier']}: "
        + "; ".join(
            check["detail"] for check in result["checks"] if not check["passed"]
        )
        for result in report["scenarios"]
        if not result["passed"]
    )
    return report["passed"], failures


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Run Frontline's hermetic production-contract validation."
    )
    parser.add_argument("--json-out", type=Path, help="Optional JSON evidence path.")
    args = parser.parse_args(argv)
    report = run_report()
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if report["passed"]:
        print("Frontline hermetic production-contract validation: PASS")
        return 0
    print("Frontline hermetic production-contract validation: FAIL")
    for result in report["scenarios"]:
        if not result["passed"]:
            details = "; ".join(
                check["detail"] for check in result["checks"] if not check["passed"]
            )
            print(f"- {result['identifier']}: {details}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
