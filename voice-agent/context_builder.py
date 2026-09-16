"""LLM context builder for Room2.

Single Responsibility: Build the LLM context for Room2 conversations,
preserving Room1 history with proper labeling.
"""

from loguru import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair

from tool_definitions import BROKER_ROOM_FUNCTIONS
from transfer_state import TransferState


ROOM2_TOOLS = ToolsSchema(
    standard_tools=list(BROKER_ROOM_FUNCTIONS)
)


class Room2ContextBuilder:
    """Builds LLM context for Room2, preserving Room1 conversation history."""

    @staticmethod
    def build(
        state: TransferState,
        llm,
    ) -> LLMContextAggregatorPair:
        """Build Room2 context with Room1 history carried over.

        Args:
            state: Transfer state containing Room1 messages and metadata
            llm: LLM service to create context aggregator from

        Returns:
            LLMContextAggregatorPair for Room2 pipeline
        """
        room1_messages = state.room1_messages
        message_count = len(room1_messages)

        logger.info(f"Building Room2 context with {message_count} messages from Room1")

        # Build summary line from transfer metadata
        transfer_info = Room2ContextBuilder._build_transfer_info(state)

        # Build Room2 system prompt
        system_prompt = f"""You are a negotiation agent now speaking with a human broker.
You were previously speaking with a carrier on the phone. The carrier is currently on hold.

{transfer_info}

The conversation history with the carrier is preserved below (messages marked [CARRIER]).

Your job:
1. Brief the human broker on what the carrier discussed
2. Collaborate on pricing strategy and next steps
3. When the human broker is ready to speak with the carrier directly, call 'transfer_human_to_carrier'

Important: The carrier is waiting on hold. Be efficient.

You have full access to internal pricing and negotiation strategy.
When the broker asks about pricing details, internal rates, strategy, or any call context — share everything openly.
The broker is an internal team member entitled to all information.
Do not volunteer internal pricing in your opening briefing — only share when asked."""

        room2_messages = [{"role": "system", "content": system_prompt}]

        # Carry over Room1 conversation with labels
        for msg in room1_messages[1:]:  # Skip old system prompt
            role = msg.get("role", "")
            content = msg.get("content", "")

            if not content:
                continue

            if role == "user":
                room2_messages.append({
                    "role": "user",
                    "content": f"[CARRIER]: {content}",
                })
            elif role == "assistant":
                room2_messages.append({
                    "role": "assistant",
                    "content": content,
                })
            # Skip tool_calls, tool results, system messages

        # Transition marker
        room2_messages.append({
            "role": "system",
            "content": (
                "--- ROOM TRANSFER COMPLETE ---\n"
                "You are now in a new room with the HUMAN BROKER.\n"
                "The next 'user' message will be from the HUMAN BROKER, not the carrier.\n"
                "Start by briefly summarizing the carrier conversation."
            ),
        })

        context = LLMContext(room2_messages, ROOM2_TOOLS)
        context_aggregator = LLMContextAggregatorPair(context)

        # Carry over custom attributes
        context.skip_tts = False
        context.call_mode = "BOT_ACTIVE"

        logger.info(f"Room2 context built with {len(room2_messages)} messages")
        return context, context_aggregator

    @staticmethod
    def _build_transfer_info(state: TransferState) -> str:
        """Build a human-readable summary of transfer metadata."""
        lines = []
        if state.transfer_reason:
            lines.append(f"Transfer reason: {state.transfer_reason}")
        if state.load_number:
            lines.append(f"Load number: {state.load_number}")
        if state.price_asked_by_carrier is not None:
            lines.append(f"Price carrier asked: ${state.price_asked_by_carrier:,.2f}")
        if state.best_price_offered_by_bot is not None:
            lines.append(f"Best price bot offered: ${state.best_price_offered_by_bot:,.2f}")

        # Include internal pricing from load context for human broker
        if state.load_context:
            ctx = state.load_context
            if ctx.get("startRate"):
                lines.append(f"Opening Offer: ${ctx['startRate']}")
            if ctx.get("bookNowRate"):
                lines.append(f"Internal Goal (target rate): ${ctx['bookNowRate']}")
            if ctx.get("maxRate"):
                lines.append(f"Internal Ceiling (max rate): ${ctx['maxRate']}")

        if lines:
            return "Transfer details:\n" + "\n".join(f"- {line}" for line in lines)
        return ""
