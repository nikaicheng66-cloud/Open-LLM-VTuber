"""[deskpet] Paths and settings shared by the desk-pet extensions."""

import json
import os
from pathlib import Path

# claude-desktop-pet/  (this file is Open-LLM-VTuber/src/open_llm_vtuber/deskpet/config.py)
PROJECT_ROOT = Path(__file__).resolve().parents[4]
AGENT_DIR = PROJECT_ROOT / "agent"  # working folder for delegated Claude Code runs
PROJECTS_FILE = AGENT_DIR / "projects.json"  # project name -> path registry
MCP_CONFIG = AGENT_DIR / "mcp.json"  # Windows-MCP: mouse/keyboard/window control for GUI tasks
INTENT_LOG = AGENT_DIR / "intent_log.jsonl"  # how each message was routed, for review
SCREEN_DIR = AGENT_DIR / "screen"  # screenshots for screen questions
HOME = Path(os.path.expanduser("~"))

# Tools Claude Code may never use in delegated runs (it reports back instead)
DENY_TOOLS = [
    "Bash(rm:*)", "Bash(rmdir:*)", "Bash(del:*)", "Bash(rd:*)",
    "Bash(Remove-Item:*)", "Bash(format:*)", "Bash(shutdown:*)",
    "Bash(git push:*)", "Bash(git reset --hard:*)", "Bash(git clean:*)",
]

TASK_TIMEOUT_SECONDS = 60 * 60


def load_projects() -> dict:
    try:
        return json.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def project_name_for(path: str) -> str | None:
    """Registry name of the project containing `path`, if any."""
    if not path:
        return None
    p = os.path.normcase(os.path.abspath(path))
    best = None
    for name, root in load_projects().items():
        r = os.path.normcase(os.path.abspath(os.path.expanduser(root)))
        if p == r or p.startswith(r + os.sep):
            if best is None or len(r) > len(best[1]):
                best = (name, r)
    return best[0] if best else None
