"""[deskpet] What happens to a user message before the chat model answers.

Since 2026-11-24 her chat model (Claude, see brain.py and pet_llm.py) reads the sentence itself,
decides what it is for and answers in her own words: `route` then only notes the turn and returns
None. The keyword rules and the small-model classifier below are the backup path, `route_local`,
used while the cloud can't be reached; it returns a short reply to speak right away, or None to
let the (local) chat model answer.
"""

import asyncio
import json
import random
import re

from . import audio_drop, brain, cloud, memory, claude_bridge, hub, intent_llm, listening, pictures as given_pictures, screen, state
from .config import INTENT_LOG
from ..conversations.deskpet_intent import detect_pet_commands

_STATUS = re.compile(
    r"(什么情况|进度|进展|到哪了|怎么样了|好了没|好了吗|完了吗|搞定了吗|弄好了吗|做好了吗|做完了吗|"
    r"(在|有在|有没有在|开始).{0,6}(做|弄|下|找|装|搞|查|办)|在干嘛|在做什么|干什么呢|还在吗)"
)
_HEALTH = re.compile(r"(连上了吗|接上了吗|连着吗|有没有连|你能操作电脑吗|你能动电脑吗|状态检查|自检)")
# short replies that continue the last task (answering its question / correcting it)
_FOLLOW = re.compile(r"(^(好|好的|可以|行|确定|装吧|下吧|装|下|要)[吧啊。！!]*$|就行|就好|就可以|推荐|那个|这个|选|装|下载|换|不是|不对|我要|我的意思|随便|都行|免费|对的|是的|嗯)")
_SCREEN = re.compile(
    r"(屏幕|画面|这个页面|这页|这个窗口|这个报错|这个错误|这段代码|我在看|看看我|看一下我|帮我看看这|截图|我刚才在|我刚刚在)"
)
_ACTION = re.compile(
    r"(打开|开一下|运行|启动|关掉|关闭|关了|关一下|关到|叉掉|插掉|退出|新建|创建|删除|修改|改一下|改成|整理|搜索|搜一下|查找|找一下|找找|"
    r"安装|执行|部署|提交|推送|备份|下载|写一个|写个|生成|投递|跑一下|装一下|配置|更新|检查一下|"
    r"调到|调高|调低|调大|调小|静音|音量|声音|亮度|"
    r"点一下|点击|点开|填一下|填写|输入|切换到|切到|最小化|最大化|播放|暂停|发送|移到|挪到|复制|粘贴|截个图|"
    r"看一下.*(文件|文件夹|项目|日志))"
)
_ASK_CC = re.compile(r"(claude ?code|让claude|交给claude|cc去)", re.I)
_APPROVE = re.compile(r"^(好的?|可以|行|确认|允许|同意|批准|没问题|点确认|帮我点确认|帮我确认|你帮我确认|帮我允许)[吧啊呀。！!]*$|(帮我|替我|你去).{0,4}(确认|允许|同意|批准)")
_REQUEST = re.compile(r"(帮我|给我|替我|你去|你帮|去把|把|请|麻烦)")
# clear computer actions that count even without "帮我"
_STRONG = re.compile(r"(打开|运行|启动|关掉|关闭|关一下|叉掉|插掉|安装|部署|备份|下载|删除|投递|投一下|投了|投掉|都投|帮我投|apply|申请一下|网申|静音|音量|整理.*(文件|文件夹)|搜索岗位)")


# work on results/files: summarize, pick, compare what another window found
_WORK = re.compile(r"(投|apply|申请|简历|求职信|cover letter|总结|汇总|筛选|筛一下|挑出|挑一下|分析|对比|比较|列出|列一下|读一下|讲一下|匹配度|适合度|"
                   r"那边|刚刚找|刚才找|找出来|搜出来|岗位|职位|邮件|日报|报告|结果)")

_HOLD = re.compile(r"(先别(提交|交|投|点)|别提交|不要提交|先不要(提交|交|投)|等我看看|我先看看|我看一下再)")
_QUESTION = re.compile(r"(对吧|是吧|是不是|对不对|有没有|吗[?？。]?$|[?？]$)")
# said with a picture: "have a look / what is this / what does it say" - looking, not a job
_LOOK = re.compile(r"(看看|看一下|看下|看一眼|瞧瞧|什么意思|啥意思|是什么|是啥|怎么回事|写了什么|写的什么|写了啥|认识|认得)")

