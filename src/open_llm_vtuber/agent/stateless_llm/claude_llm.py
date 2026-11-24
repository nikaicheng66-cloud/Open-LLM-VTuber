"""Description: This file contains the implementation of the `AsyncLLM` class for Claude API.
This class is responsible for handling asynchronous interaction with Claude API endpoints
for language generation.

[deskpet] 2026-11-24: brought up to the current models (adaptive thinking steered by `effort`,
no sampling parameters, prompt caching, refusals) and the key may come from an environment
variable instead of the config file. The desk pet itself uses the subclass in deskpet/pet_llm.py.
"""

import json
import os
from typing import AsyncIterator, List, Dict, Any

from loguru import logger
from anthropic import AsyncAnthropic, NOT_GIVEN

from .stateless_llm_interface import StatelessLLMInterface

_PLACEHOLDER_KEYS = ("", "YOUR API KEY HERE")
KEEP_MESSAGES = 40  # of the conversation, sent along with each request
TRIM_STEP = 20  # trimmed in steps, so the cached beginning stays the same for a while


class AsyncLLM(StatelessLLMInterface):
    def __init__(
        self,
        model: str = "claude-sonnet-5-5",
        base_url: str = None,
        llm_api_key: str = None,
        system: str = None,
        effort: str = "low",
        max_tokens: int = 4000,
        api_key_env: str = "",
    ):
        """
        Initialize Claude LLM.

        Args:
            model (str): Model name
            base_url (str): Base URL for Claude API
            llm_api_key (str): Claude API key; empty = read it from `api_key_env`
            system (str): System prompt
            effort (str): how much the model thinks: low | medium | high | xhigh | max
            max_tokens (int): cap for one reply (thinking counts towards it)
            api_key_env (str): name of the environment variable that holds the key
        """
        self.model = model
        self.system = system
        self.effort = effort or "low"
        self.max_tokens = max_tokens or 4000

        key = llm_api_key if (llm_api_key or "").strip() not in _PLACEHOLDER_KEYS else None
        if key is None and api_key_env:
            key = self._key_from_env(api_key_env)
        self.has_key = bool(key)
        # without a key the client is still built (requests then fail with an authentication error)
        self.client = AsyncAnthropic(
            api_key=key or "missing",
            base_url=base_url if base_url else "https://api.anthropic.com",
            max_retries=1,
            timeout=45.0,
        )

        logger.info(f"Initialized Claude AsyncLLM with model: {self.model} (effort {self.effort})")
        logger.debug(f"Base URL: {base_url}")

    @staticmethod
    def _key_from_env(name: str) -> str | None:
        value = os.environ.get(name, "")
        if not value and os.name == "nt":
            # set after Explorer started: not in the environment of what Explorer launches
            try:
                import winreg

                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                    value = str(winreg.QueryValueEx(k, name)[0])
            except OSError:
                value = ""
        return value.strip() or None

    def _convert_message_format(self, message: Dict[str, Any]) -> Dict[str, Any]:
        """Convert message format to Claude's expected format."""
        # Handle potential tool_result content blocks
        if isinstance(message.get("content"), list):
            new_content = []
            is_tool_result = False
            for content_item in message["content"]:
                if content_item.get("type") == "image_url":
                    # Extract media type and base64 data from data URL
                    data_url = content_item["image_url"]["url"]
                    # Split 'data:image/jpeg;base64,/9j/4AAQ...' into parts
                    header, base64_data = data_url.split(",", 1)
                    # Extract media type from 'data:image/jpeg;base64'
                    media_type = header.split(":")[1].split(";")[0]

                    new_content.append(
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": base64_data,
                            },
                        }
                    )
                elif content_item.get("type") == "tool_result":
                    is_tool_result = True
                    # Keep tool_result block as is, Anthropic SDK handles it
                    new_content.append(content_item)
                else:
                    # Assume text or other standard types
                    new_content.append(content_item)

            # For tool_result messages, the role should be 'user'
            # Ensure the role is correctly set before returning
            role = "user" if is_tool_result else message["role"]
            return {"role": role, "content": new_content}

        # Handle plain text content or non-list content
        return {"role": message["role"], "content": message.get("content", "")}

    def _prepare(self, messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """The conversation as the API takes it: no system entries, not endlessly long, and
        with a cache mark at the end of what was already there before the newest message."""
        msgs = [
            self._convert_message_format(m)
            for m in messages
            if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        if len(msgs) > KEEP_MESSAGES + TRIM_STEP:
            cut = ((len(msgs) - KEEP_MESSAGES) // TRIM_STEP) * TRIM_STEP
            msgs = msgs[cut:]
        while msgs and msgs[0]["role"] != "user":  # the API wants the user to speak first
            msgs = msgs[1:]
        if len(msgs) >= 2:
            earlier = msgs[-2]
            content = earlier["content"]
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            else:
                content = [dict(b) for b in content]
            if content and content[-1].get("type") in ("text", "image", "tool_result"):
                content[-1] = {**content[-1], "cache_control": {"type": "ephemeral"}}
                msgs[-2] = {"role": earlier["role"], "content": content}
        return msgs

    @staticmethod
    def _system_blocks(system: str) -> list | str:
        """The system prompt as one cached block (it is the same from turn to turn)."""
        if not system:
            return NOT_GIVEN
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]

    async def chat_completion(
        self,
        messages: List[Dict[str, Any]],
        system: str = None,
        tools: List[Dict[str, Any]] = None,
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Generates a chat completion using the Claude API asynchronously,
        handling text generation and tool use.

        Parameters:
        - messages (List[Dict[str, Any]]): The list of messages to send to the API.
        - system (str, optional): System prompt to use for this completion.
        - tools (List[Dict[str, Any]], optional): List of tools available.

        Yields:
        - Dict[str, Any]: Events representing text deltas, tool use, or errors.
          Possible event types:
            - {"type": "message_start", "data": ...}
            - {"type": "text_delta", "text": "..."}
            - {"type": "tool_use_start", "data": {"id": ..., "name": ..., "input": None}}
            - {"type": "tool_input_delta", "tool_id": ..., "partial_json": "..."} # Optional
            - {"type": "tool_use_complete", "data": {"id": ..., "name": ..., "input": {...}}}
            - {"type": "message_delta", "data": ...} # e.g., stop_reason
            - {"type": "message_stop"}
            - {"type": "error", "message": "..."}
        """
        try:
            converted_messages = self._prepare(messages)

            logger.debug(f"Sending {len(converted_messages)} messages to Claude API")
            logger.debug(f"Tools provided: {tools}")

            async with self.client.messages.stream(
                messages=converted_messages,
                system=self._system_blocks(system if system else self.system),
                model=self.model,
                max_tokens=self.max_tokens,
                output_config={"effort": self.effort},
                tools=tools if tools else NOT_GIVEN,
            ) as stream:
                current_tool_call_info = None
                partial_json_accumulator = ""

                async for event in stream:
                    if event.type == "message_start":
                        logger.debug("Stream: message_start")
                        yield {
                            "type": "message_start",
                            "data": event.message.model_dump(exclude_none=True),
                        }
                    elif event.type == "content_block_start":
                        logger.debug(
                            f"Stream: content_block_start - Index: {event.index}, Type: {event.content_block.type}"
                        )
                        if event.content_block.type == "tool_use":
                            current_tool_call_info = {
                                "id": event.content_block.id,
                                "name": event.content_block.name,
                                "input": None,
                                "index": event.index,  # Store index
                            }
                            partial_json_accumulator = ""
                            logger.debug(
                                f"Stream: tool_use started - ID: {current_tool_call_info['id']}, Name: {current_tool_call_info['name']}"
                            )
                            yield {
                                "type": "tool_use_start",
                                "data": current_tool_call_info.copy(),
                            }
                        # text is handled by text_delta; thinking blocks are not shown
                    elif event.type == "content_block_delta":
                        if event.delta.type == "text_delta":
                            yield {"type": "text_delta", "text": event.delta.text}
                        elif event.delta.type == "input_json_delta":
                            if (
                                current_tool_call_info
                                and event.index == current_tool_call_info["index"]
                            ):
                                partial_json_accumulator += event.delta.partial_json
                            else:
                                logger.warning(
                                    f"Received input_json_delta but no active tool call matching index {event.index}"
                                )
                    elif event.type == "content_block_stop":
                        # Check if this stop corresponds to the active tool call
                        if (
                            current_tool_call_info
                            and event.index == current_tool_call_info["index"]
                        ):
                            try:
                                if not partial_json_accumulator.strip():
                                    tool_input = {}
                                else:
                                    tool_input = json.loads(partial_json_accumulator)
                                current_tool_call_info["input"] = tool_input
                                logger.debug(
                                    f"Stream: tool_use completed - ID: {current_tool_call_info['id']}, Input: {tool_input}"
                                )
                                # Yield the complete tool call info
                                yield {
                                    "type": "tool_use_complete",
                                    "data": current_tool_call_info.copy(),
                                }
                            except json.JSONDecodeError as e:
                                logger.error(
                                    f"Failed to decode tool input JSON: {partial_json_accumulator}. Error: {e}"
                                )
                                yield {
                                    "type": "error",
                                    "message": f"Failed to parse tool input JSON for tool ID {current_tool_call_info['id']}",
                                }
                            finally:
                                # Reset regardless of success or failure for this index
                                current_tool_call_info = None
                                partial_json_accumulator = ""
                    elif event.type == "message_delta":
                        delta = event.delta.model_dump(exclude_none=True)
                        if delta.get("stop_reason") == "refusal":
                            # the model declined (HTTP 200, no exception): say so instead of going silent
                            logger.warning("Claude declined to answer (stop_reason: refusal)")
                            yield {"type": "error", "message": "The model declined to answer this."}
                        yield {
                            "type": "message_delta",
                            "data": {
                                "delta": delta,
                                "usage": event.usage.model_dump(),
                            },
                        }
                    elif event.type == "message_stop":
                        logger.debug("Stream: message_stop")
                        yield {"type": "message_stop"}
                        # No need to break here, the context manager handles the end
                    # pings and thinking deltas are ignored

        except Exception as e:
            logger.error(f"Claude API error occurred: {str(e)}")
            logger.info(f"Model: {self.model}")
            # Yield an error event before raising
            yield {"type": "error", "message": f"Claude API error: {str(e)}"}
            raise

        # No finally block needed for stream.close() due to async with
        logger.debug("Chat completion stream processing finished.")
