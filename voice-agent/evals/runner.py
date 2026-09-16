"""Safe, deterministic evaluator for Frontline prompt and tool contracts.

The runner deliberately avoids booting the voice pipeline.  Imports of runtime
dependencies happen only while a check is being run, so a missing dependency is
reported as a controlled runner error rather than a partial evaluation result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import threading
from ast import Call, Constant, NodeVisitor, parse, unparse
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Mapping
from unittest.mock import AsyncMock, MagicMock, patch


_HANDLER_PATCH_LOCK = threading.Lock()


@asynccontextmanager
async def _handler_patch_guard():
    """Serialize global patches safely, including when a caller cancels."""
    acquire_task = asyncio.create_task(asyncio.to_thread(_HANDLER_PATCH_LOCK.acquire))
    acquired = False
    try:
        await asyncio.shield(acquire_task)
        acquired = True
        yield
    except asyncio.CancelledError:
        # The worker may acquire after cancellation reaches this task. Attach a
        # release callback instead of awaiting it, so repeated cancellation
        # cannot strand the global lock or delay cancellation propagation.
        def release_after_acquire(task: asyncio.Task) -> None:
            if not task.cancelled() and task.exception() is None:
                _HANDLER_PATCH_LOCK.release()

        if not acquired:
            acquire_task.add_done_callback(release_after_acquire)
        raise
    finally:
        if acquired:
            _HANDLER_PATCH_LOCK.release()


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


@dataclass(frozen=True)
class ScenarioResult:
    identifier: str
    risk: str
    room: str
    prompt_stage: str
    passed: bool
    checks: tuple[CheckResult, ...]


@dataclass(frozen=True)
class EvaluationReport:
    scenarios: tuple[ScenarioResult, ...]
    enforcement_gaps: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return all(scenario.passed for scenario in self.scenarios)


def registered_tools_by_room() -> dict[str, frozenset[str]]:
    """Read the schemas actually registered by each room from their source of truth."""
    from tool_definitions import BROKER_ROOM_FUNCTIONS, CARRIER_ROOM_FUNCTIONS

    return {
        "carrier_room": frozenset(schema.name for schema in CARRIER_ROOM_FUNCTIONS),
        "broker_room": frozenset(schema.name for schema in BROKER_ROOM_FUNCTIONS),
    }


def registered_tool_required_arguments_by_room() -> dict[str, dict[str, frozenset[str]]]:
    """Read required tool parameters from the actual schemas supplied to the LLM."""
    from tool_definitions import BROKER_ROOM_FUNCTIONS, CARRIER_ROOM_FUNCTIONS

    def required_arguments(schemas) -> dict[str, frozenset[str]]:
        contracts: dict[str, frozenset[str]] = {}
        for schema in schemas:
            payload = schema.to_default_dict()
            function = payload.get("function", payload)
            parameters = function.get("parameters", {})
            contracts[function["name"]] = frozenset(parameters.get("required", []))
        return contracts

    return {
        "carrier_room": required_arguments(CARRIER_ROOM_FUNCTIONS),
        "broker_room": required_arguments(BROKER_ROOM_FUNCTIONS),
    }


def registered_tool_parameter_contracts_by_room() -> dict:
    """Read each exposed parameter's API-significant type and enum."""
    from tool_definitions import BROKER_ROOM_FUNCTIONS, CARRIER_ROOM_FUNCTIONS

    def contracts(schemas) -> dict:
        result = {}
        for schema in schemas:
            payload = schema.to_default_dict()
            function = payload.get("function", payload)
            properties = function.get("parameters", {}).get("properties", {})
            result[function["name"]] = {
                name: (
                    value.get("type"),
                    frozenset(value["enum"]) if "enum" in value else None,
                )
                for name, value in properties.items()
            }
        return result

    return {
        "carrier_room": contracts(CARRIER_ROOM_FUNCTIONS),
        "broker_room": contracts(BROKER_ROOM_FUNCTIONS),
    }


class _RegisterFunctionVisitor(NodeVisitor):
    def __init__(self) -> None:
        self.bindings: dict[str, str] = {}

    def visit_Call(self, node: Call) -> None:
        if (
            getattr(node.func, "attr", None) == "register_function"
            and node.args
            and isinstance(node.args[0], Constant)
            and isinstance(node.args[0].value, str)
            and len(node.args) >= 2
        ):
            self.bindings[node.args[0].value] = unparse(node.args[1])
        self.generic_visit(node)