# --- tiers for the hybrid router ---
# sure status questions (answered from real state, no model needed)
_STATUS_SURE = re.compile(r"(还在.{0,8}(做|弄|填|投|帮我)|怎么.{0,4}不说话|没.{0,4}(更新|动静|消息)|在找什么|在查什么|在写什么|怎么没声音|怎么不说话|在弄什么|在做什么|在干什么|在忙什么|在弄啥|在做啥|刚刚在做|刚才在做|刚刚在弄|刚才在弄|做啥任务|什么任务|现在什么情况|什么情况了|进度怎么样|进展怎么样|到哪了|弄好了吗|做好了吗|做完了吗|搞定了吗|好了没|装好了吗|下好了吗)")
# a status question about some program/thing (not about her own work): "备份脚本现在运行的怎么样", "游戏还在跑吗"
_ABOUT_THING = re.compile(r"([a-z]{2,}|运行|跑得|跑的|还在跑|脚本|程序|软件|游戏|服务器|模拟器|下载|网页|那边)")
# the user calls off what she is doing / says it wasn't a request
_CANCEL = re.compile(r"(不是让你做|不是叫你|我只是说说|我就随便说|别做了|不用做了|别弄了|不用弄了|停下|停一下|取消|算了|不用了)")
# signs a sentence is a comment/question about an action rather than a request
_COMMENT = re.compile(r"(为什么|为啥|是不是|对不对|对的|对吗|干嘛要|怎么又|吐槽|你刚才|你之前|觉得|好不好|是对的|做得)")
# a clear command: request word near the start, then an action
_COMMAND_START = re.compile(r"^[呃嗯啊哎那你，, ]{0,4}(帮我|给我|替我|麻烦你?|你去|你帮我|请你?|把)")


def _rule_label(t: str, recent: bool, waiting: bool) -> str | None:
    """Label for clear-cut cases; None means 'ask the model'."""
    action = bool(_ACTION.search(t) or _STRONG.search(t))
    if waiting and _APPROVE.search(t):
        return "APPROVE"
    if _STATUS_SURE.search(t) and not _COMMAND_START.search(t):
        return "STATUS"
    if _ASK_CC.search(t):
        return "TASK"
    if _COMMENT.search(t) and not _COMMAND_START.search(t):
        return None  # sounds like a comment: let the model judge
    if _COMMAND_START.search(t) and action:
        return "TASK"
    if _REQUEST.search(t) and _WORK.search(t):
        return "TASK"
    has_any = action or _WORK.search(t) or _STATUS.search(t) or _SCREEN.search(t) or (recent and _FOLLOW.search(t))
    if not has_any and not _REQUEST.search(t):
        return "CHAT"  # nothing computer-related and not asking for anything
    return None


def _is_task(t: str) -> bool:
    if _ASK_CC.search(t):
        return True
    return bool((_ACTION.search(t) or _WORK.search(t)) and _REQUEST.search(t)) or bool(_STRONG.search(t))


async def _approve(session) -> None:
    """Ask Claude Code to press 'allow' in the user's own Claude Code window that is waiting."""
    where = session.project or session.cwd or "某个项目"
    prompt = (
        f"用户自己开着的 Claude Code（项目：{where}，目录：{session.cwd}）正在等用户确认权限，"
        f"提示是：「{session.message}」。用户口头同意了，让你替他点确认。\n"
        "用 windows-mcp：找到那个正在等待确认的终端窗口（标题或内容里有 Claude Code、目录名），切到前台，"
        "Snapshot/Screenshot 看清它到底要执行什么。\n"
        "只有当它要做的事是安全的、常规的开发操作时，才选择「允许这一次」（通常按 1 或回车），不要选「以后都允许」。\n"
        "如果它要删除文件、git push、改系统设置、付款、发消息，或者你看不清它要干嘛，就不要点，"
        "在 SAY 里告诉用户它要做什么、请他自己决定。\n"
        "做完把焦点留在那个终端上。最后一行 SAY: 说你允许了什么或者为什么没点。"
    )
    summary = await claude_bridge.run(prompt, "替你确认", speak_progress=False)
    session.waiting = False
    await hub.say(summary, remember=True)


