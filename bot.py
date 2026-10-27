import asyncio
import base64
import html as html_lib
import io
import json
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
import requests
from aiohttp import web
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

SCRP_API_URL = os.environ.get(
    "SCRP_API_URL", "https://scrp-chat.econ.cuhk.edu.hk/api/chat/completions"
)
SCRP_API_KEY = os.environ.get("SCRP_API_KEY")
SCRP_MODEL = os.environ.get("SCRP_MODEL", "scrp-assistant")
DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT", "You are a helpful assistant in a Discord server. Keep replies concise."
)

MAX_HISTORY = 30
MAX_REQUEST_CHARS = 80000
AI_CHANNEL = os.environ.get("AI_CHANNEL", "").strip()
TIMEZONE = os.environ.get("TIMEZONE", "Asia/Hong_Kong")
HISTORY_FILE = os.environ.get(
    "HISTORY_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "history.json")
)
MAX_FILE_CHARS = 30000
MAX_URL_CHARS = 15000
MAX_URLS_PER_MESSAGE = 2
URL_RE = re.compile(r"https?://[^\s<>\"']+")
TEXT_EXTENSIONS = (
    ".txt", ".csv", ".md", ".py", ".json", ".log", ".js", ".html", ".xml", ".r", ".do", ".tex"
)
SEARXNG_URLS = [
    u.strip()
    for u in os.environ.get(
        "SEARXNG_URLS",
        "https://searx.be,https://search.rhscz.eu,https://priv.au,"
        "https://searx.tiekoetter.com,https://paulgo.io,https://opnxng.com",
    ).split(",")
    if u.strip()
]
history: dict[int, list[dict]] = {}
MEMORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory.json")
memories: dict[int, list[str]] = {}
SCHEDULE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schedules.json")
schedules: list[dict] = []
SETTINGS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")
user_settings: dict[int, dict] = {}


def load_settings() -> None:
    global user_settings
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        user_settings = {}
        for k, v in raw.items():
            try:
                user_settings[int(k)] = v
            except ValueError:
                user_settings[k] = v
    except (FileNotFoundError, json.JSONDecodeError):
        user_settings = {}


def save_settings() -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(user_settings, f, ensure_ascii=False, indent=1)
    except OSError as e:
        print(f"Warning: could not save settings: {e}")


def user_max_messages(key) -> int:
    turns = 15
    if isinstance(key, int):
        try:
            turns = int(user_settings.get(key, {}).get("max_history") or 15)
        except (TypeError, ValueError):
            turns = 15
    return max(2, min(turns, 30)) * 2


def load_schedules() -> None:
    global schedules
    try:
        with open(SCHEDULE_FILE, encoding="utf-8") as f:
            schedules = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        schedules = []


def save_schedules() -> None:
    try:
        with open(SCHEDULE_FILE, "w", encoding="utf-8") as f:
            json.dump(schedules, f, ensure_ascii=False, indent=1)
    except OSError as e:
        print(f"Warning: could not save schedules: {e}")


