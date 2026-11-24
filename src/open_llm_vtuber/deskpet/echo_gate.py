"""[deskpet] Ignore what the microphone hears from this PC's own speakers.

A background thread records what Windows is sending to the default speakers (WASAPI
loopback, via pyaudiowpatch) into a short ring buffer. When the client sends a finished
utterance, it is compared with what the speakers played during the same seconds: if the
mic audio follows the speaker audio closely (same loudness pattern over time, band by band),
it is the speakers - music, a video, a game, her own voice - not the user, and it is dropped
before speech recognition. The user talking over music does not match (the speakers never
played his voice), so it goes through.

The comparison uses per-band log-energy envelopes rather than raw waveforms: the room,
the speaker and the mic's noise suppression change the waveform a lot, but the rise and
fall of each frequency band survives the trip from speaker to mic.
"""

import collections
import ctypes
import threading
import time
import uuid
from math import gcd

import numpy as np
from loguru import logger
from scipy.signal import resample_poly

SR = 16000            # the client's mic audio and the ASR both use 16 kHz mono
KEEP_SECONDS = 30     # loopback history kept
SLACK = 3.0           # utterance end may be up to this long before mic-audio-end arrives
HOP = 160             # 10 ms frames
WIN = 400             # 25 ms window
BANDS = np.geomspace(150, 5000, 13)  # 12 bands
# Decision: correlation minus the share of unexplained frames >= THRESHOLD -> it was the speakers.
# Tuned on a real recording of this PC (fifine mic, Realtek speakers, 2026-11-20): real echoes
# scored median 0.53; his voice over the music at 3x / 10x the music's level at most 0.25 / 0.16.
THRESHOLD = 0.35
SILENT = 2e-4         # speaker RMS below this: nothing was playing, nothing to compare
MIN_STD = 0.3         # log-energy std below this over the stretch = a flat band, no evidence
EXTRA_LOG = 1.0       # a frame this much louder (ln power, ~4 dB) than the speakers predict = extra sound
# Each band's energy gets a floor of this share of its loud level (90th percentile): the mic's
# noise suppression cuts quiet music to near zero, and without a floor those gated frames
# (log of ~0) swamp the comparison.
FLOOR = 0.1
PA_LOCK = threading.Lock()  # PortAudio crashes if two threads create/terminate PyAudio at once
POLL = 1.0            # seconds between looks at the default speakers
RETRY = 3.0           # seconds before trying again after an error


def default_speakers_id() -> str:
    """Windows' id of the current default speakers, asked afresh each time; "" if there are none.

    IMMDeviceEnumerator through plain ctypes, the same default (eRender, eMultimedia) PortAudio takes.
    """
    ole = ctypes.WinDLL("ole32")  # our own handle, so the prototypes set here affect no one else
    ptr = ctypes.c_void_p
    ole.CoInitializeEx.argtypes = [ptr, ctypes.c_ulong]
    ole.CoCreateInstance.argtypes = [ptr, ptr, ctypes.c_ulong, ptr, ptr]
    ole.CoTaskMemFree.argtypes = [ptr]

    def method(obj, index, *argtypes):
        table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ptr))).contents
        return ctypes.WINFUNCTYPE(ctypes.c_long, ptr, *argtypes)(table[index])

    started = ole.CoInitializeEx(None, 2) >= 0
    devices, device, name = ptr(), ptr(), ptr()
    try:
        clsid = uuid.UUID("BCDE0395-E52F-467C-8E3D-C4579291692E").bytes_le
        iid = uuid.UUID("A95664D2-9614-4F35-A746-DE8DB63617E6").bytes_le
        if ole.CoCreateInstance(clsid, None, 23, iid, ctypes.byref(devices)) < 0:
            raise OSError("cannot ask Windows for the audio devices")
        # IMMDeviceEnumerator::GetDefaultAudioEndpoint, then IMMDevice::GetId
        if method(devices, 4, ctypes.c_int, ctypes.c_int, ptr)(devices, 0, 1, ctypes.byref(device)) < 0:
            return ""
        if method(device, 5, ptr)(device, ctypes.byref(name)) < 0:
            return ""
        return ctypes.wstring_at(name.value)
    finally:
        if name:
            ole.CoTaskMemFree(name)
        for obj in (device, devices):
            if obj:
                method(obj, 2)(obj)  # Release
        if started:
            ole.CoUninitialize()


