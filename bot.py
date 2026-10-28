"""
Spotify -> Telegram channel finder bot
لینک اسپاتیفای می‌گیره، کانال‌هایی که آهنگ رو گذاشتن پیدا می‌کنه و فایل رو می‌فرسته.
"""
import asyncio
import difflib
import html
import logging
import os
import re
import secrets
import tempfile
import time
import unicodedata
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from telethon import Button, TelegramClient, errors, events
from telethon.sessions import StringSession
from telethon.tl.types import (
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    InputMessagesFilterMusic,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("finder")


# ---------------------------------------------------------------- config
def _csv(name):
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

ALLOWED_USERS = {int(x) for x in _csv("ALLOWED_USERS")}
CHANNELS = [x.lstrip("@") for x in _csv("CHANNELS")]
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "15"))
MIN_SCORE = float(os.getenv("MIN_SCORE", "0.6"))
GLOBAL_LIMIT = int(os.getenv("GLOBAL_LIMIT", "100"))
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
PROXY_URL = os.getenv("PROXY", "").strip() or None
PORT = int(os.getenv("PORT", "0"))  # Render/Koyeb health check
SELF_URL = (os.getenv("RENDER_EXTERNAL_URL") or os.getenv("SELF_URL") or "").rstrip("/")


def _telethon_proxy():
    if not PROXY_URL:
        return None
    u = urlparse(PROXY_URL)
    return {
        "proxy_type": u.scheme,  # socks5 / http
        "addr": u.hostname,
        "port": u.port,
        "username": u.username,
        "password": u.password,
        "rdns": True,
    }


# ---------------------------------------------------------------- spotify
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}
TRACK_RE = re.compile(r"open\.spotify\.com/(?:intl-[a-zA-Z-]+/)?track/([A-Za-z0-9]{22})")
URL_RE = re.compile(r"https?://(?:open\.spotify\.com|spotify\.link|spoti\.fi)/\S+")


def _metas(page):
    out = {}
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        attrs = dict(re.findall(r'([\w:-]+)\s*=\s*"([^"]*)"', tag))
        key = attrs.get("property") or attrs.get("name")
        if key and "content" in attrs:
            out.setdefault(key, html.unescape(attrs["content"]))
    return out


async def _spotify_api(c, tid):
    tok = await c.post(
        "https://accounts.spotify.com/api/token",
        data={"grant_type": "client_credentials"},
        auth=(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
    )
    tok.raise_for_status()
    r = await c.get(
        f"https://api.spotify.com/v1/tracks/{tid}",
        headers={"Authorization": f"Bearer {tok.json()['access_token']}"},
    )
    r.raise_for_status()
    j = r.json()
    return {
        "title": j["name"],
        "artists": [a["name"] for a in j["artists"]],
        "duration": j["duration_ms"] // 1000,
    }


def parse_track_page(page):
    meta = _metas(page)
    title = meta.get("og:title", "").strip()
    artists = []
    md = meta.get("music:musician_description")
    if md:
        artists = [a.strip() for a in md.split(",") if a.strip()]
    if not artists:
        t = re.search(r"<title>(.*?)</title>", page, re.S | re.I)
        if t:
            mm = re.search(r"by (.+?) \| Spotify", html.unescape(t.group(1)))
            if mm:
                artists = [a.strip() for a in mm.group(1).split(",") if a.strip()]
    if not artists:
        parts = [p.strip() for p in meta.get("og:description", "").split("·") if p.strip()]
        if parts:
            artists = [parts[0]]
    dur = meta.get("music:duration", "")
    if not title:
        return None
    return {"title": title, "artists": artists, "duration": int(dur) if dur.isdigit() else None}


async def resolve_spotify(url):
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=20, headers=UA, proxy=PROXY_URL
    ) as c:
        m = TRACK_RE.search(url)
        if not m:  # لینک کوتاه spotify.link / spoti.fi
            r = await c.get(url)
            m = TRACK_RE.search(str(r.url)) or TRACK_RE.search(r.text)
        if not m:
            return None
        tid = m.group(1)
        if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
            try:
                return await _spotify_api(c, tid)
            except Exception as e:
                log.warning("Spotify API failed, falling back to page: %s", e)
        r = await c.get(f"https://open.spotify.com/track/{tid}")
        r.raise_for_status()
        return parse_track_page(r.text)


# ---------------------------------------------------------------- matching
_PAREN = re.compile(r"[\(\[].*?[\)\]]")
_JUNK = re.compile(
    r"\b(feat|ft|featuring|remaster(?:ed)?|official|audio|video|lyrics?|mp3|320|128|kbps)\b\.?"
)