def load_memories() -> None:
    global memories
    try:
        with open(MEMORY_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        memories = {}
        for k, v in raw.items():
            try:
                memories[int(k)] = v
            except ValueError:
                memories[k] = v
    except (FileNotFoundError, json.JSONDecodeError):
        memories = {}


def save_memories() -> None:
    try:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(memories, f, ensure_ascii=False)
    except OSError as e:
        print(f"Warning: could not save memories: {e}")


def load_history() -> None:
    global history
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        history = {}
        for k, v in raw.items():
            try:
                history[int(k)] = v
            except ValueError:
                history[k] = v
    except (FileNotFoundError, json.JSONDecodeError):
        history = {}


def save_history() -> None:
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
    except OSError as e:
        print(f"Warning: could not save history: {e}")


def ask_scrp(messages: list[dict]) -> str:
    for attempt in range(2):
        resp = requests.post(
            SCRP_API_URL,
            headers={"Authorization": f"Bearer {SCRP_API_KEY}"},
            json={"model": SCRP_MODEL, "messages": messages},
            timeout=120,
        )
        if resp.ok:
            return resp.json()["choices"][0]["message"]["content"]
        try:
            detail = resp.json().get("detail") or resp.text[:300]
        except ValueError:
            detail = resp.text[:300]
        if (
            resp.status_code == 400
            and attempt == 0
            and any(k in str(detail).lower() for k in ("pipe", "socket", "timed out", "timeout"))
        ):
            time.sleep(2)
            continue
        raise RuntimeError(f"API error {resp.status_code}: {detail}")


def get_tz() -> ZoneInfo:
    try:
        return ZoneInfo(TIMEZONE)
    except Exception:
        return ZoneInfo("UTC")


def message_size(m: dict) -> int:
    c = m.get("content")
    if isinstance(c, str):
        return len(c)
    if isinstance(c, list):
        total = 0
        for p in c:
            if not isinstance(p, dict):
                continue
            total += len(p.get("text", ""))
            img = p.get("image_url")
            if isinstance(img, dict):
                total += len(img.get("url", ""))
        return total
    return 0


def clamp_messages(msgs: list[dict]) -> list[dict]:
    total = sum(message_size(m) for m in msgs)
    while total > MAX_REQUEST_CHARS and len(msgs) > 3:
        removed = msgs.pop(1)
        total -= message_size(removed)
    if total > MAX_REQUEST_CHARS and len(msgs) > 1:
        for m in msgs[:-1]:
            c = m.get("content")
            if isinstance(c, list):
                for p in c:
                    if isinstance(p, dict) and "image_url" in p:
                        total -= len(p["image_url"].get("url", ""))
                        p.pop("image_url")
                        p.setdefault("text", "")
                        p["text"] += "\n[image omitted to save space]"
    cap = MAX_REQUEST_CHARS // 4
    for m in msgs[1:]:
        c = m.get("content")
        if isinstance(c, str) and len(c) > cap:
            m["content"] = c[:cap]
        elif isinstance(c, list):
            for p in c:
                if isinstance(p, dict) and len(p.get("text", "")) > cap:
                    p["text"] = p["text"][:cap]
    return msgs


def get_style(history_key, user_id: int) -> str:
    if isinstance(history_key, str) and history_key.startswith("channel:"):
        return (user_settings.get(history_key, {}) or {}).get("style") or ""
    return (user_settings.get(user_id, {}) or {}).get("style") or ""


def build_messages(user_id: int, user_message, author: str = None, history_key=None) -> list[dict]:
    now = datetime.now(get_tz())
    if isinstance(history_key, str) and history_key.startswith("channel:"):
        who = (
            "\n\nThis is a SHARED group channel: multiple people talk here, and past user "
            "messages in the history are prefixed with [DisplayName]. You are currently "
            f"talking to {author}. Keep track of who said what."
        ) if author else ""
    else:
        who = f"\n\nYou are currently talking to {author} in Discord. Address them by name when natural." if author else ""
    facts = []
    if isinstance(history_key, str) and history_key.startswith("channel:"):
        facts.extend(memories.get(history_key, []))
    facts.extend(memories.get(user_id, []))
    known = ""
    if facts:
        known = "\n\nKnown facts (long-term memory):\n- " + "\n- ".join(facts)
    msgs = [
        {
            "role": "system",
            "content": (
                f"{SYSTEM_PROMPT}\n\n"
                f"Current date and time: {now.strftime('%A, %Y-%m-%d %H:%M (%Z)')}.{who}{known}\n\n"
                "You are a Discord bot. Available commands:\n"
                "- `/summarize [count]` — summarize recent channel messages\n"
                "- `/ask <question>` — chat with the bot\n"
                "- `/search <query>` — search the web\n"
                "- `/read <url> [question]` — read and summarize a webpage\n"
                "- `/remind <duration> <text>` — set a one-off reminder\n"
                "- `/schedule <HH:MM> <prompt>` — set a DAILY scheduled message: at the "
                "given time each day (Hong Kong time), you generate the prompt's content "
                "and post it in the channel (or DM) where the command was used. "
                "Example: `/schedule 08:30 今日香港天氣摘要`\n"
                "- `/schedules` / `/unschedule <id|all>` — list or delete scheduled messages "
                "(`/unschedule all` deletes every schedule at once)\n"
                "- `/poll <question> [options]` — create a poll\n"
                "- `/remember <fact>` — save a long-term fact about yourself\n"
                "- `/memory` / `/forget` — view or delete long-term memory\n"
                "- `/history` — show conversation turns\n"
                "- `/reset` — clear your chat history\n"
                "- `/aisetting` — set personal memory limit and tone/style\n"
                "- `/aihelp` — show help\n\n"
                "You do NOT have image generation. If asked to generate, draw, or create "
                "an image, politely say you cannot do that.\n"
                "Do NOT end your replies with follow-up questions or offers such as "
                "「還有什麼想知道？」「需要我幫你做其他事嗎？」— just answer the question "
                "and stop. Only ask a clarifying question if the request is genuinely "
                "impossible to answer without it.\n"
                "If the user's message contains a URL, webpage content is automatically "
                "fetched and included in your context as \"[Web page N: ...]\" — use that "
                "content to answer directly. Only suggest `/read` if no webpage content "
                "appears in your context."
            ),
        }
    ]
    msgs.extend(history.get(history_key if history_key is not None else user_id, []))
    msgs.append({"role": "user", "content": user_message})
    style = get_style(history_key, user_id)
    if style:
        msgs[0]["content"] += f"\n\nPreferred tone/style: {style}"
    return clamp_messages(msgs)


def update_history(user_id: int, user_message: str, reply: str) -> None:
    history.setdefault(user_id, [])
    history[user_id].append({"role": "user", "content": user_message})
    history[user_id].append({"role": "assistant", "content": reply})
    limit = user_max_messages(user_id)
    if len(history[user_id]) > limit:
        history[user_id] = history[user_id][-limit:]
    save_history()


async def append_url_parts(parts: list[dict], question: str) -> None:
    urls = URL_RE.findall(question or "")
    for i, url in enumerate(urls[:MAX_URLS_PER_MESSAGE]):
        try:
            text = await asyncio.to_thread(fetch_url_text, url)
            if text:
                parts.append(
                    {"type": "text", "text": f"[Web page {i+1}: {url}]\n{text}"}
                )
        except Exception as e:
            parts.append(
                {"type": "text", "text": f"[Failed to read {url}: {type(e).__name__}: {e}]"}
            )


def compress_image(data: bytes) -> bytes:
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        img = img.convert("RGB")
        img.thumbnail((1024, 1024))
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception:
        return data


async def build_content(message: discord.Message, question: str) -> list[dict] | str:
    if not message.attachments:
        parts: list[dict] = [{"type": "text", "text": question or ""}]
        await append_url_parts(parts, question)
        return parts if len(parts) > 1 else question
    parts = [{"type": "text", "text": question or "Please analyze the attached file(s)."}]
    for att in message.attachments:
        name = att.filename.lower()
        try:
            if att.content_type and att.content_type.startswith("image/"):
                data = await att.read()
                data = await asyncio.to_thread(compress_image, data)
                b64 = base64.b64encode(data).decode()
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    }
                )
            elif name.endswith(".pdf"):
                from pypdf import PdfReader

                data = await att.read()
                reader = PdfReader(io.BytesIO(data))
                text = "\n".join((page.extract_text() or "") for page in reader.pages[:30])
                parts.append(
                    {
                        "type": "text",
                        "text": f"[File: {att.filename}]\n{text[:MAX_FILE_CHARS]}",
                    }
                )
            elif name.endswith(TEXT_EXTENSIONS):
                data = await att.read()
                text = data.decode("utf-8", errors="replace")
                parts.append(
                    {
                        "type": "text",
                        "text": f"[File: {att.filename}]\n{text[:MAX_FILE_CHARS]}",
                    }
                )
            else:
                parts.append(
                    {
                        "type": "text",
                        "text": f"[Attached file: {att.filename} ({att.content_type}) — unsupported file type]",
                    }
                )
        except Exception as e:
            parts.append(
                {"type": "text", "text": f"[Failed to read {att.filename}: {type(e).__name__}: {e}]"}
            )
    await append_url_parts(parts, question)
    return parts


DURATION_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(s|m|h|d|w)", re.IGNORECASE)


def parse_duration(text: str) -> float | None:
    total = 0.0
    for value, unit in DURATION_RE.findall(text):
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit.lower()]
        total += float(value) * mult
    return total or None


def format_duration(seconds: float) -> str:
    parts = []
    for label, size in (("w", 604800), ("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        if seconds >= size:
            n, seconds = divmod(seconds, size)
            parts.append(f"{int(n)}{label}")
    return " ".join(parts) if parts else "0s"


def fetch_url_text(url: str) -> str:
    resp = requests.get(
        url,
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "")
    if "pdf" in ctype or url.lower().endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(resp.content))
        text = "\n".join((page.extract_text() or "") for page in reader.pages[:20])
    else:
        page = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", resp.text)
        text = html_lib.unescape(re.sub(r"(?s)<[^>]+>", " ", page))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:MAX_URL_CHARS]


def searxng_search(query: str, count: int = 5) -> list[dict] | None:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    for base in SEARXNG_URLS:
        try:
            r = requests.get(
                f"{base.rstrip('/')}/search",
                params={"q": query, "format": "json"},
                headers=headers,
                timeout=10,
            )
            if r.ok and r.headers.get("content-type", "").startswith("application/json"):
                results = r.json().get("results", [])[:count]
                if results:
                    return results
        except (requests.RequestException, ValueError):
            continue
    return None


def duckduckgo_search(query: str, count: int = 5) -> list[dict] | None:
    try:
        from ddgs import DDGS
    except ImportError:
        return None
    try:
        with DDGS() as ddgs:
            hits = list(ddgs.text(query, max_results=count))
        return [
            {"title": h.get("title", ""), "url": h.get("href", ""), "content": h.get("body", "")}
            for h in hits
        ]
    except Exception:
        return None


def web_search(query: str, count: int = 5) -> list[dict] | None:
    return searxng_search(query, count) or duckduckgo_search(query, count)


async def start_web_server() -> None:
    app = web.Application()

    async def handler(request: web.Request) -> web.Response:
        return web.Response(text=f"{bot.user} is alive" if bot.user else "starting...")

    app.router.add_get("/", handler)
    port = int(os.environ.get("PORT", 8080))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Keep-alive web server listening on port {port}")


background_tasks: set[asyncio.Task] = set()


class SCRPBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        task = asyncio.create_task(schedule_loop())
        background_tasks.add(task)
        task.add_done_callback(background_tasks.discard)
        for v in (
            RemindButtonView(),
            ScheduleButtonView(),
            PollButtonView(),
            HelpButtonView(),
            AnswerButtonView(),
            SettingsButtonView(),
        ):
            self.add_view(v)
        await self.tree.sync()

    async def on_ready(self) -> None:
        print(f"Logged in as {self.user} (id: {self.user.id})")


bot = SCRPBot()


async def send_reply(target, reply: str) -> None:
    for chunk in [reply[i : i + 2000] for i in range(0, len(reply), 2000)]:
        await target.send(chunk)


def format_search_results(results: list[dict]) -> str:
    return "\n\n".join(
        f"[{i + 1}] {r.get('title', '')}\n{r.get('url', '')}\n{r.get('content', '')}"
        for i, r in enumerate(results)
    )


SEARCH_INSTRUCTION = (
    "\n\nIf answering this requires up-to-date information from the internet "
    "(e.g. news, weather, prices, sports, recent events, current data) that is not "
    "already in your context, reply with ONLY the line "
    "'SEARCH: <search query>' and nothing else. Otherwise just answer normally."
)


WEATHER_TERMS = ("weather", "temperature", "forecast", "rain", "天氣", "气温", "氣溫", "溫度", "温度", "預報", "下雨")
HK_TERMS = ("hong kong", " hk", "香港", "港島", "九龍", "新界")


def fetch_hko_weather() -> str:
    out = []
    for dtype in ("flw", "fnd"):
        try:
            r = requests.get(
                "https://data.weather.gov.hk/weatherAPI/opendata/weather.php",
                params={"dataType": dtype, "lang": "tc"},
                timeout=15,
            )
            r.raise_for_status()
            out.append(json.dumps(r.json(), ensure_ascii=False))
        except Exception:
            continue
    return "\n".join(out)


def wants_hko_weather(query: str) -> bool:
    q = query.lower()
    return any(t in q for t in WEATHER_TERMS) and any(t in q for t in HK_TERMS)


async def answer_with_search(
    user_id: int, content, message: discord.Message = None, author: str = None, history_key=None
):
    msgs = build_messages(user_id, content, author, history_key)
    base_system = msgs[0]["content"]
    msgs[0] = {"role": "system", "content": base_system + SEARCH_INSTRUCTION}
    reply = await asyncio.to_thread(ask_scrp, msgs)
    if not reply.strip().upper().startswith("SEARCH:"):
        return reply, None
    query = reply.strip()[len("SEARCH:"):].strip().splitlines()[0].strip()
    status = None
    if message is not None:
        try:
            status = await message.reply(f"🔍 Searching the web: {query}")
        except discord.HTTPException:
            pass
    results = await asyncio.to_thread(web_search, query)
    if status:
        try:
            await status.delete()
        except discord.HTTPException:
            pass
    if not results:
        return "我想幫你上網搜尋，但搜尋服務暫時無法使用。", query
    pages = []
    if any(t in query.lower() for t in WEATHER_TERMS):
        try:
            hko = await asyncio.to_thread(fetch_hko_weather)
            if hko:
                pages.append(
                    "[HKO Hong Kong Observatory official data (current, fetched just now). "
                    "This is for Hong Kong only — ignore it if the question is about "
                    "another location.]\n" + hko
                )
        except Exception:
            pass
    for r in results[:2]:
        url = r.get("url", "")
        if not url:
            continue
        try:
            text = await asyncio.to_thread(fetch_url_text, url)
            if text:
                pages.append(f"[Fetched page: {url}]\n{text}")
        except Exception:
            continue
    msgs[0] = {
        "role": "system",
        "content": (
            base_system
            + "\n\nIMPORTANT: Real-time web search results have ALREADY been fetched for "
            "you and are included below, along with the full content of the top result "
            "page(s). They contain current, up-to-date information. "
            "You MUST answer using them and cite sources as [1], [2] etc. Do NOT say you "
            "cannot access real-time data. Do NOT suggest using /search or any other "
            "command.\n\nSEARCH RESULTS:\n" + format_search_results(results)
            + ("\n\nFULL PAGE CONTENT:\n" + "\n\n".join(pages) if pages else "")
        ),
    }
    clamp_messages(msgs)
    reply = await asyncio.to_thread(ask_scrp, msgs)
    return reply, query


@bot.tree.command(name="ask", description="向我提問")
@app_commands.describe(question="你的問題")
async def ask(interaction: discord.Interaction, question: str) -> None:
    await interaction.response.defer()
    try:
        reply, query = await answer_with_search(
            interaction.user.id, question, author=interaction.user.display_name
        )
    except Exception as e:
        await interaction.followup.send(f"Error: {type(e).__name__}: {e}")
        return
    update_history(interaction.user.id, question, reply)
    prefix = f"🔍 (web search: {query})\n" if query else ""
    await send_reply(interaction.followup, prefix + reply)


HELP_TEXT = (
    "**如何與我對話**\n"
    "- 在任何頻道 **@提及我**，例如 `@SatoAI 你好`\n"
    "- **回覆**我的任何一則訊息\n"
    "- 在 AI 頻道直接對話 — 我會回覆每一句\n"
    "- 附加 **圖片 / PDF / 文字檔**，我會幫你閱讀\n"
    "- 貼上 **網頁連結**，我會自動讀取內容（回覆會顯示 🌐）\n"
    "- 如果問題需要最新資訊，我會**自動上網搜尋**"
    "（回覆會顯示 🔍 並附來源）\n\n"
    "**指令**\n"
    "- `/ask <問題>` — 向我提問\n"
    "- `/search <關鍵字>` — 上網搜尋並附來源回答\n"
    "- `/read <網址> [問題]` — 讀取網頁並回答問題\n"
    "- `/remind <時間> <內容>` — 設定提醒，例如 `/remind 30m 做功課`\n"
    "- `/poll <問題> [選項]` — 建立投票，例如 `/poll 晚餐吃什麼 壽司, 披薩`\n"
    "- `/summarize [數量]` — 總結最近的頻道對話，快速追上進度\n"
    "- `/remember <內容>` — 讓我長期記住關於你的事，例如 `/remember 我喜歡吃壽司`\n"
    "- `/memory` / `/forget` — 查看及刪除長期記憶\n"
    "- `/schedule <時間> <內容>` — 設定每日排程訊息，例如 `/schedule 08:30 今日香港天氣摘要`\n"
    "- `/schedules` / `/unschedule` — 查看及刪除排程\n"
    "- `/history` — 顯示我記得多少段對話\n"
    "- `/reset` — 清除你的對話記錄\n"
    "- `/aisetting` — 設定對話記憶上限及語氣風格\n\n"
    "**你訊息上的反應**\n"
    "👀 處理中 → ✅ 完成 → ❌ 出錯"
)


async def get_referenced_text(interaction: discord.Interaction) -> str:
    msg = interaction.message
    orig = None
    if msg is not None and msg.reference:
        if isinstance(msg.reference.resolved, discord.Message):
            orig = msg.reference.resolved
        else:
            try:
                orig = await msg.channel.fetch_message(msg.reference.message_id)
            except discord.HTTPException:
                orig = None
    return orig.content if orig else ""


DURATION_WORDS_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|秒|分鐘|分鐘|小時|個鐘|日|天|星期|週)",
    re.IGNORECASE,
)