def registered_handler_bindings_by_room() -> dict[str, dict[str, str]]:
    """Inspect the two pipeline modules that bind handlers to exposed tools.

    This source-level check is intentionally import-free: importing the full
    voice pipeline would initialize heavyweight provider dependencies merely to
    validate its static registration statements.
    """
    root = Path(__file__).resolve().parents[1]

    def registrations(relative_path: str) -> dict[str, str]:
        visitor = _RegisterFunctionVisitor()
        visitor.visit(parse((root / relative_path).read_text(encoding="utf-8")))
        return visitor.bindings

    return {
        "carrier_room": registrations("bot.py"),
        "broker_room": registrations("room2_pipeline_service.py"),
    }


def evaluate_prompt_scenario(
    scenario,
    tools_by_room: Mapping[str, frozenset[str]],
    required_arguments_by_room: Mapping[str, Mapping[str, frozenset[str]]] | None = None,
    parameter_contracts_by_room: Mapping | None = None,
    handler_bindings_by_room: Mapping[str, Mapping[str, str]] | None = None,
) -> ScenarioResult:
    """Evaluate one rendered prompt without calling a model or an external service."""
    checks: list[CheckResult] = []
    actual_tools = tools_by_room.get(scenario.room)
    if actual_tools is None:
        checks.append(
            CheckResult("room_tool_contract", False, f"Unknown room: {scenario.room}")
        )
    else:
        checks.append(
            CheckResult(
                "room_tool_contract",
                actual_tools == scenario.expected_tool_names,
                "registered tools match expected contract"
                if actual_tools == scenario.expected_tool_names
                else f"expected {sorted(scenario.expected_tool_names)}, got {sorted(actual_tools)}",
            )
        )

    if required_arguments_by_room is not None:
        actual_required = required_arguments_by_room.get(scenario.room)
        expected_required = dict(scenario.expected_required_arguments)
        checks.append(
            CheckResult(
                "tool_parameter_contract",
                actual_required == expected_required,
                "required tool parameters match expected contract"
                if actual_required == expected_required
                else f"expected {expected_required}, got {actual_required}",
            )
        )

    if parameter_contracts_by_room is not None:
        actual_parameters = parameter_contracts_by_room.get(scenario.room)
        expected_parameters = dict(scenario.expected_parameter_contracts)
        checks.append(
            CheckResult(
                "tool_schema_contract",
                actual_parameters == expected_parameters,
                "tool parameter types and enums match expected contract"
                if actual_parameters == expected_parameters
                else f"expected {expected_parameters}, got {actual_parameters}",
            )
        )

    if handler_bindings_by_room is not None:
        actual_bindings = handler_bindings_by_room.get(scenario.room)
        checks.append(
            CheckResult(
                "tool_handler_binding_contract",
                actual_bindings == dict(scenario.expected_handler_bindings),
                "registered handler bindings match expected contract"
                if actual_bindings == dict(scenario.expected_handler_bindings)
                else (
                    f"expected {dict(scenario.expected_handler_bindings)}, "
                    f"got {actual_bindings}"
                ),
            )
        )
    prompt = scenario.render()
    for fragment in scenario.required_fragments:
        checks.append(
            CheckResult(
                f"requires:{fragment}",
                fragment in prompt,
                "required contract fragment found"
                if fragment in prompt
                else "required contract fragment is absent",
            )
        )
    for fragment in scenario.prohibited_fragments:
        checks.append(
            CheckResult(
                f"forbids:{fragment}",
                fragment not in prompt,
                "prohibited fragment is absent"
                if fragment not in prompt
                else "prohibited fragment is present",
            )
        )
    last_index = -1
    for fragment in scenario.ordered_fragments:
        index = prompt.find(fragment)
        in_order = index >= 0 and index > last_index
        checks.append(
            CheckResult(
                f"ordered:{fragment}",
                in_order,
                "required sequence preserved"
                if in_order
                else "required sequence is absent or out of order",
            )
        )
        if index >= 0:
            last_index = index
    return ScenarioResult(
        scenario.identifier,
        scenario.risk,
        scenario.room,
        scenario.prompt_stage,
        all(check.passed for check in checks),
        tuple(checks),
    )


def _scenario_error(scenario, error: Exception) -> ScenarioResult:
    """Turn one scenario's initialization/rendering error into a diagnostic."""
    return ScenarioResult(
        scenario.identifier,
        scenario.risk,
        scenario.room,
        scenario.prompt_stage,
        False,
        (
            CheckResult(
                "scenario_execution",
                False,
                f"{type(error).__name__}: {error}",
            ),
        ),
    )


