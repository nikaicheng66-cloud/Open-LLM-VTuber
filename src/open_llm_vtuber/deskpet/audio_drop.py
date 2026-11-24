"""[deskpet] Recordings dropped on the pet or her chat box: queue them, run audio_worker.py, tell the user.

The Electron main process posts the paths. Web pages must not be able to make her read files, so
this endpoint is stricter than origin_guard: no Origin at all (a browser always sends one with a
POST), a header of its own, and loopback only.

While the WeChat auto-reply is on, results are only spoken and opened with the user at the PC: its
job records the speakers while the user is away and really sends a summary of what it heard.

What is said in a recording is untrusted: it never goes into state.remember / state.tasks, which
feed the agent that can run commands.
"""

import asyncio
import ctypes
import json
import os
import re
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from loguru import logger

from . import hub, quiet, state
from .config import PROJECT_ROOT

router = APIRouter()

WORKER = Path(__file__).with_name("audio_worker.py")
MODELS = PROJECT_ROOT / "Open-LLM-VTuber" / "models" / "whisper"
NOTES = PROJECT_ROOT / "audio_notes"
EXTENSIONS = set(
    ".mp3 .wav .m4a .m4b .aac .ogg .oga .opus .flac .wma .amr .aif .aiff .caf .3gp .mp4 .mov .webm .mka".split()
)
MAX_WAITING = 10
WECHAT_ON = PROJECT_ROOT / "wechat_auto" / "state" / "enabled"
PRESENT = 15.0  # idle less than this = at the PC. A WeChat job needs 60 s idle, so she can finish talking.
HOLD_POLL = 2.0
# no network shares (they leak the Windows login), no streams
_LOCAL_PATH = re.compile(r"^[A-Za-z]:[\\/][^:]*$")
_LOCAL_HOST = re.compile(r"^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$", re.I)
_CANCEL = re.compile(
    r"^(取消|停止|停下|别|不要|不用)(再)?(总结|转写|听)?(这个|那个|这段|那段)?(录音|音频)(的?(总结|转写))?[了吧啊。！!]*$"
    r"|^(取消|停止|别|不用)(再)?(总结|转写)[了吧啊。！!]*$"
)

_waiting: deque[str] = deque()
_current: str | None = None
_proc: asyncio.subprocess.Process | None = None
_runner: asyncio.Task | None = None
_background: set[asyncio.Task] = set()
_telling = asyncio.Lock()  # one after the other, in order
_cancelled = False


