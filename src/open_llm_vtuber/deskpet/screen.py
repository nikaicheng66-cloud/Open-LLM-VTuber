"""[deskpet] Cheap 'eyes': a timeline of the active window, plus a screenshot on demand.

Nothing is sent anywhere until the user asks about the screen; then a fresh screenshot and the
recent timeline go to her own model (brain.look_at_screen), or, on the local backup path, to
Claude Code to answer.
"""

import asyncio
import ctypes
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

from loguru import logger

from .config import SCREEN_DIR

timeline: deque = deque(maxlen=720)  # (time, process, title); ~1h at one change per 5s
_started = False


def _foreground() -> tuple[str, str]:
    """(process name, window title) of the focused window (Windows only)."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    hwnd = user32.GetForegroundWindow()
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    pid = ctypes.c_ulong()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = ""
    h = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        size = ctypes.c_ulong(260)
        pbuf = ctypes.create_unicode_buffer(260)
        if kernel32.QueryFullProcessImageNameW(h, 0, pbuf, ctypes.byref(size)):
            name = pbuf.value.rsplit("\\", 1)[-1]
        kernel32.CloseHandle(h)
    return name, buf.value


def _watch() -> None:
    last = None
    while True:
        try:
            cur = _foreground()
            if cur != last and (cur[0] or cur[1]):
                timeline.append((time.time(), cur[0], cur[1]))
                last = cur
        except Exception:
            pass
        time.sleep(5)


def start() -> None:
    global _started
    if _started or sys.platform != "win32":
        return
    _started = True
    threading.Thread(target=_watch, daemon=True, name="deskpet-screen").start()
    logger.info("[deskpet] screen timeline started")


def recent_text(minutes: int = 30) -> str:
    cutoff = time.time() - minutes * 60
    rows = [
        f"{datetime.fromtimestamp(t):%H:%M} {proc} | {title}"
        for t, proc, title in timeline
        if t >= cutoff and "open-llm-vtuber" not in proc.lower()
    ]
    return "\n".join(rows[-40:]) or "（没有记录）"


_PS_SHOT = r"""
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$b = [System.Windows.Forms.SystemInformation]::VirtualScreen
$bmp = New-Object System.Drawing.Bitmap $b.Width, $b.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($b.Left, $b.Top, 0, 0, $bmp.Size)
$bmp.Save('{path}', [System.Drawing.Imaging.ImageFormat]::Png)
$g.Dispose(); $bmp.Dispose()
"""


MODEL_SIDE = 1920  # longer side, in pixels, of the smaller picture her own model is given


def _small_shot(path: str) -> bool:
    """A screenshot of all screens, shrunk and saved as JPEG: small enough to send to her (cloud)
    model quickly, still sharp enough to read. (Done with Pillow: a PowerShell script that grabs and
    re-encodes the screen is stopped by the antivirus, and that is not something to work around.)"""
    from PIL import ImageGrab

    shot = ImageGrab.grab(all_screens=True)
    k = min(1.0, MODEL_SIDE / max(shot.size))
    if k < 1.0:
        shot = shot.resize((int(shot.width * k), int(shot.height * k)))
    shot.convert("RGB").save(path, "JPEG", quality=88)
    return True


async def screenshot(for_model: bool = False) -> str | None:
    """Save a full-screen picture and return its path (PNG at full size; `for_model`: a smaller JPEG)."""
    if sys.platform != "win32":
        return None
    SCREEN_DIR.mkdir(parents=True, exist_ok=True)
    for old in sorted(SCREEN_DIR.glob("screen_*"))[:-5]:  # keep only the last few
        old.unlink(missing_ok=True)
    path = SCREEN_DIR / f"screen_{datetime.now():%Y%m%d_%H%M%S}.{'jpg' if for_model else 'png'}"
    if for_model:
        try:
            await asyncio.to_thread(_small_shot, str(path))
        except Exception as e:
            logger.warning(f"[deskpet] screenshot failed: {e!r}")
        return str(path) if path.exists() else None
    script = _PS_SHOT.replace("{path}", str(path))
    proc = await asyncio.create_subprocess_exec(
        "powershell", "-NoProfile", "-NonInteractive", "-Command", script,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    await proc.wait()
    return str(path) if path.exists() else None