def extract_duration_token(text: str) -> str | None:
    m = DURATION_WORDS_RE.search(text or "")
    if not m:
        return None
    value = m.group(1)
    unit = m.group(2).lower()
    if unit.startswith(("second", "sec")) or unit == "秒":
        u = "s"
    elif unit.startswith(("minute", "min")) or unit in ("分鐘", "分"):
        u = "m"
    elif unit.startswith(("hour", "hr")) or unit in ("小時", "個鐘", "鐘"):
        u = "h"
    elif unit.startswith("day") or unit in ("日", "天"):
        u = "d"
    else:
        u = "w"
    return f"{value}{u}"


def settings_target_for_channel(channel) -> tuple:
    if in_ai_channel(channel):
        return f"channel:{channel.id}", True
    return None, False


class AISettingsModal(discord.ui.Modal, title="設定"):
    def __init__(self, uid: int, target_key=None, channel_mode: bool = False) -> None:
        super().__init__()
        self.target_key = target_key if target_key is not None else uid
        self.channel_mode = channel_mode
        if channel_mode:
            self.title = "頻道設定"
        cur = user_settings.get(self.target_key, {})
        self.max_history = discord.ui.TextInput(
            label="對話記憶上限（段數，2-30）",
            placeholder="例如 10",
            default=str(cur.get("max_history") or 15),
            max_length=2,
        )
        self.style = discord.ui.TextInput(
            label="語氣及風格（可留空）" if not channel_mode else "頻道語氣風格（適用於所有人）",
            placeholder="例如：正式一點、用英文、多一些emoji、簡短直接…",
            default=cur.get("style") or "",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=300,
        )
        self.add_item(self.max_history)
        self.add_item(self.style)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            turns = int(str(self.max_history).strip())
        except ValueError:
            await interaction.response.send_message(
                "記憶上限請輸入 2 至 30 之間的數字。", ephemeral=True
            )
            return
        turns = max(2, min(turns, 30))
        cur = user_settings.get(self.target_key, {})
        cur["max_history"] = turns
        cur["style"] = str(self.style).strip()
        user_settings[self.target_key] = cur
        save_settings()
        scope = "此頻道（適用於所有人）" if self.channel_mode else "你的個人設定"
        await interaction.response.send_message(
            f"✅ 已更新{scope}：\n- 對話記憶上限：{turns} 段\n- 語氣風格：{cur['style'] or '（預設）'}",
            ephemeral=True,
        )


class SettingsButtonView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="設定", emoji="⚙️", style=discord.ButtonStyle.primary, custom_id="satoai:btn_settings")
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        target_key, channel_mode = settings_target_for_channel(interaction.channel)
        if channel_mode and not can_manage_channel_settings(interaction):
            await interaction.response.send_message(
                "頻道設定只限擁有「管理伺服器」權限的成員修改。",
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            AISettingsModal(interaction.user.id, target_key, channel_mode)
        )


def can_manage_channel_settings(interaction: discord.Interaction) -> bool:
    if interaction.guild is None:
        return True
    perms = interaction.user.guild_permissions
    return perms.administrator or perms.manage_guild


@bot.tree.command(name="aisetting", description="設定對話記憶上限及語氣風格")
async def aisetting(interaction: discord.Interaction) -> None:
    target_key, channel_mode = settings_target_for_channel(interaction.channel)
    if channel_mode and not can_manage_channel_settings(interaction):
        await interaction.response.send_message(
            "頻道設定只限擁有「管理伺服器」權限的成員修改。你可以隨時用 `/aisetting` "
            "在私訊或其他頻道設定你的個人風格。",
            ephemeral=True,
        )
        return
    await interaction.response.send_modal(
        AISettingsModal(interaction.user.id, target_key, channel_mode)
    )