def norm(s):
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = s.replace("ي", "ی").replace("ك", "ک")
    s = _PAREN.sub(" ", s)
    s = re.split(r"\s+-\s+(?:\d{4}\s+)?remaster", s)[0]
    s = _JUNK.sub(" ", s)
    s = re.sub(r"[^\w\s]|_", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _contains(hay, needle):
    return bool(needle) and re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", hay) is not None


def _sim(a, b):
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def score(track, msg):
    attrs = msg.document.attributes
    audio = next((a for a in attrs if isinstance(a, DocumentAttributeAudio)), None)
    fname = next((a.file_name for a in attrs if isinstance(a, DocumentAttributeFilename)), "")
    a_title = norm(audio.title if audio else "")
    a_perf = norm(audio.performer if audio else "")
    hay = " ".join(filter(None, [a_title, a_perf, norm(fname), norm(msg.message or "")]))

    t = norm(track["title"]) or track["title"].lower().strip()
    arts = [norm(a) for a in track["artists"] if norm(a)]

    s_t = max(_sim(t, a_title), 0.95 if _contains(hay, t) else 0.0)
    if arts:
        s_a = max(
            max(_sim(a, a_perf) for a in arts),
            0.95 if any(_contains(hay, a) for a in arts) else 0.0,
        )
        s = 0.6 * s_t + 0.4 * s_a
    else:
        s = s_t

    if track.get("duration") and audio and audio.duration:
        diff = abs(audio.duration - track["duration"])
        if diff <= 3:
            s += 0.05
        elif diff > 20:
            s -= 0.15
    return max(0.0, min(s, 1.0))


# ---------------------------------------------------------------- search
async def search(user, track):
    artist = track["artists"][0] if track["artists"] else ""
    queries = list(dict.fromkeys(
        q.strip() for q in (f"{artist} {track['title']}", track["title"]) if q.strip()
    ))
    found = {}

    def consider(m):
        if not m or not m.document:
            return
        key = (m.chat_id, m.id)
        if key in found:
            return
        sc = score(track, m)
        if sc >= MIN_SCORE:
            found[key] = (sc, m)

    # ۱) جستجوی سراسری در همه‌ی کانال/گروه‌هایی که اکانت عضوشونه
    for q in queries:
        try:
            async for m in user.iter_messages(
                None, search=q, filter=InputMessagesFilterMusic, limit=GLOBAL_LIMIT
            ):
                consider(m)
        except Exception as e:
            log.warning("global search '%s': %s", q, e)

    # ۲) کانال‌های عمومی لیست CHANNELS (عضویت لازم نیست)
    for ch in CHANNELS:
        for q in queries:
            try:
                async for m in user.iter_messages(
                    ch, search=q, filter=InputMessagesFilterMusic, limit=10
                ):
                    consider(m)
            except Exception as e:
                log.warning("channel %s: %s", ch, e)
                break

    # بهترین نتیجه از هر کانال
    best = {}
    for sc, m in found.values():
        cur = best.get(m.chat_id)
        if not cur or (sc, m.file.size or 0) > (cur[0], cur[1].file.size or 0):
            best[m.chat_id] = (sc, m)
    ranked = sorted(best.values(), key=lambda x: (-x[0], -(x[1].file.size or 0)))
    return ranked[:MAX_RESULTS]


# ---------------------------------------------------------------- formatting
def fmt_dur(s):
    return f"{s // 60}:{s % 60:02d}"


def msg_link(chat, mid):
    u = getattr(chat, "username", None)
    return f"https://t.me/{u}/{mid}" if u else f"https://t.me/c/{chat.id}/{mid}"


def describe(m):
    audio = next((a for a in m.document.attributes if isinstance(a, DocumentAttributeAudio)), None)
    size = m.file.size or 0
    bits = [f"💾 {size / 1048576:.1f}MB"]
    if audio and audio.duration:
        bits.append(f"⏱ {fmt_dur(audio.duration)}")
        kbps = size * 8 / audio.duration / 1000
        bits.append(f"🎧 ~{int(round(kbps / 32) * 32)}kbps")
    return " · ".join(bits)


# ---------------------------------------------------------------- bot
CACHE = {}  # token -> {"ts": float, "items": [(chat, msg_id, title)]}
CACHE_TTL = 6 * 3600
user: TelegramClient = None
bot: TelegramClient = None

HELP = (
    "🎵 لینک آهنگ از اسپاتیفای رو بفرست.\n"
    "یا دستی بنویس:  <code>Artist - Song</code>\n\n"
    "اتصال اکانت جستجوگر: <code>/login +شماره</code>\n\n"
    "آیدی عددی تو: <code>{uid}</code>"
)


def allowed(uid):
    return not ALLOWED_USERS or uid in ALLOWED_USERS


def _prune():
    now = time.time()
    for k in [k for k, v in CACHE.items() if now - v["ts"] > CACHE_TTL]:
        CACHE.pop(k, None)


# ---------------------------------------------------------------- in-bot login
LOGIN = {}  # sender_id -> {"step": "code"|"password", "phone": str, "hash": str}


async def handle_login(ev, text):
    """لاگین اکانت جستجوگر از داخل ربات (فقط ALLOWED_USERS)."""
    uid = ev.sender_id
    if text.startswith("/login"):
        if not ALLOWED_USERS:
            await ev.respond("اول ALLOWED_USERS رو تنظیم کن.")
            return True
        phone = re.sub(r"[^\d+]", "", text[6:])
        if not phone:
            await ev.respond("شماره رو این‌طوری بفرست:\n<code>/login +4915xxxxxxx</code>", parse_mode="html")
            return True
        try:
            sent = await user.send_code_request(phone)
        except Exception as e:
            await ev.respond(f"❌ {e}")
            return True
        LOGIN[uid] = {"step": "code", "phone": phone, "hash": sent.phone_code_hash}
        await ev.respond(
            "📩 کد تأیید رو تو تلگرامِ اون شماره ببین و اینجا بفرست.\n"
            "⚠️ حتماً با فاصله بین رقم‌ها، مثلاً <code>1 2 3 4 5</code>\n"
            "(وگرنه تلگرام کد رو باطل می‌کنه)", parse_mode="html")
        return True
    st = LOGIN.get(uid)
    if not st:
        return False
    try:
        if st["step"] == "code":
            code = re.sub(r"\D", "", text)
            try:
                await user.sign_in(st["phone"], code, phone_code_hash=st["hash"])
            except errors.SessionPasswordNeededError:
                st["step"] = "password"
                await ev.respond("🔐 رمز دو مرحله‌ای اکانت رو بفرست:")
                return True
        else:
            await user.sign_in(password=text)
            try:
                await ev.delete()
            except Exception:
                pass
    except Exception as e:
        LOGIN.pop(uid, None)
        await ev.respond(f"❌ لاگین ناموفق: {e}\nدوباره /login بزن.")
        return True
    LOGIN.pop(uid, None)
    me = await user.get_me()
    sess = user.session.save()
    log.info("SESSION_STRING=%s", sess)
    await ev.respond(
        f"✅ اکانت {me.first_name} وصل شد و ربات آماده‌ست.\n\n"
        "برای اینکه بعد از ری‌استارت سرور هم وصل بمونه، این رشته رو تو "
        "متغیر SESSION_STRING سرور بذار:\n\n"
        f"<code>{sess}</code>", parse_mode="html")
    return True


# ---------------------------------------------------------------- keep-alive
async def health_server():
    async def handle(reader, writer):
        try:
            await asyncio.wait_for(reader.read(1024), 5)
        except Exception:
            pass
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\n"
                     b"Content-Length: 2\r\nConnection: close\r\n\r\nok")
        try:
            await writer.drain()
        finally:
            writer.close()
    srv = await asyncio.start_server(handle, "0.0.0.0", PORT)
    log.info("Health server on :%s", PORT)
    async with srv:
        await srv.serve_forever()


