"""[deskpet] Fix words the speech recognizer keeps getting wrong (app names, English terms)."""

import json
import re

from loguru import logger

from .config import AGENT_DIR

FIXES_FILE = AGENT_DIR / "asr_fixes.json"
_cache: tuple[float, list] = (0.0, [])


def _rules() -> list:
    global _cache
    try:
        mtime = FIXES_FILE.stat().st_mtime
    except OSError:
        return []
    if mtime != _cache[0]:  # reload when the file is edited
        try:
            data = json.loads(FIXES_FILE.read_text(encoding="utf-8"))
            rules = [(re.compile(k, re.I), v) for k, v in data.items() if not k.startswith("_")]
            _cache = (mtime, rules)
        except Exception as e:
            logger.warning(f"[deskpet] asr_fixes.json unreadable: {e}")
    return _cache[1]


def fix(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    fixed = text
    for pattern, repl in _rules():
        fixed = pattern.sub(repl, fixed)
    if fixed != text:
        logger.info(f"[deskpet] asr fix: {text} -> {fixed}")
    return fixed
