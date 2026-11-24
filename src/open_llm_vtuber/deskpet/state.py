"""[deskpet] What Claude Code is doing: delegated tasks and hook events from the user's own sessions."""

import time
from dataclasses import dataclass, field


@dataclass
class Task:
    id: int
    request: str
    started: float = field(default_factory=time.time)
    finished: float | None = None
    status: str = "running"  # running | done | failed | cancelled
    summary: str = ""
    step: str = ""  # what she is doing right now, e.g. "我在看一下屏幕"
    session_id: str = ""  # Claude Code session, so follow-ups can continue it
    progress: str = ""  # latest one-line progress report (what has actually been done so far)
    notes: list = field(default_factory=list)  # its own words between steps, newest last
    waiting: bool = False  # asked the user to do something on a page and is waiting for it (same task)


@dataclass
class Session:
    session_id: str
    cwd: str
    project: str | None
    last_event: str = ""
    message: str = ""
    updated: float = field(default_factory=time.time)
    waiting: bool = False  # waiting for the user (permission / input)
    prompt_at: float = 0.0  # when the user last sent a prompt in this window
    announced_at: float = 0.0  # when she last said "that window stopped"
    handed_over: bool = False  # she typed a request into this window; report when it finishes


def other_windows() -> str:
    """What the user's own Claude windows (other projects) did last, so 'what it found' makes sense."""
    lines = []
    for s in sorted(sessions.values(), key=lambda x: -x.updated)[:3]:
        if time.time() - s.updated > 3 * 3600 or not s.message:
            continue
        lines.append(f"- {s.project or s.cwd}（{ago(s.updated)}，项目目录 {s.cwd}）最后说：{s.message[:600]}")
    if not lines:
        return ""
    return ("用户自己开着的其他 Claude 窗口（只是参考，不要往那些窗口里输入东西；"
            "他说「那边」「刚才找出来的」多半指这里）：\n" + "\n".join(lines) + "\n\n")


tasks: list[Task] = []
feed: list[dict] = []  # what was said, for the phone app: {"t": time, "who": "你"|"小克", "text": ...}


def log_feed(who: str, text: str) -> None:
    if text and text.strip():
        feed.append({"t": time.time(), "who": who, "text": text.strip()})
        del feed[:-200]
dialogue: list[tuple[str, str]] = []  # recent (speaker, text), speaker = "用户" | "我"


def remember(speaker: str, text: str) -> None:
    if text:
        dialogue.append((speaker, text.strip()))
        del dialogue[:-12]


def recent_dialogue(n: int = 8) -> str:
    return "\n".join(f"{who}：{text}" for who, text in dialogue[-n:])


def last_task():
    return tasks[-1] if tasks else None
sessions: dict[str, Session] = {}
audio_now = ""  # set by audio_drop while she listens to dropped recordings (fixed words, nothing from the file)


def ago(t: float) -> str:
    s = int(time.time() - t)
    if s < 60:
        return "刚刚"
    if s < 3600:
        return f"{s // 60}分钟前"
    return f"{s // 3600}小时前"


def describe() -> str:
    """Short spoken answer to 'are you working / how far is it': first whether anything is running."""
    running = [t for t in tasks if t.status == "running"]
    if audio_now and not running:
        return audio_now
    if running:
        t = running[-1]
        secs = int(time.time() - t.started)
        took = f"{secs}秒" if secs < 60 else f"{secs // 60}分钟"
        what = t.progress or (f"现在{t.step}" if t.step else "")
        more = f"后面还排着{len(running) - 1}件。" if len(running) > 1 else ""
        return f"在跑，「{t.request[:12]}」这件已经{took}了。{what}{more}"
    done = [t for t in tasks if t.finished and time.time() - t.finished < 3 * 3600]
    if not done:
        return "现在没有在跑的任务。"
    t = done[-1]
    if t.status == "cancelled":
        return "现在没有在跑的任务。上一件你让我停下了，没做完。"
    # the summary describes how things were back then, not now: say so when it is not fresh
    if time.time() - t.finished > 300:
        return f"现在没有在跑的任务。上一件{ago(t.finished)}做完了，那时候的情况是：{t.summary}"
    return f"现在没有在跑的任务。上一件{ago(t.finished)}做完了：{t.summary}"
