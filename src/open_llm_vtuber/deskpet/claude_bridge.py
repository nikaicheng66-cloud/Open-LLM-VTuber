"""[deskpet] Hand computer tasks to Claude Code (`claude -p`, uses the user's subscription login).

To the user it should feel like the pet herself is doing the work: she acknowledges,
mentions what she is doing every so often, and reports the result in her own voice.
"""

import asyncio
import itertools
import json
import os
import random
import re
import shutil
import subprocess
import time

from loguru import logger

from . import hub, intent_llm, state
from . import memory
from .config import AGENT_DIR, DENY_TOOLS, HOME, MCP_CONFIG, TASK_TIMEOUT_SECONDS

_ids = itertools.count(1)
_procs: dict[int, asyncio.subprocess.Process] = {}  # running tasks, for cancelling

SYSTEM_APPEND = (
    "你是桌宠「Claude」本人，在替用户操作他的 Windows 电脑。"
    "项目名称和路径见当前目录的 CLAUDE.md 和 projects.json。"
    "做完后，回复的最后一行必须以「SAY:」开头，用第一人称、一两句简短自然的中文口语告诉用户结果"
    "（比如「SAY: 微信关掉了，音量也调到零了。」），不要提到 Claude、Claude Code、终端、窗口里的另一个 AI、工具或脚本，别的项目用它的中文名称呼（比如「自动投递那边」），不要 markdown、路径和代码。给用户的回复（正文和 SAY）一律用中文写，项目文件里是英文也一样。SAY 是要念出来的：用中文说，公司名、岗位名、技术词保留英文原词就行（她能说英文）。"
    "SAY 只说结果，不要复述你中间看了什么、点了什么。SAY 最多三句、八十个字以内；需要他决定的事一次最多问一件，其余写在正文里。但只要还有事在等他（要他登录、要他确认、网站打不开要他处理、要他自己做的测评等），SAY 里必须点到：几件、分别是哪几家、大概要他做什么（比如「还有三家等你：A 公司要你登录，B 公司网站打不开，C 公司等你看回答」），不能只写在正文里。"
    "【投递】找岗位、筛选、改简历、写求职信、网上提交，全部你自己做，照那个项目目录里的 CLAUDE.md 和技能流程来，不要转交给别的终端窗口。"
    "改完文件后自己检查一遍生成的 PDF 内容对不对（比如求职信是不是这家公司的）。"
    "【数字要对得上】说「找到几个」「做了几份」「投了几家」之前，先把清单逐条数一遍，说出的数字必须等于列出的条数（同一岗位在两个地点发布算一条，并写明）。"
    "他用一个范围下指令时（「之前搜到没投的高分的」「今天那22个」「这些都」），从追踪表和岗位存档里按状态和分数实际筛一遍，不要只拿上次回复里列过的那几条；追踪表里已投、已拒、定了不投的先去掉。"
    "说数字时说清是哪一种、按哪个字段数的；同一岗位重复发布（比如两个城市）照样数进去，但要写明是几条重复。"
    "做完一批时，正文第一段先对账：他要的一共几个 = 做好几个 + 没做几个（每个没做的写原因）；SAY 里也要说「你要的 N 个里做好了 X 个，Y 个没做」，不能只报做好的数。"
    "表单里的文字题（为什么选这家、项目描述、短文、限字数的回答）和期望薪资不算编造，也不是停下的理由：按投递项目的档案自己写、按项目的薪资规则自己填，直接提交，在正文里列出写了什么。"
    "【接着他登录好的页面做】他说「登好了」「验证好了」「帮你登录好了」时，先用 tabs_context 找到他刚登录的那个标签页，直接在那一页往下填；不要新开标签页、不要重新打开申请链接、不要刷新，否则会重新触发登录或验证码。"
    "【用完关标签页】每次任务结束前，用 tabs_close_mcp 关掉你这次在 Chrome 里开的所有标签页（标签组会跟着消失），不要把空的或做完的标签组留在标签栏上；只有需要他接着操作的那一页（比如要他登录、输验证码）可以留着，并在 SAY 里说清楚是哪一页。"
    "【要他在页面上动手时，不要结束任务，就地等他】（Chrome 插件只能操作你这次任务自己开的标签页，任务一结束他在那页做的事你就接不上了。）遇到要他注册、登录、做人机验证、或你填不进去的验证码时：把那一页留在你自己的标签页里，单独输出一行「ASK: 一句中文，说清是哪一家、要他做什么」（这一行会马上念给他），然后每隔约 15 秒（Bash 里 sleep 15）看一次那一页，最多等 15 分钟；看到他做完了，就在同一页接着往下填、提交。超过 15 分钟没动静再结束任务，并在 SAY 里说明。投一批岗位时：先投完不需要他的；需要他的那几家，把页面一次全开好（一家一个标签页），用一条 ASK 一次说清，再挨个等、挨个投完。账号密码由他自己填，你不注册、不设密码。"
    "【投递直接提交】他让你投递时，简历和求职信做好并自己检查过 PDF 之后，不用等他审核，直接去网上提交；表单里遇到要他动手的事，按上面【要他在页面上动手时】用 ASK 叫他、就地等。"
    "用户可能同时在用电脑（打游戏、看视频）：网申只用 Chrome 工具，不要用 Windows-MCP 动鼠标键盘，也不要叫用户停下手上的事。"
    "网上提交用 Claude in Chrome（mcp__claude-in-chrome 工具）：Chrome 没开就先用 PowerShell 的 Start-Process chrome 打开。"
    "提交时遇到登录、要密码、人机验证、你取不到的验证码、要你编造的信息、或者表单里不确定怎么填的问题，不要猜：页面上要他动手的按【要他在页面上动手时】用 ASK 叫他、就地等；要他拿主意的，在 SAY 里说清楚是哪家、卡在哪一步、需要他做什么。"
    "每投完一家：先更新追踪表，再用 Python315 运行 tools/after_submit.py（自动归档、同步谷歌表格、清掉 LaTeX 临时文件），看它的输出，哪一步失败就在 SAY 里直说是哪一步、为什么。编译留下的 .aux/.log/.out 不用你删，也不要叫用户删。"
    "回答内容较长（列表、总结）时，正文照常写完整，会自动存成文件打开给用户看；SAY 里只说重点，可以说「详细的我打开给你了」，不要说「在窗口里」。"
    "安装软件或下载文件之前，必须先在 SAY 里说清楚要装/下载什么，并问用户要不要，等他同意了再做。"
    "意思不明确时不要猜，先问。"
    "如果用户这句话其实不是要你做事（只是评论、吐槽、提问或闲聊），什么都不要操作，在 SAY 里自然地接一句话就好。"
    "如果某一步需要用户确认、被权限挡住、或者有风险（删除、推送、付款、发消息），不要硬做，"
    "停下来，在 SAY 里告诉用户需要他做什么。"
    "被安全规则拦下的命令不要换个写法绕过。"
    "【优先级】上面带【】的投递规则和这些一般规则冲突时以那几条为准；除此之外的发消息、删除、推送、付款仍然一律停下问他。"
)

