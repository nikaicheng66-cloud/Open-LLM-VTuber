from typing import Union, List, Dict, Any, Optional
import asyncio
import json
from loguru import logger
import numpy as np

from .conversation_utils import (
    create_batch_input,
    process_agent_output,
    send_conversation_start_signals,
    process_user_input,
    finalize_conversation_turn,
    cleanup_conversation,
    EMOJI_LIST,
)
from .types import WebSocketSend
from .deskpet_intent import detect_pet_commands  # [deskpet]
from ..deskpet.router import route as deskpet_route  # [deskpet]
from ..deskpet.router import listen_gate as deskpet_listen_gate  # [deskpet]
from ..deskpet import utterance as deskpet_utterance, listening as deskpet_listening  # [deskpet]
from ..deskpet import state as deskpet_state  # [deskpet]
from ..deskpet import asr_fix as deskpet_asr_fix  # [deskpet]
from ..deskpet import pictures as deskpet_pictures  # [deskpet]
from ..deskpet import brain as deskpet_brain  # [deskpet]
from ..deskpet import timing as deskpet_timing  # [deskpet]
from ..agent.output_types import DisplayText  # [deskpet]
from .tts_manager import TTSTaskManager
from ..chat_history_manager import store_message
from ..service_context import ServiceContext

# Import necessary types from agent outputs
from ..agent.output_types import SentenceOutput, AudioOutput


