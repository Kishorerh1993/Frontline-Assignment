"""Scripted, turn-level contracts for Frontline's offline conversation flow.

These are deterministic trajectory checks, not LLM simulations.  A turn names
the expected state, tool action (if any), and prompt instruction that governs
it.  Tool outcomes are synthetic, so the checks remain credential-free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from evals.fixtures import SYNTHETIC_CARRIER_NAME, SYNTHETIC_ORG_NAME


@dataclass(frozen=True)
class ConversationTurn:
    number: int
    speaker: str
    utterance_or_tool_result: str
    expected_state: str
    expected_action: str | None
    prompt_stage: str
    governing_fragment: str


@dataclass(frozen=True)
class ConversationScenario:
    identifier: str
    risk: str
    turns: tuple[ConversationTurn, ...]


def render_prompts() -> Mapping[str, str]:
    """Render the three production prompt states with synthetic values."""
    from voice_prompt import (
        build_full_negotiation_prompt,
        get_initial_greeting_prompt,
        get_known_carrier_greeting_prompt,
    )
    from evals.fixtures import SYNTHETIC_LOAD

    return {
        "initial_mc": get_initial_greeting_prompt(SYNTHETIC_ORG_NAME),
        "initial_phone_verified": get_known_carrier_greeting_prompt(
            SYNTHETIC_CARRIER_NAME, SYNTHETIC_ORG_NAME
        ),
        "loaded_negotiation": build_full_negotiation_prompt(SYNTHETIC_LOAD.copy()),
    }


def conversation_scenarios() -> tuple[ConversationScenario, ...]:
    """Return high-risk scripted turns covering each product state transition."""
    return (
        ConversationScenario(
            "normal-carrier-to-agreement-turns",
            "Carrier flow skips verification, load lookup, or agreement prerequisites",
            (
                ConversationTurn(1, "agent", "initial greeting", "awaiting_mc", None, "initial_mc", "can I get your MC number?"),
                ConversationTurn(2, "caller", "MC 123456", "awaiting_mc_lookup", "verify_carrier", "initial_mc", "Every time the caller gives you a NEW MC number"),
                ConversationTurn(3, "tool", "verify_carrier: success", "awaiting_identity_confirmation", None, "initial_mc", "Ask: \"Is this [carrier_name]?\""),
                ConversationTurn(4, "caller", "Yes", "awaiting_reference", None, "initial_mc", "Once carrier is confirmed, ask for the reference number"),
                ConversationTurn(5, "caller", "Reference EVAL-1042", "awaiting_load_lookup", "get_load_context", "initial_mc", "use the get_load_context function in the same response"),
                ConversationTurn(6, "tool", "get_load_context: success", "negotiating", None, "loaded_negotiation", "YOUR FIRST RESPONSE MUST PRESENT THE LOAD"),
                ConversationTurn(7, "caller", "I can do 1800", "awaiting_agreement_confirmation", None, "loaded_negotiation", "CONFIRMATION STEP"),
                ConversationTurn(8, "caller", "Yes, confirmed", "awaiting_contact", None, "loaded_negotiation", "Ask: \"Can you send me your best phone number and contact name?\""),
                ConversationTurn(9, "caller", "Sam, 555-0100", "recording_agreement", "record_agreement", "loaded_negotiation", "carrier_contact_name, carrier_contact_phone"),
                ConversationTurn(10, "tool", "record_agreement: success", "closing_agreement", "end_call", "loaded_negotiation", "reason='agreement'"),
            ),
        ),
        ConversationScenario(
            "mc-three-strike-closure-turns",
            "Unverified carrier is allowed beyond the third lookup failure",
            (
                ConversationTurn(1, "caller", "MC 111111", "awaiting_mc_lookup", "verify_carrier", "initial_mc", "Every time the caller gives you a NEW MC number"),
                ConversationTurn(2, "tool", "verify_carrier: not_found (1)", "awaiting_mc", None, "initial_mc", "not_found counter < 3"),
                ConversationTurn(3, "caller", "MC 222222", "awaiting_mc_lookup", "verify_carrier", "initial_mc", "2nd and 3rd attempts"),
                ConversationTurn(4, "tool", "verify_carrier: not_found (2)", "awaiting_mc", None, "initial_mc", "not_found counter < 3"),
                ConversationTurn(5, "caller", "MC 333333", "awaiting_mc_lookup", "verify_carrier", "initial_mc", "2nd and 3rd attempts"),
                ConversationTurn(6, "tool", "verify_carrier: not_found (3)", "closing_mc_not_found", "end_call", "initial_mc", "verify_carrier AND end_call must BOTH be called"),
            ),
        ),
        ConversationScenario(
            "phone-identity-replacement-turns",
            "Denied phone identity is reused without new verbal confirmation",
            (
                ConversationTurn(1, "agent", "known-carrier greeting", "awaiting_phone_identity_confirmation", None, "initial_phone_verified", "is this Example Carrier LLC?"),
                ConversationTurn(2, "caller", "No, my MC is 555555", "awaiting_mc_lookup", "verify_carrier", "initial_phone_verified", "Only call verify_carrier if the caller explicitly says they are NOT"),
                ConversationTurn(3, "tool", "verify_carrier: success for new carrier", "awaiting_new_identity_confirmation", None, "initial_phone_verified", "brand-new identity"),
                ConversationTurn(4, "caller", "Yes", "awaiting_reference", None, "initial_phone_verified", "Text only: \"Great, do you have a reference number?\""),
            ),
        ),
        ConversationScenario(
            "transfer-prerequisite-turns",
            "Human transfer starts before a successful load lookup",
            (
                ConversationTurn(1, "caller", "Can I speak with a person?", "awaiting_reference", None, "loaded_negotiation", "Do NOT transfer without a confirmed load reference."),
                ConversationTurn(2, "tool", "get_load_context: success", "transfer_eligible", None, "loaded_negotiation", "get_load_context completed successfully"),
                ConversationTurn(3, "caller", "Please transfer me", "transferring", "transfer_to_human", "loaded_negotiation", "Carrier identity verified"),
            ),
        ),
    )
