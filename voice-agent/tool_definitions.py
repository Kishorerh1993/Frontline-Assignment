"""Canonical tool definitions for the voice agent.

Single source of truth for all FunctionSchema definitions used in the
voice pipeline. Imported by bot.py, context_builder.py, and tests.

Each tool's FunctionSchema can be converted to OpenAI dict format via
``.to_default_dict()`` for direct API calls.
"""

from pipecat.adapters.schemas.function_schema import FunctionSchema


# ── Pre-negotiation tools ────────────────────────────────────────────────

VERIFY_CARRIER_FUNCTION = FunctionSchema(
    name="verify_carrier",
    description=(
        "Verify the carrier's identity using their MC (Motor Carrier) number. "
        "Call this when the caller provides their MC number at the beginning of the call."
    ),
    properties={
        "mc_number": {
            "type": "string",
            "description": "The MC number provided by the caller (e.g., '1233445')",
        }
    },
    required=["mc_number"],
)

GET_LOAD_CONTEXT_FUNCTION = FunctionSchema(
    name="get_load_context",
    description=(
        "Retrieve detailed information about a freight load from the database. "
        "Call this when the caller mentions a load ID or number they want to discuss."
    ),
    properties={
        "load_id": {
            "type": "string",
            "description": "The load ID or reference number (e.g., 'LOAD-1234' or just '1234')",
        }
    },
    required=["load_id"],
)


# ── Negotiation tools ───────────────────────────────────────────────────

RECORD_AGREEMENT_FUNCTION = FunctionSchema(
    name="record_agreement",
    description=(
        "Record a negotiation agreement or above-max bid. For regular agreements, "
        "just pass agreed_price. For above-max bids (when carrier won't go below "
        "max rate), also pass above_max=true with their contact info."
    ),
    properties={
        "agreed_price": {
            "type": "number",
            "description": "The agreed price (or proposed price for above-max bids) in dollars",
        },
        "above_max": {
            "type": "boolean",
            "description": "Set to true when storing an above-max bid for follow-up (default: false)",
        },
        "carrier_contact_name": {
            "type": "string",
            "description": "Carrier contact name (required if above_max is true)",
        },
        "carrier_contact_phone": {
            "type": "string",
            "description": (
                "Carrier contact phone. Leave empty if caller says "
                "'use this one' or similar - their caller ID will be used."
            ),
        },
    },
    required=["agreed_price"],
)

END_CALL_FUNCTION = FunctionSchema(
    name="end_call",
    description=(
        "End the call with a specific reason. Use 'agreement' after recording a deal, "
        "'bid_placed' after storing an above-max bid, 'no_agreement' when carrier "
        "declines (bad fit, wrong location, etc.), 'load_not_found'/'mc_not_found' "
        "for lookup failures, 'abrupt' for disconnects, 'error' for technical issues."
    ),
    properties={
        "reason": {
            "type": "string",
            "enum": [
                "abrupt",
                "agreement",
                "no_agreement",
                "bid_placed",
                "error",
                "load_not_found",
                "mc_not_found",
            ],
            "description": "The reason for ending the call",
        },
    },
    required=["reason"],
)

TRANSFER_TO_HUMAN_FUNCTION = FunctionSchema(
    name="transfer_to_human",
    description="Transfer the call to a human broker. ONLY call this after get_load_context has succeeded.",
    properties={
        "reason": {
            "type": "string",
            "description": "Reason for transfer",
        },
        "load_number": {
            "type": "string",
            "description": "Load identifier",
        },
        "price_asked_by_carrier": {
            "type": "number",
            "description": "Carrier requested price",
        },
        "best_price_offered_by_bot": {
            "type": "number",
            "description": "Bot offered best price",
        },
    },
    required=["reason", "load_number"],
)


# ── Room2 tools ──────────────────────────────────────────────────────────

TRANSFER_HUMAN_TO_CARRIER_FUNCTION = FunctionSchema(
    name="transfer_human_to_carrier",
    description=(
        "Transfer the human broker to the carrier's room after finishing discussion. "
        "Call this when the human broker is ready to speak directly with the carrier."
    ),
    properties={
        "summary": {
            "type": "string",
            "description": "Brief summary of what was discussed with the human broker",
        },
    },
    required=["summary"],
)


# Room-level collections are the canonical schemas supplied to each LLM
# context. Keep callers on these collections rather than rebuilding lists in
# multiple pipeline modules.
CARRIER_ROOM_FUNCTIONS = (
    VERIFY_CARRIER_FUNCTION,
    GET_LOAD_CONTEXT_FUNCTION,
    RECORD_AGREEMENT_FUNCTION,
    END_CALL_FUNCTION,
    TRANSFER_TO_HUMAN_FUNCTION,
)

BROKER_ROOM_FUNCTIONS = (TRANSFER_HUMAN_TO_CARRIER_FUNCTION,)