ACKS = [
    "好，我这就去弄。",
    "交给我吧。",
    "嗯，我来。",
    "行，等我一会儿。",
    "知道了，这就去。",
    "好嘞，马上。",
]

# what to say (at most every PROGRESS_GAP seconds) when a tool starts
# what she might say when a step starts; several phrasings so it doesn't sound like a machine
_TOOL_WORDS = [
    (("navigate", "form_input", "read_page", "get_page_text", "file_upload", "tabs_", "find"),
     ["网页这边我在弄着。", "表格我一项项在填，别急。", "在网页上忙着呢。"]),
    (("screenshot", "snapshot", "state"), ["我先瞄一眼屏幕。", "让我看看现在屏幕上是什么。"]),
    (("click", "move", "scroll", "drag"), ["我来点一下。", "稍等，我找找按钮在哪。"]),
    (("type", "shortcut", "clipboard"), ["我来输进去。", "在打字呢。"]),
    (("app", "process", "launch"), ["我去把它找出来。", "等我把程序叫出来。"]),
    (("read", "glob", "grep", "filesystem"), ["我先翻翻资料。", "让我看看之前的记录。", "我查一下文件里怎么写的。"]),
    (("edit", "write", "multiedit"), ["我动手写了。", "在改了，马上好。"]),
    (("websearch", "webfetch", "scrape"), ["我上网查查。", "我去搜搜看。"]),
]
PROGRESS_GAP = 12  # seconds between spoken progress lines
MAX_PROGRESS_LINES = 2
HEARTBEAT = 150  # long tasks: say what has actually happened every ~2.5 minutes
_last_progress = 0.0  # shared by all tasks, so two tasks never talk over each other


