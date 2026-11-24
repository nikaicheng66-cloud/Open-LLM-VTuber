"""[deskpet] The pet's chat model: Claude in the cloud with her tools, the local model as backup.

One request both understands the user's sentence and answers it (see brain.py). If the cloud can't
be reached, the same sentence goes down the old local path: router.route_local decides what it is
for with rules and the small local model, and the local model does the talking.
"""

import asyncio
import threading
from typing import Any, AsyncIterator, Dict, List

import anthropic
import httpx
from loguru import logger

from ..agent.stateless_llm.claude_llm import AsyncLLM as ClaudeLLM
from . import brain, cloud, timing

MAX_ROUNDS = 4  # tool rounds within one reply
# Now and then a request sits for 5-10 seconds before its first event arrives (seen on about one
# request in twenty, 2026-11-24; not a rate limit). Too long for a spoken answer: after HEDGE_AFTER
# seconds of nothing the same request is sent a second time and whichever starts first is used.
HEDGE_AFTER = 2.0
FIRST_EVENT_DEADLINE = 12.0  # nothing from either by then: the cloud counts as unreachable for now
REFUSED = "这个我不方便说，换个话题吧。"


def _quietly(task: asyncio.Task) -> None:
    """A request that was no longer needed: fetch its outcome so nothing is reported as unhandled,
    and close it if it did arrive after all."""
    if task.cancelled():
        return
    if task.exception() is None:
        asyncio.ensure_future(PetClaude._close(task.result()[0]))


def _warm_up() -> None:
    """Load, before her first turn, what is otherwise loaded during it: cutting a text stream into
    sentences reads its language profiles on first use (most of a second), and so does the audio
    library that packs her voice for the client."""
    try:
        from ..utils.sentence_divider import segment_text_by_pysbd

        segment_text_by_pysbd("在呢。有什么事，说吧。")
        import pydub  # noqa: F401
    except Exception as e:
        logger.debug(f"[deskpet] warm-up skipped: {e!r}")


