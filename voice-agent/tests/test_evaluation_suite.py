"""Tests for the safe, deterministic evaluation runner."""

from __future__ import annotations

import pytest

from evals.runner import (
    CheckResult,
    EvaluationReport,
    ScenarioResult,
    _handler_scenarios,
    _write_json,
    evaluate_conversation_scenario,
    evaluate_prompt_scenario,
    main,
    registered_tools_by_room,
    run_evaluations,
    run_evaluations_async,
)
from evals.scenarios import (
    CARRIER_ROOM_HANDLER_BINDINGS,
    CARRIER_ROOM_PARAMETER_CONTRACTS,
    CARRIER_ROOM_REQUIRED_ARGUMENTS,
    PromptScenario,
)
from evals.conversations import ConversationScenario, ConversationTurn
from evals.hermetic_validation import run as run_hermetic_validation


def test_default_evaluation_catalog_passes():
    report = run_evaluations()

    assert report.passed
    assert {scenario.identifier for scenario in report.scenarios} == {
        "initial-mc-lookup-contract",
        "phone-identity-replacement-contract",
        "loaded-negotiation-confidentiality-contract",
        "broker-room-tool-contract",
        "invalid-reference-handler-contract",
        "agreement-prerequisite-handler-contract",
        "transfer-prerequisite-handler-contract",
        "normal-carrier-to-agreement-turns",
        "mc-three-strike-closure-turns",
        "phone-identity-replacement-turns",
        "transfer-prerequisite-turns",
    }
    assert report.enforcement_gaps


def test_hermetic_validation_exercises_real_prompt_and_schema_source():
    passed, failures = run_hermetic_validation()

    assert passed, failures


def test_runner_reports_missing_contract_fragment_without_raising():
    scenario = PromptScenario(
        identifier="diagnostic-fixture",
        risk="diagnostic behavior",
        room="carrier_room",
        prompt_stage="test",
        render=lambda: "safe prompt",
        expected_tool_names=registered_tools_by_room()["carrier_room"],
        required_fragments=("required but absent",),
    )

    result = evaluate_prompt_scenario(scenario, registered_tools_by_room())

    assert not result.passed
    failed_checks = [check for check in result.checks if not check.passed]
    assert len(failed_checks) == 1
    assert failed_checks[0].name == "requires:required but absent"


def test_runner_reports_unknown_room_as_a_failed_contract():
    scenario = PromptScenario(
        identifier="unknown-room-fixture",
        risk="diagnostic behavior",
        room="missing_room",
        prompt_stage="test",
        render=lambda: "safe prompt",
        expected_tool_names=frozenset(),
        required_fragments=(),
    )

    result = evaluate_prompt_scenario(scenario, registered_tools_by_room())

    assert not result.passed
    assert result.checks[0].name == "room_tool_contract"


def test_runner_reports_schema_and_handler_contract_drift():
    scenario = PromptScenario(
        identifier="tool-drift-fixture",
        risk="tool compatibility",
        room="carrier_room",
        prompt_stage="test",
        render=lambda: "safe prompt",
        expected_tool_names=frozenset({"safe_tool"}),
        required_fragments=(),
        expected_required_arguments={"safe_tool": frozenset({"required_value"})},
    )

    result = evaluate_prompt_scenario(
        scenario,
        {"carrier_room": frozenset({"safe_tool"})},
        {"carrier_room": {"safe_tool": frozenset()}},
        {"carrier_room": {"safe_tool": {"type": "wrong"}}},
        {"carrier_room": {"safe_tool": "wrong_handler"}},
    )

    failures = {check.name for check in result.checks if not check.passed}
    assert {
        "tool_parameter_contract",
        "tool_schema_contract",
        "tool_handler_binding_contract",
    } <= failures


def test_turn_evaluator_checks_every_turn_and_reports_missing_contract():
    scenario = ConversationScenario(
        "turn-fixture",
        "turn-level diagnostics",
        (
            ConversationTurn(
                1,
                "caller",
                "synthetic request",
                "state_a",
                "verify_carrier",
                "initial",
                "governing text",
            ),
            ConversationTurn(
                2,
                "tool",
                "synthetic result",
                "state_b",
                None,
                "initial",
                "missing text",
            ),
        ),
    )

    result = evaluate_conversation_scenario(
        scenario,
        {"initial": "governing text verify_carrier"},
    )

    assert not result.passed
    assert any(
        check.name == "turn-2:governing-contract" and not check.passed
        for check in result.checks
    )