async def _answer_screen(question: str) -> None:
    path = await screen.screenshot()
    timeline = screen.recent_text(30)
    prompt = (
        f"用户问了一个关于他电脑屏幕的问题：「{question}」\n\n"
        f"当前屏幕截图：{path or '（截图失败）'}（用 Read 工具打开这张图看）\n"
        f"最近 30 分钟的前台窗口记录：\n{timeline}\n\n"
        "根据截图和记录回答。只需要回答问题，不要做任何修改操作。"
    )
    task = state.Task(id=0, request="看屏幕")
    state.tasks.append(task)
    task.summary = await claude_bridge.run(prompt, "看屏幕", task, speak_progress=False)
    task.status = "done"
    task.finished = state.time.time()
    await hub.say(task.summary, remember=True)


def _log(text: str, label: str, source: str) -> None:
    """Every decision goes to agent/intent_log.jsonl so misroutes can be reviewed and fixed."""
    try:
        with open(INTENT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": state.time.strftime("%Y-%m-%d %H:%M:%S"), "text": text,
                                "label": label, "by": source}, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def _act(cmds: list) -> None:
    """Move her (the server's copy of hidden/vanished follows along)."""
    if cmds:
        listening.note_moves(cmds)
        await hub.send_action(cmds)


async def listen_gate(text: str) -> str | None:
    """'Not listening' mode. None = handle normally; "" = ignore silently; other text = say it and stop.

    Moves said in the same breath are never dropped any more:
      asleep + "小克，去左边"            -> wakes up and walks there
      asleep + "小克，隐身 / 去躲着"      -> goes away and stays asleep (going away is no reason to wake)
      asleep + "小克，先去右上角，然后继续睡" -> walks there and stays asleep
      asleep + "Claude 出来一下"          -> wakes up and comes out of hiding / back into sight
      awake  + "隐身吧，不要听我说话"      -> sleeps and vanishes
    """
    cmds = detect_pet_commands(text)
    if listening.asleep:
        named = bool(listening.NAME.search(text))
        if named and cmds:
            going_away = all(c == "vanish" or c.startswith("hide") for c in cmds)
            if listening.STAY_ASLEEP.search(text) or (going_away and not listening.WAKE_WORDS.search(text)):
                _log(text, "ASLEEP-COMMAND", "rule")
                await _act(cmds)
                return ""
            return await _wake(text, "rule+move", cmds)
        if named and listening.STAY_ASLEEP.search(text):
            # "小克，帮我查一下，然后继续睡": do what she's told but keep not listening
            _log(text, "ASLEEP-COMMAND", "rule")
            return None
        # the small model is asked only about what could be a call to her: a long sentence with
        # neither her name nor a come-back word (a meeting, a phone call) stays on this PC
        if listening.wake_by_rule(text) or (listening.maybe_calling(text) and await intent_llm.is_wake(text)):
            return await _wake(text, "rule/model", cmds)
        _log(text, "IGNORED", "asleep")
        return ""
    if listening.is_noise(text):
        _log(text, "IGNORED", "noise")
        return ""
    if not cloud.usable() and listening.sleep_by_rule(text):
        return await _go_to_sleep(text, "rule")  # (with the cloud up she judges this herself: brain.stop_listening)
    # (no "only her name counts while media plays" rule any more: echo_gate already dropped
    # what the mic heard from this PC's speakers before it was transcribed)
    return None


async def _wake(text: str, source: str, cmds: list) -> str:
    listening.asleep = False
    # called back while invisible: being called back means "be here", so she reappears too
    if listening.vanished and "come_out" not in cmds:
        cmds = ["come_out", *cmds]
    _log(text, "WAKE", source)
    await _act(["wake", *cmds])
    if cmds:
        return random.choice(["来了。", "好，我在。", "嗯，这就来。"])
    return random.choice(["我在。", "嗯，回来了。", "在呢，你说。"])


async def _go_to_sleep(text: str, source: str) -> str:
    """Stop listening; a move in the same sentence ("隐身吧，别听我说话") is done too."""
    listening.asleep = True
    cmds = detect_pet_commands(text)
    _log(text, "SLEEP", source)
    await _act(["sleep", *cmds])
    if "vanish" in cmds:
        return "好，我先不出现也不听了。叫我名字我再回来。"
    if any(c.startswith("hide") for c in cmds):
        return "好，我躲起来，先不听了。叫我名字我再出来。"
    return "好，我先不听了。叫我名字我再回来。"


