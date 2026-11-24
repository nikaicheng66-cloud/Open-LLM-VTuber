"""[deskpet] Small questions about a user message: one word or one line back.

Asked of the small cloud model first (cloud.small); the local model in Ollama is the backup and is
loaded only then, for five minutes (it used to stay in the graphics memory for good).
`classify` belongs to the local path only: with the cloud up, her chat model decides itself what a
sentence is for (see brain.py).

Labels:
  TASK    - wants something done on the computer now (or answers a question about such a task)
  STATUS  - asks how a task is going / whether she is doing it
  SCREEN  - asks about what is on the screen or what they were doing on the computer
  APPROVE - says yes to letting a waiting Claude Code session continue (only when one is waiting)
  CHAT    - anything else: chit-chat, opinions, complaints, questions, praise or criticism
"""

import httpx
from loguru import logger

from . import cloud

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
MODEL = "qwen3.5:2b"
KEEP_ALIVE = 300  # seconds the backup model stays loaded after it was needed
LABELS = ("TASK", "STATUS", "SCREEN", "APPROVE", "MOVE", "SLEEP", "CHAT")

_SYSTEM = """你是桌宠助手的分类器。用户在和桌宠语音聊天（语音识别可能有错字）。根据之前的对话和用户最后一句话，判断最后一句属于哪一类，只输出一个英文标签：
TASK：要求现在去电脑上做一件具体的事（开关程序、调音量、播放、找文件、下载安装、点击输入、处理项目等），或者是在回答/补充刚才那件事（比如「调到25」「装吧」「就要那个」「不是这个，换一个」）。
STATUS：问刚才交代的事做得怎么样、有没有在做、做完没有。
SCREEN：问屏幕上是什么、某个窗口或报错是什么、自己刚才在电脑上干什么。
APPROVE：同意让正在等待确认的操作继续（只有提示里说「有操作在等用户确认」时才可能是这个）。
MOVE：让桌宠自己的身体动：走到屏幕某处、躲到边上露半个头、隐身、出来。
SLEEP：让桌宠先别听他说话、别理他、安静待着（不是关机，之后叫她名字会回来）。
CHAT：其他一切：闲聊、问问题、发表看法、吐槽、夸奖或批评桌宠做得对不对、感叹。"""

_EXAMPLES = [
    ("帮我把微信关了", "TASK"),
    ("声音太大了", "TASK"),
    ("（之前：用户：帮我把音量调大  我：音量调到80了）\n最后一句：帮我调到25", "TASK"),
    ("播放音乐", "TASK"),
    ("你打开东西是对的", "CHAT"),
    ("我都吐槽一句啊，你要干什么？", "CHAT"),
    ("你刚才为什么要打开它", "CHAT"),
    ("你现在在下了吗", "STATUS"),
    ("弄好了没", "STATUS"),
    ("你在弄什么？", "STATUS"),
    ("哈喽，你现在还在帮我填表吗？好像没有更新了", "STATUS"),
    ("你怎么忽然不说话了？", "STATUS"),
    ("怎么忽然没声音了，你在找什么", "STATUS"),
    ("你刚刚在做啥任务，跟我说一下", "STATUS"),
    ("看看我屏幕上这个报错", "SCREEN"),
    ("我刚才在干嘛来着", "SCREEN"),
    ("今天天气怎么样", "CHAT"),
    ("你帮我看看刚才找出来的岗位，哪些匹配度高", "TASK"),
    ("帮我把本地的和那几个最匹配的都投一下吧", "TASK"),
    ("我要那几个高匹配的，给我讲一下有什么特别要求", "TASK"),
    ("你去角落待着吧", "MOVE"),
    ("别挡着我看视频", "MOVE"),
    ("先别听我说话了，我要开会", "SLEEP"),
    ("你安静一会儿，别理我", "SLEEP"),
    ("（有操作在等用户确认）\n最后一句：可以，帮我点一下", "APPROVE"),
]


async def classify(text: str, context: str = "", waiting_approval: bool = False) -> str | None:
    """Return one of LABELS, or None if the model could not be asked."""
    messages = [{"role": "system", "content": _SYSTEM}]
    for q, a in _EXAMPLES:
        messages += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    head = []
    if context:
        head.append(f"（之前的对话：\n{context}）")
    if waiting_approval:
        head.append("（有操作在等用户确认）")
    user = ("\n".join(head) + f"\n最后一句：{text}") if head else text
    messages.append({"role": "user", "content": user})
    answer = await cloud.small(_SYSTEM, _EXAMPLES, user, 12)
    if answer is None and not cloud.usable():
        answer = await _local(messages, 4)
    if answer is None:
        return None
    answer = answer.strip().upper()
    label = next((l for l in LABELS if answer.startswith(l)), "CHAT")
    if label == "APPROVE" and not waiting_approval:
        label = "TASK"
    logger.info(f"[deskpet] intent '{text[:30]}' -> {label} ({answer})")
    return label


