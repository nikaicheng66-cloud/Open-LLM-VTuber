"""[deskpet] 'Not listening' mode: she ignores everything until called back by name.

Rules catch the clear phrasings; the local model is the second opinion.
"""

import re

asleep = False

SLEEP = re.compile(r"(接着睡|继续睡|去睡吧|去睡觉|先睡|睡一会|睡吧|睡觉吧|休眠|待机|"
                   r"别听(?!歌|音乐)|不要听(?!歌|音乐)|不用听(?!歌|音乐)|先别理我|不要理我|别理我|你先休息|你休息一下|"
                   r"安静一会|安静点|闭嘴|别说话了|不要说话了|我要开会|勿扰)")
# words that mean "come back / listen again"; a move said by name while asleep wakes her only with one of these
# (or when it brings her somewhere), "小克，隐身" just makes her go away and keep sleeping
WAKE_WORDS = re.compile(r"(醒|可以听|听我说|回来|出来|现身|别睡|不要睡|起来)")
# her name / clear call-backs; short so a passing mention in a long sentence doesn't count
# "…然后继续睡觉 / 接着别听": a call by name that asks her to stay in not-listening mode
STAY_ASLEEP = re.compile(r"(继续睡|接着睡|继续休息|接着休息|继续别听|还是别听|还是不要听|继续不听|别听我|不要听我)")
WAKE = re.compile(r"(claude|克劳德|克劳迪|小克|小柯|小课|小科|出来吧|醒醒|醒一醒|可以听了|回来吧|听我说)", re.I)
NAME = re.compile(r"(claude|克劳德|克劳迪|小克|小柯|小课|小科|小哥)", re.I)

# whether she is hiding at an edge (kept for other uses; hiding does not change how she listens)
hidden = False
# (Music or a video playing no longer makes her answer only to her name: echo_gate drops
# whatever the mic hears from this PC's own speakers, so she listens to him normally.)
# things the speech recognizer invents from noise / music
_HALLUCINATION = re.compile(r"(点赞.{0,6}订阅|订阅.{0,6}转发|打赏支持|明镜与点点|字幕由|字幕志愿者|字幕作成者|字幕制作|"
                            r"李宗盛|中文字幕|amara|谢谢观看|感谢观看|请不吝|订阅我的频道|"
                            r"subscribe|thanks for watching)", re.I)


# fully invisible (vanish) - waking her up brings her back into sight
vanished = False


def note_moves(cmds: list) -> None:
    """Follow what the client does with these commands (same rules as pet-actions.ts)."""
    global hidden, vanished
    for c in cmds or []:
        if c == "vanish":
            vanished = True
        elif c.startswith("hide"):
            hidden, vanished = True, False   # asked to hide somewhere: she shows up there
        elif c.startswith(("walk", "nudge")) or c in ("come_out", "appear"):
            hidden, vanished = False, False


def is_noise(text: str) -> bool:
    return bool(_HALLUCINATION.search(text))



def wake_by_rule(text: str) -> bool:
    t = text.strip().lower()
    return bool(WAKE.search(t)) and len(t) <= 20


def maybe_calling(text: str) -> bool:
    """While asleep: could this be a call to her at all? Only then is the small model asked
    (it runs in the cloud now, and what is said in a meeting should not go there for nothing)."""
    t = text.strip().lower()
    return len(t) <= 60 or bool(NAME.search(t) or WAKE.search(t) or WAKE_WORDS.search(t))


# "让电脑休眠 / 显示器待机" is a computer task, not "stop listening"
_DEVICE = re.compile(r"(电脑|屏幕|显示器|系统|主机|笔记本|手机|windows)", re.I)


def sleep_by_rule(text: str) -> bool:
    t = text.strip().lower()
    return bool(SLEEP.search(t)) and not _DEVICE.search(t)