def _evaluate_prompt_scenario_safely(
    scenario,
    tools_by_room: Mapping[str, frozenset[str]],
    required_arguments_by_room: Mapping[str, Mapping[str, frozenset[str]]],
    parameter_contracts_by_room: Mapping,
    handler_bindings_by_room: Mapping[str, Mapping[str, str]],
) -> ScenarioResult:
    try:
        return evaluate_prompt_scenario(
            scenario,
            tools_by_room,
            required_arguments_by_room,
            parameter_contracts_by_room,
            handler_bindings_by_room,
        )
    except Exception as error:
        return _scenario_error(scenario, error)


def evaluate_conversation_scenario(scenario, prompts: Mapping[str, str]) -> ScenarioResult:
    """Check every scripted conversation turn against its governing prompt state."""
    checks: list[CheckResult] = []
    turns = getattr(scenario, "turns", ()) or ()
    prompt_mapping = prompts if hasattr(prompts, "get") else {}
    if not turns:
        checks.append(
            CheckResult(
                "conversation_has_turns",
                False,
                "conversation must contain at least one turn",
            )
        )
    prior_number = 0
    for turn in turns:
        numbered = turn.number == prior_number + 1
        checks.append(
            CheckResult(
                f"turn-{turn.number}:sequence",
                numbered,
                "turn number follows the prior turn"
                if numbered
                else f"expected turn {prior_number + 1}, got {turn.number}",
            )
        )
        prior_number = turn.number
        prompt = prompt_mapping.get(turn.prompt_stage)
        usable_prompt = isinstance(prompt, str)
        checks.append(
            CheckResult(
                f"turn-{turn.number}:prompt-stage",
                usable_prompt,
                f"prompt stage {turn.prompt_stage} is rendered"
                if usable_prompt
                else f"unknown prompt stage {turn.prompt_stage}",
            )
        )
        checks.append(
            CheckResult(
                f"turn-{turn.number}:governing-contract",
                isinstance(turn.governing_fragment, str)
                and bool(turn.governing_fragment)
                and usable_prompt
                and turn.governing_fragment in prompt,
                "governing prompt contract is present"
                if (
                    isinstance(turn.governing_fragment, str)
                    and bool(turn.governing_fragment)
                    and usable_prompt
                    and turn.governing_fragment in prompt
                )
                else f"missing contract: {turn.governing_fragment}",
            )
        )
        if turn.expected_action is not None:
            checks.append(
                CheckResult(
                    f"turn-{turn.number}:expected-action",
                    isinstance(turn.expected_action, str)
                    and bool(turn.expected_action)
                    and turn.expected_action in prompt
                    if usable_prompt
                    else False,
                    f"expected action {turn.expected_action} is available in this stage"
                    if (
                        prompt is not None
                        and isinstance(turn.expected_action, str)
                        and bool(turn.expected_action)
                        and turn.expected_action in prompt
                    )
                    else f"expected action {turn.expected_action} is unavailable",
                )
            )
    return ScenarioResult(
        scenario.identifier,
        scenario.risk,
        "carrier_room",
        "scripted_conversation",
        all(check.passed for check in checks),
        tuple(checks),
    )


def _conversation_error(scenario, error: Exception) -> ScenarioResult:
    return ScenarioResult(
        scenario.identifier,
        scenario.risk,
        "carrier_room",
        "scripted_conversation",
        False,
        (CheckResult("conversation_execution", False, f"{type(error).__name__}: {error}"),),
    )


async def _capture_callback(values: list[str], value: str) -> None:
    values.append(value)


