"""[deskpet] Her brain in the cloud: the Anthropic API (since 2026-11-24).

The key lives in the user's own environment variable `DeskPet`, never in a config file or a log.
The local model (qwen in Ollama) is only the backup: when the cloud can't be reached - no network,
key expired, no credit, monthly budget used up - `usable()` turns False for a while and everything
that asks here falls back to it.
"""

import json
import os
import time

from loguru import logger

from .config import AGENT_DIR

KEY_VAR = "DeskPet"
BASE_URL = "https://api.anthropic.com"  # always this host, whatever ANTHROPIC_BASE_URL says
CHAT_MODEL = "claude-sonnet-5-5"  # talks, and decides what a sentence is for
SMALL_MODEL = "claude-haiku-5-5"  # one-word judgements and one-line retellings
MONTHLY_BUDGET_USD = 100.0  # the user's limit (2026-11-24); past it she uses the local model
USAGE_FILE = AGENT_DIR / "cloud_usage.json"

# $ per million tokens: input, output, cache read, 5-minute cache write
_PRICES = {
    "claude-sonnet-5-5": (2.00, 10.00, 0.10, 2.50),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
}

# True once the chat model in conf.yaml is the cloud one (set by pet_llm.PetClaude). With a local
# chat model chosen there (llm_provider: 'ollama_llm'), nothing goes to the cloud: the old rules,
# the old persona and the local small model do everything, as before 2026-11-24.
enabled = False

_client = None
_down_until = 0.0
_down_why = ""
_told = True  # whether the user has heard about the current outage
_usage: dict | None = None
_saved_at = 0.0


class Unavailable(Exception):
    """The cloud could not answer; `why` is a few plain words she can say."""

    def __init__(self, why: str, retry_in: float = 120):
        super().__init__(why)
        self.why = why
        self.retry_in = retry_in


def key() -> str | None:
    """The API key from the `DeskPet` variable (the process environment, else the user's registry entry:
    a variable set after Explorer started is not in the environment of what Explorer launches)."""
    value = os.environ.get(KEY_VAR, "")
    if not value and os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                value = str(winreg.QueryValueEx(k, KEY_VAR)[0])
        except OSError:
            value = ""
    return value.strip() or None


def client():
    """One shared async client, or None when there is no key."""
    global _client
    if _client is None:
        k = key()
        if not k:
            return None
        from anthropic import AsyncAnthropic

        _client = AsyncAnthropic(api_key=k, base_url=BASE_URL, max_retries=1, timeout=45.0)
    return _client


def usable() -> bool:
    if not enabled or time.time() < _down_until:
        return False
    if spent_this_month() >= MONTHLY_BUDGET_USD:
        _note_down(f"这个月的额度（{MONTHLY_BUDGET_USD:.0f} 美元）用完了", 3600)
        return False
    return client() is not None


def _note_down(why: str, seconds: float) -> None:
    global _down_until, _down_why, _told
    if time.time() >= _down_until:
        _told = False  # a new outage: worth one sentence
    _down_until = time.time() + seconds
    _down_why = why
    logger.warning(f"[deskpet] cloud brain unavailable for {int(seconds)}s: {why}")


def failed(e: Exception) -> None:
    """Remember that the cloud is down, so the next sentences don't each wait for it again."""
    if isinstance(e, Unavailable):
        _note_down(e.why, e.retry_in)
    else:
        _note_down("连不上", 120)


def worked() -> None:
    global _down_until, _told
    _down_until = 0.0
    _told = True


def outage_notice() -> str:
    """Said once per outage, before the local model's answer."""
    global _told
    if _told or time.time() >= _down_until:
        return ""
    _told = True
    # (no brackets around it: what is in brackets is shown but not spoken)
    return f"我云端的脑子暂时用不了，{_down_why}。先用本地的顶着，可能会慢一点、笨一点。"


