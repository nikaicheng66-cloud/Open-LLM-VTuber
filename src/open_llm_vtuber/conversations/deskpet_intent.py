"""[deskpet] Turn movement requests in the user's own words into pet commands.

Rules catch the common phrasings instantly; anything that sounds like a move but
doesn't parse is checked by the local model (see deskpet/intent_llm.pick_moves).

Commands (run in order by the client, see Open-LLM-VTuber-Web/.../utils/pet-actions.ts):
  walk:<place>   walk to a screen place (left, top_right, center, ...)
  nudge:<dir>    take a few steps that way (left/right/up/down), "往右走一点"
  hide:<edge>    tuck into an edge (left/right/bottom/top), half her head showing
  vanish         fully invisible and click-through
  come_out       back from hiding / vanishing, to where she was
"""

import re

# only real screen places count ("去检查一下" must not read as "go down")
_PLACES = [
    (r"(左上角|左上方)", "top_left"), (r"(右上角|右上方)", "top_right"),
    (r"(左下角|左下方)", "bottom_left"), (r"(右下角|右下方)", "bottom_right"),
    (r"(左边|左侧|最左|屏幕左|left)", "left"), (r"(右边|右侧|最右|屏幕右|right)", "right"),
    (r"(上面|上方|顶上|顶部|最上|屏幕上|top)", "top"), (r"(下面|下方|底下|底部|最下|屏幕下|bottom)", "bottom"),
    (r"(中间|中央|正中|center|middle)", "center"), (r"(角落)", "bottom_right"),
]

_NEG = r"(?<!别)(?<!不要)(?<!不用)(?<!不)"
# fully invisible (different from hiding at an edge)
VANISH = re.compile(r"(" + _NEG + r"(隐身|消失)(吧|一下|一会|起来|$|。|！|，|,|\s)|别出现|不要出现|vanish|invisible)")
# back out: checked before HIDE, because "别躲了 / 不用藏了" contain the hide words themselves
COME_OUT = re.compile(r"(出来吧|你出来|出来一下|快出来|现身|出现吧|别躲了|不用躲了|不要躲了|别藏了|不用藏了|不要藏了|"
                      r"显形|come out)")
# "回来吧" is also everyday speech ("帮我把文件下载回来吧"): only short, or clearly said to her
COME_BACK = re.compile(r"(回来吧|快回来|回来一下|露面)[。！!~，,\s]*$")
HIDE = re.compile(r"(躲|藏起来|藏一下|藏到|藏着|藏好|别挡|挡住我|挡着我|让开|让一让|hide)")
WALK = re.compile(r"(走|去|过去|跑|移|挪|待|站|walk|go|move)")
# "往右走一点 / 再往左挪挪 / 往上一点": a few steps, not all the way to the edge
NUDGE = re.compile(r"(往|向|朝)(左|右|上|下)(边|面)?(走|挪|移|靠|去)?(一点|一些|一下|点儿|点|挪|移)")
_NUDGE_DIR = {"左": "left", "右": "right", "上": "up", "下": "down"}
# words that suggest a move even when the rules above can't parse it
MOVE_HINT = re.compile(r"(左边|右边|上面|下面|角落|边上|中间|躲|藏|隐身|消失|出来|过来|走到|挪|往左|往右|往上|往下)")
# the rules only judge short sentences or ones clearly said to her; the rest goes to the model
_TO_HER = re.compile(r"(你|桌宠|claude|小克)", re.I)


def _direction(t: str) -> str | None:
    """Screen place named in the text, e.g. 'top_right', 'left', 'center'."""
    for pat, place in _PLACES:
        if re.search(pat, t):
            return place
    return None


def _hide(t: str, d: str | None) -> list:
    """Hide at an edge, going to the named place first when it isn't simply that edge."""
    if d is None:
        return ["hide:bottom"]
    parts = d.split("_")
    # the edge she peeks from: left/right if named (a corner too), else top/bottom
    side = "left" if "left" in parts else "right" if "right" in parts else "top" if "top" in parts else "bottom"
    cmds = []
    if d != side:  # a corner or the centre: walk there, then tuck into its side edge
        cmds.append(f"walk:{d}")
    cmds.append(f"hide:{side}")
    return cmds


def detect_pet_commands(text: str) -> list:
    if not text:
        return []
    t = text.lower().strip()
    if len(t) > 24 and not _TO_HER.search(t):
        return []

    if VANISH.search(t):
        return ["vanish"]
    if COME_OUT.search(t) or (COME_BACK.search(t) and (len(t) <= 8 or _TO_HER.search(t))):
        return ["come_out"]

    d = _direction(t)
    if HIDE.search(t):
        return _hide(t, d)

    m = NUDGE.search(t)
    if m:
        return [f"nudge:{_NUDGE_DIR[m.group(2)]}"]
    if WALK.search(t) and d:
        return [f"walk:{d}"]
    return []
