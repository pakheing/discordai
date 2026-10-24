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
from datetime import datetime
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
SCRP_MODEL = os.environ.get("SCRP_MODEL", "default")
DISCORD_TOKEN = os.environ.get("DISCORD_BOT_TOKEN")
SYSTEM_PROMPT = os.environ.get(
    "SYSTEM_PROMPT", "You are a helpful assistant in a Discord server. Keep replies concise."
)

MAX_HISTORY = 20
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


def load_history() -> None:
    global history
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        history = {int(k): v for k, v in raw.items()}
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        history = {}


def save_history() -> None:
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
    except OSError as e:
        print(f"Warning: could not save history: {e}")


def ask_scrp(messages: list[dict]) -> str:
    resp = requests.post(
        SCRP_API_URL,
        headers={"Authorization": f"Bearer {SCRP_API_KEY}"},
        json={"model": SCRP_MODEL, "messages": messages},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def get_tz() -> ZoneInfo:
    try:
        return ZoneInfo(TIMEZONE)
    except Exception:
        return ZoneInfo("UTC")


def build_messages(user_id: int, user_message) -> list[dict]:
    now = datetime.now(get_tz())
    msgs = [
        {
            "role": "system",
            "content": (
                f"{SYSTEM_PROMPT}\n\n"
                f"Current date and time: {now.strftime('%A, %Y-%m-%d %H:%M (%Z)')}.\n\n"
                "You are a Discord bot. Available commands:\n"
                "- `/image <prompt>` — generate an image\n"
                "- `/search <query>` — search the web\n"
                "- `/read <url> [question]` — read and summarize a webpage\n"
                "- `/remind <duration> <text>` — set a reminder\n"
                "- `/ask <question>` — chat with the bot\n"
                "- `/history` — show conversation turns\n"
                "- `/reset` — clear your chat history\n\n"
                "You cannot create images yourself — tell users to use `/image`.\n"
                "If the user's message contains a URL, webpage content is automatically "
                "fetched and included in your context as \"[Web page N: ...]\" — use that "
                "content to answer directly. Only suggest `/read` if no webpage content "
                "appears in your context."
            ),
        }
    ]
    msgs.extend(history.get(user_id, []))
    msgs.append({"role": "user", "content": user_message})
    return msgs


def update_history(user_id: int, user_message: str, reply: str) -> None:
    history.setdefault(user_id, [])
    history[user_id].append({"role": "user", "content": user_message})
    history[user_id].append({"role": "assistant", "content": reply})
    if len(history[user_id]) > MAX_HISTORY:
        history[user_id] = history[user_id][-MAX_HISTORY:]
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
                b64 = base64.b64encode(data).decode()
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{att.content_type};base64,{b64}"},
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


class SCRPBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
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


async def answer_with_search(user_id: int, content, message: discord.Message = None):
    msgs = build_messages(user_id, content)
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
        return "I tried to search the web, but no search backend was available.", query
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
    reply = await asyncio.to_thread(ask_scrp, msgs)
    return reply, query


@bot.tree.command(name="ask", description="Ask the AI a question")
@app_commands.describe(question="Your question")
async def ask(interaction: discord.Interaction, question: str) -> None:
    await interaction.response.defer()
    try:
        reply, query = await answer_with_search(interaction.user.id, question)
    except Exception as e:
        await interaction.followup.send(f"Error: {type(e).__name__}: {e}")
        return
    update_history(interaction.user.id, question, reply)
    prefix = f"🔍 (web search: {query})\n" if query else ""
    await send_reply(interaction.followup, prefix + reply)


@bot.tree.command(name="aihelp", description="Show what this bot can do")
async def aihelp(interaction: discord.Interaction) -> None:
    await interaction.response.send_message(
        "** 點樣同我傾偈**\n"
        "- 喺任何頻道 **@提及我**，例如 `@bot 你好`\n"
        "- **回覆**我任何一則訊息\n"
        "- 喺 AI 頻道自由傾偈 — 我會回覆每一句\n"
        "- 附加 **圖片 / PDF / 文字檔**，我會幫你讀\n"
        "- 貼上 **網頁連結**，我會自動讀取內容（回覆會顯示 🌐）\n"
        "- 如果問題需要最新資訊，我會**自動上網搜尋**"
        "（回覆會顯示 🔍 並附來源）。香港天氣會用天文台官方資料。\n\n"
        "**指令**\n"
        "- `/ask <問題>` — 問我問題\n"
        "- `/image <描述>` — 生成圖片（10–60秒）\n"
        "- `/search <關鍵字>` — 上網搜尋並附來源回答\n"
        "- `/read <網址> [問題]` — 讀取網頁並回答問題\n"
        "- `/remind <時間> <內容>` — 設定提醒，例如 `/remind 30m 做功課`\n"
        "- `/history` — 顯示我記得幾多段對話\n"
        "- `/reset` — 清除你的對話記錄\n\n"
        "**你訊息上的反應**\n"
        "👀 處理中 → ✅ 完成 → ❌ 出錯",
        ephemeral=True,
    )


@bot.tree.command(name="reset", description="Clear your conversation history")
async def reset(interaction: discord.Interaction) -> None:
    history.pop(interaction.user.id, None)
    save_history()
    await interaction.response.send_message("Conversation history cleared.")


@bot.tree.command(name="history", description="Show your saved conversation turns")
async def history_cmd(interaction: discord.Interaction) -> None:
    turns = len(history.get(interaction.user.id, [])) // 2
    await interaction.response.send_message(
        f"You have {turns} saved conversation turn(s) (last {MAX_HISTORY // 2} kept)."
    )


@bot.tree.command(name="remind", description="Set a reminder")
@app_commands.describe(
    duration="How long from now, e.g. 30s, 10m, 2h, 1d, or 1h30m",
    text="What to remind you about",
)
async def remind(interaction: discord.Interaction, duration: str, text: str) -> None:
    seconds = parse_duration(duration)
    if seconds is None:
        await interaction.response.send_message(
            "Invalid duration. Use something like `30s`, `10m`, `2h`, `1d`, or `1h30m`.",
            ephemeral=True,
        )
        return

    async def fire() -> None:
        await asyncio.sleep(seconds)
        await interaction.channel.send(f"{interaction.user.mention} ⏰ Reminder: {text}")

    asyncio.create_task(fire())
    await interaction.response.send_message(
        f"Got it — I'll remind you in {format_duration(seconds)}."
    )


@bot.tree.command(name="search", description="Search the web")
@app_commands.describe(query="What to search for")
async def search(interaction: discord.Interaction, query: str) -> None:
    await interaction.response.defer()
    results = await asyncio.to_thread(web_search, query)
    if not results:
        await interaction.followup.send(
            "Search failed — no search backend available. "
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


@bot.tree.command(name="read", description="Read a webpage and answer questions about it")
@app_commands.describe(url="The URL to read", question="Optional question about the page")
async def read_url(interaction: discord.Interaction, url: str, question: str = None) -> None:
    await interaction.response.defer()
    try:
        text = await asyncio.to_thread(fetch_url_text, url)
    except Exception as e:
        await interaction.followup.send(f"Failed to fetch {url}: {type(e).__name__}: {e}")
        return
    if not text:
        await interaction.followup.send("Could not extract any text from that page.")
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


@bot.tree.command(name="image", description="Generate an image from a text prompt")
@app_commands.describe(prompt="What to draw")
async def image(interaction: discord.Interaction, prompt: str) -> None:
    await interaction.response.defer()
    url = (
        "https://image.pollinations.ai/prompt/"
        f"{requests.utils.quote(prompt)}?width=1024&height=1024&nologo=true"
    )
    try:
        resp = await asyncio.to_thread(requests.get, url, None, {"timeout": 300})
    except requests.RequestException as e:
        await interaction.followup.send(f"Error generating image: {e}")
        return
    if resp.status_code != 200 or "image" not in resp.headers.get("Content-Type", ""):
        await interaction.followup.send(
            f"Image generation failed (HTTP {resp.status_code}) — try a different prompt."
        )
        return
    if len(resp.content) < 5000:
        await interaction.followup.send("Image generation returned an empty result — try again.")
        return
    fd, filename = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        with open(filename, "wb") as f:
            f.write(resp.content)
        await interaction.followup.send(file=discord.File(filename, filename="generated.png"))
    finally:
        os.remove(filename)


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
            await message.reply("Mention me with a question, e.g. @bot hello!")
            return
    elif message.reference and isinstance(message.reference.resolved, discord.Message):
        if message.reference.resolved.author.id == bot.user.id:
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
            content = await build_content(message, question or "")
            if status:
                await status.delete()
            async with message.channel.typing():
                reply, query = await answer_with_search(message.author.id, content, message)
            fetched = []
            if isinstance(content, list):
                for p in content:
                    t = p.get("text", "")
                    if t.startswith("[Web page"):
                        fetched.append(t.split("]")[0].split(": ", 1)[-1])
            prefix = f"🌐 (read: {', '.join(fetched)})\n" if fetched else ""
            if query:
                prefix += f"🔍 (web search: {query})\n"
            update_history(message.author.id, question or "[attached file(s)]", reply)
            chunks = [reply[i : i + 2000] for i in range(0, len(reply), 2000)]
            for i, chunk in enumerate(chunks):
                await message.reply((prefix if i == 0 else "") + chunk)
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
    threading.Thread(target=run_web_server, daemon=True).start()
    delay = 30
    while True:
        try:
            bot.run(DISCORD_TOKEN)
            break
        except discord.HTTPException as e:
            if e.status == 429:
                print(f"Rate limited on login — retrying in {delay}s...")
                time.sleep(delay)
                delay = min(delay * 2, 900)
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