class RemindModal(discord.ui.Modal, title="設定提醒"):
    def __init__(self, default_text: str = "", default_duration: str = None) -> None:
        super().__init__()
        self.duration = discord.ui.TextInput(
            label="多久之後",
            placeholder="例如 30s、10m、2h、1d、1h30m",
            default=default_duration or "",
            max_length=20,
        )
        self.text = discord.ui.TextInput(
            label="提醒內容",
            default=(default_text or "")[:900],
            style=discord.TextStyle.paragraph,
            max_length=1000,
        )
        self.add_item(self.duration)
        self.add_item(self.text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        seconds = parse_duration(str(self.duration))
        if seconds is None:
            await interaction.response.send_message(
                "時間格式無效，請用 `30s`、`10m`、`2h`、`1d` 或 `1h30m` 等格式。", ephemeral=True
            )
            return
        text = str(self.text)

        async def fire() -> None:
            await asyncio.sleep(seconds)
            try:
                await interaction.channel.send(f"{interaction.user.mention} ⏰ 提醒：{text}")
            except discord.HTTPException:
                pass

        asyncio.create_task(fire())
        await interaction.response.send_message(
            f"收到！我會在 {format_duration(seconds)} 之後提醒你。⏰", ephemeral=True
        )


def extract_event(text: str) -> str:
    t = text or ""
    t = re.sub(
        r"(in|after|之後|之后|後)?\s*\d+(?:\.\d+)?\s*"
        r"(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?|weeks?|秒|分鐘|分|小時|個鐘|日|天|星期|週)"
        r"\s*(later|後|后)?",
        " ",
        t,
        flags=re.I,
    )
    t = re.sub(
        r"(每天|每日|每晚|今晚|凌晨|早上|上午|下午|傍晚|晚上|夜晚)?\s*\d{1,2}\s*[點点]\s*(半|\d{1,2}\s*分?)?",
        " ",
        t,
    )
    t = re.sub(r"\b\d{1,2}[:：]\d{2}\s*(am|pm)?\b", " ", t, flags=re.I)
    t = re.sub(r"\b\d{1,2}\s*(am|pm|o'?clock)\b", " ", t, flags=re.I)
    t = re.sub(
        r"(remind me( to)?|set a reminder( to)?|reminder( to)?|提醒我|幫我提醒|提醒|"
        r"set a schedule|schedule|排程|定時|每日|每天|每晚)",
        " ",
        t,
        flags=re.I,
    )
    t = t.strip()
    for _ in range(3):
        t = re.sub(
            r"^(please\s+|help\s+|can you\s+|the\s+|a\s+|an\s+|to\s+|去|做|要)+",
            "",
            t,
            flags=re.I,
        ).strip()
    t = re.sub(r"\s+(at|on|to|在|於)$", "", t, flags=re.I)
    t = re.sub(r"\s+", " ", t).strip(" ，。,.!！?？:：")
    return t or text


def extract_time_token(text: str) -> str | None:
    t = text or ""
    m = re.search(r"\b(\d{1,2})[:：](\d{2})\s*(am|pm)\b", t, re.I)
    if m:
        hh, mm, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
        if ap == "pm" and hh < 12:
            hh += 12
        if ap == "am" and hh == 12:
            hh = 0
        if hh < 24 and mm < 60:
            return f"{hh:02d}:{mm:02d}"
    m = re.search(r"\b(\d{1,2})[:：](\d{2})\b", t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if hh < 24 and mm < 60:
            return f"{hh:02d}:{mm:02d}"
    m = re.search(
        r"(凌晨|早上|上午|下午|傍晚|晚上|今晚|每晚|夜晚|夜間|晚|夜)?\s*(\d{1,2})\s*[點点]\s*(半|(\d{1,2})\s*分?)?",
        t,
    )
    if m:
        qual, hh, half, mins = m.group(1), int(m.group(2)), m.group(3), m.group(4)
        mm = 30 if half == "半" else (int(mins) if mins else 0)
        if mm >= 60:
            mm = 0
        if qual and any(q in qual for q in ("下午", "傍晚", "晚", "夜")) and hh < 12:
            hh += 12
        if qual and "凌晨" in qual and hh == 12:
            hh = 0
        if hh < 24:
            return f"{hh:02d}:{mm:02d}"
    m = re.search(r"\b(\d{1,2})\s*(am|pm)\b", t, re.I)
    if m:
        hh, ap = int(m.group(1)), m.group(2).lower()
        if ap == "pm" and hh < 12:
            hh += 12
        if ap == "am" and hh == 12:
            hh = 0
        if hh < 24:
            return f"{hh:02d}:00"
    return None


class ScheduleModal(discord.ui.Modal, title="設定每日排程"):
    def __init__(self, default_text: str = "", default_time: str = None) -> None:
        super().__init__()
        self.time = discord.ui.TextInput(
            label="每日幾點幾分（24小時制）",
            placeholder="例如 08:30",
            default=default_time or "",
            max_length=5,
        )
        self.prompt = discord.ui.TextInput(
            label="排程內容",
            default=(default_text or "")[:900],
            style=discord.TextStyle.paragraph,
            max_length=1000,
        )
        self.add_item(self.time)
        self.add_item(self.prompt)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        m = TIME_RE.match(str(self.time).strip())
        if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
            await interaction.response.send_message(
                "時間格式無效，請用 24 小時制 HH:MM，例如 `08:30`。", ephemeral=True
            )
            return
        hh, mm = int(m.group(1)), int(m.group(2))
        sid = max((s["id"] for s in schedules), default=0) + 1
        schedules.append(
            {
                "id": sid,
                "channel_id": interaction.channel_id,
                "time": f"{hh:02d}:{mm:02d}",
                "prompt": str(self.prompt)[:500],
                "owner_id": interaction.user.id,
                "last_run": None,
            }
        )
        save_schedules()
        now = datetime.now(get_tz())
        run = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if run <= now:
            run += timedelta(days=1)
        await interaction.response.send_message(
            f"✅ 已設定每日 {hh:02d}:{mm:02d} 在此頻道發送排程訊息（編號 {sid}）。\n"
            f"下次執行：{run.strftime('%Y-%m-%d %H:%M')}",
            ephemeral=True,
        )


class PollModal(discord.ui.Modal, title="建立投票"):
    def __init__(self, default_text: str = "") -> None:
        super().__init__()
        self.question = discord.ui.TextInput(
            label="投票問題", default=(default_text or "")[:200], max_length=300
        )
        self.options = discord.ui.TextInput(
            label="選項（逗號分隔，留空則為是/否）", default="", max_length=300, required=False
        )
        self.add_item(self.question)
        self.add_item(self.options)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        question = str(self.question)[:300]
        opts = [o.strip()[:55] for o in str(self.options).split(",") if o.strip()]
        p = discord.Poll(question=discord.PollMedia(text=question), duration=timedelta(hours=24))
        if opts:
            if not 2 <= len(opts) <= 10:
                await interaction.response.send_message(
                    "選項數量需在 2 至 10 個之間。", ephemeral=True
                )
                return
            for o in opts:
                p.add_answer(text=o)
        else:
            p.add_answer(text="是", emoji="👍")
            p.add_answer(text="否", emoji="👎")
        await interaction.response.send_message(poll=p)


class RemindButtonView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="設定提醒", emoji="⏰", style=discord.ButtonStyle.primary, custom_id="satoai:btn_remind")
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        prefill = await get_prefill(interaction)
        await interaction.response.send_modal(
            RemindModal(prefill.get("event") or "", prefill.get("duration"))
        )


class ScheduleButtonView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="設定排程", emoji="📅", style=discord.ButtonStyle.primary, custom_id="satoai:btn_schedule")
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        prefill = await get_prefill(interaction)
        await interaction.response.send_modal(
            ScheduleModal(prefill.get("event") or "", prefill.get("time"))
        )


class PollButtonView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="建立投票", emoji="📊", style=discord.ButtonStyle.primary, custom_id="satoai:btn_poll")
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(PollModal(await get_referenced_text(interaction)))


class HelpButtonView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="說明", emoji="❓", style=discord.ButtonStyle.secondary, custom_id="satoai:btn_help")
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_message(HELP_TEXT, ephemeral=True)


