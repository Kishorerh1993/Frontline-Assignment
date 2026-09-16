"""Stage-aware, deterministic contracts for the offline evaluation suite.

These scenarios deliberately evaluate prompt and tool *contracts*. They do not
claim to execute, sample, or judge a live LLM response.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, FrozenSet, Mapping

from evals.fixtures import (
    SYNTHETIC_CARRIER_NAME,
    SYNTHETIC_LOAD,
    SYNTHETIC_ORG_NAME,
)


CARRIER_ROOM_TOOL_NAMES: FrozenSet[str] = frozenset(
    {
        "verify_carrier",
        "get_load_context",
        "record_agreement",
        "end_call",
        "transfer_to_human",
    }
)

BROKER_ROOM_TOOL_NAMES: FrozenSet[str] = frozenset({"transfer_human_to_carrier"})

CARRIER_ROOM_REQUIRED_ARGUMENTS: Mapping[str, FrozenSet[str]] = {
    "verify_carrier": frozenset({"mc_number"}),
    "get_load_context": frozenset({"load_id"}),
    "record_agreement": frozenset({"agreed_price"}),
    "end_call": frozenset({"reason"}),
    "transfer_to_human": frozenset({"reason", "load_number"}),
}

BROKER_ROOM_REQUIRED_ARGUMENTS: Mapping[str, FrozenSet[str]] = {
    "transfer_human_to_carrier": frozenset({"summary"}),
}

# Parameter type and enum contracts are deliberately separate from prose
# descriptions: these values define the LLM-facing API and must not drift.
CARRIER_ROOM_PARAMETER_CONTRACTS: Mapping[
    str, Mapping[str, tuple[str, FrozenSet[str] | None]]
] = {
    "verify_carrier": {"mc_number": ("string", None)},
    "get_load_context": {"load_id": ("string", None)},
    "record_agreement": {
        "agreed_price": ("number", None),
        "above_max": ("boolean", None),
        "carrier_contact_name": ("string", None),
        "carrier_contact_phone": ("string", None),
    },
    "end_call": {
        "reason": (
            "string",
            frozenset(
                {
                    "abrupt",
                    "agreement",
                    "no_agreement",
                    "bid_placed",
                    "error",
                    "load_not_found",
                    "mc_not_found",
                }
            ),
        ),
    },
    "transfer_to_human": {
        "reason": ("string", None),
        "load_number": ("string", None),
        "price_asked_by_carrier": ("number", None),
        "best_price_offered_by_bot": ("number", None),
    },
}

BROKER_ROOM_PARAMETER_CONTRACTS: Mapping[
    str, Mapping[str, tuple[str, FrozenSet[str] | None]]
] = {"transfer_human_to_carrier": {"summary": ("string", None)}}

CARRIER_ROOM_HANDLER_BINDINGS: Mapping[str, str] = {
    "verify_carrier": "verify_carrier",
    "get_load_context": "get_load_context_with_metadata",
    "record_agreement": "record_agreement",
    "end_call": "end_call_with_task",
    "transfer_to_human": "transfer_handler.handle_transfer_to_human",
}

BROKER_ROOM_HANDLER_BINDINGS: Mapping[str, str] = {
    "transfer_human_to_carrier": "transfer_handler.handle_transfer_human_to_carrier",
}


@dataclass(frozen=True)
class PromptScenario:
    """One high-risk prompt contract for a specific room and prompt stage."""

    identifier: str
    risk: str
    room: str
    prompt_stage: str
    render: Callable[[], str]
    expected_tool_names: FrozenSet[str]
    required_fragments: tuple[str, ...]
    # Keep this position for callers that constructed PromptScenario with the
    # original positional API.
    prohibited_fragments: tuple[str, ...] = ()
    ordered_fragments: tuple[str, ...] = ()
    expected_required_arguments: Mapping[str, FrozenSet[str]] = field(
        default_factory=dict
    )
    expected_parameter_contracts: Mapping[
        str, Mapping[str, tuple[str, FrozenSet[str] | None]]
    ] = field(default_factory=dict)
    expected_handler_bindings: Mapping[str, str] = field(default_factory=dict)


def _render_initial_mc_prompt() -> str:
    from voice_prompt import get_initial_greeting_prompt

    return get_initial_greeting_prompt(SYNTHETIC_ORG_NAME)


def _render_phone_verified_prompt() -> str:
    from voice_prompt import get_known_carrier_greeting_prompt

    return get_known_carrier_greeting_prompt(
        SYNTHETIC_CARRIER_NAME, SYNTHETIC_ORG_NAME
    )


def _render_loaded_negotiation_prompt() -> str:
    from voice_prompt import build_full_negotiation_prompt

    return build_full_negotiation_prompt(SYNTHETIC_LOAD.copy())


def prompt_scenarios() -> tuple[PromptScenario, ...]:
    """Return the small, high-value prompt contract catalog.

    Fragment assertions are intentionally limited to rules whose removal would
    change a safety-critical product contract. Copy/style changes should not
    require updating these scenarios.
    """

    return (
        PromptScenario(
            identifier="initial-mc-lookup-contract",
            risk="Identity verification and lookup-result hallucination",
            room="carrier_room",
            prompt_stage="initial_mc",
            render=_render_initial_mc_prompt,
            expected_tool_names=CARRIER_ROOM_TOOL_NAMES,
            expected_required_arguments=CARRIER_ROOM_REQUIRED_ARGUMENTS,
            expected_parameter_contracts=CARRIER_ROOM_PARAMETER_CONTRACTS,
            expected_handler_bindings=CARRIER_ROOM_HANDLER_BINDINGS,
            required_fragments=(
                "can I get your MC number?",
                "MANDATORY TOOL-CALL RULE",
                "Every time the caller gives you a NEW MC number",
                "NEVER state a lookup outcome",
                "verify_carrier AND end_call must BOTH be called",
                "reason='mc_not_found'",
                "IMPORTANT: You MUST verify the carrier BEFORE asking about the reference number.",
            ),
            prohibited_fragments=("confirm_carrier_identity",),
        ),
        PromptScenario(
            identifier="phone-identity-replacement-contract",
            risk="Stale phone-preverified identity after caller denial",
            room="carrier_room",
            prompt_stage="initial_phone_verified",
            render=_render_phone_verified_prompt,
            expected_tool_names=CARRIER_ROOM_TOOL_NAMES,
            expected_required_arguments=CARRIER_ROOM_REQUIRED_ARGUMENTS,
            expected_parameter_contracts=CARRIER_ROOM_PARAMETER_CONTRACTS,
            expected_handler_bindings=CARRIER_ROOM_HANDLER_BINDINGS,
            required_fragments=(
                SYNTHETIC_CARRIER_NAME,
                "IDENTITY REPLACEMENT RULE",
                "brand-new identity",
                "EXAMPLE — DENY THEN REVERIFY",
                "Text only: \"Great, do you have a reference number?\"",
                "backend records the confirmed identity automatically",
            ),
            prohibited_fragments=("confirm_carrier_identity",),
        ),
        PromptScenario(
            identifier="loaded-negotiation-confidentiality-contract",
            risk="Confidential-price leakage and invalid negotiation closure",
            room="carrier_room",
            prompt_stage="loaded_negotiation",
            render=_render_loaded_negotiation_prompt,
            expected_tool_names=CARRIER_ROOM_TOOL_NAMES,
            expected_required_arguments=CARRIER_ROOM_REQUIRED_ARGUMENTS,
            expected_parameter_contracts=CARRIER_ROOM_PARAMETER_CONTRACTS,
            expected_handler_bindings=CARRIER_ROOM_HANDLER_BINDINGS,
            required_fragments=(
                "YOUR FIRST RESPONSE MUST PRESENT THE LOAD",
                "Reference Number: EVAL-1042",
                "Opening Offer: $1800.0",
                "Internal Goal: $1950.0 (CONFIDENTIAL",
                "Internal Ceiling: $2200.0 (NEVER EXCEED",
                "FORBIDDEN DOLLAR AMOUNTS",
                "NEVER counter with a rate HIGHER than the carrier's bid",
                "above_max=true",
                "confirm price → collect contact info → record_agreement → end_call",
                "Do NOT transfer without a confirmed load reference.",
            ),
            ordered_fragments=(
                "YOUR FIRST RESPONSE MUST PRESENT THE LOAD",
                "Reference Number: EVAL-1042",
                "Opening Offer: $1800.0",
                "confirm price → collect contact info → record_agreement → end_call",
            ),
        ),
        PromptScenario(
            identifier="broker-room-tool-contract",
            risk="Broker handoff tool is available only in the broker room",
            room="broker_room",
            prompt_stage="broker_handoff",
            render=lambda: "",
            expected_tool_names=BROKER_ROOM_TOOL_NAMES,
            expected_required_arguments=BROKER_ROOM_REQUIRED_ARGUMENTS,
            expected_parameter_contracts=BROKER_ROOM_PARAMETER_CONTRACTS,
            expected_handler_bindings=BROKER_ROOM_HANDLER_BINDINGS,
            required_fragments=(),
        ),
    )