async def self_ping():
    # Render free بعد از ۱۵ دقیقه بدون درخواست ورودی می‌خوابه؛ هر ۱۰ دقیقه خودمون رو صدا می‌زنیم
    async with httpx.AsyncClient(timeout=20) as c:
        while True:
            await asyncio.sleep(600)
            try:
                await c.get(SELF_URL + "/")
            except Exception as e:
                log.warning("self-ping: %s", e)


async def on_message(ev):
    if not ev.is_private:
        return
    text = (ev.raw_text or "").strip()
    if text.startswith("/start") or text.startswith("/help"):
        return await ev.respond(HELP.format(uid=ev.sender_id), parse_mode="html")
    if not allowed(ev.sender_id):
        return await ev.respond("⛔️ دسترسی نداری.")
    if await handle_login(ev, text):
        return
    if not text or text.startswith("/"):
        return
    if not await user.is_user_authorized():
        return await ev.respond(
            "⚠️ اکانت جستجوگر هنوز وصل نیست. با شماره‌ی دوم لاگین کن:\n"
            "<code>/login +4915xxxxxxx</code>", parse_mode="html")

    status = await ev.respond("⏳ دارم اطلاعات آهنگ رو می‌خونم…")
    m = URL_RE.search(text)
    if m:
        try:
            track = await resolve_spotify(m.group(0))
        except Exception as e:
            log.warning("spotify: %s", e)
            track = None
        if not track:
            return await status.edit(
                "❌ نتونستم اطلاعات آهنگ رو از اسپاتیفای بخونم.\n"
                "دستی بفرست:  <code>Artist - Song</code>", parse_mode="html")
    else:
        artist, _, title = text.rpartition(" - ")
        track = {"title": title.strip(), "artists": [artist.strip()] if artist.strip() else [],
                 "duration": None}

    artist_str = ", ".join(track["artists"]) or "—"
    head = f"🎵 <b>{html.escape(track['title'])}</b> — {html.escape(artist_str)}"
    await status.edit(f"{head}\n\n🔎 دارم کانال‌ها رو می‌گردم…", parse_mode="html")

    results = await search(user, track)
    if not results:
        return await status.edit(
            f"{head}\n\n😕 تو کانال‌ها چیزی پیدا نشد.\n"
            "اکانت جستجوگر رو تو کانال‌های موزیک بیشتری عضو کن یا CHANNELS رو کامل‌تر کن.",
            parse_mode="html")

    _prune()
    token = secrets.token_hex(4)
    items, lines = [], [f"{head}\n\n✅ {len(results)} کانال پیدا شد:\n"]
    for i, (sc, msg) in enumerate(results, 1):
        chat = msg.chat or await msg.get_chat()
        title = getattr(chat, "title", None) or "کانال"
        items.append((chat, msg.id, title))
        lines.append(
            f"{i}. <a href=\"{msg_link(chat, msg.id)}\">{html.escape(title)}</a>\n"
            f"    {describe(msg)} · تطابق {int(sc * 100)}٪"
        )
    CACHE[token] = {"ts": time.time(), "items": items}

    btns = [Button.inline(f"⬇️ {i}", f"d:{token}:{i - 1}".encode()) for i in range(1, len(items) + 1)]
    rows = [btns[i:i + 5] for i in range(0, len(btns), 5)]
    await status.edit("\n".join(lines), parse_mode="html", buttons=rows, link_preview=False)