class AnswerButtonView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="回答問題", emoji="💬", style=discord.ButtonStyle.success, custom_id="satoai:btn_answer")
    async def go(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.defer()
        question = ""
        if interaction.message and interaction.message.content:
            for line in interaction.message.content.splitlines():
                if line.startswith(("📅", "🔍", "🌐")):
                    continue
                question += line + "\n"
        question = question.strip() or "（找不到問題內容）"
        try:
            reply, query = await answer_with_search(
                interaction.user.id, question, author=interaction.user.display_name
            )
        except Exception as e:
            await interaction.followup.send(f"出錯：{type(e).__name__}: {e}")
            return
        prefix = f"🔍 (web search: {query})\n" if query else ""
        await interaction.followup.send(prefix + reply[:2000])


EXTRACTION_PROMPT = (
    "Extract structured info from the user's message. Reply with ONLY a JSON object, "
    "no markdown, no explanation:\n"
    '{"event": "the thing to do (strip trigger words like remind me/提醒我, times, '
    'and durations; empty string if none)", '
    '"duration": "offset like 5m, 2h, 1d, 1h30m or null", '
    '"time": "daily time HH:MM 24h format or null", '
    '"intents": ["any of: remind, schedule, poll, help — only if the user wants that '
    'feature now; empty list otherwise. Daily/recurring requests (每天/每晚/daily) '
    'mean schedule. One-off requests relative to now (in X minutes/hours, X分鐘後, '
    'later today, tomorrow) mean remind, NOT schedule"]}'
)

extractions: dict[int, dict] = {}


def llm_extract(text: str) -> dict:
    try:
        reply = ask_scrp(
            [
                {"role": "system", "content": EXTRACTION_PROMPT},
                {"role": "user", "content": text or ""},
            ]
        )
        start, end = reply.find("{"), reply.rfind("}")
        if start == -1 or end == -1:
            return {}
        data = json.loads(reply[start : end + 1])
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def remember_extraction(message_id: int, data: dict) -> None:
    if not data:
        return
    extractions[message_id] = data
    while len(extractions) > 200:
        extractions.pop(next(iter(extractions)))


async def get_prefill(interaction: discord.Interaction) -> dict:
    mid = None
    if interaction.message is not None and interaction.message.reference:
        mid = interaction.message.reference.message_id
    if mid is not None and mid in extractions:
        return extractions[mid]
    text = await get_referenced_text(interaction)
    return {
        "event": extract_event(text),
        "duration": extract_duration_token(text),
        "time": extract_time_token(text),
        "intents": [],
    }


def views_for_intents(intents: list) -> list:
    out = []
    for i in intents or []:
        if i == "remind":
            out.append(RemindButtonView())
        elif i == "schedule":
            out.append(ScheduleButtonView())
        elif i == "poll":
            out.append(PollButtonView())
        elif i == "help":
            out.append(HelpButtonView())
    return out


def detect_intents(text: str) -> list[dict]:
    t = (text or "").lower()
    out = []
    if any(k in t for k in ("remind", "reminder", "提醒", "倒數")):
        out.append(RemindButtonView())
    if any(k in t for k in ("schedule", "排程", "定時", "每日", "每天", "daily", "每天")):
        out.append(ScheduleButtonView())
    if any(k in t for k in ("poll", "投票", "vote")):
        out.append(PollButtonView())
    if any(
        k in t
        for k in (
            "who are you", "what can you", "你是誰", "你可以做什麼", "幫助", "說明",
            "介紹", "aihelp", "use you", "怎麼用",
        )
    ):
        out.append(HelpButtonView())
    return out


def build_intent_view(text: str) -> discord.ui.View | None:
    views = detect_intents(text)
    if not views:
        return None
    combined = discord.ui.View(timeout=None)
    for v in views:
        for child in v.children:
            combined.add_item(child)
    return combined


@bot.tree.command(name="aihelp", description="顯示我能做什麼")
async def aihelp(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(HELP_TEXT, ephemeral=True)


@bot.tree.command(name="remember", description="讓我記住一些事")
@app_commands.describe(text="要記住的內容，例如：我喜歡吃壽司")
async def remember(interaction: discord.Interaction, text: str) -> None:
    target_key, channel_mode = settings_target_for_channel(interaction.channel)
    key = target_key if target_key is not None else interaction.user.id
    memories.setdefault(key, [])
    if len(memories[key]) >= 50:
        await interaction.response.send_message(
            "記憶已滿（50條），請先用 /forget 刪除部分舊記憶。", ephemeral=True
        )
        return
    memories[key].append(text[:300])
    save_memories()
    scope = "（已存入此頻道的共享記憶，所有人適用）" if channel_mode else ""
    await interaction.response.send_message(
        f"記住了！📝 ({len(memories[key])}/50) {scope}", ephemeral=True
    )


@bot.tree.command(name="memory", description="查看我記住了什麼")
async def memory_cmd(interaction: discord.Interaction) -> None:
    target_key, channel_mode = settings_target_for_channel(interaction.channel)
    lines = []
    if channel_mode:
        shared = memories.get(target_key, [])
        if shared:
            listing = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(shared))
            lines.append(f"**此頻道的共享記憶（{len(shared)}條）：**\n{listing}")
        else:
            lines.append("**此頻道的共享記憶：** 無")
    personal = memories.get(interaction.user.id, [])
    if personal:
        listing = "\n".join(f"{i + 1}. {f}" for i, f in enumerate(personal))
        lines.append(f"**你的個人記憶（{len(personal)}條）：**\n{listing}")
    else:
        lines.append("**你的個人記憶：** 無（在私訊或其他頻道用 /remember 添加）")
    await interaction.response.send_message("\n\n".join(lines)[:1900], ephemeral=True)


class ConfirmForgetAllView(discord.ui.View):
    def __init__(self, key) -> None:
        super().__init__(timeout=60)
        self.key = key

    @discord.ui.button(label="確定清空", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        memories[self.key] = []
        save_memories()
        await interaction.response.edit_message(content="已清空所有記憶。🧹", view=None)

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="已取消，記憶保持不變。", view=None)


@bot.tree.command(name="forget", description="忘記記憶")
@app_commands.describe(number="要刪除的記憶編號（用 /memory 查看編號），或 all 全部清除")
async def forget(interaction: discord.Interaction, number: str) -> None:
    target_key, channel_mode = settings_target_for_channel(interaction.channel)
    if channel_mode and not can_manage_channel_settings(interaction):
        await interaction.response.send_message(
            "頻道共享記憶只限擁有「管理伺服器」權限的成員刪除。", ephemeral=True
        )
        return
    key = target_key if target_key is not None else interaction.user.id
    facts = memories.get(key, [])
    if not facts:
        await interaction.response.send_message("沒有可忘記的記憶。", ephemeral=True)
        return
    if number.lower() == "all":
        await interaction.response.send_message(
            f"確定要清空全部 {len(facts)} 條記憶嗎？此操作無法復原。",
            view=ConfirmForgetAllView(key),
            ephemeral=True,
        )
        return
    try:
        idx = int(number) - 1
        removed = facts.pop(idx)
        save_memories()
        await interaction.response.send_message(f"已忘記：{removed[:100]}", ephemeral=True)
    except (ValueError, IndexError):
        await interaction.response.send_message(
            "請輸入有效的編號（用 /memory 查看編號）或 all。", ephemeral=True
        )


@bot.tree.command(name="summarize", description="總結最近的頻道對話")
@app_commands.describe(count="要總結的訊息數量（預設50，最多200）")
async def summarize(interaction: discord.Interaction, count: int = 50) -> None:
    await interaction.response.defer()
    count = max(10, min(count, 200))
    lines = []
    async for m in interaction.channel.history(limit=count):
        if m.author.bot:
            continue
        text = m.content.strip()
        if not text:
            continue
        if m.attachments:
            text += f" [附件 x{len(m.attachments)}]"
        stamp = m.created_at.astimezone(get_tz()).strftime("%m-%d %H:%M")
        lines.append(f"[{stamp}] {m.author.display_name}: {text[:400]}")
    lines.reverse()
    if not lines:
        await interaction.followup.send("這個頻道最近沒有可總結的訊息。")
        return
    transcript = "\n".join(lines)[-15000:]
    try:
        reply = await asyncio.to_thread(
            ask_scrp,
            [
                {
                    "role": "system",
                    "content": "Summarize the following Discord channel conversation. "
                    "Structure your answer as:\n"
                    "1. 主要話題（bullet points）\n"
                    "2. 各參與者的重點（by speaker name）\n"
                    "3. 結論或待辦事項（如有）\n"
                    "Reply in the same language as the conversation. Be concise.\n\n"
                    f"CONVERSATION TRANSCRIPT:\n{transcript}",
                },
                {"role": "user", "content": "請總結以上對話。"},
            ],
        )
    except Exception as e:
        await interaction.followup.send(f"出錯：{type(e).__name__}: {e}")
        return
    await interaction.followup.send(reply[:2000])


@bot.tree.command(name="poll", description="建立投票")
@app_commands.describe(
    question="投票問題",
    options="選項，用逗號分隔（2至10個）；留空則為是/否投票",
    hours="投票持續多少小時（預設24）",
)
async def poll(
    interaction: discord.Interaction, question: str, options: str = "", hours: int = 24
) -> None:
    question = question[:300]
    opts = [o.strip()[:55] for o in options.split(",") if o.strip()]
    hours = max(1, min(hours, 7 * 24))
    if options and not (2 <= len(opts) <= 10):
        await interaction.response.send_message(
            "選項數量需在 2 至 10 個之間，用逗號分隔，例如：`選項一, 選項二, 選項三`",
            ephemeral=True,
        )
        return
    p = discord.Poll(
        question=discord.PollMedia(text=question),
        duration=timedelta(hours=hours),
    )
    if opts:
        for o in opts:
            p.add_answer(text=o)
    else:
        p.add_answer(text="是", emoji="👍")
        p.add_answer(text="否", emoji="👎")
    await interaction.response.send_message(poll=p)


@bot.tree.command(name="reset", description="清除對話記錄")
async def reset(interaction: discord.Interaction) -> None:
    history.pop(interaction.user.id, None)
    cleared = "已清除你的個人對話記錄。"
    if in_ai_channel(interaction.channel):
        if can_manage_channel_settings(interaction):
            history.pop(f"channel:{interaction.channel_id}", None)
            cleared += "（此頻道的共享對話記錄亦已清除）"
        else:
            cleared += "（此頻道的共享對話記錄只限擁有「管理伺服器」權限的成員清除）"
    save_history()
    await interaction.response.send_message(cleared)


@bot.tree.command(name="history", description="顯示我記得多少段對話")
async def history_cmd(interaction: discord.Interaction) -> None:
    personal = len(history.get(interaction.user.id, [])) // 2
    msg = f"你的個人對話記錄：{personal} 段。"
    if in_ai_channel(interaction.channel):
        shared = len(history.get(f"channel:{interaction.channel_id}", [])) // 2
        msg += f"\n此頻道的共享對話記錄：{shared} 段（多人共用）。"
    await interaction.response.send_message(msg)


@bot.tree.command(name="remind", description="設定提醒")
@app_commands.describe(
    duration="多久之後，例如 30s、10m、2h、1d 或 1h30m",
    text="要提醒的內容",
)
async def remind(interaction: discord.Interaction, duration: str, text: str) -> None:
    seconds = parse_duration(duration)
    if seconds is None:
        await interaction.response.send_message(
            "時間格式無效，請用 `30s`、`10m`、`2h`、`1d` 或 `1h30m` 等格式。",
            ephemeral=True,
        )
        return

    async def fire() -> None:
        await asyncio.sleep(seconds)
        await interaction.channel.send(f"{interaction.user.mention} ⏰ Reminder: {text}")

    asyncio.create_task(fire())
    await interaction.response.send_message(
        f"收到！我會在 {format_duration(seconds)} 之後提醒你。⏰"
    )


@bot.tree.command(name="search", description="上網搜尋")
@app_commands.describe(query="要搜尋的內容")
async def search(interaction: discord.Interaction, query: str) -> None:
    await interaction.response.defer()
    results = await asyncio.to_thread(web_search, query)
    if not results:
        await interaction.followup.send(
            "搜尋失敗 — 搜尋服務暫時無法使用。"
            "Install the ddgs package (pip install ddgs) or set your own SearXNG "
            "instance with the SEARXNG_URLS environment variable in .env."
        )
        return
    context = "\n\n".join(
        f"[{i + 1}] {r.get('title', '')}\n{r.get('url', '')}\n{r.get('content', '')}"
        for i, r in enumerate(results)
    )
    try:
        reply = await asyncio.to_thread(
            ask_scrp,
            [
                {
                    "role": "system",
                    "content": "Answer the user's question using ONLY the search results "
                    "below. Cite sources as [1], [2] etc. If the results don't contain "
                    "the answer, say so.\n\nSEARCH RESULTS:\n" + context,
                },
                {"role": "user", "content": query},
            ],
        )
    except Exception as e:
        await interaction.followup.send(f"Error: {type(e).__name__}: {e}")
        return
    sources = "\n".join(f"[{i + 1}] <{r.get('url', '')}>" for i, r in enumerate(results))
    await interaction.followup.send(
        f"{reply[:1900]}\n\n**Sources:**\n{sources[:1000]}"
        if len(reply) + len(sources) < 2900
        else f"{reply[:1900]}\n\n**Sources:**\n{sources[:1000]}\n(truncated)"
    )


@bot.tree.command(name="read", description="讀取網頁並回答問題")
@app_commands.describe(url="要讀取的網址", question="關於該網頁的問題（可選）")
async def read_url(interaction: discord.Interaction, url: str, question: str = None) -> None:
    await interaction.response.defer()
    try:
        text = await asyncio.to_thread(fetch_url_text, url)
    except Exception as e:
        await interaction.followup.send(f"無法讀取 {url}：{type(e).__name__}: {e}")
        return
    if not text:
        await interaction.followup.send("無法從該網頁提取任何文字。")
        return
    try:
        reply = await asyncio.to_thread(
            ask_scrp,
            [
                {
                    "role": "system",
                    "content": "Answer the user's question using ONLY the webpage content "
                    "below. If it doesn't contain the answer, say so.\n\n"
                    f"WEBPAGE CONTENT:\n{text}",
                },
                {"role": "user", "content": question or "Summarize this page."},
            ],
        )
    except Exception as e:
        await interaction.followup.send(f"Error: {type(e).__name__}: {e}")
        return
    await interaction.followup.send(reply[:2000])


async def run_schedule(s: dict) -> None:
    channel = bot.get_channel(s["channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(s["channel_id"])
        except discord.HTTPException:
            channel = None
    print(f"[schedule] firing #{s['id']} at {s['time']} in channel {s['channel_id']}")
    if channel is None:
        print(f"[schedule] channel {s['channel_id']} not found/visible — skipped")
        return
    try:
        reply, query = await answer_with_search(s.get("owner_id", 0), s["prompt"])
    except Exception as e:
        try:
            await channel.send(f"📅 排程訊息出錯：{type(e).__name__}: {e}")
        except discord.HTTPException:
            pass
        return
    prefix = "📅 排程訊息\n" + (f"🔍 (web search: {query})\n" if query else "")
    question_like = (
        "?" in reply
        or "？" in reply
        or any(
            k in s.get("prompt", "").lower()
            for k in ("question", "問題", "quiz", "考", "數學", "math")
        )
    )
    view = AnswerButtonView() if question_like else None
    chunks = [reply[i : i + 2000] for i in range(0, len(reply), 2000)]
    for i, chunk in enumerate(chunks):
        try:
            await channel.send(
                (prefix if i == 0 else "") + chunk,
                view=view if i == len(chunks) - 1 else None,
            )
        except discord.HTTPException:
            return


async def schedule_loop() -> None:
    print(
        f"[schedule] loop started, {len(schedules)} schedule(s) loaded, "
        f"timezone: {get_tz()}, current time here: {datetime.now(get_tz()).strftime('%Y-%m-%d %H:%M')}"
    )
    for s in schedules:
        print(f"[schedule] #{s['id']} daily {s['time']} -> channel {s['channel_id']}: {s['prompt'][:50]}")
    while True:
        try:
            now = datetime.now(get_tz())
            today = now.strftime("%Y-%m-%d")
            hhmm = now.strftime("%H:%M")
            for s in schedules:
                if s.get("time") == hhmm and s.get("last_run") != today:
                    s["last_run"] = today
                    save_schedules()
                    asyncio.create_task(run_schedule(s))
        except Exception as e:
            print(f"[schedule] loop error: {type(e).__name__}: {e}")
        await asyncio.sleep(20)


TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


@bot.tree.command(name="schedule", description="設定每日排程訊息")
@app_commands.describe(
    time="每日幾點幾分（24小時制，例如 08:30）",
    prompt="到時要AI生成的內容，例如：今日香港天氣摘要",
)
async def schedule_cmd(interaction: discord.Interaction, time: str, prompt: str) -> None:
    m = TIME_RE.match(time.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        await interaction.response.send_message(
            "時間格式無效，請用 24 小時制 HH:MM，例如 `08:30`。", ephemeral=True
        )
        return
    hh, mm = int(m.group(1)), int(m.group(2))
    time_str = f"{hh:02d}:{mm:02d}"
    sid = max((s["id"] for s in schedules), default=0) + 1
    schedules.append(
        {
            "id": sid,
            "channel_id": interaction.channel_id,
            "time": time_str,
            "prompt": prompt[:500],
            "owner_id": interaction.user.id,
            "last_run": None,
        }
    )
    save_schedules()
    now = datetime.now(get_tz())
    run = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if run <= now:
        run += timedelta(days=1)
    await interaction.response.send_message(
        f"✅ 已設定每日 {time_str} 在此頻道發送排程訊息（編號 {sid}）。\n"
        f"下次執行：{run.strftime('%Y-%m-%d %H:%M')}\n"
        f"內容：{prompt[:200]}",
        ephemeral=True,
    )


@bot.tree.command(name="schedules", description="列出所有排程訊息")
async def schedules_cmd(interaction: discord.Interaction) -> None:
    if not schedules:
        await interaction.response.send_message("目前沒有任何排程訊息。", ephemeral=True)
        return
    listing = "\n".join(
        f"{s['id']}. 每日 {s['time']} — <#{s['channel_id']}> — {s['prompt'][:80]}"
        for s in schedules
    )
    await interaction.response.send_message(f"**排程訊息：**\n{listing[:1900]}", ephemeral=True)


class ConfirmUnscheduleAllView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=60)

    @discord.ui.button(label="確定刪除", style=discord.ButtonStyle.danger, emoji="🗑️")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        count = len(schedules)
        schedules.clear()
        save_schedules()
        await interaction.response.edit_message(
            content=f"已刪除全部 {count} 個排程。🧹", view=None
        )

    @discord.ui.button(label="取消", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="已取消，排程保持不變。", view=None)


@bot.tree.command(name="unschedule", description="刪除排程訊息")
@app_commands.describe(number="要刪除的排程編號（用 /schedules 查看），或 all 全部刪除")
async def unschedule(interaction: discord.Interaction, number: str) -> None:
    if not can_manage_channel_settings(interaction):
        await interaction.response.send_message(
            "刪除排程只限擁有「管理伺服器」權限的成員。", ephemeral=True
        )
        return
    if number.lower() == "all":
        await interaction.response.send_message(
            f"確定要刪除全部 {len(schedules)} 個排程嗎？此操作無法復原。",
            view=ConfirmUnscheduleAllView(),
            ephemeral=True,
        )
        return
    if not number or not number.isdigit():
        await interaction.response.send_message(
            "請輸入要刪除的排程編號（用 /schedules 查看），或輸入 all 全部刪除。", ephemeral=True
        )
        return
    num = int(number)
    for i, s in enumerate(schedules):
        if s["id"] == num:
            schedules.pop(i)
            save_schedules()
            await interaction.response.send_message(f"已刪除排程 {num}。", ephemeral=True)
            return
    await interaction.response.send_message("找不到該編號的排程。", ephemeral=True)


def in_ai_channel(channel) -> bool:
    if isinstance(channel, discord.Thread):
        channel = channel.parent or channel
    return bool(
        AI_CHANNEL
        and (str(channel.id) == AI_CHANNEL or getattr(channel, "name", None) == AI_CHANNEL)
    )


async def react(message: discord.Message, emoji: str) -> None:
    try:
        await message.add_reaction(emoji)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def unreact(message: discord.Message, emoji: str) -> None:
    try:
        await message.remove_reaction(emoji, bot.user)
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return
    question = None
    if bot.user and bot.user.mention in message.content:
        question = message.content.replace(bot.user.mention, "").strip()
        if not question and not message.attachments:
            await message.reply("提及我並輸入問題，例如 @SatoAI 你好！")
            return
    elif message.reference and isinstance(message.reference.resolved, discord.Message):
        if message.reference.resolved.author.id == bot.user.id:
            question = message.content.strip()
    elif isinstance(message.channel, discord.DMChannel):
        question = message.content.strip()
    elif in_ai_channel(message.channel):
        question = message.content.strip()
    if question is not None or message.attachments:
        await react(message, "👀")
        urls = URL_RE.findall(question or "")
        status = None
        if urls:
            status = await message.reply(
                f"🌐 Fetching {min(len(urls), MAX_URLS_PER_MESSAGE)} web page(s)..."
            )
        try:
            hkey = message.author.id
            if in_ai_channel(message.channel):
                hkey = f"channel:{message.channel.id}"
            content = await build_content(message, question or "")
            if status:
                await status.delete()
            async with message.channel.typing():
                reply_task = asyncio.create_task(
                    answer_with_search(
                        message.author.id,
                        content,
                        message,
                        author=message.author.display_name,
                        history_key=hkey,
                    )
                )
                extract_task = asyncio.to_thread(llm_extract, question or "")
                reply, query = await reply_task
                extraction = await extract_task
            remember_extraction(message.id, extraction)
            fetched = []
            if isinstance(content, list):
                for p in content:
                    t = p.get("text", "")
                    if t.startswith("[Web page"):
                        fetched.append(t.split("]")[0].split(": ", 1)[-1])
            prefix = f"🌐 (read: {', '.join(fetched)})\n" if fetched else ""
            if query:
                prefix += f"🔍 (web search: {query})\n"
            if isinstance(hkey, str):
                stored_question = f"[{message.author.display_name}] {question or ''}".strip()
            else:
                stored_question = question or "[attached file(s)]"
            update_history(hkey, stored_question, reply)
            if message.author.id not in user_settings:
                user_settings[message.author.id] = {"max_history": 15, "style": ""}
                save_settings()
                try:
                    await message.reply(
                        "這是第一次和我對話呢！🎉 按下面的按鈕設定你的偏好"
                        "（之後可隨時用 `/aisetting` 修改）",
                        view=SettingsButtonView(),
                    )
                except discord.HTTPException:
                    pass
            intent_views = views_for_intents(extraction.get("intents")) or detect_intents(question or "")
            combined = None
            if intent_views:
                combined = discord.ui.View(timeout=None)
                for v in intent_views:
                    for child in v.children:
                        combined.add_item(child)
            chunks = [reply[i : i + 2000] for i in range(0, len(reply), 2000)]
            for i, chunk in enumerate(chunks):
                await message.reply(
                    (prefix if i == 0 else "") + chunk,
                    view=combined if i == len(chunks) - 1 else None,
                )
            await unreact(message, "👀")
            await react(message, "✅")
        except Exception as e:
            await message.reply(f"Error: {type(e).__name__}: {e}")
            await unreact(message, "👀")
            await react(message, "❌")


def run_web_server() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(start_web_server())
    loop.run_forever()


def main() -> None:
    if not DISCORD_TOKEN:
        raise SystemExit("Set the DISCORD_BOT_TOKEN environment variable.")
    if not SCRP_API_KEY:
        raise SystemExit("Set the SCRP_API_KEY environment variable.")
    load_history()
    load_memories()
    load_schedules()
    load_settings()
    threading.Thread(target=run_web_server, daemon=True).start()
    delay = 30
    while True:
        try:
            bot.run(DISCORD_TOKEN)
            break
        except discord.HTTPException as e:
            if e.status == 429:
                retry_after = None
                try:
                    retry_after = e.response.json().get("retry_after")
                except Exception:
                    pass
                wait = max(float(retry_after or 0), delay)
                print(f"Rate limited on login (retry_after={retry_after}) — retrying in {int(wait)}s...")
                time.sleep(wait)
                delay = min(delay * 2, 3600)
                continue
            raise
        except Exception:
            if bot.is_closed():
                print("Bot disconnected unexpectedly — restarting in 30s...")
                time.sleep(30)
                continue
            raise


if __name__ == "__main__":
    main()