def ack() -> str:
    return random.choice(ACKS)


def _claude_exe() -> str | None:
    return shutil.which("claude") or shutil.which("claude.cmd")


def _spoken(result: str) -> str:
    for line in reversed((result or "").strip().splitlines()):
        if line.strip().upper().startswith("SAY:"):
            return line.split(":", 1)[1].strip()
    text = (result or "").strip().replace("\n", " ")
    return text[:60] + ("……" if len(text) > 60 else "")


def _company_from_file(path: str) -> str | None:
    """cover_acme_xxx.tex -> ('Acme', '求职信')."""
    name = os.path.basename(path).lower()
    for prefix, kind in (("cover_", "求职信"), ("main_", "简历")):
        if name.startswith(prefix):
            company = name[len(prefix):].split("_")[0]
            return f"{company.capitalize()} 的{kind}"
    return None


def _site_from_url(url: str) -> str | None:
    m = re.search(r"https?://([^/]+)", url or "")
    if not m:
        return None
    host = m.group(1).lower()
    for key, name in (("myworkdayjobs", "Workday"), ("workday", "Workday"), ("greenhouse", "Greenhouse"),
                      ("lever.co", "Lever"), ("icims", "iCIMS"), ("successfactors", "SuccessFactors"),
                      ("smartrecruiters", "SmartRecruiters"), ("linkedin", "LinkedIn")):
        if key in host:
            return name
    parts = [p for p in host.split(".") if p not in ("www", "careers", "jobs", "com", "org", "net", "io")]
    return parts[0].capitalize() if parts else None


