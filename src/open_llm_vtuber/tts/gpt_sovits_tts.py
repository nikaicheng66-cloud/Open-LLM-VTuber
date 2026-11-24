####
# change from xTTS.py
# [deskpet] sends a JSON POST so that auxiliary reference audio and the
# tuning parameters (speed, temperature, seed, ...) can be set from conf.yaml
####

import re

from ..deskpet import speech as deskpet_speech
import asyncio
import os
import time

_gsv_slow_until = 0.0


def _mark_slow() -> None:
    global _gsv_slow_until
    _gsv_slow_until = time.time() + 300


def _edge_fallback(text: str, file_name: str):
    """Same voice family as the reference audio, synthesized in the cloud (no GPU)."""
    try:
        import edge_tts

        mp3 = os.path.splitext(file_name)[0] + ".mp3"
        asyncio.run(edge_tts.Communicate(text, "zh-CN-XiaoxiaoNeural", rate="-12%").save(mp3))
        return mp3
    except Exception as e:
        logger.critical(f"[deskpet] Edge TTS fallback failed too: {e}")
        return None
import requests
from loguru import logger
from .tts_interface import TTSInterface


class TTSEngine(TTSInterface):
    def __init__(
        self,
        api_url: str = "http://127.0.0.1:9880/tts",
        text_lang: str = "zh",
        ref_audio_path: str = "",
        prompt_lang: str = "zh",
        prompt_text: str = "",
        text_split_method: str = "cut5",
        batch_size: str = "1",
        media_type: str = "wav",
        streaming_mode: str = "false",
        aux_ref_audio_paths: list | None = None,
        speed_factor: float = 1.0,
        temperature: float = 1.0,
        top_k: int = 15,
        top_p: float = 1.0,
        repetition_penalty: float = 1.35,
        fragment_interval: float = 0.3,
        seed: int = -1,
    ):
        self.api_url = api_url
        self.media_type = media_type
        self.base = {
            "text_lang": text_lang,
            "ref_audio_path": ref_audio_path,
            "aux_ref_audio_paths": list(aux_ref_audio_paths or []),
            "prompt_lang": prompt_lang,
            "prompt_text": prompt_text,
            "text_split_method": text_split_method,
            "batch_size": int(batch_size),
            "media_type": media_type,
            "streaming_mode": str(streaming_mode).lower() in ("true", "1"),
            "speed_factor": float(speed_factor),
            "temperature": float(temperature),
            "top_k": int(top_k),
            "top_p": float(top_p),
            "repetition_penalty": float(repetition_penalty),
            "fragment_interval": float(fragment_interval),
            "seed": int(seed),
        }
        self._turns = None  # (event loop, lock): see async_generate_audio

    async def async_generate_audio(self, text: str, file_name_no_ext=None) -> str:
        """[deskpet] One sentence at a time, in the order they were asked for.

        The GPT-SoVITS server works through its requests one by one anyway, but when several are
        waiting it does not take them in order of arrival: the first words of a reply were often
        synthesized after the second or third sentence, and she started speaking 1-2 s later
        than she could (measured 2026-11-24). An asyncio.Lock hands itself on first come, first
        served, and the sentences of a reply ask for it in the order they are spoken.
        """
        loop = asyncio.get_running_loop()
        if self._turns is None or self._turns[0] is not loop:
            self._turns = (loop, asyncio.Lock())
        async with self._turns[1]:
            return await asyncio.to_thread(self.generate_audio, text, file_name_no_ext)

    def generate_audio(self, text, file_name_no_ext=None):
        file_name = self.generate_cache_file_name(file_name_no_ext, self.media_type)
        cleaned_text = re.sub(r"\[.*?\]", "", text).strip()
        cleaned_text = deskpet_speech.normalize(cleaned_text)  # [deskpet] fewer English words, no symbols
        if not cleaned_text:
            return None
        data = dict(self.base, text=cleaned_text)

        # [deskpet] when the GPU is busy (a game, a video) GPT-SoVITS can take minutes per line;
        # then speak with Edge TTS (same Xiaoxiao voice, a bit less tuned) for a while instead of going silent
        if time.time() < _gsv_slow_until:
            return _edge_fallback(cleaned_text, file_name)
        try:
            response = requests.post(self.api_url, json=data, timeout=12)
        except requests.RequestException as e:
            logger.warning(f"[deskpet] GPT-SoVITS too slow or down ({e}); using Edge TTS for 5 minutes")
            _mark_slow()
            return _edge_fallback(cleaned_text, file_name)

        if response.status_code == 200:
            with open(file_name, "wb") as audio_file:
                audio_file.write(response.content)
            return file_name
        logger.critical(
            f"Error: Failed to generate audio. Status code: {response.status_code}, {response.text[:300]}"
        )
        return None