class PetClaude(ClaudeLLM):
    def __init__(self, fallback_model: str = "", fallback_base_url: str = "", thinking: str = "auto", **kwargs):
        kwargs.setdefault("api_key_env", cloud.KEY_VAR)
        super().__init__(**kwargs)
        cloud.enabled = True  # the chat model in conf.yaml is the cloud one
        self.client = cloud.client() or self.client  # one shared client, with the key from `DeskPet`
        cloud.CHAT_MODEL = self.model
        # "auto": the model thinks first when it finds the sentence tricky (about one turn in five,
        # 2-4 s to the first word instead of ~0.8 s). "off": never, every turn ~1.1 s (measured 2026-11-24).
        self.thinking = {}
        if (thinking or "auto") == "off":
            if self.model.startswith("claude-sonnet-5-5"):
                self.thinking = {"thinking": {"type": "between_tools"}}
            elif self.model.startswith("claude-haiku-5-5"):
                self.thinking = {"thinking": {"type": "disabled"}}
            else:
                logger.warning(f"[deskpet] thinking can't be switched off on {self.model}: left on")
        self.fallback_model = fallback_model or "qwen3.5:2b"
        # 127.0.0.1, not "localhost": on this PC that name is tried as ::1 first, where Ollama does not
        # listen, and every request then starts 2 s late
        self.fallback_base_url = (fallback_base_url or "http://127.0.0.1:11434/v1").replace("//localhost:", "//127.0.0.1:")
        self._local = None
        threading.Thread(target=_warm_up, daemon=True, name="deskpet-warm-up").start()
        if not cloud.key():
            logger.warning(f"[deskpet] no API key in the `{cloud.KEY_VAR}` variable: she runs on the local model")

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        system: str = None,
        tools: List[Dict[str, Any]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        if tools:  # the MCP tool loop of the agent: not the pet's own path
            async for event in super().chat_completion(messages, system, tools):
                yield event
            return

        turn = brain.take()
        if cloud.usable():
            said = ""
            try:
                async for piece in self._cloud(messages, system or self.system or "", turn):
                    said += piece
                    yield {"type": "text_delta", "text": piece}
                cloud.worked()
                brain.finish(turn, said)
                return
            except cloud.Unavailable as e:
                cloud.failed(e)
                if said or (turn is not None and turn.did):
                    # part of an answer is already out, or something was already set going:
                    # a second answer from the local model would only repeat it
                    brain.finish(turn, said)
                    return

        async for piece in self._local_answer(messages, system or self.system or "", turn):
            yield {"type": "text_delta", "text": piece}

    # ---- cloud --------------------------------------------------------------------------------

    async def _cloud(self, messages: list, system: str, turn) -> AsyncIterator[str]:
        msgs = self._prepare(messages)
        if not msgs:
            return
        # how things stand right now: an operator note after the user's words, never stored
        if msgs[-1]["role"] == "user":
            msgs.append({"role": "system", "content": brain.state_note(turn)})
        system_blocks = self._system_blocks(system + brain.GUIDE)
        said = False
        clock = asyncio.get_running_loop().time
        for _ in range(MAX_ROUNDS):
            manager = None
            began, first_event, first_word = clock(), None, None
            try:
                manager, stream, event = await self._begin_fast(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system_blocks,
                    messages=msgs,
                    tools=brain.TOOLS,
                    output_config={"effort": self.effort},
                    **self.thinking,
                )
                first_event = clock() - began
                while event is not None:
                    if event.type == "content_block_delta" and event.delta.type == "text_delta":
                        if event.delta.text:
                            said = True
                            first_word = first_word or clock() - began
                            timing.mark("text")
                            yield event.delta.text
                    event = await anext(stream, None)
                final = await stream.get_final_message()
            except ValueError as e:  # a tool input the SDK could not parse at all
                logger.warning(f"[deskpet] unreadable tool input from the model: {e}")
                return
            except (anthropic.APIError, httpx.HTTPError) as e:
                raise cloud.explain(e) from e
            finally:
                if manager is not None:
                    await self._close(manager)
            cloud.count(self.model, final.usage)
            usage = final.usage
            logger.info(
                f"[deskpet] brain: answer began {first_event:.1f}s, first word "
                f"{'-' if first_word is None else format(first_word, '.1f') + 's'}, done {clock() - began:.1f}s; "
                f"{[b.type if b.type != 'tool_use' else b.name for b in final.content]}; "
                f"tokens in {usage.input_tokens}+{usage.cache_read_input_tokens or 0} cached, out {usage.output_tokens}")

            if final.stop_reason == "refusal":
                logger.warning(f"[deskpet] the model declined: {getattr(final, 'stop_details', None)}")
                if not said:
                    yield REFUSED
                return
            uses = [b for b in final.content if b.type == "tool_use"]
            if not uses or final.stop_reason == "max_tokens":  # a cut-off tool input must not be run
                return
            results, speak_again = await brain.run_tools(turn, uses, said)
            if not speak_again:
                return
            msgs = msgs + [{"role": "assistant", "content": final.content}, {"role": "user", "content": results}]

    async def _begin(self, **request) -> tuple:
        """Send one streaming request and wait for its first event: (manager, stream, event)."""
        manager = self.client.messages.stream(**request)
        stream = await manager.__aenter__()
        try:
            return manager, stream, await anext(stream, None)
        except BaseException:
            await self._close(manager)
            raise

    @staticmethod
    async def _close(manager) -> None:
        try:
            await manager.__aexit__(None, None, None)
        except Exception:
            pass

    async def _begin_fast(self, **request) -> tuple:
        """`_begin`, sent a second time when the first shows no sign of life (see HEDGE_AFTER)."""
        loop = asyncio.get_running_loop()
        tries = {asyncio.create_task(self._begin(**request))}
        started = loop.time()
        hedged = False
        winner, error = None, None
        try:
            while winner is None and tries:
                left = FIRST_EVENT_DEADLINE - (loop.time() - started)
                if left <= 0:
                    break
                wait_for = left if hedged else min(left, HEDGE_AFTER)
                done, tries = await asyncio.wait(tries, timeout=wait_for, return_when=asyncio.FIRST_COMPLETED)
                for t in done:
                    if t.exception() is not None:
                        error = t.exception()
                    elif winner is None:
                        winner = t.result()
                    else:
                        await self._close(t.result()[0])  # both arrived at once: one is enough
                if not done and not hedged:
                    hedged = True
                    logger.info(f"[deskpet] no sign of an answer after {HEDGE_AFTER:.0f}s: asking a second time alongside")
                    tries.add(asyncio.create_task(self._begin(**request)))
        finally:
            for t in tries:  # the slower one, or both when she was interrupted while waiting
                t.cancel()
                t.add_done_callback(_quietly)
        if winner is not None:
            return winner
        if error is not None:
            raise error
        raise cloud.Unavailable("那边半天没有反应", 60)

    # ---- local backup -------------------------------------------------------------------------

    def _local_llm(self):
        if self._local is None:
            from ..agent.stateless_llm.openai_compatible_llm import AsyncLLM as OpenAILike

            self._local = OpenAILike(model=self.fallback_model, base_url=self.fallback_base_url,
                                     llm_api_key="ollama", temperature=0.7)
            self._local.reasoning_effort = "none"  # no long "thinking" phase before the reply
        return self._local

    async def _local_answer(self, messages: list, system: str, turn) -> AsyncIterator[str]:
        notice = cloud.outage_notice()
        if notice:
            yield notice
        if turn is not None and turn.text and not turn.routed:
            from . import router

            turn.routed = True
            try:
                quick = await router.route_local(turn.text, turn.pictures, turn.about_picture)
            except Exception as e:
                logger.exception(f"[deskpet] local routing failed: {e}")
                quick = None
            if quick:
                yield quick
                return
        # the small model's window is 4096 tokens: the latest turns only, and its own old persona
        recent = [m for m in messages if m.get("role") in ("user", "assistant")][-8:]
        async for chunk in self._local_llm().chat_completion(recent, brain.local_system(system)):
            if isinstance(chunk, str) and chunk:
                yield chunk
        await self._let_go()

    async def _let_go(self) -> None:
        """Tell Ollama to drop the backup model after five idle minutes (it used to stay for good)."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                await client.post(self.fallback_base_url.replace("/v1", "") + "/api/generate",
                                  json={"model": self.fallback_model, "keep_alive": 300})
        except Exception as e:
            logger.debug(f"[deskpet] could not set the local model's keep-alive: {e}")