async def _handler_scenarios() -> tuple[ScenarioResult, ...]:
    """Exercise rejection paths with all reachable effect boundaries blocked.

    Each mocked side-effect raises if called.  Therefore a future regression in
    a guard fails safely instead of making a database, quote, or transfer call.
    """
    class Context:
        call_id = "evaluation-call"

    def blocked_boundary(name: str) -> MagicMock:
        return MagicMock(side_effect=AssertionError(f"side effect attempted: {name}"))

    def handler_result(
        identifier: str,
        risk: str,
        check_name: str,
        passed: bool,
        detail: str,
    ) -> ScenarioResult:
        return ScenarioResult(
            identifier,
            risk,
            "carrier_room",
            "tool_handler",
            passed,
            (CheckResult(check_name, passed, detail),),
        )

    async def run_handler_safely(
        identifier: str,
        risk: str,
        check_name: str,
        scenario,
    ) -> ScenarioResult:
        try:
            return await scenario()
        except Exception as error:
            return handler_result(
                identifier,
                risk,
                check_name,
                False,
                f"{type(error).__name__}: {error}",
            )

    async def invalid_reference() -> ScenarioResult:
        from call_helpers import get_load_context

        callbacks: list[str] = []
        async with _handler_patch_guard():
            with (
                patch(
                    "call_helpers.NegotiationDBService.get_load_by_reference",
                    blocked_boundary("load lookup"),
                ),
                patch(
                    "call_helpers.persist_confirmed_carrier_identity",
                    AsyncMock(
                        side_effect=AssertionError(
                            "side effect attempted: identity persistence"
                        )
                    ),
                ),
            ):
                try:
                    await get_load_context(
                        "get_load_context",
                        "eval",
                        {"load_id": "not a ref"},
                        None,
                        Context(),
                        lambda value: _capture_callback(callbacks, value),
                    )
                except AssertionError as error:
                    return handler_result(
                        "invalid-reference-handler-contract",
                        "Fabricated reference reaches a data lookup",
                        "rejects_without_lookup",
                        False,
                        str(error),
                    )
        payload = json.loads(callbacks[0]) if callbacks else {}
        passed = (
            len(callbacks) == 1
            and payload.get("status") == "error"
            and "reference number" in payload.get("message", "").lower()
        )
        return handler_result(
            "invalid-reference-handler-contract",
            "Fabricated reference reaches a data lookup",
            "rejects_without_lookup",
            passed,
            "invalid reference was rejected before a database lookup"
            if passed
            else f"unexpected callback: {callbacks}",
        )

    async def agreement_without_load() -> ScenarioResult:
        from call_helpers import record_agreement

        callbacks: list[str] = []
        async with _handler_patch_guard():
            with (
                patch(
                    "call_helpers.save_agreement",
                    blocked_boundary("agreement persistence"),
                ),
                patch(
                    "call_helpers.NegotiationDBService.notify_carrier_quote",
                    blocked_boundary("quote notification"),
                ),
            ):
                try:
                    await record_agreement(
                        "record_agreement",
                        "eval",
                        {"agreed_price": 1900},
                        None,
                        Context(),
                        lambda value: _capture_callback(callbacks, value),
                    )
                except AssertionError as error:
                    return handler_result(
                        "agreement-prerequisite-handler-contract",
                        "Agreement is persisted without a loaded load",
                        "rejects_without_persisting",
                        False,
                        str(error),
                    )
        payload = json.loads(callbacks[0]) if callbacks else {}
        passed = (
            len(callbacks) == 1
            and payload.get("status") == "error"
            and "load not loaded" in payload.get("message", "")
        )
        return handler_result(
            "agreement-prerequisite-handler-contract",
            "Agreement is persisted without a loaded load",
            "rejects_without_persisting",
            passed,
            "agreement rejected before persistence"
            if passed
            else f"unexpected callback: {callbacks}",
        )

    async def transfer_without_load() -> ScenarioResult:
        from transfer_tool_handler import TransferToolHandler

        callbacks: list[str] = []
        orchestrator = MagicMock()
        orchestrator.set_transfer_metadata.side_effect = AssertionError(
            "side effect attempted: transfer orchestration"
        )
        speech_sync = MagicMock()
        speech_sync.schedule_after_speech = AsyncMock(
            side_effect=AssertionError("side effect attempted: transfer scheduling")
        )
        handler = TransferToolHandler(
            orchestrator=orchestrator,
            http_session=MagicMock(),
            stt=MagicMock(),
            tts=MagicMock(),
            llm=MagicMock(),
            skip_tts_processor=MagicMock(),
            transcript=MagicMock(),
            speech_sync=speech_sync,
            audiobuffer=MagicMock(),
            context_aggregator=MagicMock(),
        )
        async with _handler_patch_guard():
            with patch(
                "transfer_tool_handler.NegotiationDBService.get_load",
                blocked_boundary("transfer load lookup"),
            ):
                try:
                    await handler.handle_transfer_to_human(
                        "transfer_to_human",
                        "eval",
                        {"reason": "evaluation", "load_number": "EVAL-1042"},
                        None,
                        Context(),
                        lambda value: _capture_callback(callbacks, value),
                    )
                except AssertionError as error:
                    return handler_result(
                        "transfer-prerequisite-handler-contract",
                        "Transfer begins before load lookup",
                        "rejects_without_transfer",
                        False,
                        str(error),
                    )
        payload = json.loads(callbacks[0]) if callbacks else {}
        passed = (
            len(callbacks) == 1
            and payload.get("status") == "error"
            and "no load reference" in payload.get("message", "").lower()
        )
        return handler_result(
            "transfer-prerequisite-handler-contract",
            "Transfer begins before load lookup",
            "rejects_without_transfer",
            passed,
            "transfer rejected before orchestrator use"
            if passed
            else f"unexpected callback: {callbacks}",
        )

    return (
        await run_handler_safely(
            "invalid-reference-handler-contract",
            "Fabricated reference reaches a data lookup",
            "rejects_without_lookup",
            invalid_reference,
        ),
        await run_handler_safely(
            "agreement-prerequisite-handler-contract",
            "Agreement is persisted without a loaded load",
            "rejects_without_persisting",
            agreement_without_load,
        ),
        await run_handler_safely(
            "transfer-prerequisite-handler-contract",
            "Transfer begins before load lookup",
            "rejects_without_transfer",
            transfer_without_load,
        ),
    )