def explain(e: Exception) -> Unavailable:
    """Turn an SDK error into `Unavailable` with words for the user (most specific first)."""
    import anthropic

    if isinstance(e, Unavailable):
        return e
    if isinstance(e, anthropic.AuthenticationError):
        return Unavailable("API key 失效或者过期了，要换一个新的", 1800)
    if isinstance(e, anthropic.PermissionDeniedError):
        return Unavailable("这个 API key 没有权限", 1800)
    if isinstance(e, anthropic.RateLimitError):
        return Unavailable("请求太频繁，被限速了", 60)
    if isinstance(e, anthropic.BadRequestError):
        text = str(getattr(e, "message", e)).lower()
        if "credit balance" in text or "billing" in text:
            return Unavailable("API 账户里没有余额了", 1800)
        if "usage limits" in text or "spend limit" in text:
            return Unavailable("API 账户到了设定的花费上限", 1800)
        logger.error(f"[deskpet] the API refused our request (a bug on our side): {text[:400]}")
        return Unavailable("请求出了点问题", 30)
    if isinstance(e, anthropic.APIStatusError):
        return Unavailable(f"那边的服务器出错了（{e.status_code}）", 120)
    if isinstance(e, anthropic.APITimeoutError):
        return Unavailable("网络太慢，超时了", 60)
    if isinstance(e, anthropic.APIConnectionError):
        return Unavailable("网络连不上", 120)
    return Unavailable("出了点意外", 60)


# ---- what it costs ------------------------------------------------------------------------------
# agent/cloud_usage.json holds the tally per month. More than one process may add to it (the pet,
# a test run), so each keeps only what it has not written yet and adds that to what is on disk.

_pending = {"usd": 0.0, "calls": 0, "input": 0, "output": 0}


def _on_disk() -> dict:
    try:
        return json.loads(USAGE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _load() -> dict:
    global _usage
    if _usage is None:
        _usage = _on_disk()
    return _usage


def spent_this_month() -> float:
    return float(_load().get(time.strftime("%Y-%m"), {}).get("usd", 0.0)) + _pending["usd"]


def count(model: str, usage) -> None:
    """Add one answer's tokens to this month's tally."""
    if usage is None:
        return
    p_in, p_out, p_read, p_write = _PRICES.get(model, _PRICES[CHAT_MODEL])
    fresh = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    _pending["usd"] += (fresh * p_in + out * p_out + read * p_read + write * p_write) / 1_000_000
    _pending["calls"] += 1
    _pending["input"] += fresh + read + write
    _pending["output"] += out
    if time.time() - _saved_at > 20:  # not on every sentence
        flush()


def flush() -> None:
    """Write what has been counted since the last time."""
    global _usage, _saved_at
    _saved_at = time.time()
    if not _pending["calls"]:
        return
    data = _on_disk()
    month = data.setdefault(time.strftime("%Y-%m"), {"usd": 0.0, "calls": 0, "input": 0, "output": 0})
    for k, v in _pending.items():
        month[k] = round(month.get(k, 0) + v, 6) if k == "usd" else month.get(k, 0) + v
    try:
        USAGE_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
    except OSError as e:
        logger.warning(f"[deskpet] could not save the usage tally: {e!r}")
        return
    for k in _pending:
        _pending[k] = 0.0 if k == "usd" else 0
    _usage = data


# ---- small questions ----------------------------------------------------------------------------

async def small(system: str, examples: list, text: str, max_tokens: int = 200, timeout: float = 6.0) -> str | None:
    """A short answer from the small model, or None when the cloud can't be asked.

    `examples` are (question, answer) pairs shown as earlier turns.
    """
    if not usable():
        return None
    import anthropic

    messages = []
    for q, a in examples:
        messages += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    messages.append({"role": "user", "content": text})
    try:
        # two tries of `timeout / 2` each: a request that hangs is rare, and rarely hangs twice
        r = await client().with_options(timeout=timeout / 2, max_retries=1).messages.create(
            model=SMALL_MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            thinking={"type": "disabled"},  # one word or one line: nothing to think over
            output_config={"effort": "low"},
        )
    except anthropic.APITimeoutError:
        logger.warning("[deskpet] the small model did not answer in time (this once; the cloud is not marked down)")
        return None
    except anthropic.APIError as e:
        failed(explain(e))
        return None
    count(SMALL_MODEL, r.usage)
    if r.stop_reason == "refusal":
        return None
    worked()
    return "".join(b.text for b in r.content if b.type == "text").strip()
