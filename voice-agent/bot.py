"""Daily PSTN dial-in bot.

This bot demonstrates how to receive inbound phone calls using Daily's PSTN capabilities.
The bot answers incoming calls and conducts voice conversations with callers.
"""

import asyncio
import os
import re
import uvicorn
from functools import partial
from pathlib import Path

import aiohttp
from dotenv import load_dotenv
from loguru import logger
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from tool_definitions import (
    CARRIER_ROOM_FUNCTIONS,
)
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import LLMRunFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.runner import PipelineRunner
from pipecat.pipeline.task import PipelineParams, PipelineTask
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
)
from pipecat.processors.transcript_processor import TranscriptProcessor
from pipecat.processors.audio.audio_buffer_processor import AudioBufferProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.services.deepgram.stt import DeepgramSTTService, LiveOptions
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import BaseTransport
from pipecat.transports.daily.transport import (
    DailyDialinSettings,
    DailyParams,
    DailyTransport,
)
from tts import Qwen3VoiceCloneService
from pipecat.transcriptions.language import Language
from pipecat.services.cartesia.tts import CartesiaTTSService

from pipecat.audio.mixers.soundfile_mixer import SoundfileMixer

from audio_storage import save_audio_to_supabase, store_audio_url_in_call
from response_aware_sentence_aggregator import ResponseAwareSentenceAggregator
from call_helpers import (
    end_call,
    finish_call,
    get_load_context,
    persist_confirmed_carrier_identity,
    record_agreement,
    snapshot_universal_context,
    start_call,
    verify_carrier,
)
from db_operations import get_org_id_for_phone, get_org_name_for_phone
from load_context_utils import get_initial_system_prompt
from phone_verification import resolve_caller_identity
from server_utils import AgentRequest
from speech_sync import SpeechSyncProcessor
from src import SupabaseService
from datetime import datetime, timezone
from tts_skip_processor import TTSSkipGateProcessor

# === TRANSFER: Import transfer orchestration ===
# from transfer.infrastructure.transport_registry import set_transport
from orchestrator import TransferOrchestrator
from transfer_tool_handler import TransferToolHandler

import pipecat.runner.run as runner_module

_request = None
_daily_dialin_settings = None

def set_request(request: AgentRequest):
    global _request
    _request = request

def set_daily_dialin_settings(settings: DailyDialinSettings):
    global _daily_dialin_settings
    _daily_dialin_settings = settings

def get_daily_dialin_settings():
    return _daily_dialin_settings

def get_request():
    return _request



load_dotenv(override=True)

# Initialize Supabase service
SupabaseService.initialize()

# === TRANSFER: Global orchestrator instance ===
_orchestrator: TransferOrchestrator = None


_AFFIRMATIVE_IDENTITY_CONFIRMATION_PHRASES = (
    "yes",
    "yeah",
    "yep",
    "yup",
    "correct",
    "right",
    "that's right",
    "that is right",
    "that's correct",
    "that is correct",
    "that's us",
    "that is us",
    "it is",
    "we are",
    "sure",
    "affirmative",
)
_NEGATIVE_IDENTITY_CONFIRMATION_PHRASES = (
    "no",
    "nope",
    "nah",
    "not us",
    "not our",
    "not correct",
    "that's not us",
    "that is not us",
    "wrong",
    "incorrect",
    "different company",
)