def _words_for(tool: str, args: dict | None = None) -> str | None:
    """A natural line for a step, using what it is working on (which company, which site) when known."""
    t = tool.lower().rsplit("__", 1)[-1]
    args = args or {}
    target = _company_from_file(args.get("file_path", "")) if t in ("write", "edit", "multiedit") else None
    if target:
        return random.choice([f"在写{target}。", f"{target}我在改，快好了。"])
    if t == "navigate":
        site = _site_from_url(args.get("url", ""))
        if site:
            return random.choice([f"我打开{site}那边的页面了。", f"去{site}上看看。"])
    for keys, words in _TOOL_WORDS:
        if any(k in t for k in keys):
            return random.choice(words)
    return None


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Stop claude and everything it started. `claude` may be claude.cmd, so killing only the
    direct child would leave node (and its browser/MCP helpers) running."""
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            proc.kill()
    except ProcessLookupError:
        pass


READ_ONLY_DENY = ["Edit", "Write", "MultiEdit", "NotebookEdit", "Bash", "PowerShell",
                  "mcp__windows-mcp", "mcp__claude-in-chrome"]


async def run(prompt: str, label: str, task: "state.Task | None" = None, speak_progress: bool = True,
              resume: str = "", read_only: bool = False) -> str:
    """Run one Claude Code task and return the spoken summary.

    read_only: for questions about the work - it may look at files but the tools that change
    anything are not given to it at all (not just asked not to in the prompt).
    """
    exe = _claude_exe()
    if not exe:
        return "我这边的工具好像没装好，做不了。"
    if read_only:
        tool_args = ["--permission-mode", "default",
                     "--allowedTools", "Read", "Glob", "Grep",
                     "--disallowedTools", *READ_ONLY_DENY]
    else:
        tool_args = ["--permission-mode", "acceptEdits",
                     "--allowedTools", "Bash", "Read", "Edit", "Write", "Glob", "Grep", "WebSearch", "WebFetch",
                     "mcp__windows-mcp", "mcp__claude-in-chrome",
                     "--chrome",
                     "--disallowedTools", *DENY_TOOLS]
    args = [
        exe, "-p",
        "--output-format", "stream-json", "--verbose",
        *tool_args,
        "--add-dir", str(HOME),
        *(["--mcp-config", str(MCP_CONFIG)] if MCP_CONFIG.exists() else []),
        "--append-system-prompt", SYSTEM_APPEND + memory.prompt_block(),
        *(["--resume", resume] if resume else []),
    ]
    env = dict(os.environ, DESKPET_DELEGATED="1")
    env.pop("ANTHROPIC_API_KEY", None)  # use the subscription login, not API billing
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(AGENT_DIR),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        limit=16 * 1024 * 1024,
    )
    if task is not None:
        _procs[task.id] = proc
    proc.stdin.write(prompt.encode("utf-8"))
    await proc.stdin.drain()
    proc.stdin.close()

    result = ""
    last_spoken = time.time()  # the acknowledgement was just spoken
    spoken_count = 0
    said = set()
    notes: list[str] = []   # what it wrote between steps since the last report
    files: list[str] = []   # files it created/changed since the last report
    started = time.time()

    async def read_stream():
        nonlocal result, last_spoken, spoken_count
        global _last_progress
        async for raw in proc.stdout:
            try:
                ev = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if task is not None and ev.get("session_id"):
                task.session_id = ev["session_id"]
            if ev.get("type") == "result":
                result = ev.get("result", "") or ""
                if ev.get("is_error"):
                    logger.warning(f"[deskpet] claude error: {result[:300]}")
            elif ev.get("type") == "assistant":
                for c in (ev.get("message") or {}).get("content", []):
                    if c.get("type") == "text" and c.get("text", "").strip():
                        # "ASK: ..." = she needs the user on a page right now: say it at once, keep the task running
                        for line in c["text"].splitlines():
                            if line.strip().upper().startswith("ASK:"):
                                ask = line.strip()[4:].strip()
                                if ask and ask not in said:
                                    said.add(ask)
                                    _last_progress = last_spoken = time.time()
                                    if task is not None:
                                        task.waiting = True
                                    asyncio.create_task(hub.say(ask, remember=True))
                        notes.append(c["text"].strip()[-300:])
                        if task is not None:
                            task.notes = (task.notes + [c["text"].strip()[-300:]])[-6:]
                    if c.get("type") != "tool_use":
                        continue
                    if task is not None and task.waiting and c.get("name", "").startswith("mcp__claude-in-chrome__") \
                            and c.get("name", "").split("__")[-1] in ("form_input", "file_upload"):
                        task.waiting = False  # filling the page again: the user's part is done
                    fp = (c.get("input") or {}).get("file_path")
                    if fp and c.get("name") in ("Write", "Edit", "MultiEdit"):
                        name = os.path.basename(fp)
                        if name not in files:
                            files.append(name)
                    if c.get("name", "").startswith("mcp__claude-in-chrome") and "chrome" not in said:
                        said.add("chrome")
                        _last_progress = last_spoken = time.time()
                        applying = task is not None and re.search(r"(投|apply|申请|提交|网申)", task.request, re.I)
                        asyncio.create_task(hub.say(random.choice([
                            "材料都备好了，我开始投了。有卡住的地方我叫你。",
                            "好，开始投了。遇到要你出手的我再喊你。",
                        ] if applying else [
                            "我用 Chrome 看几个网页，不会动你的东西。",
                            "我借 Chrome 查一下网页，一会儿就好。",
                        ])))
                    words = _words_for(c.get("name", ""), c.get("input") or {})
                    if task is not None and words:
                        task.step = words.rstrip("。，")
                    if (
                        speak_progress and words and words not in said
                        and spoken_count < MAX_PROGRESS_LINES
                        and time.time() - last_spoken >= PROGRESS_GAP
                        and time.time() - _last_progress >= PROGRESS_GAP
                    ):
                        _last_progress = time.time()
                        said.add(words)
                        spoken_count += 1
                        last_spoken = time.time()
                        asyncio.create_task(hub.say(words))

    async def heartbeat():
        global _last_progress
        while True:
            await asyncio.sleep(HEARTBEAT)
            if time.time() - _last_progress < 60:
                continue  # something was said recently
            mins = int((time.time() - started) // 60)
            line = ""
            docs = []
            for f in files:
                c = _company_from_file(f) if f.endswith(".tex") else None
                if c and c not in docs:
                    docs.append(c)
            if docs:
                notes.clear()
                files.clear()
                shown = "、".join(docs[:4]) + ("等" if len(docs) > 4 else "")
                line = f"刚写好{shown}。"
                if task is not None:
                    task.progress = line
            elif notes or files:
                report = (f"改了这些文件：{'、'.join(files[-6:])}\n" if files else "") + "\n".join(notes[-4:])
                notes.clear()
                files.clear()
                line = await intent_llm.one_line("（这是还没做完的中间进展）\n" + report)
                if task is not None and line:
                    task.progress = line
            _last_progress = time.time()
            logger.info(f"[deskpet] heartbeat {mins}min: {line}")
            if line:
                await hub.say(random.choice([f"跟你说下进度，{line}", f"{line}还没完，我接着弄。", f"弄了{mins}分钟了，{line}"]))
            else:
                await hub.say(random.choice([f"还在弄呢，{mins}分钟了，没卡住。", "别担心，还在做，没停。", f"再等等，已经{mins}分钟了，在推进。"]))

    # stderr must be read while it runs: if nobody drains it and it fills the pipe buffer,
    # claude blocks on the write and the task hangs until the timeout.
    err_tail = bytearray()

    async def drain_stderr():
        async for chunk in proc.stderr:
            err_tail.extend(chunk)
            del err_tail[:-8000]

    errs = asyncio.create_task(drain_stderr())
    beat = asyncio.create_task(heartbeat()) if speak_progress else None
    try:
        await asyncio.wait_for(read_stream(), timeout=TASK_TIMEOUT_SECONDS)
        await proc.wait()
    except asyncio.TimeoutError:
        _kill_tree(proc)
        return "这个弄了太久了，我先停下来了。"
    finally:
        if beat:
            beat.cancel()
        if task is not None:
            _procs.pop(task.id, None)
        try:
            await asyncio.wait_for(errs, timeout=5)
        except (asyncio.TimeoutError, Exception):
            errs.cancel()
    if task is not None and task.status == "cancelled":
        return ""
    if not result:
        err = err_tail.decode("utf-8", "replace")
        logger.warning(f"[deskpet] claude gave no result: {err[-500:]}")
        return "这次没弄成，好像出了点问题。"
    logger.info(f"[deskpet] task finished '{label}': {result[-500:]}")
    _show_details(result, label)
    spoken = _spoken(result)
    if not _mostly_chinese(spoken):
        spoken = await _to_chinese(spoken, result)
    return spoken


def _mostly_chinese(text: str) -> bool:
    """A Chinese sentence with English names in it counts as Chinese; only a report in English doesn't."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    cjk = sum(1 for c in letters if "\u4e00" <= c <= "\u9fff")
    return cjk >= 8 or cjk / len(letters) > 0.5


