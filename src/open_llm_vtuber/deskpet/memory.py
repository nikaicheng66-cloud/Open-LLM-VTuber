"""[deskpet] Long-term memory: things the user asked her to remember.

Kept as a plain list in agent/memory.md, so it does not depend on the chat model:
it is added to the chat model's system prompt (qwen now, any model later) and to every
Claude Code task, and the user can read or edit the file by hand.
"""

import re
import time

from .config import AGENT_DIR

MEMORY_FILE = AGENT_DIR / "memory.md"
_HEADER = "# 小克的记忆\n\n用户让小克记住的事，一行一条。可以直接改这个文件。\n\n"

# "记住…" / "帮我记一下…": an explicit request, saved at once
REMEMBER = re.compile(r"(记住|记一下|帮我记|给我记|你要记得)")
# "以后都/别/不要…": may just be a passing remark ("以后别这样了"), so she asks first
SOFT_RULE = re.compile(r"以后(都|别|不要|不用|每次|一律|记得|要)")
YES = re.compile(r"^(好|好的|对|对的|是|是的|嗯|记吧|记住|记下|记下来|可以|行|要)[吧啊呀的。！!，,\s]*$")
CONFIRM_WINDOW = 90  # seconds the question stays open
pending: tuple[str, float] | None = None  # (what she offered to remember, when)


def offer(text: str) -> str:
    """Hold a soft rule until the user says yes; returns the fact as she will repeat it."""
    global pending
    fact = _LEAD.sub("", text.strip()).strip(" ，,。")
    pending = (fact, time.time()) if fact else None
    return fact


def take_pending(reply: str) -> str | None:
    """If `reply` answers yes to an open offer, save it and return the fact. Any reply closes the offer."""
    global pending
    if pending is None:
        return None
    fact, at = pending
    pending = None
    if time.time() - at > CONFIRM_WINDOW or not YES.match(reply.strip()):
        return None
    return add(fact)
FORGET = re.compile(r"(忘掉|忘了吧|忘记|不用记|别记|删掉.{0,6}记忆|那条.{0,4}(不算|作废))")
RECALL = re.compile(r"(你记得我.{0,6}(什么|啥)|你都记(得|住)了(什么|啥|哪些)|你的记忆|记了(什么|啥|哪些))")
_LEAD = re.compile(r"^(小克|小柯|小课|小科|小哥|嗯|呃|那个|好|对)?[，,。\s]*(你|请|帮我|给我)?[，,\s]*(要)?(记住|记一下|帮我记(一下|住)?|给我记(一下|住)?|你要记得)[：:，,。\s]*")


def items() -> list[str]:
    try:
        lines = MEMORY_FILE.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    return [l[2:].strip() for l in lines if l.startswith("- ") and l[2:].strip()]


def _save(entries: list[str]) -> None:
    MEMORY_FILE.write_text(_HEADER + "".join(f"- {e}\n" for e in entries), encoding="utf-8")


def add(text: str) -> str:
    fact = _LEAD.sub("", text.strip()).strip(" ，,。")
    if not fact:
        return ""
    entries = items()
    if fact not in [re.sub(r"（\d{4}-\d{2}-\d{2}）$", "", e) for e in entries]:
        entries.append(f"{fact}（{time.strftime('%Y-%m-%d')}）")
        _save(entries)
    return fact


def _overlap(a: str, b: str) -> float:
    a2 = {a[i:i + 2] for i in range(len(a) - 1)}
    b2 = {b[i:i + 2] for i in range(len(b) - 1)}
    return len(a2 & b2) / max(1, len(a2))


def forget(text: str) -> str | None:
    """Remove the entry that best matches what the user said; returns it, or None."""
    entries = items()
    if not entries:
        return None
    q = FORGET.sub("", text)
    best = max(entries, key=lambda e: _overlap(q, e))
    if _overlap(q, best) < 0.2:
        return None
    entries.remove(best)
    _save(entries)
    return re.sub(r"（\d{4}-\d{2}-\d{2}）$", "", best)


def prompt_block() -> str:
    entries = items()
    if not entries:
        return ""
    return "\n\n## 用户让你记住的事（一直有效，照着做）\n" + "\n".join(f"- {e}" for e in entries)