_MOVES = ["walk:left", "walk:right", "walk:top", "walk:bottom", "walk:center",
          "walk:top_left", "walk:top_right", "walk:bottom_left", "walk:bottom_right",
          "nudge:left", "nudge:right", "nudge:up", "nudge:down",
          "hide:left", "hide:right", "hide:bottom", "hide:top", "vanish", "come_out"]


async def _local(messages: list, max_tokens: int) -> str | None:
    """The backup: the local model. None if it could not be asked either."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:  # includes loading it, when it was not in memory
            r = await client.post(OLLAMA_URL, json={
                "model": MODEL, "messages": messages, "stream": False, "think": False,
                "keep_alive": KEEP_ALIVE, "options": {"temperature": 0, "num_predict": max_tokens},
            })
            return r.json().get("message", {}).get("content", "").strip()
    except Exception as e:
        logger.warning(f"[deskpet] intent model unavailable: {e}")
        return None


async def _ask(system: str, examples: list, text: str, max_tokens: int) -> str | None:
    answer = await cloud.small(system, examples, text, max(max_tokens * 3, 24))
    if answer is not None:
        return answer
    if cloud.usable():
        return None  # the cloud is there, this one question just got no answer: no reason to load the local model
    messages = [{"role": "system", "content": system}]
    for q, a in examples:
        messages += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
    messages.append({"role": "user", "content": text})
    return await _local(messages, max_tokens)


async def one_line(text: str) -> str:
    """One short spoken sentence about what another Claude window just reported."""
    if not text:
        return ""
    system = ("把下面这段工作汇报压缩成一句口语中文，二十五个字以内，只说结果或做到哪了；只有原文明确写了要用户做什么，才提用户要做的事，不要自己编「待您确认」，"
              "不要提 Claude，不要路径和英文缩写以外的代码。"
              "学校、公司、人名等专有名词不准缩写或改成别的名字：要么照原文英文写，要么用完整准确的中文全称"
              "（比如 Ohio University = 俄亥俄大学，不是俄亥俄州立大学）；字数不够就不提这个名字。")
    answer = await _ask(system, [], text[-1500:], 60)
    return (answer or "").split("\n")[0][:60]


async def pick_moves(text: str) -> list:
    """Body commands for a move request the rules couldn't parse."""
    system = (
        "把用户让桌宠做的身体动作翻译成命令，按顺序用空格分开，只能用这些命令：\n" + " ".join(_MOVES) +
        "\n躲 = hide（露半个头），隐身/消失 = vanish（完全看不见），出来/回来 = come_out，往某边挪一点 = nudge。"
        "先走到某处再躲，就先写 walk 再写 hide。"
        "没有身体动作就输出 none。"
    )
    examples = [("去右上角躲着", "walk:top_right hide:right"), ("你去角落待着吧", "walk:bottom_right"),
                ("别挡着我", "hide:bottom"), ("消失一会儿", "vanish"), ("回来吧", "come_out"),
                ("往左挪一点", "nudge:left"), ("藏到上面去", "hide:top"), ("别躲了", "come_out"),
                ("你好", "none")]
    answer = await _ask(system, examples, text, 16)
    if not answer:
        return []
    cmds = [w for w in answer.split() if w in _MOVES]
    logger.info(f"[deskpet] moves for '{text[:30]}' -> {cmds} ({answer})")
    return cmds


async def is_wake(text: str) -> bool:
    """While she isn't listening: is the user calling her back?"""
    system = ("桌宠现在处于不听模式。判断用户这句话是不是在叫桌宠回来、让她重新听自己说话"
              "（叫她名字 Claude/小克，或者说出来吧、醒醒、可以听了、回来吧）。"
              "用户和别人说话、自言自语、开会发言都不算。只回答 是 或 否。")
    examples = [("Claude", "是"), ("小克你出来吧", "是"), ("好了，你可以听了", "是"),
                ("这个季度的指标我们再看一下", "否"), ("嗯对，我同意你的看法", "否")]
    answer = await _ask(system, examples, text, 3)
    return bool(answer and answer.startswith("是"))