class _Loopback:
    def __init__(self):
        self.chunks: collections.deque = collections.deque()  # (end_time, samples@16k)
        self.lock = threading.Lock()
        self.ok = False
        self.device = ""
        threading.Thread(target=self._run, name="deskpet-loopback", daemon=True).start()

    def _run(self):
        try:
            import pyaudiowpatch as pyaudio
        except Exception as e:
            logger.warning(f"[deskpet] echo gate off: pyaudiowpatch not available ({e})")
            return
        while True:
            pa = stream = None
            try:
                # read before PortAudio takes its snapshot: a change in between is seen at the next poll
                following = default_speakers_id()
                with PA_LOCK:
                    pa = pyaudio.PyAudio()
                dev = pa.get_default_wasapi_loopback()
                rate = int(dev["defaultSampleRate"])
                ch = max(1, int(dev["maxInputChannels"]))
                g = gcd(SR, rate)
                up, down = SR // g, rate // g

                def cb(data, frames, info, status):
                    x = np.frombuffer(data, dtype=np.float32).reshape(-1, ch).mean(axis=1)
                    y = resample_poly(x, up, down).astype(np.float32)
                    now = time.time()
                    with self.lock:
                        self.chunks.append((now, y))
                        while self.chunks and now - self.chunks[0][0] > KEEP_SECONDS:
                            self.chunks.popleft()
                    return (None, pyaudio.paContinue)

                stream = pa.open(format=pyaudio.paFloat32, channels=ch, rate=rate, input=True,
                                 input_device_index=dev["index"], frames_per_buffer=rate // 50,
                                 stream_callback=cb)
                self.device, self.ok = dev["name"], True
                logger.info(f"[deskpet] echo gate listening to speakers: {self.device}")
                # Reopen when the default speakers change. Ask Windows, not `pa`: PortAudio lists the
                # devices once, when it is initialised, and keeps giving that same answer.
                while True:
                    time.sleep(POLL)
                    if default_speakers_id() != following:
                        logger.info("[deskpet] default speakers changed, reopening echo gate")
                        break
                    if not stream.is_active():
                        logger.info("[deskpet] speaker recording stopped, reopening echo gate")
                        break
            except Exception as e:
                self.ok = False
                logger.warning(f"[deskpet] echo gate loopback error: {e}")
                time.sleep(RETRY)
            finally:
                # Separately: terminate() must run even if closing fails (the device is gone),
                # otherwise PortAudio stays initialised and never lists the devices again.
                try:
                    if stream is not None:
                        stream.close()
                except Exception as e:
                    logger.warning(f"[deskpet] closing the old loopback stream failed: {e}")
                try:
                    if pa is not None:
                        with PA_LOCK:
                            pa.terminate()
                except Exception as e:
                    logger.warning(f"[deskpet] releasing PortAudio failed: {e}")

    def window(self, start: float, end: float) -> np.ndarray:
        """Speaker audio between two wall-clock times at 16 kHz; gaps (nothing playing) are zeros."""
        out = np.zeros(int((end - start) * SR) + 1, dtype=np.float32)
        with self.lock:
            chunks = list(self.chunks)
        for t_end, y in chunks:
            t0 = t_end - len(y) / SR
            if t_end < start or t0 > end:
                continue
            i = int(round((t0 - start) * SR))
            a, b = max(0, i), min(len(out), i + len(y))
            if b > a:
                out[a:b] = y[a - i:b - i]
        return out


