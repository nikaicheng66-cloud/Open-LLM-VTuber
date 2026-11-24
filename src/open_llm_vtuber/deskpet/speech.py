"""[deskpet] Clean text before it is spoken: no links, paths or markdown symbols read aloud.

The voice was trained bilingual (2026-11-19), so English words, names and abbreviations stay as written.
"""

import re


def normalize(text: str) -> str:
    t = text
    t = re.sub(r"https?://\S+", "", t)            # never read links
    t = re.sub(r"[`*_#>|]", "", t)                 # markdown
    t = re.sub(r"[A-Za-z]:\\\S+|/\S+/\S+", "", t)  # file paths
    t = re.sub(r"(?<=[A-Za-z]) ?& ?(?=[A-Za-z])", " and ", t)  # Dodge & Cox -> Dodge and Cox
    t = t.replace("&", "和").replace("~", "到")
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()