def _normalize_confirmation_text(text: str) -> str:
    cleaned = text.lower().replace("’", "'")
    cleaned = re.sub(r"[^a-z0-9'\s]", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _has_confirmation_phrase(text: str, phrases: tuple[str, ...]) -> bool:
    padded = f" {text} "
    return any(f" {phrase} " in padded for phrase in phrases)


def _clear_staged_carrier_identity(context) -> None:
    context.awaiting_carrier_identity_confirmation = False
    context.carrier_identity_confirmed = False
    context.phone_verified = False
    context.caller_mc = None
    context.carrier_name = None
    context.caller_dot_number = None
    context.identity_source = None


async def _persist_confirmed_carrier_identity_from_transcript(context) -> None:
    try:
        succeeded = await persist_confirmed_carrier_identity(context)
        if succeeded:
            logger.info("Carrier identity confirmed from caller transcript")
        else:
            logger.info(
                "Carrier identity confirmation observed, but persistence "
                "did not complete; get_load_context fallback can retry"
            )
    except Exception:
        logger.exception("Unexpected carrier identity confirmation failure")


def _track_background_task(context, task: asyncio.Task) -> None:
    tasks = getattr(context, "_background_tasks", None)
    if tasks is None:
        tasks = set()
        context._background_tasks = tasks
    tasks.add(task)
    task.add_done_callback(tasks.discard)


def _handle_carrier_identity_confirmation_from_transcript(
    context,
    text: str,
) -> None:
    if not getattr(context, "awaiting_carrier_identity_confirmation", False):
        return

    normalized = _normalize_confirmation_text(text)
    if not normalized:
        return

    if _has_confirmation_phrase(normalized, _NEGATIVE_IDENTITY_CONFIRMATION_PHRASES):
        logger.info(
            "Caller denied staged carrier identity; clearing pending identity"
        )
        _clear_staged_carrier_identity(context)
        return

    if _has_confirmation_phrase(normalized, _AFFIRMATIVE_IDENTITY_CONFIRMATION_PHRASES):
        context.awaiting_carrier_identity_confirmation = False
        task = asyncio.create_task(
            _persist_confirmed_carrier_identity_from_transcript(context)
        )
        _track_background_task(context, task)


def get_orchestrator() -> TransferOrchestrator:
    """Get the global transfer orchestrator instance."""
    return _orchestrator


async def return_room_to_server(room_config: dict) -> None:
    """Return room to pool via HTTP call to webhook server."""
    server_url = os.getenv("LOCAL_SERVER_URL", "http://localhost:8080")
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(f"{server_url}/return-room", json=room_config)
            logger.info(f"Room returned to server pool: {room_config.get('room_url')}")
    except Exception as e:
        logger.error(f"Error returning room to pool: {e}")


async def run_bot(
    transport: BaseTransport,
    handle_sigint: bool,
    daily_call_id: str | None = None,
    caller_phone: str | None = None,
    bot_phone: str | None = None,
    room_config: dict | None = None,
    room_url: str | None = None,
) -> None:
    """Run the voice bot for an inbound call."""

    global _orchestrator

    stt = DeepgramSTTService(
        api_key=os.getenv("DEEPGRAM_API_KEY"),
        live_options=LiveOptions(
            model="nova-3",
            language="en-US",
            smart_format=True,
            interim_results=True,
            encoding="linear16",
            sample_rate=8000,
            endpointing=600,
        ),
    )

    params_kwargs = {
        "language": Language.EN,
        "speaker": "vivian",
        "chunk_size": 37,
        "speed": 1.2,
        "instruct": (
            "Use a neutral american female accent. Speak with a warm, conversational, "
            "friendly tone. Use natural, slight conversational fillers like 'well' or "
            "'you know', and ensure the pacing is relaxed, like a podcast host. Your "
            "speech needs to be very clear and precise. When reading letters and numbers "
            "be extra clear and precise, be mindful of the pronounciation."
        ),
    }

    # tts = Qwen3VoiceCloneService(
    #     websocket_url=os.getenv("QWEN3_TTS_WEBSOCKET_URL"),
    #     params=Qwen3VoiceCloneService.InputParams(**params_kwargs),
    #     sample_rate=24000,
    #     text_aggregator=ResponseAwareSentenceAggregator(),
    # )
    CARTESIA_VOICE = "e07c00bc-4134-4eae-9ea4-1a55fb45746b"
    tts = CartesiaTTSService(
        api_key=os.environ["CARTESIA_API_KEY"],
        voice_id=CARTESIA_VOICE,
    )

    llm = OpenAILLMService(
        api_key=os.getenv("OPENAI_API_KEY"),
        model="gpt-4.1",
        params=OpenAILLMService.InputParams(
            frequency_penalty=0.6,
            presence_penalty=0.5,
            temperature=0.7,
        ),
    )

    # Bind metadata to get_load_context using partial
    get_load_context_with_metadata = partial(
        get_load_context, daily_call_id=daily_call_id, caller_phone=caller_phone
    )

    # Phone-first carrier verification feature flag — gates the entire
    # phone-first behavior so flag=false produces zero behavior change vs.
    # the pre-PR baseline. When true: lookup runs and the transcript-driven
    # confirmation path can persist the phone→carrier mapping.
    phone_first_enabled = (
        os.getenv("HIGHWAY_PHONE_LOOKUP_ENABLED", "false").lower() == "true"
    )

    tools = ToolsSchema(standard_tools=list(CARRIER_ROOM_FUNCTIONS))

    # Fetch organization name + id for greeting and Supabase scoping.
    # We look up org_id directly here (rather than relying on context.org_id
    # which gets set later in create_call_async) because the parallel
    # phone_carrier_lookup query needs it before the LLM context is built.
    org_name = get_org_name_for_phone(bot_phone) if bot_phone else None
    org_id_for_lookup = get_org_id_for_phone(bot_phone) if bot_phone else None
    logger.info(
        f"Organization for greeting: name={org_name} id={org_id_for_lookup}"
    )

    # Phone-first carrier verification (backend, pre-LLM). Runs Highway and
    # Supabase phone_carrier_lookup in parallel. Never raises; failure modes
    # collapse to CarrierLookupResult.unknown(...) and the bot falls through
    # to the current MC-first greeting.
    phone_verification = await resolve_caller_identity(
        caller_phone, org_id_for_lookup
    )
    logger.info(
        f"Phone verification: status={phone_verification.status} "
        f"reason={phone_verification.reason} "
        f"carrier={phone_verification.carrier_name}"
    )

    # Initialize LLM context
    messages = [
        {
            "role": "system",
            "content": get_initial_system_prompt(
                org_name=org_name,
                phone_verification=phone_verification,
                phone_first_enabled=phone_first_enabled,
            ),
        },
    ]

    # Setup the conversational context with tools
    context = LLMContext(messages, tools)
    context_aggregator = LLMContextAggregatorPair(context)

    # Store room_name for call transfer (extract from room_url)
    context.room_name = room_url.split("/")[-1] if room_url else None
    context.session_id = None  # Will be set when participant joins
    context.skip_tts = False
    context.call_mode = "BOT_ACTIVE"

    # Make caller_phone + org_id available on context early so verify_carrier
    # can upsert into phone_carrier_lookup when the LLM verifies an MC,
    # WITHOUT waiting for create_call_async to finish setting them.
    # Otherwise a fast LLM turn (e.g. caller denies a phone-verified carrier
    # and immediately gives a corrected MC) could fire verify_carrier before
    # context.org_id is populated, and the upsert guard would skip the write
    # — leaving the stale phone→MC mapping in place. create_call_async still
    # overwrites context.org_id later with the same value (idempotent).
    context.caller_phone = caller_phone
    context.org_id = org_id_for_lookup
    context.awaiting_carrier_identity_confirmation = False

    # Pre-populate carrier identity on known-carrier path so downstream tools
    # (record_agreement, transfer_to_human, etc.) see the MC without needing
    # the LLM to call verify_carrier.
    if phone_verification.status == "known_carrier":
        context.caller_mc = phone_verification.mc_number
        context.carrier_name = phone_verification.carrier_name
        context.phone_verified = True
        # Track where the identity came from so the backend confirmation path
        # can avoid a no-op write to Supabase when the data is already there.
        # "supabase" => row already exists with same data, skip the write.
        # "highway"  => not yet in Supabase, write on confirmation.
        context.identity_source = (
            "supabase"
            if phone_verification.reason == "from_supabase_lookup"
            else "highway"
        )
        context.carrier_identity_confirmed = False
        context.awaiting_carrier_identity_confirmation = True

    # Transcript processor for capturing human-readable conversation
    transcript = TranscriptProcessor()

    # Create audio buffer processor for recording (mono for telephony)
    audiobuffer = AudioBufferProcessor(num_channels=1)

    # Speech sync processor - tracks TTS start/stop for coordinating function calls
    speech_sync = SpeechSyncProcessor()

    # Skip tts processor
    skip_tts_processor = TTSSkipGateProcessor(context)

    # Curious question processor - wraps questions in <curious> tags for natural intonation
    # curious_processor = CuriousQuestionProcessor()

    # Register the function handlers
    llm.register_function("verify_carrier", verify_carrier)
    llm.register_function("get_load_context", get_load_context_with_metadata)
    llm.register_function("record_agreement", record_agreement)
    # llm.register_function("post_human_joined", post_human_joined)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            transcript.user(),  # Capture user transcript
            context_aggregator.user(),
            llm,
            # curious_processor,  # Wrap questions in <curious> tags for natural intonation
            # frame_log_processor,
            # sentence_pause_processor,
            skip_tts_processor,
            tts,
            speech_sync,  # After TTS to track BotStarted/StoppedSpeakingFrame
            transport.output(),
            audiobuffer,  # Add audio buffer to pipeline for recording
            transcript.assistant(),  # Capture assistant transcript
            context_aggregator.assistant(),
        ]
    )

    task = PipelineTask(
        pipeline,
        params=PipelineParams(
            enable_metrics=True,
            enable_usage_metrics=True,
            allow_interruptions=True,
            audio_in_sample_rate=8000,  # Native G.711 telephony rate
            audio_out_sample_rate=8000,  # Native G.711 telephony rate
            turn_analyzer=LocalSmartTurnAnalyzerV3(),
        ),
    )

    # === TRANSFER: Initialize orchestrator and set Room1 references ===
    _orchestrator = TransferOrchestrator()
    _orchestrator.set_room1_references(
        room1_url=room_url,
        room1_token=get_request().token,  # Token from the original request — see bot() below
        task=task,
        context=context,
        context_aggregator=context_aggregator,
        _daily_dialin_settings=get_daily_dialin_settings(),
        call_id=get_request().call_id,
        sip_endpoint=get_request().sip_endpoint,
    )

    # === TRANSFER: Create transfer tool handler ===
    # We need an aiohttp session for the transfer. Create one or reuse.
    transfer_http_session = aiohttp.ClientSession()

    transfer_handler = TransferToolHandler(
        orchestrator=_orchestrator,
        http_session=transfer_http_session,
        stt=stt,
        tts=tts,
        llm=llm,
        skip_tts_processor=skip_tts_processor,
        transcript=transcript,
        speech_sync=speech_sync,
        audiobuffer=audiobuffer,
        context_aggregator=context_aggregator,
    )

    # === TRANSFER: Register transfer tool handlers on LLM ===
    llm.register_function("transfer_to_human", transfer_handler.handle_transfer_to_human)

    # Bind task to end_call function
    end_call_with_task = partial(end_call, task=task)
    llm.register_function("end_call", end_call_with_task)

    # Handler for audio recording
    @audiobuffer.event_handler("on_audio_data")
    async def on_audio_data(buffer, audio, sample_rate, num_channels):
        if hasattr(context, "call_id") and context.call_id:
            call_id_str = str(context.call_id)
            audio_url = await save_audio_to_supabase(
                audio, call_id_str, sample_rate, num_channels
            )
            if audio_url:
                await store_audio_url_in_call(call_id_str, audio_url)

    # Transcript event handler
    processed_messages = set()

    @transcript.event_handler("on_transcript_update")
    async def on_transcript_update(processor, frame):
        if not frame.messages:
            return
        message = frame.messages[-1]
        message_id = f"{message.role}:{message.content}"
        if message_id in processed_messages:
            return
        processed_messages.add(message_id)

        role = "caller" if message.role == "user" else "agent"
        display_role = "Caller" if message.role == "user" else "Agent"
        logger.info(f"[Transcript] {display_role}: {message.content}")

        if phone_first_enabled and message.role == "user":
            _handle_carrier_identity_confirmation_from_transcript(
                context, message.content
            )

        if not hasattr(context, "transcript_messages"):
            context.transcript_messages = []
        context.transcript_messages.append(
            {
                "role": role,
                "content": message.content,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    # Gate call setup behind both `on_dialin_connected` and a participant join.
    # On slow links Daily can briefly create a SIP-transit participant that
    # joins and leaves before the actual phone call connects; without this
    # gate, on_client_disconnected would tear the pipeline down before the
    # caller's audio path is up.
    _dialin_connected = False
    _first_participant: dict | None = None
    _call_started = False

    async def _start_call(participant: dict) -> None:
        nonlocal _call_started
        if _call_started:
            return
        _call_started = True

        logger.info(f"Starting call for participant: {participant.get('id')}")
        context.session_id = participant.get("id")

        # === TRANSFER: Track carrier participant ID ===
        _orchestrator.state.carrier_participant_id = participant.get("id")
        _orchestrator.state.carrier_session_id = participant.get("sessionId")
        logger.info(f"Carrier tracked: id={participant.get('id')}, session={participant.get('sessionId')}")

        await audiobuffer.start_recording()

        async def create_call_async():
            try:
                result = start_call(
                    load_id=None,
                    daily_call_id=daily_call_id,
                    caller_number=caller_phone,
                    caller_country_code=None,
                    caller_mc=(
                        phone_verification.mc_number
                        if phone_verification.status == "known_carrier"
                        else None
                    ),
                    bot_phone=bot_phone,
                )
                if result:
                    context.call_id = result.call_id
                    context.org_id = (
                        result.org_id
                    )  # Store org_id for multi-tenant isolation
                    logger.info(
                        f"Created call record: {result.call_id} (org: {result.org_id})"
                    )
            except Exception as e:
                logger.error(f"Failed to create call record: {e}")

        context._call_task = asyncio.create_task(create_call_async())

        # Start the conversation - single entry point avoids "ringing state" errors
        await task.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_dialin_connected")
    async def on_dialin_connected(transport, data):
        nonlocal _dialin_connected
        logger.info(f"Dial-in connected: {data}")
        _dialin_connected = True
        if _first_participant is not None:
            await _start_call(_first_participant)

    @transport.event_handler("on_first_participant_joined")
    async def on_first_participant_joined(transport, participant):
        nonlocal _first_participant
        logger.info(f"Participant joined: {participant['id']}")
        _first_participant = participant
        if _dialin_connected:
            await _start_call(participant)
        else:
            logger.info("Deferring call start until dial-in is connected")

    @transport.event_handler("on_participant_joined")
    async def on_participant_joined(transport, participant):
        # Fallback: if a phantom set pipecat's internal _other_participant_has_joined
        # flag, on_first_participant_joined won't fire again for the real caller.
        nonlocal _first_participant
        if _call_started or not _dialin_connected:
            return
        if _first_participant is not None and _first_participant.get("id") == participant.get("id"):
            return
        logger.info(f"Real caller joined post-dialin: {participant['id']}")
        _first_participant = participant
        await _start_call(participant)

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        client_id = client.get("id") if isinstance(client, dict) else client
        if not _call_started:
            logger.info(
                f"Disconnect before call started; ignoring (likely SIP transit): id={client_id}"
            )
            return
        carrier_id = _orchestrator.state.carrier_participant_id
        if carrier_id and client_id and client_id != carrier_id:
            logger.info(f"Non-carrier disconnect; ignoring: id={client_id}")
            return
        logger.info("Client disconnected")
        # Stop recording first - this triggers on_audio_data
        try:
            await audiobuffer.stop_recording()
        except Exception as e:
            logger.exception(f"Failed to stop recording: {e}")
        await task.cancel()
        if room_config:
            await return_room_to_server(room_config)

    @transport.event_handler("on_dialin_error")
    async def on_dialin_error(transport, data):
        logger.error(f"Dial-in error: {data}")
        if room_config:
            await return_room_to_server(room_config)
        await task.cancel()

    runner = PipelineRunner(handle_sigint=handle_sigint)
    await runner.run(task)

    # Room1 pipeline has ended. Two possibilities:
    # 1. Normal disconnect (no transfer) → clean up and exit
    # 2. Transfer in progress → wait for transfer to complete

    # Check if a transfer is in progress
    if _orchestrator.is_transfer_in_progress():
        logger.info("Room1 pipeline ended due to transfer. Waiting for transfer to complete...")

        # Wait for the transfer to finish
        # The transfer_handler's asyncio.create_task is running the full
        # Phase 2 + Phase 3 flow. We need to wait for it.
        await _orchestrator.wait_for_transfer_complete()

        logger.info("Transfer flow complete.")
    else:
        logger.info("Room1 pipeline ended normally (no transfer).")

    # === Now safe to clean up ===
    # === TRANSFER: Clean up transfer HTTP session ===
    await transfer_http_session.close()

    # Save transcript after pipeline ends
    if hasattr(context, "call_id") and context.call_id:
        end_reason = getattr(context, "end_reason", "abrupt")
        transcription = getattr(context, "transcript_messages", None)
        call_summary = {"outcome": end_reason}
        
        try:
            universal_context = snapshot_universal_context(context)
            if universal_context is not None:
                call_summary["universal_context"] = universal_context
                call_summary["universal_context_captured_at"] = datetime.now(timezone.utc).isoformat()
        except Exception as e:
                logger.warning(f"Failed to snapshot universal context: {e}")
        finish_call(context.call_id, call_summary, end_reason, transcription)
        logger.info(
            f"Call saved: {context.call_id} with reason: {end_reason}, "
            f"{len(transcription or [])} messages"
        )


async def bot(runner_args: RunnerArguments):
    """Main bot entry point compatible with Pipecat Cloud.

    Parses the runner arguments, configures the Daily transport with dial-in
    settings, and starts the bot to handle the incoming call.

    Args:
        runner_args: Arguments from the Pipecat runner containing room details,
            call ID, and call domain for the inbound call

    Raises:
        Exception: If bot initialization or execution fails
    """

    try:
        request = AgentRequest.model_validate(runner_args.body)

        daily_dialin_settings = DailyDialinSettings(
            call_id=request.call_id, call_domain=request.call_domain
        )

        set_request(request)
        set_daily_dialin_settings(daily_dialin_settings)

        transport = DailyTransport(
            request.room_url,
            request.token,
            "Negotiation Agent",
            params=DailyParams(
                api_key=os.getenv("DAILY_API_KEY"),
                dialin_settings=daily_dialin_settings,
                audio_in_enabled=True,
                audio_in_passthrough=True,  # Keep audio flowing for Deepgram keepalive
                audio_out_enabled=True,
                audio_out_mixer=SoundfileMixer(
                    sound_files={
                        "office": str(
                            Path(__file__).parent
                            / "static"
                            / "audio"
                            / "office_noise.wav"
                        )
                    },
                    default_sound="office",
                    volume=0.1,
                    loop=True,
                ),
                vad_analyzer=SileroVADAnalyzer(
                    params=VADParams(
                        stop_secs=0.3,
                    )
                ),
            ),
        )

        import base64
        import json

        token_payload = request.token.split(".")[1]
        token_payload += "=" * (4 - len(token_payload) % 4)
        decoded = json.loads(base64.b64decode(token_payload))
        logger.info(f"Bot token payload: {decoded}")

        # set_transport(transport)

        await run_bot(
            transport,
            runner_args.handle_sigint,
            request.call_id,
            request.caller_phone,
            request.bot_phone,
            request.room_config,
            request.room_url,
        )

    except Exception as e:
        logger.error(f"Error running bot: {e}")
        raise e


# --- add this just after load_dotenv/initialize portions ---
def _build_runner_app():
    """
    Create the Pipecat runner FastAPI app so the container exposes /start and /health.

    Returns:
        FastAPI application instance configured for Daily transport.
    """
    port = int(os.getenv("PORT", "7860"))

    # Make sure the runner’s globals match what we’re about to use.
    runner_module.RUNNER_HOST = "0.0.0.0"
    runner_module.RUNNER_PORT = port

    app = runner_module._create_server_app(
        transport_type="daily",
        host=runner_module.RUNNER_HOST,
        proxy=None,
    )

    @app.get("/health")
    async def health_check():
        return {"status": "ok"}

    return app


app = _build_runner_app()

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=runner_module.RUNNER_HOST,
        port=runner_module.RUNNER_PORT,
    )