async def route(text: str, pictures: list[str] | None = None, about_picture: bool = False) -> str | None:
    """pictures: files of the pictures given with these words. about_picture: the words come with a
    picture, or with one of a moment ago that they still talk about."""
    pictures = pictures or []
    turn = brain.begin(text, pictures, about_picture)
    if not text:
        return None
    context = state.recent_dialogue(6)
    state.remember("用户", text + given_pictures.mention(pictures))

    stopped = audio_drop.cancel_request(text.strip().lower())  # "别总结了" while she listens to a dropped recording
    if stopped:
        brain.take()
        _log(text, "CANCEL", "audio")
        return stopped
    if cloud.usable():
        return None  # her own model reads it, decides what it is for and answers (brain.py)

    turn.routed = True
    quick = await route_local(text, pictures, about_picture, context)
    if quick:
        brain.take()
        quick = cloud.outage_notice() + quick
    return quick


async def route_local(text: str, pictures: list[str] | None = None, about_picture: bool = False,
                      context: str | None = None) -> str | None:
    """The backup path (cloud unreachable): keyword rules plus the small model decide. This was
    all of `route` until 2026-11-24. The user's words are already in state.dialogue."""
    t = text.strip().lower()
    if context is None:
        context = "\n".join(state.recent_dialogue(7).split("\n")[:-1])
    pictures = pictures or []
    job = text + given_pictures.note(pictures)  # what the executor is given: it can open the files
    last = state.last_task()
    recent = last is not None and (
        last.status == "running" or (last.finished and state.time.time() - last.finished < 300)
    )
    waiting = [x for x in state.sessions.values() if x.waiting and state.time.time() - x.updated < 1800]

    if _HEALTH.search(t):
        _log(text, "HEALTH", "rule")
        return claude_bridge.health()

    # long-term memory: "记住…" / "以后都…" / "忘掉…" / "你记得我什么"
    confirmed = memory.take_pending(t)
    if confirmed:
        _log(text, "MEMORY", "remember-confirmed")
        return f"好，记住了：{confirmed}。"
    if memory.RECALL.search(t):
        _log(text, "MEMORY", "recall")
        got = [re.sub(r"（\d{4}-\d{2}-\d{2}）$", "", e) for e in memory.items()]
        if not got:
            return "我现在还没记着什么。你说「记住……」，我就记下来。"
        return f"我记着这{len(got)}条：" + "；".join(got[:6]) + ("……还有几条，记在我的记忆文件里。" if len(got) > 6 else "。")
    if memory.FORGET.search(t) and len(t) <= 40:
        _log(text, "MEMORY", "forget")
        gone = memory.forget(text)
        return f"好，「{gone}」这条我忘掉了。" if gone else "我没找到你说的那条，你再说具体点？"
    if memory.REMEMBER.search(t) and not _CANCEL.search(t):
        fact = memory.add(text)
        if fact:
            _log(text, "MEMORY", "remember")
            return random.choice([f"好，记住了：{fact}。", f"嗯，我记下了，以后都这样。"])
    if memory.SOFT_RULE.search(t) and not _CANCEL.search(t) and len(t) <= 60:
        fact = memory.offer(text)
        if fact:
            _log(text, "MEMORY", "offer")
            return f"要我把「{fact}」记成以后一直照做的规矩吗？说「好」我就记下。"

    # calling it off: stop the running task (and note the misroute if it had only just started)
    if _CANCEL.search(t) and last is not None and last.status == "running":
        claude_bridge.cancel(last)
        _log(text, "CANCEL", "rule")
        if state.time.time() - last.started < 90:
            _log(last.request, "CHAT", "user-correction")
        return random.choice(["好，那我不弄了。", "嗯，停下了。", "知道了，不做了。"])

    # "先别提交 / 等我看看" while she is working: stop right where she is (the page stays as it is)
    if _HOLD.search(t) and last is not None and last.status == "running":
        claude_bridge.cancel(last)
        _log(text, "CANCEL", "hold")
        rest = _HOLD.sub("", t).strip(" ，,。.、")
        if len(rest) >= 8:  # "先别提交，先把简历重排一下再投" carries a new instruction: do it now
            claude_bridge.start(
                f"刚才的任务我让你停下了（没提交的先别提交）。现在按我的新要求做：{job}",
                follow_up_of=last if last.session_id else None,
            )
            return "好，没提交的先停住，我按你说的改完再接着弄。"
        return "好，我停在这儿了，没提交。你看完跟我说。"

    # She just asked something and this is a short reply: it answers her question.
    # (Doesn't need the model, which can be too slow while a game holds the GPU.)
    if (last is not None and last.status != "running" and last.finished
            and state.time.time() - last.finished < 900 and len(t) <= 25
            and last.summary.rstrip().endswith(("？", "?", "吗", "吗。"))
            and not _QUESTION.search(t)):
        _log(text, "TASK", "answer-to-her-question")
        claude_bridge.start(job, follow_up_of=last if last.session_id else None)
        return random.choice(["好。", "嗯，知道了。"])

    # The local model decides. Rules only: (a) stand in when the model is down,
    # (b) catch a clear command the model called chat (missing a task costs more than an extra check).
    rule = _rule_label(t, recent, bool(waiting))
    if pictures and rule == "CHAT":
        # a picture with words that ask for nothing on the computer ("这是什么", or none at all):
        # she looks at it herself, no need to ask the model what it is for
        _log(text, "CHAT", "picture")
        return None
    label = await intent_llm.classify(text, context, waiting_approval=bool(waiting))
    source = "model"
    if pictures and rule is None and not (label == "TASK" and _REQUEST.search(t) and not _LOOK.search(t)):
        # A picture leaves this PC only with a job for the executor, so the small model's word alone
        # is not enough for that: the words must ask for something and not just for a look
        # ("帮我看看这个报错是什么意思" is hers to answer). A missed job costs one more sentence.
        _log(text, "CHAT", "picture")
        return None
    if label == "CHAT" and rule == "TASK":
        # a question about the work ("这些都是高匹配的对吧?") gets looked up and answered, not turned into a job
        label = "STATUS" if _QUESTION.search(t) else "TASK"
        source = "rule-override"
    if label == "SCREEN" and _is_task(t):
        # "帮我重新开一下那几个网页" asks her to do something, not just to look at the screen
        label, source = "TASK", "rule-override"
    if label in ("TASK", "CHAT") and rule == "STATUS":  # "你在弄什么" asks about the work: answer from real state
        label, source = "STATUS", "rule-override"
    if label == "SCREEN" and about_picture:
        # "帮我看看这个报错" said with a picture is about the picture, not the screen: she looks herself
        label, source = "CHAT", "picture"
    if label is None:
        source = "fallback"
        if rule is not None:
            label = rule
        elif _SCREEN.search(t):
            label = "SCREEN"
        elif _STATUS.search(t):
            label = "STATUS"
        elif _is_task(t):
            label = "TASK"
        else:
            label = "CHAT"
    _log(text, label, source)

    if label == "APPROVE" and waiting:
        session = max(waiting, key=lambda x: x.updated)
        asyncio.create_task(_approve(session))
        return "好，我先看看它要做什么，没问题就帮你点。"
    if label == "SLEEP" and listening.WAKE.search(t) and not re.search(r"(睡|别听|不要听|休息)", t):
        label = "CHAT"  # "小克小克醒醒" calls her, it never means "go to sleep"
    if label == "SLEEP":
        return await _go_to_sleep(text, source)
    if label == "MOVE":
        cmds = await intent_llm.pick_moves(text)
        listening.note_moves(cmds)
        if cmds:
            await hub.send_action(cmds)
        return None  # she still answers normally; her own tags are ignored right after a move
    if label == "SCREEN":
        asyncio.create_task(_answer_screen(text))
        return random.choice(["我看看。", "嗯，让我看一眼。", "好，我看一下你的屏幕。"])
    if label == "STATUS":
        running = any(x.status == "running" for x in state.tasks)
        # "备份脚本现在运行的怎么样" asks how a program is doing now, not how her own task list looks:
        # an old task summary is no answer to that, so she goes and looks.
        if _ABOUT_THING.search(t) and not running:
            asyncio.create_task(claude_bridge.ask(job))
            return random.choice(["我去看一下它现在的情况。", "好，我看看它现在怎么样。"])
        quick = state.describe()
        # more than "好了没": also let Claude look it up and answer properly
        if (len(t) > 12 or _ABOUT_THING.search(t)) and not re.fullmatch(r".{0,6}(好了没|好了吗|进度呢|怎么样了|在跑吗|在做吗).{0,4}", t):
            asyncio.create_task(claude_bridge.ask(job))
            quick += "具体的我查一下再告诉你。"
        return quick
    if label == "TASK":
        # if the last task ended by asking the user something, continue that same session
        asked = recent and last.status != "running" and last.session_id and (
            last.summary.rstrip().endswith(("？", "?", "吗", "吗。"))
        )
        return claude_bridge.start(job, follow_up_of=last if asked else None)
    return None