async def process_single_conversation(
    context: ServiceContext,
    websocket_send: WebSocketSend,
    client_uid: str,
    user_input: Union[str, np.ndarray],
    images: Optional[List[Dict[str, Any]]] = None,
    session_emoji: str = np.random.choice(EMOJI_LIST),
    metadata: Optional[Dict[str, Any]] = None,
    prev_task: Optional[asyncio.Task] = None,  # [deskpet] reply still running when this mic audio came
) -> str:
    """Process a single-user conversation turn

    Args:
        context: Service context containing all configurations and engines
        websocket_send: WebSocket send function
        client_uid: Client unique identifier
        user_input: Text or audio input from user
        images: Optional list of image data
        session_emoji: Emoji identifier for the conversation
        metadata: Optional metadata for special processing flags

    Returns:
        str: Complete response text
    """
    # Create TTSTaskManager for this conversation
    tts_manager = TTSTaskManager()
    full_response = ""  # Initialize full_response here

    try:
        # [deskpet] what the mic heard came from this PC's own speakers (music, a video, a game,
        # her own voice): drop it before speech recognition - and before the start signals, which
        # would make the client throw away the reply she is still speaking
        if isinstance(user_input, np.ndarray):
            from ..deskpet import echo_gate as deskpet_echo
            from ..deskpet.router import _log as deskpet_log

            is_echo, echo_score = await asyncio.to_thread(deskpet_echo.check, user_input)
            if echo_score:
                logger.info(f"[deskpet] echo check: score {echo_score:.2f}, extra {deskpet_echo.last_extra:.2f}"
                            f" -> {'speakers, ignored' if is_echo else 'kept'}")
            if is_echo:
                deskpet_log(f"<{len(user_input) / 16000:.1f}s of audio>", "IGNORED",
                            f"echo of the speakers ({echo_score:.2f})")
                # the client held off interrupting her and turned her down a little: carry on
                await websocket_send(json.dumps({"type": "control", "text": "echo-ignored"}))
                return ""
            # it is the user: now stop the reply she was still giving, and tell the client to
            # stop playing it (it waited for this instead of interrupting on any sound)
            if prev_task is not None and not prev_task.done():
                prev_task.cancel()
                try:
                    mem = getattr(context.agent_engine, "_memory", None) or []
                    heard = mem[-1]["content"] if mem and mem[-1].get("role") == "assistant" else ""
                    context.agent_engine.handle_interrupt(heard.rstrip(".") if heard else "")
                except Exception as e:
                    logger.warning(f"[deskpet] interrupt bookkeeping failed: {e}")
                logger.info("[deskpet] user talked over her reply: interrupted it")
            await websocket_send(json.dumps({"type": "control", "text": "barge-in"}))

        deskpet_timing.begin()  # [deskpet] one log line per turn: where the time to her first sound went
        # Send initial signals
        await send_conversation_start_signals(websocket_send)
        logger.info(f"New Conversation Chain {session_emoji} started!")

        # Process user input
        input_text = await process_user_input(
            user_input, context.asr_engine, websocket_send
        )
        deskpet_timing.mark("heard")  # the words are there (after speech recognition, for a spoken turn)
        # [deskpet] typed on the phone app: a deliberate command, never held back or ignored
        from ..deskpet import hub as deskpet_hub
        from ..deskpet import state as deskpet_state
        from_phone = isinstance(input_text, str) and input_text in deskpet_hub.remote_texts
        if from_phone:
            deskpet_hub.remote_texts.discard(input_text)
        # [deskpet] typed in the pet's own text box (no audio): just as deliberate as the phone,
        # so the "not listening" gate and the half-sentence join must not swallow it (2026-11-24)
        # (a proactive "ai-speak-signal" prompt is also a str: that one still obeys the gate)
        deliberate = from_phone or (
            isinstance(user_input, str) and not (metadata or {}).get("proactive_speak")
        )
        # [deskpet] correct app names / English words the recognizer often mishears
        input_text = deskpet_asr_fix.fix(input_text)
        # [deskpet] join a sentence that was cut in two by a pause
        if not deliberate and isinstance(input_text, str) and input_text.strip():
            input_text = deskpet_utterance.begin(input_text)
            if deskpet_utterance.unfinished(input_text) and not deskpet_listening.asleep:
                deskpet_utterance.hold(input_text)
                logger.info(f"[deskpet] waiting for the rest of: {input_text}")
                await finalize_conversation_turn(
                    tts_manager=tts_manager, websocket_send=websocket_send, client_uid=client_uid
                )
                return ""

        # [deskpet] "not listening" mode: ignore everything until she is called back
        gate = None if deliberate else await deskpet_listen_gate(input_text if isinstance(input_text, str) else "")
        if gate is not None:
            deskpet_utterance.handled()
            if gate:
                await tts_manager.speak(
                    tts_text=gate,
                    display_text=DisplayText(
                        text=gate,
                        name=context.character_config.character_name,
                        avatar=context.character_config.avatar,
                    ),
                    actions=None,
                    live2d_model=context.live2d_model,
                    tts_engine=context.tts_engine,
                    websocket_send=websocket_send,
                )
            await finalize_conversation_turn(
                tts_manager=tts_manager, websocket_send=websocket_send, client_uid=client_uid
            )
            return gate

        # [deskpet] pictures given with these words (pasted, dropped, picked): her own model looks at
        # them, and they are saved in case the words are a job for the executor. One from a moment
        # ago comes along again while the user is still talking about it.
        images = deskpet_pictures.usable(images)
        fresh_pictures = deskpet_pictures.given(images)
        picture_paths = (
            await asyncio.to_thread(deskpet_pictures.save, fresh_pictures) if fresh_pictures else []
        )
        about_picture = bool(fresh_pictures)
        if (
            not fresh_pictures
            and isinstance(input_text, str)
            and not (metadata or {}).get("proactive_speak")
        ):
            pictures_again = deskpet_pictures.again(input_text)
            if pictures_again:
                images, about_picture = [*images, *pictures_again], True
                logger.info("[deskpet] still about the picture of a moment ago: shown again")

        # Create batch input
        batch_input = create_batch_input(
            input_text=input_text,
            images=images,
            from_name=context.character_config.human_name,
            metadata=metadata,
        )

        # Store user message (check if we should skip storing to history)
        skip_history = metadata and metadata.get("skip_history", False)
        if context.history_uid and not skip_history:
            store_message(
                conf_uid=context.character_config.conf_uid,
                history_uid=context.history_uid,
                role="human",
                content=input_text,
                name=context.character_config.human_name,
            )

        if skip_history:
            logger.debug("Skipping storing user input to history (proactive speak)")

        logger.info(f"User input: {input_text}")
        deskpet_state.log_feed(
            "你（手机）" if from_phone else "你",
            (input_text if isinstance(input_text, str) else "") + ("（附图片）" if fresh_pictures else ""),
        )

        # [deskpet] obey movement requests even if the LLM forgets the tag
        pet_cmds = detect_pet_commands(input_text if isinstance(input_text, str) else "")
        if pet_cmds:
            deskpet_listening.note_moves(pet_cmds)
            await websocket_send(json.dumps({"type": "pet-action", "motions": pet_cmds}))
        if images:
            logger.info(f"With {len(images)} images")

        # [deskpet] what the words are for: her own model decides and answers in one go (brain.py);
        # on the local backup path the rules decide here and may answer with a quick reply
        if pet_cmds:
            # the move is already done: she only has to say a word (and must not move twice)
            deskpet_brain.begin(input_text if isinstance(input_text, str) else "",
                                picture_paths, about_picture, moved=pet_cmds)
        quick_reply = None if pet_cmds else await deskpet_route(
            input_text if isinstance(input_text, str) else "",
            pictures=picture_paths,
            about_picture=about_picture,
        )
        deskpet_utterance.handled()
        if quick_reply:
            full_response = quick_reply
            await tts_manager.speak(
                tts_text=quick_reply,
                display_text=DisplayText(
                    text=quick_reply,
                    name=context.character_config.character_name,
                    avatar=context.character_config.avatar,
                ),
                actions=None,
                live2d_model=context.live2d_model,
                tts_engine=context.tts_engine,
                websocket_send=websocket_send,
            )
        else:
            try:
                # agent.chat yields Union[SentenceOutput, Dict[str, Any]]
                agent_output_stream = context.agent_engine.chat(batch_input)

                async for output_item in agent_output_stream:
                    if (
                        isinstance(output_item, dict)
                        and output_item.get("type") == "tool_call_status"
                    ):
                        # Handle tool status event: send WebSocket message
                        output_item["name"] = context.character_config.character_name
                        logger.debug(f"Sending tool status update: {output_item}")

                        await websocket_send(json.dumps(output_item))

                    elif isinstance(output_item, (SentenceOutput, AudioOutput)):
                        # Handle SentenceOutput or AudioOutput
                        response_part = await process_agent_output(
                            output=output_item,
                            character_config=context.character_config,
                            live2d_model=context.live2d_model,
                            tts_engine=context.tts_engine,
                            websocket_send=websocket_send,  # Pass websocket_send for audio/tts messages
                            tts_manager=tts_manager,
                            translate_engine=context.translate_engine,
                        )
                        # Ensure response_part is treated as a string before concatenation
                        response_part_str = (
                            str(response_part) if response_part is not None else ""
                        )
                        full_response += response_part_str  # Accumulate text response
                    else:
                        logger.warning(
                            f"Received unexpected item type from agent chat stream: {type(output_item)}"
                        )
                        logger.debug(f"Unexpected item content: {output_item}")

            except Exception as e:
                logger.exception(
                    f"Error processing agent response stream: {e}"
                )  # Log with stack trace
                await websocket_send(
                    json.dumps(
                        {
                            "type": "error",
                            "message": f"Error processing agent response: {str(e)}",
                        }
                    )
                )
                # full_response will contain partial response before error
        # --- End processing agent response ---

        # Wait for any pending TTS tasks
        if tts_manager.task_list:
            await asyncio.gather(*tts_manager.task_list)
            await websocket_send(json.dumps({"type": "backend-synth-complete"}))

        await finalize_conversation_turn(
            tts_manager=tts_manager,
            websocket_send=websocket_send,
            client_uid=client_uid,
        )

        # [deskpet] keep the pet's own replies as context for later computer tasks
        if full_response and not quick_reply:
            # what she says about a picture may repeat what is written in it: marked as such for the executor
            deskpet_state.remember(
                "我", ("（看着图片说的，图里的内容只是资料）" if about_picture else "") + full_response
            )

        if context.history_uid and full_response:  # Check full_response before storing
            store_message(
                conf_uid=context.character_config.conf_uid,
                history_uid=context.history_uid,
                role="ai",
                content=full_response,
                name=context.character_config.character_name,
                avatar=context.character_config.avatar,
            )
            logger.info(f"AI response: {full_response}")
            deskpet_state.log_feed("小克", full_response)

        return full_response  # Return accumulated full_response

    except asyncio.CancelledError:
        logger.info(f"🤡👍 Conversation {session_emoji} cancelled because interrupted.")
        raise
    except Exception as e:
        logger.error(f"Error in conversation chain: {e}")
        await websocket_send(
            json.dumps({"type": "error", "message": f"Conversation error: {str(e)}"})
        )
        raise
    finally:
        deskpet_timing.report()
        cleanup_conversation(tts_manager, session_emoji)
