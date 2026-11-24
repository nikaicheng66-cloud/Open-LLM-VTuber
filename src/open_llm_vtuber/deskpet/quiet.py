"""[deskpet] She makes no sound while a WeChat auto-reply job runs.

That job records the speakers and really sends a summary of what it heard, so whatever she said
meanwhile could reach the contact. Every sound she makes is an "audio" message on a client's
websocket (replies, hub.say, proactive speech), so that is the one place it is stopped: the words
are shown at once, the voice is kept and said when the job is over. Nothing waits for it, so
conversations and tasks carry on.

Sound that was already playing when a job starts is the job's own check (worker.QUIET_FOR).
"""

import asyncio
import ctypes
import json
from collections import deque
from ctypes import wintypes

from loguru import logger

JOB_MUTEX = "deskpet-wechat-auto-job"  # worker.job() holds it; Windows frees it however the job ends
MAX_KEPT = 20  # sentences; older ones have been shown as text
POLL = 1.0

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_kernel32.OpenMutexW.restype = wintypes.HANDLE
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_SYNCHRONIZE = 0x00100000
_NOT_THERE = 2


def job_running() -> bool:
    handle = _kernel32.OpenMutexW(_SYNCHRONIZE, False, JOB_MUTEX)
    if handle:
        _kernel32.CloseHandle(handle)
        return True
    return ctypes.get_last_error() != _NOT_THERE  # can't tell = quiet


class Gate:
    """In front of one client's send_text."""

    def __init__(self, send):
        self._raw = send
        self._writing = asyncio.Lock()
        self._kept: deque[dict] = deque(maxlen=MAX_KEPT)
        self._replay: asyncio.Task | None = None

    async def _send(self, data: str) -> None:
        """One message at a time. The voice sender, the end-of-turn signals and background speech
        all write to this socket; two writes at once, while a big voice message is still going
        out, raise an AssertionError inside the websocket and the turn dies with "Conversation
        error" (seen 2026-11-24 with short, fast answers)."""
        async with self._writing:
            await self._raw(data)

    async def send_text(self, data: str) -> None:
        message = json.loads(data) if '"audio"' in data else None
        if not message or message.get("type") != "audio" or not message.get("audio"):
            await self._send(data)
            return
        replaying = self._replay is not None and not self._replay.done()
        if not replaying and not job_running():
            await self._send(data)
            return
        # the voice alone: its words and moves go out now, in the silent copy
        self._kept.append(
            {**message, "display_text": None, "actions": None, "forwarded": True}
        )
        if not replaying:
            self._replay = asyncio.create_task(self._say_later())
            logger.info(
                "[deskpet] a WeChat job is running: keeping her voice for later"
            )
        await self._send(json.dumps({**message, "audio": None, "volumes": []}))

    async def _say_later(self) -> None:
        try:
            while self._kept:
                if job_running():
                    await asyncio.sleep(POLL)
                    continue
                piece = self._kept.popleft()
                await self._send(json.dumps(piece))
                # one at a time: a job may start in between, and then the rest must not be at the client
                length = len(piece.get("volumes") or []) * piece.get("slice_length", 20)
                await asyncio.sleep(length / 1000)
        except Exception as e:  # the client is gone
            self._kept.clear()
            logger.info(f"[deskpet] could not say what was kept: {e!r}")

    def drop(self) -> None:
        self._kept.clear()


def guard(websocket) -> None:
    gate = Gate(websocket.send_text)
    websocket.send_text = gate.send_text
    websocket.deskpet_gate = gate


def drop(websocket) -> None:
    """He interrupted her: what was kept isn't said any more."""
    gate = getattr(websocket, "deskpet_gate", None)
    if gate:
        gate.drop()