async def on_download(ev):
    if not allowed(ev.sender_id):
        return await ev.answer("⛔️ دسترسی نداری", alert=True)
    token = ev.pattern_match.group(1).decode()
    idx = int(ev.pattern_match.group(2))
    entry = CACHE.get(token)
    if not entry or idx >= len(entry["items"]):
        return await ev.answer("نتایج منقضی شده؛ لینک رو دوباره بفرست.", alert=True)
    chat, mid, title = entry["items"][idx]
    await ev.answer("⏳ در حال دریافت فایل…")
    try:
        msg = await user.get_messages(chat, ids=mid)
        if not msg or not msg.document:
            return await ev.respond("❌ این پیام دیگه در دسترس نیست.")
        with tempfile.TemporaryDirectory() as d:
            path = await user.download_media(msg, file=d)
            await bot.send_file(
                ev.chat_id, path,
                attributes=msg.document.attributes,
                caption=f"📥 از: {title}",
            )
    except Exception as e:
        log.exception("download failed")
        await ev.respond(f"❌ ارسال فایل ناموفق بود: {e}")


async def main():
    global user, bot
    proxy = _telethon_proxy()
    user = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH, proxy=proxy)
    if PORT:
        asyncio.create_task(health_server())
    if SELF_URL:
        asyncio.create_task(self_ping())
    await user.connect()
    if await user.is_user_authorized():
        me = await user.get_me()
        log.info("Searcher account: %s (%s)", me.first_name, me.id)
    else:
        log.warning("Searcher account not logged in — use /login in the bot")

    bot = TelegramClient(StringSession(), API_ID, API_HASH, proxy=proxy)
    await bot.start(bot_token=BOT_TOKEN)
    bot.add_event_handler(on_message, events.NewMessage(incoming=True))
    bot.add_event_handler(on_download, events.CallbackQuery(pattern=rb"d:(\w+):(\d+)"))

    if not ALLOWED_USERS:
        log.warning("ALLOWED_USERS خالیه — همه می‌تونن از ربات استفاده کنن!")
    log.info("Bot is running…")
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
