"""[deskpet] Make the CUDA libraries from the nvidia-* pip packages visible on Windows.

faster-whisper (CTranslate2) needs cublas64_12.dll and cuDNN 9; the GPU driver does not
ship them, so they are installed as pip packages and their bin folders added here.
"""

import os
import sys
from pathlib import Path


def add_cuda_dll_dirs() -> None:
    if sys.platform != "win32":
        return
    for base in map(Path, sys.path):
        nvidia = base / "nvidia"
        if not nvidia.is_dir():
            continue
        for bin_dir in nvidia.glob("*/bin"):
            try:
                os.add_dll_directory(str(bin_dir))
            except OSError:
                pass
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")