async def _to_chinese(say: str, full: str) -> str:
    """The report came back in English: have a small Claude model retell it as two short Chinese sentences."""
    prompt = (
        "下面是一段工作汇报。用中文口语、第一人称、最多两句话（六十字以内）告诉用户结果和他需要做的事，"
        "公司名、岗位名可以保留英文，不要 markdown、不要路径。只输出这两句话。\n\n"
        f"汇报正文：\n{full[-2500:]}\n\n原来的总结：{say}"
    )
    exe = _claude_exe()
    if exe:
        try:
            env = dict(os.environ, DESKPET_DELEGATED="1")
            env.pop("ANTHROPIC_API_KEY", None)
            proc = await asyncio.create_subprocess_exec(
                exe, "-p", "--model", "haiku", "--output-format", "text",
                cwd=str(AGENT_DIR), env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            out, _ = await asyncio.wait_for(proc.communicate(prompt.encode("utf-8")), timeout=40)
            text = out.decode("utf-8", "replace").strip().replace("\n", "")
            if text and _mostly_chinese(text):
                return text[:120]
        except Exception as e:
            logger.warning(f"[deskpet] retelling in Chinese failed: {e}")
    line = await intent_llm.one_line(full)  # fallback: the local model
    return line or say


def _show_details(result: str, label: str) -> None:
    """Only the SAY line is spoken. A long answer (a list, a summary) is saved and opened so it can be read."""
    body = "\n".join(l for l in result.strip().splitlines() if not l.strip().upper().startswith("SAY:")).strip()
    if len(body) < 300:
        return
    try:
        folder = AGENT_DIR / "reports"
        folder.mkdir(exist_ok=True)
        path = folder / f"{time.strftime('%m%d-%H%M%S')}.md"
        path.write_text(f"# {label}\n\n{body}\n", encoding="utf-8")
        os.startfile(str(path))  # Windows: opens in the default editor
    except Exception as e:
        logger.warning(f"[deskpet] could not open details: {e}")


def _job_root():
    from .config import load_projects
    for name, path in load_projects().items():
        if name in ("自动投递", "投简历"):
            return path
    return None


def _tracker_mtime() -> float:
    """Latest change to the job project's tracker or documents (any of them changing = run the bookkeeping)."""
    root = _job_root()
    if not root:
        return 0.0
    t = 0.0
    for rel in ("job_search_tracker.csv", "cv", "cover_letters"):
        try:
            t = max(t, os.path.getmtime(os.path.join(root, rel)))
        except OSError:
            pass
    return t


_sheet_python: str | None = None


async def _python_with_gspread() -> str | None:
    """The job project's sheet sync needs gspread; find a Python on this PC that has it."""
    global _sheet_python
    if _sheet_python:
        return _sheet_python
    cands = []
    try:  # every python.exe on PATH, not just the first (the server's own venv comes first and lacks gspread)
        out = subprocess.run(["where", "python"], capture_output=True, text=True, timeout=10).stdout
        cands += [l.strip() for l in out.splitlines() if l.strip()]
    except Exception:
        pass
    cands += [x for x in (shutil.which("py"), shutil.which("python3")) if x]
    for exe in cands:
        if ".venv" in exe.lower():
            continue
        try:
            p = await asyncio.create_subprocess_exec(exe, "-c", "import gspread",
                                                     stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            if await asyncio.wait_for(p.wait(), timeout=20) == 0:
                _sheet_python = exe
                return exe
        except Exception:
            continue
    return None


async def _after_submit() -> None:
    """Safety net: archive submitted applications and sync the Google Sheet after any task that touched the tracker."""
    root = _job_root()
    exe = await _python_with_gspread() or shutil.which("python")
    if not root or not exe:
        return
    try:
        p = await asyncio.create_subprocess_exec(exe, os.path.join(root, "tools", "after_submit.py"), cwd=root,
                                                 stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await asyncio.wait_for(p.communicate(), timeout=180)
        text = out.decode("utf-8", "replace")
        logger.info(f"[deskpet] after_submit ({exe}): {text[-600:]}")
        archived = "archived:" in text
        sheet_failed = p.returncode != 0 or "skipped:" in text
        if archived and sheet_failed:
            await hub.say("新投的我归档好了，不过谷歌表格没同步上，回头我再试。", remember=True)
    except Exception as e:
        logger.warning(f"[deskpet] after_submit failed: {e}")


async def delegate(request: str, follow_up_of: "state.Task | None" = None) -> None:
    """Background: do the task, then tell the user how it went."""
    tracker_before = _tracker_mtime()
    task = state.Task(id=next(_ids), request=request)
    state.tasks.append(task)
    if follow_up_of is not None:
        prompt = f"用户接着说：「{request}」\n（这是对你上一次回复的补充或回答，继续把那件事做完。）"
        resume = follow_up_of.session_id
    else:
        prompt = (
            f"用户刚才对桌宠说：「{request}」\n\n"
            f"最近的对话（帮你理解他指的是什么，语音识别可能有错字）：\n{state.recent_dialogue()}\n\n"
            f"{state.other_windows()}"
            "照他的意思去做。意思明确就直接做；意思不明确、或者要安装/下载东西时，先在 SAY 里简短地问一句。"
        )
        resume = ""
    try:
        task.summary = await run(prompt, request[:20], task, resume=resume)
        if task.status == "cancelled":
            return
        task.status = "done"
    except Exception as e:
        logger.exception(f"[deskpet] delegate failed: {e}")
        task.summary = "这次没弄成，出了点问题。"
        task.status = "failed"
    task.finished = time.time()
    state.remember("我", task.summary)
    await hub.say(task.summary, remember=True)
    if _tracker_mtime() != tracker_before:
        asyncio.create_task(_after_submit())


async def ask(question: str) -> None:
    """A question about the work (what's done, where files are): answered by Claude, read-only."""
    prompt = (
        f"用户问：「{question}」\n\n最近的对话：\n{state.recent_dialogue()}\n\n"
        f"{state.other_windows()}"
        "只回答问题，需要的话去项目目录里查文件、追踪表、生成的简历和求职信来确认；问的是某个程序或脚本现在怎么样，就去看它最新的日志，"
        "按日志里最后的时间说它现在的状态，别拿之前任务的总结当成现在。不要修改任何东西，不要开始新的工作。"
        "（这次你只有读文件的工具；如果回答需要动手做事，就在 SAY 里说清楚，让他再吩咐一次。）"
    )
    task = state.Task(id=next(_ids), request="回答问题")
    task.status = "answering"  # not counted as running work
    summary = await run(prompt, "回答问题", task, speak_progress=False, read_only=True)
    if summary:
        state.remember("我", summary)
        await hub.say(summary, remember=True)


_queued: dict[int, list[str]] = {}  # task id -> requests that arrived while it was running


_CONSTRAINT = re.compile(r"(跳过|别投|不要投|不用投|不投|别申请|不要申请|只投|只要|只挑|除了|排除|换成|改成|别弄|不要弄|别用|不要用)")


def start(request: str, follow_up_of: "state.Task | None" = None) -> str:
    """Start a task and return what to say.

    While one is running, new requests are never allowed to kill it (that threw away finished
    work). Only if it has just started and done nothing yet is it restarted with the added words;
    otherwise everything said meanwhile is collected and done as one follow-up right after it.
    """
    running = [t for t in state.tasks if t.status == "running" and t.request != "看屏幕"]
    if running and follow_up_of is None:
        cur = running[-1]
        untouched = time.time() - cur.started < 20 and not cur.step and not cur.notes
        if untouched:
            cancel(cur)
            asyncio.create_task(delegate(f"{cur.request}；{request}"))
            return random.choice(["好，我按这个一起弄。", "嗯，知道了。"])
        if cur.waiting and len(request) <= 16 and re.search(r"(好了|好啦|登好|登录好|注册好|弄好|填好|输好|可以了|完成了|ok)", request, re.I):
            return random.choice(["好，我看到了就接着往下填。", "嗯，我在那一页接着弄。"])
        if _CONSTRAINT.search(request):
            # A rule for the work in progress (skip X, only Y) must apply now, not after it.
            cancel(cur)
            asyncio.create_task(delegate(
                f"刚才这件事被我打断了，请接着做，但从现在起加上新要求：{request}。"
                f"原来的要求是：{cur.request}。已经做完、已经提交的不要重做，先查清楚做到哪了再继续。",
                follow_up_of=cur if cur.session_id else None,
            ))
            return random.choice(["好，我按这个要求接着弄。", "知道了，马上按这个改。"])
        waiting = _queued.setdefault(cur.id, [])
        waiting.append(request)
        if len(waiting) == 1:
            asyncio.create_task(_after(cur))
        return random.choice(["好，记下了，手上这件弄完就做。", "知道了，这件做完马上接着弄。"])
    asyncio.create_task(delegate(request, follow_up_of))
    return ack()


async def _after(cur: "state.Task") -> None:
    while cur.status == "running":
        await asyncio.sleep(2)
    requests = _queued.pop(cur.id, [])
    if not requests:
        return
    text = requests[0] if len(requests) == 1 else "；".join(f"（{i + 1}）{r}" for i, r in enumerate(requests))
    await delegate(text, follow_up_of=cur if cur.session_id else None)


def cancel(task: "state.Task") -> None:
    """Stop a running task (the user called it off)."""
    task.status = "cancelled"
    task.finished = time.time()
    _queued.pop(task.id, None)  # requests waiting on a called-off task are called off too
    proc = _procs.pop(task.id, None)
    if proc:
        _kill_tree(proc)


def health() -> str:
    """Spoken answer to 'are you connected?'."""
    if not _claude_exe():
        return "我现在动不了电脑，后台的工具没找到。"
    t = state.last_task()
    tail = ""
    if t and t.status == "running":
        tail = f"现在正在弄「{t.request[:16]}」。"
    elif t:
        tail = f"上一件事「{t.request[:16]}」{'弄完了' if t.status == 'done' else '没弄成'}。"
    gui = "也能点屏幕" if MCP_CONFIG.exists() else "但点屏幕的功能没接上"
    return f"连着呢，能帮你操作电脑，{gui}。{tail}"


def stop_all() -> None:
    """Server is shutting down: no task may keep running on its own (it would keep clicking around)."""
    for tid, proc in list(_procs.items()):
        try:
            _kill_tree(proc)  # the whole tree: claude and the tools it started
        except Exception as e:
            logger.warning(f"[deskpet] could not stop task {tid}: {e}")
    for t in state.tasks:
        if t.status == "running":
            t.status = "cancelled"
