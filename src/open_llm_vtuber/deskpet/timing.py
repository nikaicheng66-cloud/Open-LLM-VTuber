"""[deskpet] Where the time of one turn goes: one log line per turn.

    [deskpet] turn: heard 0.4s | text 1.2s | said 1.3s | voice 1.9s (synthesis 0.6s) | sent 1.9s

Seconds since the turn began (for a spoken turn: since the audio arrived):
    heard  the words are there (speech recognition done)
    text   first piece of text from the chat model
    said   first sentence handed to the voice
    voice  that sentence synthesized
    sent   first sound on its way to the client
A step that did not happen in a turn (a fixed quick reply has no "text") is left out.
The marks travel with the turn's own tasks (a context variable), so two clients don't mix.
"""

import contextvars
import time

from loguru import logger

_ORDER = ("heard", "text", "said", "voice", "sent")
_turn: contextvars.ContextVar = contextvars.ContextVar("deskpet_turn_timing", default=None)


def begin() -> None:
    _turn.set({"t0": time.perf_counter(), "marks": {}, "extra": {}})


def mark(name: str, **extra: float) -> None:
    """Note the first time `name` happens in this turn."""
    turn = _turn.get()
    if turn is None or name in turn["marks"]:
        return
    turn["marks"][name] = time.perf_counter() - turn["t0"]
    turn["extra"].update({f"{name}:{k}": v for k, v in extra.items()})


def marks() -> dict:
    turn = _turn.get()
    return dict(turn["marks"]) if turn else {}


def report() -> None:
    turn = _turn.get()
    if turn is None or not turn["marks"].get("said"):
        return
    parts = []
    for name in _ORDER:
        if name in turn["marks"]:
            part = f"{name} {turn['marks'][name]:.1f}s"
            if name == "voice" and "voice:synthesis" in turn["extra"]:
                part += f" (synthesis {turn['extra']['voice:synthesis']:.1f}s)"
            parts.append(part)
    logger.info("[deskpet] turn: " + " | ".join(parts))
    _turn.set(None)
