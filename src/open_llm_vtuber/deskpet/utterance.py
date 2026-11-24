"""[deskpet] Glue sentences the speech detector cut in two.

People pause mid-sentence; the second half then interrupts the first before it was handled,
and the first half is lost. Here an unhandled or trailing-off sentence is joined to the next one.
"""

import time

_open = ("", 0.0)     # last sentence, until it has been handled
_pending = ("", 0.0)  # a sentence that trailed off ("还有你之前列举的那些……"), waiting for the rest


def begin(text: str) -> str:
    global _open, _pending
    now = time.time()
    merged = text
    if _pending[0] and now - _pending[1] < 20:
        merged = f"{_pending[0].rstrip('…. ')}，{text}"
    elif _open[0] and now - _open[1] < 15:
        merged = f"{_open[0]}，{text}"  # the previous one was cut off before it was handled
    _pending = ("", 0.0)
    _open = (merged, now)
    return merged


def unfinished(text: str) -> bool:
    t = text.strip()
    return len(t) < 40 and t.endswith(("……", "…", "..."))


def hold(text: str) -> None:
    global _pending, _open
    _pending = (text, time.time())
    _open = ("", 0.0)


def handled() -> None:
    global _open
    _open = ("", 0.0)
