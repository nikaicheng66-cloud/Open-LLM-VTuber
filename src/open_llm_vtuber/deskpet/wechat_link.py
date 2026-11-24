"""[deskpet] Runs the WeChat auto-reply worker while it is switched on (wechat_auto/state/enabled).

The worker is its own process (see wechat_auto/worker.py). It dies with the server: its stdin
closes, also when the server is killed. Its "SAY: " lines are spoken and shown by the pet.
"""

import asyncio
import os
import subprocess
import sys
import threading

from loguru import logger

from . import hub
from .config import PROJECT_ROOT

ROOT = PROJECT_ROOT / "wechat_auto"
ENABLED = ROOT / "state" / "enabled"
WORKER = ROOT / "worker.py"
POLL = 3.0
RESTART_AFTER = 20.0

_stop = threading.Event()
_proc: subprocess.Popen | None = None


def start(loop: asyncio.AbstractEventLoop) -> None:
    if sys.platform != "win32" or not WORKER.exists():
        return
    threading.Thread(target=_run, args=(loop,), name="deskpet-wechat", daemon=True).start()


def _run(loop: asyncio.AbstractEventLoop) -> None:
    global _proc
    while not _stop.wait(POLL):
        if not ENABLED.exists():
            continue
        try:
            env = dict(os.environ, PYTHONIOENCODING="utf-8", DESKPET_DELEGATED="1")
            with (ROOT / "state" / "worker.err").open("ab") as errors:
                _proc = subprocess.Popen(
                    [sys.executable, "-u", "-B", str(WORKER), "watch", "--child"],
                    cwd=str(ROOT), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            if _stop.is_set():
                stop()
                return
            logger.info("[deskpet] wechat worker started")
            for raw in _proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if line.startswith("SAY: "):
                    asyncio.run_coroutine_threadsafe(hub.say(line[5:]), loop)
            code = _proc.wait()
            logger.info(f"[deskpet] wechat worker ended ({code})")
        except Exception as e:
            logger.warning(f"[deskpet] wechat worker failed: {e}")
        if ENABLED.exists():  # it did not end because it was switched off
            _stop.wait(RESTART_AFTER)


def stop() -> None:
    _stop.set()
    proc = _proc
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.stdin.close()  # watcher and job end by themselves
        proc.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
