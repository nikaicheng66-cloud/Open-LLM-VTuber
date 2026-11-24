import numpy as np
from .cuda_dlls import add_cuda_dll_dirs

add_cuda_dll_dirs()  # [deskpet] before faster_whisper loads CTranslate2
from faster_whisper import WhisperModel  # noqa: E402
from loguru import logger
from .asr_interface import ASRInterface


class VoiceRecognition(ASRInterface):
    BEAM_SEARCH = True
    # SAMPLE_RATE # Defined in asr_interface.py

    def __init__(
        self,
        model_path: str = "distil-medium.en",
        download_root: str = None,
        language: str = "en",
        device: str = "auto",
        compute_type: str = "int8",
        prompt: str = None,
    ) -> None:
        self.MODEL_PATH = model_path
        self.LANG = language
        self.prompt = prompt
        self.download_root = download_root
        # [deskpet] try the requested device (GPU); if it can't run there, use the CPU instead
        try:
            self.model = self._load(device, compute_type)
            self._run(np.zeros(16000, dtype=np.float32))  # surfaces missing CUDA libraries now
            logger.info(f"[deskpet] faster-whisper running on {device} ({compute_type})")
        except Exception as e:
            logger.warning(f"[deskpet] faster-whisper can't use {device} ({e}); falling back to CPU")
            self._to_cpu()

    def _load(self, device: str, compute_type: str) -> WhisperModel:
        return WhisperModel(
            model_size_or_path=self.MODEL_PATH,
            download_root=self.download_root,
            device=device,
            compute_type=compute_type,
        )

    def _to_cpu(self) -> None:
        self.model = self._load("cpu", "int8")
        logger.info("[deskpet] faster-whisper running on cpu (int8)")

    def _run(self, audio: np.ndarray) -> str:
        segments, info = self.model.transcribe(
            audio,
            beam_size=5 if self.BEAM_SEARCH else 1,
            language=self.LANG if self.LANG else None,
            condition_on_previous_text=False,
            initial_prompt=self.prompt or None,
            vad_filter=True,  # [deskpet] cut silence/music first: whisper invents subtitle credits from it
            vad_parameters={"min_silence_duration_ms": 500},
        )
        # [deskpet] drop segments the model itself thinks are not speech
        return "".join(s.text for s in segments if not (s.no_speech_prob > 0.6 and s.avg_logprob < -0.8))

    def transcribe_np(self, audio: np.ndarray) -> str:
        try:
            return self._run(audio)
        except RuntimeError as e:  # e.g. GPU out of memory mid-session
            logger.warning(f"[deskpet] faster-whisper failed ({e}); switching to CPU")
            self._to_cpu()
            return self._run(audio)