def _envelopes(x: np.ndarray) -> np.ndarray:
    """(frames, bands) log energy."""
    if len(x) < WIN:
        return np.zeros((0, len(BANDS) - 1), dtype=np.float32)
    n = 1 + (len(x) - WIN) // HOP
    idx = np.arange(WIN)[None, :] + HOP * np.arange(n)[:, None]
    spec = np.abs(np.fft.rfft(x[idx] * np.hanning(WIN), axis=1)) ** 2
    freqs = np.fft.rfftfreq(WIN, 1 / SR)
    bands = [spec[:, (freqs >= lo) & (freqs < hi)].sum(axis=1) for lo, hi in zip(BANDS[:-1], BANDS[1:])]
    e = np.stack(bands, axis=1)
    floor = np.percentile(e, 90, axis=0) * FLOOR + 1e-12
    return np.log(e + floor).astype(np.float32)


def similarity(mic: np.ndarray, speakers: np.ndarray) -> float:
    """Best mean band-envelope correlation of `mic` against any stretch of the (longer) `speakers`."""
    return _match(mic, speakers)[0]


def _match(mic: np.ndarray, speakers: np.ndarray) -> tuple[float, float]:
    """(best correlation, share of frames with sound the speakers don't explain) at the best lag.

    The second number catches the user talking over the music: at the best alignment the
    speaker envelope predicts the mic envelope, and his syllables stick out above it.
    """
    m = _envelopes(mic)
    s = _envelopes(speakers)
    if len(m) < 20 or len(s) < len(m):
        return 0.0, 1.0
    mz = (m - m.mean(0)) / (m.std(0) + 1e-6)
    n = len(m)
    # sliding mean/std of the speaker envelopes over every lag at once
    c1 = np.vstack([np.zeros((1, s.shape[1])), np.cumsum(s, 0)])
    c2 = np.vstack([np.zeros((1, s.shape[1])), np.cumsum(s * s, 0)])
    mean = (c1[n:] - c1[:-n]) / n
    std = np.sqrt(np.maximum((c2[n:] - c2[:-n]) / n - mean ** 2, 0))
    # sum over time of mz * s for every lag: correlate each band
    dots = np.stack([np.correlate(s[:, b], mz[:, b], mode="valid") for b in range(s.shape[1])], axis=1)
    # a band that barely changes (silence between songs, a flat mic band) carries no evidence:
    # it counts as 0, instead of a near-zero std blowing the ratio up
    flat_spk = std < MIN_STD
    flat_mic = (m.std(0) < MIN_STD)[None, :]
    corr = np.clip(np.where(flat_spk | flat_mic, 0.0, dots / (n * np.maximum(std, MIN_STD))), -1, 1)
    per_lag = corr.mean(axis=1)
    k = int(per_lag.argmax())
    # predict each mic band from the aligned speaker band (least squares), look at what is left over
    seg = s[k:k + n]
    sz = (seg - mean[k]) / np.maximum(std[k], MIN_STD)
    pred = m.mean(0) + corr[k] * m.std(0) * sz
    excess = (m - pred).mean(axis=1)
    extra = float(np.mean(excess > EXTRA_LOG))
    return float(per_lag[k]), extra


_loop: _Loopback | None = None


def start() -> None:
    global _loop
    if _loop is None:
        _loop = _Loopback()


def check(mic: np.ndarray, received_at: float | None = None) -> tuple[bool, float]:
    """(is it the speakers?, score). Never blocks the utterance when anything is uncertain."""
    if _loop is None or not _loop.ok or mic is None or len(mic) < SR * 0.3:
        return False, 0.0
    end = received_at or time.time()
    dur = len(mic) / SR
    spk = _loop.window(end - dur - SLACK, end)
    if float(np.sqrt(np.mean(spk ** 2))) < SILENT:
        return False, 0.0
    try:
        score, extra = _match(mic.astype(np.float32), spk)
    except Exception as e:
        logger.debug(f"[deskpet] echo check failed: {e}")
        return False, 0.0
    global last_extra
    last_extra = extra
    return score - extra >= THRESHOLD, score - extra


last_extra = 0.0  # for logs / tuning: share of frames the speakers did not explain, last check
