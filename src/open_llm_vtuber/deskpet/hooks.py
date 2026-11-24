"""[deskpet] HTTP endpoints: Claude Code hooks post here so the pet can tell the user."""

import json
import os

from fastapi import APIRouter, Request
from loguru import logger

from . import hub, intent_llm, state
from .config import AGENT_DIR, project_name_for

router = APIRouter()


def _last_assistant_text(transcript_path: str) -> str:
    try:
        with open(transcript_path, encoding="utf-8") as f:
            lines = f.readlines()[-50:]
        for line in reversed(lines):
            d = json.loads(line)
            msg = d.get("message") or {}
            if d.get("type") == "assistant" and isinstance(msg.get("content"), list):
                texts = [c.get("text", "") for c in msg["content"] if c.get("type") == "text"]
                if any(texts):
                    return " ".join(texts).strip()
    except Exception:
        pass
    return ""


@router.post("/deskpet/hook")
async def claude_hook(request: Request):
    try:
        data = await request.json()
    except Exception:
        return {"ok": False}
    cwd = data.get("cwd", "")
    # delegated runs report back themselves
    if os.path.normcase(os.path.abspath(cwd or ".")).startswith(os.path.normcase(str(AGENT_DIR))):
        return {"ok": True, "skipped": "delegated"}
    # ...and when it moved into another project folder: its transcript stays under the agent folder's name
    tp = (data.get("transcript_path") or "").replace("\\", "/").lower()
    if "claude-desktop-pet-agent" in tp or any(t.session_id and t.session_id == data.get("session_id") for t in state.tasks):
        return {"ok": True, "skipped": "delegated"}

    event = data.get("hook_event_name", "")
    sid = data.get("session_id", "")
    project = project_name_for(cwd) or (os.path.basename(cwd.rstrip("\\/")) if cwd else None)
    s = state.sessions.get(sid) or state.Session(session_id=sid, cwd=cwd, project=project)
    state.sessions[sid] = s
    s.last_event = event
    s.updated = state.time.time()
    logger.info(f"[deskpet] hook {event} from {project}: {data.get('message', '')}")

    name = project or "另一个窗口"
    busy = any(t.status == "running" or (t.finished and state.time.time() - t.finished < 30)
               for t in state.tasks[-5:])
    if event == "Notification":
        msg = data.get("message", "")
        s.message = msg
        s.waiting = True
        if "permission" in msg.lower() or "权限" in msg:
            await hub.say(f"{name}那边要你确认一下。要我帮你点，就说「确认」。", remember=True)
        elif not busy:
            await hub.say(f"{name}那边在等你回话。", remember=True)
    elif event == "Stop":
        s.waiting = False
        last = _last_assistant_text(data.get("transcript_path", ""))
        s.message = last
        now = state.time.time()
        # A quick reply right after the user typed in that window: they are looking at it, no need to say so.
        # Only a run long enough that they probably went elsewhere is worth a word, and not more than every 5 min.
        long_run = now - (s.prompt_at or now) > 90
        if not busy and long_run and now - s.announced_at > 300:
            s.announced_at = now
            await hub.say(f"{name}那边跑完了，有空去看看。", remember=True)
    elif event in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
        s.waiting = False
        if event == "UserPromptSubmit":
            s.prompt_at = state.time.time()

    return {"ok": True}


@router.get("/deskpet/status")
async def status():
    return {
        "summary": state.describe(),
        "tasks": [t.__dict__ for t in state.tasks[-10:]],
        "sessions": [s.__dict__ for s in state.sessions.values()],
    }


@router.get("/deskpet/remote/state")
async def remote_state():
    """Everything the phone app shows (reachable from this PC only; the remote service relays it)."""
    from . import listening
    now = state.time.time()
    running = [t for t in state.tasks if t.status == "running"]
    cur = running[-1] if running else None
    return {
        "summary": state.describe(),
        "asleep": listening.asleep,
        "task": None if cur is None else {
            "request": cur.request, "minutes": int((now - cur.started) // 60),
            "progress": cur.progress, "step": cur.step,
        },
        "recent_tasks": [
            {"request": t.request[:80], "status": t.status, "summary": (t.summary or "")[:300],
             "finished": t.finished} for t in state.tasks[-8:]
        ],
        "feed": state.feed[-60:],
    }


@router.post("/deskpet/remote/say")
async def remote_say(body: dict):
    from . import hub
    text = str(body.get("text", "")).strip()
    if not text:
        return {"ok": False, "error": "empty"}
    ok = await hub.inject_text(text)
    return {"ok": ok, "error": None if ok else "pet window not connected"}