def _later(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def check(path) -> str | None:
    """Why this can't be taken, or None. Nothing is opened before the path is known to be local."""
    if not isinstance(path, str) or not _LOCAL_PATH.fullmatch(path):
        return "只能收电脑本地的文件，先存到电脑上再拖给我。"
    if os.path.splitext(path)[1].lower() not in EXTENSIONS:
        return "我只听得了录音和音频文件。"
    if not os.path.isfile(path):
        return "找不到这个文件。"
    return None


@router.post("/deskpet/audio")
async def drop(request: Request):
    h = request.headers
    if (
        "origin" in h
        or h.get("x-deskpet-drop") != "1"
        or not _LOCAL_HOST.match(h.get("host", ""))
        or (request.client and request.client.host not in ("127.0.0.1", "::1"))
    ):
        return JSONResponse({"ok": False, "message": "forbidden"}, status_code=403)
    try:
        paths = (await request.json())["paths"]
        assert isinstance(paths, list) and paths
    except Exception:
        return JSONResponse({"ok": False, "message": "没收到文件。"}, status_code=400)
    return add(paths)


def add(paths: list) -> dict:
    global _runner
    taken, refused = 0, ""
    for path in paths:
        why = check(path)
        if not why and (path == _current or path in _waiting):
            why = "这个已经在听了。"
        if not why and len(_waiting) >= MAX_WAITING:
            why = "排队的太多了，等我听完几个再拖。"
        if why:
            refused = refused or why
            continue
        _waiting.append(path)
        taken += 1
    if not taken:
        return {"ok": False, "message": refused}
    _describe()
    if _runner is None or _runner.done():
        _runner = asyncio.create_task(_run_all())
        message = "收到，我听一下，听完告诉你。"
    else:
        message = "收到，排在后面了。"
    if refused:
        message += f"有 {len(paths) - taken} 个没收：{refused}"
    _later(_tell(message))
    return {"ok": True, "message": message}


def _describe(step: str = "") -> None:
    if _current is None and not _waiting:
        state.audio_now = ""
        return
    more = f"，后面还排着{len(_waiting)}个" if _waiting and _current else ""
    state.audio_now = f"在听你拖给我的录音{'，' + step if step else ''}{more}。"


def _step(event: dict) -> str:
    stage = event.get("stage")
    if stage == "transcribe":
        done = event.get("percent")
        return (
            f"转文字 {done}%" if done is not None else f"转文字，到 {event.get('at')}"
        )
    if stage == "summarise":
        return (
            f"总结第 {event['part']}/{event['parts']} 段"
            if event.get("part")
            else "正在写总结"
        )
    return "准备中"


def folder_for(path: str) -> Path:
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", Path(path).stem)[:50].rstrip(" .")
    base = NOTES / f"{time.strftime('%Y%m%d-%H%M%S')}_{name}"
    folder, n = base, 1
    while folder.exists():
        n += 1
        folder = base.with_name(f"{base.name}_{n}")
    return folder


async def _run_all() -> None:
    global _current
    while _waiting:
        _current = _waiting.popleft()
        try:
            await _run_one(_current)
        except Exception as e:
            logger.warning(f"[deskpet] audio job failed: {e!r}")
            await hub.send_note("处理录音时出错了。", final=True)
        finally:
            _current = None
            _describe()


async def _run_one(path: str) -> None:
    global _proc, _cancelled
    _cancelled = False
    name, folder = Path(path).name, folder_for(path)
    _describe("准备中")
    await hub.send_note(f"{name}\n准备中…")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0
    )
    _proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-B",
        str(WORKER),
        path,
        str(folder),
        str(MODELS),
        cwd=str(WORKER.parent),
        creationflags=flags,
        limit=1 << 20,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    proc, result = _proc, None
    try:
        if _cancelled:  # called off while it was starting
            _kill(proc)
        async for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").strip()
            try:
                event = json.loads(line)
                kind = event.get("type")
            except (ValueError, AttributeError):
                if line:
                    logger.info(f"[deskpet] audio worker: {line[:300]}")
                continue
            if kind == "progress":
                step = _step(event)
                _describe(step)
                await hub.send_note(f"{name}\n{step}")
            elif kind in ("done", "error"):
                result = event
        await proc.wait()
    finally:
        if proc.returncode is None:
            _kill(proc)
        _proc = None
    if _cancelled:
        await hub.send_note("录音不听了。", final=True)
        return
    text = (result or {}).get("say") or "听录音的时候出错了，没做完。"
    await hub.send_note(f"{name}\n{text}", final=True)
    _later(_tell(text, (result or {}).get("open"), name))


def _idle_seconds() -> float:
    class LastInput(ctypes.Structure):
        _fields_ = [("size", ctypes.c_uint), ("time", ctypes.c_uint)]

    info = LastInput(ctypes.sizeof(LastInput), 0)
    if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
        return float("inf")  # unknown = don't speak
    now = ctypes.windll.kernel32.GetTickCount()
    return ((now - info.time) & 0xFFFFFFFF) / 1000


async def _may_tell() -> bool:
    if not WECHAT_ON.exists():
        return True
    # a job's own clicks reset the idle time, so that alone can't say the user is back
    return _idle_seconds() < PRESENT and not quiet.job_running()


async def _tell(text: str, path: str | None = None, name: str = "") -> None:
    async with _telling:
        held = False
        while not await _may_tell():
            held = True
            await asyncio.sleep(HOLD_POLL)
        if held and name:  # the bubble is long gone
            await hub.send_note(f"{name}\n{text}", final=True)
        await _show_and_say(text, path)


async def _show_and_say(text: str, path: str | None) -> None:
    if path:
        try:
            # like the long answers of her other tasks: open it to be read
            os.startfile(path)
        except Exception as e:
            logger.warning(f"[deskpet] could not open the notes: {e!r}")
    await hub.say(text)


def _kill(proc) -> None:
    try:  # the whole tree: the venv's python is a launcher, and claude runs under it
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"[deskpet] could not stop the audio worker: {e!r}")


def cancel_request(text: str) -> str | None:
    """What she answers if `text` calls the recordings off, else None."""
    if (_current is None and not _waiting) or not _CANCEL.match(text.strip()):
        return None
    stop()
    return "好，录音不听了。"


def stop() -> None:
    global _cancelled
    _waiting.clear()
    _cancelled = True
    if _proc is not None and _proc.returncode is None:
        _kill(_proc)