def test_turn_evaluator_rejects_empty_and_invalid_turn_contracts():
    empty = ConversationScenario("empty", "invalid input", ())
    empty_result = evaluate_conversation_scenario(empty, {"initial": "safe"})
    assert not empty_result.passed
    assert empty_result.checks[0].name == "conversation_has_turns"

    invalid = ConversationScenario(
        "invalid", "invalid input", (
            ConversationTurn(
                2,
                "caller",
                "request",
                "state",
                "",
                "initial",
                "",
            ),
        )
    )
    invalid_result = evaluate_conversation_scenario(invalid, {"initial": "safe"})
    assert not invalid_result.passed
    failed = {check.name for check in invalid_result.checks if not check.passed}
    assert {
        "turn-2:sequence",
        "turn-2:governing-contract",
        "turn-2:expected-action",
    } <= failed


def test_prompt_scenario_preserves_original_positional_prohibited_argument():
    scenario = PromptScenario(
        "positional",
        "compatibility",
        "carrier_room",
        "test",
        lambda: "safe forbidden",
        frozenset({"safe_tool"}),
        ("safe",),
        ("forbidden",),
    )

    result = evaluate_prompt_scenario(
        scenario,
        {"carrier_room": frozenset({"safe_tool"})},
    )

    assert not result.passed
    assert any(check.name == "forbids:forbidden" for check in result.checks)


def test_runner_reports_prompt_render_failure_as_one_failed_scenario():
    scenario = PromptScenario(
        identifier="render-error-fixture",
        risk="diagnostic behavior",
        room="carrier_room",
        prompt_stage="test",
        render=lambda: (_ for _ in ()).throw(RuntimeError("render failed")),
        expected_tool_names=registered_tools_by_room()["carrier_room"],
        required_fragments=(),
        expected_required_arguments=CARRIER_ROOM_REQUIRED_ARGUMENTS,
        expected_parameter_contracts=CARRIER_ROOM_PARAMETER_CONTRACTS,
        expected_handler_bindings=CARRIER_ROOM_HANDLER_BINDINGS,
    )

    report = run_evaluations((scenario,))

    assert not report.passed
    assert report.scenarios[0].checks[0].name == "scenario_execution"
    assert "render failed" in report.scenarios[0].checks[0].detail


@pytest.mark.asyncio
async def test_async_runner_api_supports_an_active_event_loop():
    scenario = PromptScenario(
        identifier="async-fixture",
        risk="API compatibility",
        room="carrier_room",
        prompt_stage="test",
        render=lambda: "safe prompt",
        expected_tool_names=registered_tools_by_room()["carrier_room"],
        required_fragments=("safe",),
        expected_required_arguments=CARRIER_ROOM_REQUIRED_ARGUMENTS,
        expected_parameter_contracts=CARRIER_ROOM_PARAMETER_CONTRACTS,
        expected_handler_bindings=CARRIER_ROOM_HANDLER_BINDINGS,
    )

    report = await run_evaluations_async((scenario,))

    assert report.passed


@pytest.mark.asyncio
async def test_handler_evaluation_fails_safely_if_reference_guard_regresses(
    monkeypatch,
):
    import call_helpers

    monkeypatch.setattr(call_helpers, "_is_plausible_load_reference", lambda _: True)

    results = await _handler_scenarios()
    invalid_reference = next(
        result
        for result in results
        if result.identifier == "invalid-reference-handler-contract"
    )

    assert not invalid_reference.passed
    assert "side effect attempted" in invalid_reference.checks[0].detail


def test_empty_catalog_is_rejected():
    with pytest.raises(ValueError, match="must contain at least one scenario"):
        run_evaluations(())


def test_main_returns_controlled_error_for_runner_initialization_failure(
    monkeypatch, capsys
):
    def fail_runner():
        raise RuntimeError("boom")

    monkeypatch.setattr("evals.runner.run_evaluations", fail_runner)

    assert main([]) == 2
    assert "Frontline offline evaluation: ERROR: boom" in capsys.readouterr().err


def test_json_artifact_contains_report_data(tmp_path):
    report = EvaluationReport(
        scenarios=(
            ScenarioResult(
                "fixture",
                "fixture risk",
                "carrier_room",
                "fixture",
                True,
                (CheckResult("fixture", True, "fixture passed"),),
            ),
        ),
        enforcement_gaps=("fixture limitation",),
    )
    output_path = tmp_path / "report.json"

    _write_json(output_path, report)

    assert '"passed": true' in output_path.read_text(encoding="utf-8")
