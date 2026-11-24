"""[deskpet] Lets background jobs make the pet speak on every connected client."""

import json
from datetime import datetime
from uuid import uuid4

from loguru import logger

from ..agent.output_types import DisplayText
from ..utils.stream_audio import prepare_audio_payload

ws_handler = None  # set by routes.init_client_ws_route


remote_texts: set[str] = set()  # texts sent from the phone app, waiting to be processed


async def inject_text(text: str) -> bool:
    """Handle `text` as if it had been typed into the pet on the PC (used by the phone app)."""
    if ws_handler is None or not ws_handler.client_connections:
        return False
    uid, ws = next(iter(ws_handler.client_connections.items()))
    remote_texts.add(text)
    await ws_handler._handle_conversation_trigger(ws, uid, {"type": "text-input", "text": text, "images": []})
    return True


def note_said(text: str) -> None:
    """Put something she said in the background into her chat memory, so that she knows it when
    the user answers it ("这几家都投吧" after a report). Only for her own reports: never for what
    was heard in a recording or a video, which is material and not her words."""
    try:
        agent = ws_handler.default_context_cache.agent_engine
        agent._add_message(text, "assistant")
    except Exception:
        pass


async def say(text: str, remember: bool = False) -> None:
    """Speak `text` (with subtitles) on every connected client."""
    from . import state
    state.log_feed("小克", text)
    if remember and text:
        note_said(text)
    if not text or ws_handler is None:
        logger.info(f"[deskpet] (no client) would say: {text}")
        return
    for uid, ws in list(ws_handler.client_connections.items()):
        ctx = ws_handler.client_contexts.get(uid)
        if ctx is None:
            continue
        audio_path = None
        try:
            audio_path = await ctx.tts_engine.async_generate_audio(
                text=text,
                file_name_no_ext=f"deskpet_{datetime.now():%Y%m%d_%H%M%S}_{uuid4().hex[:6]}",
            )
            payload = prepare_audio_payload(
                audio_path=audio_path,
                display_text=DisplayText(
                    text=text,
                    name=ctx.character_config.character_name,
                    avatar=ctx.character_config.avatar,
                ),
            )
            await ws.send_text(json.dumps(payload))
        except Exception as e:
            logger.error(f"[deskpet] say failed: {e}")
        finally:
            if audio_path:
                try:
                    ctx.tts_engine.remove_file(audio_path)
                except Exception:
                    pass


async def send_note(text: str, final: bool = False) -> None:
    """The small bubble for a dropped recording (seen also when her voice can't be heard)."""
    if ws_handler is None:
        return
    for ws in list(ws_handler.client_connections.values()):
        try:
            await ws.send_text(json.dumps({"type": "audio-note", "text": text, "final": final}))
        except Exception as e:
            logger.error(f"[deskpet] send_note failed: {e}")


async def send_action(cmds: list) -> None:
    """Move the pet on every connected client (same as the user asking directly)."""
    if not cmds or ws_handler is None:
        return
    for ws in list(ws_handler.client_connections.values()):
        try:
            await ws.send_text(json.dumps({"type": "pet-action", "motions": cmds}))
        except Exception as e:
            logger.error(f"[deskpet] send_action failed: {e}")