async def run_evaluations_async(catalog: Iterable | None = None) -> EvaluationReport:
    """Run the small, high-risk contract catalog and return actionable diagnostics."""
    from evals.scenarios import prompt_scenarios

    scenarios = tuple(prompt_scenarios() if catalog is None else catalog)
    if not scenarios:
        raise ValueError("evaluation catalog must contain at least one scenario")
    tools = registered_tools_by_room()
    required_arguments = registered_tool_required_arguments_by_room()
    parameter_contracts = registered_tool_parameter_contracts_by_room()
    handler_bindings = registered_handler_bindings_by_room()
    results = tuple(
        _evaluate_prompt_scenario_safely(
            scenario,
            tools,
            required_arguments,
            parameter_contracts,
            handler_bindings,
        )
        for scenario in scenarios
    )
    if catalog is None:
        results += await _handler_scenarios()
        from evals.conversations import conversation_scenarios, render_prompts

        try:
            prompts = render_prompts()
        except Exception as error:
            for scenario in conversation_scenarios():
                results += (_conversation_error(scenario, error),)
        else:
            for scenario in conversation_scenarios():
                try:
                    results += (evaluate_conversation_scenario(scenario, prompts),)
                except Exception as error:
                    results += (_conversation_error(scenario, error),)
    return EvaluationReport(
        results,
        (
            "Prompt checks cannot prove stochastic LLM compliance or tool-call ordering.",
            "This offline suite does not exercise Daily, PSTN, speech, LLM, "
            "Supabase, carrier, quote, Slack, or broker-transfer providers.",
        ),
    )


def run_evaluations(catalog: Iterable | None = None) -> EvaluationReport:
    """Synchronous CLI/test entry point; async callers should await the async API."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run_evaluations_async(catalog))
    raise RuntimeError(
        "run_evaluations cannot run inside an active event loop; "
        "await run_evaluations_async(...) instead"
    )


def _write_json(path: Path, report: EvaluationReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"passed": report.passed, **asdict(report)}, indent=2) + "\n",
        encoding="utf-8",
    )


def _print_report(report: EvaluationReport) -> None:
    for scenario in report.scenarios:
        status = "PASS" if scenario.passed else "FAIL"
        print(
            f"{status} {scenario.identifier} "
            f"[{scenario.room}/{scenario.prompt_stage}]: {scenario.risk}"
        )
        for check in scenario.checks:
            if not check.passed:
                print(f"  - {check.name}: {check.detail}")
    print(f"Overall: {'PASS' if report.passed else 'FAIL'} ({len(report.scenarios)} scenarios)")
    print("Known enforcement gaps:")
    for gap in report.enforcement_gaps:
        print(f"  - {gap}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Frontline's offline evaluation contracts.")
    parser.add_argument("--json-out", type=Path, help="Optional path for a JSON report artifact.")
    args = parser.parse_args(argv)
    try:
        report = run_evaluations()
        if args.json_out:
            _write_json(args.json_out, report)
        _print_report(report)
        return 0 if report.passed else 1
    except Exception as exc:
        print(f"Frontline offline evaluation: ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
