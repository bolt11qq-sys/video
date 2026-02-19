"""
╔══════════════════════════════════════════════════════════════╗
║           ELITE VIDEO CIRCLE BOT  •  v3.0                   ║
║  Async • CRF-encode • Elite Admin Panel • Notify on Upload   ║
╚══════════════════════════════════════════════════════════════╝

YANGI IMKONIYATLAR (v3.0):
  ✅ Admin panel: majburiy kanallar qo'shish/o'chirish/ro'yxat
  ✅ Foydalanuvchi video yuborganda adminga bildirishnoma
  ✅ Admin: foydalanuvchini ban/unban
  ✅ Admin: bot statistikasi (kunlik, haftalik, umumiy)
  ✅ Admin: foydalanuvchiga shaxsiy xabar yuborish
  ✅ Ko'p kanallar uchun majburiy obuna
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from functools import wraps
from pathlib import Path

os.environ["TZ"] = "UTC"

import ffmpeg
from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    BotCommand,
    InputMediaVideo,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TimedOut, TelegramError, Forbidden
from telegram.ext import (
    Application,
    CallbackContext,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
)

from config import API_TOKEN

# ──────────────────────────────────────────────
#  LOGGING
# ──────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    level=logging.INFO,
)
logger = logging.getLogger("EliteBot")

# ──────────────────────────────────────────────
#  ASOSIY SOZLAMALAR
# ──────────────────────────────────────────────
ADMIN_IDS: set[int] = {6393412744}

# Majburiy kanallar DB da saqlanadi (quyida)
# Bu faqat boshlang'ich kanal — DB bo'sh bo'lsa ishlaydi
DEFAULT_CHANNELS: list[dict] = [
    {"username": "@uzdevnet", "link": "https://t.me/uzdevnet", "title": "UzDevNet"},
]

MAX_DURATION_SECONDS = 60
TG_API_LIMIT_MB      = 20
MAX_ACCEPT_MB        = 19
MAX_ACCEPT_BYTES     = MAX_ACCEPT_MB * 1024 * 1024
MAX_SEND_MB          = 18
MAX_SEND_BYTES       = MAX_SEND_MB * 1024 * 1024

CIRCLE_SIZE   = 384
AUDIO_BITRATE = "96k"
TEMP_DIR      = Path("tmp")
DB_FILE       = Path("db.json")

RATE_LIMIT_WINDOW = 30
RATE_LIMIT_MAX    = 5
MAX_WORKERS       = os.cpu_count() or 2

# Admin notify: foydalanuvchi video yuborganda adminga xabar berish
ADMIN_NOTIFY_ENABLED = True

# ──────────────────────────────────────────────
#  CONVERSATION STATES
# ──────────────────────────────────────────────
(
    ADM_BROADCAST,
    ADM_ADD_CHANNEL,
    ADM_MSG_USER,
    ADM_MSG_USER_TEXT,
) = range(4)

# ──────────────────────────────────────────────
#  GLOBAL STATE
# ──────────────────────────────────────────────
TEMP_DIR.mkdir(exist_ok=True)

_rate_store: dict[int, list[float]] = defaultdict(list)
_conv_semaphore: asyncio.Semaphore | None = None

# callback_data qisqa kalit xaritasi
_uid_map: dict[str, str] = {}
_uid_counter: int = 0


def _make_key(file_uid: str) -> str:
    global _uid_counter
    for k, v in _uid_map.items():
        if v == file_uid:
            return k
    _uid_counter += 1
    key = str(_uid_counter)
    _uid_map[key] = file_uid
    if len(_uid_map) > 2000:
        del _uid_map[next(iter(_uid_map))]
    return key


# ══════════════════════════════════════════════
#  DATABASE  (JSON)
# ══════════════════════════════════════════════
_db_lock = asyncio.Lock()

_DEFAULT_DB = {
    "users":    {},      # uid → {id, first_name, username, created_at, last_seen,
                         #         total_conversions, banned}
    "channels": [],      # [{username, link, title}]
    "settings": {
        "notify_admin": True,
    },
    "stats": {
        "total_conversions": 0,
        "daily": {},     # "YYYY-MM-DD" → count
    },
}


def _load_db() -> dict:
    if not DB_FILE.exists():
        db = _DEFAULT_DB.copy()
        db["channels"] = list(DEFAULT_CHANNELS)
        _write_db_sync(db)
        return db
    try:
        data = json.loads(DB_FILE.read_text(encoding="utf-8"))
        # migrate: eski faylda channels yo'q bo'lsa
        if "channels" not in data:
            data["channels"] = list(DEFAULT_CHANNELS)
        if "settings" not in data:
            data["settings"] = {"notify_admin": True}
        if "stats" not in data:
            data["stats"] = {"total_conversions": 0, "daily": {}}
        return data
    except Exception:
        return _DEFAULT_DB.copy()


def _write_db_sync(db: dict) -> None:
    tmp = DB_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(DB_FILE)


async def _awrite_db(db: dict) -> None:
    async with _db_lock:
        _write_db_sync(db)


# ──────────────────────────────────────────────
#  USER HELPERS
# ──────────────────────────────────────────────
async def track_user(update: Update) -> None:
    user = update.effective_user
    if not user:
        return
    async with _db_lock:
        db  = _load_db()
        uid = str(user.id)
        now = datetime.now(timezone.utc).isoformat()
        entry = db["users"].setdefault(uid, {
            "id": user.id, "first_name": user.first_name or "",
            "username": user.username, "created_at": now,
            "total_conversions": 0, "banned": False,
            "last_seen": now,
        })
        entry.update({
            "last_seen": now,
            "first_name": user.first_name or "",
            "username": user.username,
        })
        _write_db_sync(db)


async def increment_conversion(user_id: int) -> int:
    async with _db_lock:
        db  = _load_db()
        uid = str(user_id)
        today = datetime.now(timezone.utc).date().isoformat()
        if uid in db["users"]:
            n = db["users"][uid].get("total_conversions", 0) + 1
            db["users"][uid]["total_conversions"] = n
        db["stats"]["total_conversions"] = db["stats"].get("total_conversions", 0) + 1
        db["stats"].setdefault("daily", {})[today] = \
            db["stats"]["daily"].get(today, 0) + 1
        _write_db_sync(db)
        return db["users"].get(uid, {}).get("total_conversions", 0)


def is_banned(user_id: int) -> bool:
    db = _load_db()
    return db["users"].get(str(user_id), {}).get("banned", False)


# ──────────────────────────────────────────────
#  CHANNELS HELPERS
# ──────────────────────────────────────────────
def get_channels() -> list[dict]:
    return _load_db().get("channels", [])


async def add_channel(username: str, link: str, title: str) -> None:
    async with _db_lock:
        db = _load_db()
        # dublikat tekshirish
        for ch in db["channels"]:
            if ch["username"].lower() == username.lower():
                return
        db["channels"].append({"username": username, "link": link, "title": title})
        _write_db_sync(db)


async def remove_channel(username: str) -> bool:
    async with _db_lock:
        db = _load_db()
        before = len(db["channels"])
        db["channels"] = [c for c in db["channels"]
                          if c["username"].lower() != username.lower()]
        changed = len(db["channels"]) < before
        if changed:
            _write_db_sync(db)
        return changed


# ──────────────────────────────────────────────
#  SUBSCRIPTION CHECK  (ko'p kanal)
# ──────────────────────────────────────────────
async def get_unsub_channels(bot, user_id: int) -> list[dict]:
    """Foydalanuvchi a'zo bo'lmagan kanallar ro'yxati."""
    channels = get_channels()
    if not channels:
        return []
    unsub = []
    for ch in channels:
        try:
            m = await bot.get_chat_member(ch["username"], user_id)
            if m.status not in ("creator", "administrator", "member"):
                unsub.append(ch)
        except TelegramError:
            pass  # kanal topilmasa yoki xato — o'tkazib yuboramiz
    return unsub


def sub_keyboard(unsub_channels: list[dict]) -> InlineKeyboardMarkup:
    buttons = []
    for ch in unsub_channels:
        buttons.append([InlineKeyboardButton(
            f"📢 {ch['title']} ga a'zo bo'lish", url=ch["link"]
        )])
    buttons.append([InlineKeyboardButton("✅ Tekshirish", callback_data="checksub")])
    return InlineKeyboardMarkup(buttons)


# ──────────────────────────────────────────────
#  RATE LIMITER
# ──────────────────────────────────────────────
def check_rate_limit(user_id: int) -> tuple[bool, int]:
    if user_id in ADMIN_IDS:
        return True, 0
    now    = time.monotonic()
    window = [t for t in _rate_store[user_id] if now - t < RATE_LIMIT_WINDOW]
    _rate_store[user_id] = window
    if len(window) >= RATE_LIMIT_MAX:
        return False, int(RATE_LIMIT_WINDOW - (now - window[0])) + 1
    _rate_store[user_id].append(now)
    return True, 0


# ──────────────────────────────────────────────
#  DECORATORS
# ──────────────────────────────────────────────
def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, context: CallbackContext):
        uid = update.effective_user.id if update.effective_user else None
        if uid not in ADMIN_IDS:
            if update.message:
                await update.message.reply_text("🚫 Ruxsat yo'q.")
            elif update.callback_query:
                await update.callback_query.answer("🚫 Ruxsat yo'q.", show_alert=True)
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


# ──────────────────────────────────────────────
#  HELPERS
# ──────────────────────────────────────────────
def esc(text: str) -> str:
    return re.sub(r"([_*\[\]()~`>#+=|{}.!\\-])", r"\\\1", str(text))


def cleanup(*paths):
    for p in paths:
        try:
            if p and Path(p).exists():
                Path(p).unlink()
        except OSError:
            pass


def get_paths(user_id: int) -> tuple[Path, Path]:
    return TEMP_DIR / f"in_{user_id}.tmp", TEMP_DIR / f"out_{user_id}.mp4"


async def safe_edit(msg, text: str, **kw):
    try:
        await msg.edit_text(text, **kw)
    except (BadRequest, TelegramError):
        pass


def fmt_size(b: int) -> str:
    if b >= 1024 * 1024:
        return f"{b / 1024 / 1024:.1f} MB"
    return f"{b // 1024} KB"


def fmt_dur(sec: float) -> str:
    sec = int(sec)
    m, s = divmod(sec, 60)
    return f"{m}:{s:02d}" if m else f"{s}s"


# ══════════════════════════════════════════════
#  ADMIN NOTIFY  — foydalanuvchi video yuborganda
# ══════════════════════════════════════════════
async def notify_admins(bot, user, media_info: dict) -> None:
    """Har bir adminga foydalanuvchi haqida xabar yuboradi va videoni ulashadi."""
    db = _load_db()
    if not db["settings"].get("notify_admin", True):
        return

    uname  = f"@{user.username}" if user.username else "—"
    uid    = user.id
    name   = user.first_name or "?"
    mtype  = media_info.get("type", "video")
    mdur   = media_info.get("duration", 0)
    msize  = media_info.get("size", 0)
    total  = db["users"].get(str(uid), {}).get("total_conversions", 0)

    fchat = media_info.get("chat_id")
    fmsg  = media_info.get("message_id")

    text = (
        f"🎬 *Yangi so'rov*\n\n"
        f"👤 Foydalanuvchi: [{esc(name)}](tg://user?id={uid})\n"
        f"🆔 ID: `{uid}`\n"
        f"📛 Username: {esc(uname)}\n"
        f"📁 Tur: {esc(mtype.upper())}\n"
        f"⏱ Davomiylik: {esc(fmt_dur(mdur))}\n"
        f"💾 Hajm: {esc(fmt_size(msize))}\n"
        f"🔢 Jami konvertatsiyalar: {esc(str(total))}"
    )

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✉️ Xabar yuborish", callback_data=f"adm_msg:{uid}"),
        InlineKeyboardButton("🚫 Ban", callback_data=f"adm_ban:{uid}"),
    ]])

    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode=ParseMode.MARKDOWN_V2,
                reply_markup=kb,
            )
        except Exception as e:
            logger.warning(f"Admin notify xatosi [{admin_id}]: {e}")

        if fchat and fmsg:
            try:
                await bot.forward_message(
                    chat_id=admin_id,
                    from_chat_id=fchat,
                    message_id=fmsg,
                )
            except Exception as e:
                logger.warning(f"Admin forward xatosi [{admin_id}]: {e}")


# ══════════════════════════════════════════════
#  FFMPEG
# ══════════════════════════════════════════════
def _encode_once(inp_path, out_path, trim_sec, cx, cy, side, crf, has_audio, mute):
    inp   = ffmpeg.input(inp_path, ss=0, t=trim_sec)
    video = (
        inp.video
        .crop(x=cx, y=cy, width=side, height=side)
        .filter("scale", CIRCLE_SIZE, CIRCLE_SIZE, flags="lanczos")
    )
    base = {
        "vcodec": "libx264", "crf": crf, "preset": "fast",
        "profile:v": "baseline", "level": "3.0",
        "pix_fmt": "yuv420p", "movflags": "+faststart",
    }
    no_audio = mute or not has_audio
    if no_audio:
        out = ffmpeg.output(video, out_path, **base, an=None)
    else:
        out = ffmpeg.output(video, inp.audio, out_path,
                            **base, acodec="aac",
                            **{"b:a": AUDIO_BITRATE}, ar=44100)
    out.overwrite_output().run(capture_stdout=True, capture_stderr=True)


def _ffmpeg_convert(inp_path: str, out_path: str, mute: bool) -> None:
    try:
        probe = ffmpeg.probe(inp_path)
    except ffmpeg.Error as e:
        raise RuntimeError(f"Fayl o'qilmadi: {(e.stderr or b'').decode(errors='replace')[-150:]}")

    vs = next((s for s in probe["streams"] if s["codec_type"] == "video"), None)
    if not vs:
        raise RuntimeError("Video stream topilmadi")

    w, h      = int(vs.get("width", 0)), int(vs.get("height", 0))
    has_audio = any(s["codec_type"] == "audio" for s in probe["streams"])
    raw_dur   = float(probe["format"].get("duration", MAX_DURATION_SECONDS))
    trim_sec  = min(raw_dur, float(MAX_DURATION_SECONDS))
    side      = min(w, h)
    cx, cy    = (w - side) // 2, (h - side) // 2

    for crf in [22, 28, 32]:
        _encode_once(inp_path, out_path, trim_sec, cx, cy, side, crf, has_audio, mute)
        size = Path(out_path).stat().st_size if Path(out_path).exists() else 0
        logger.info(f"CRF={crf} → {size / 1024 / 1024:.1f}MB")
        if size <= MAX_SEND_BYTES:
            return
    logger.error("Barcha CRF bosqichlaridan so'ng ham katta")


async def convert_async(inp: str, out: str, mute: bool) -> None:
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _ffmpeg_convert, inp, out, mute)


# ══════════════════════════════════════════════
#  /start  /help  /stats
# ══════════════════════════════════════════════
async def cmd_start(update: Update, context: CallbackContext):
    await track_user(update)
    user = update.effective_user

    if is_banned(user.id):
        await update.message.reply_text("🚫 Siz bloklangansiz.")
        return

    unsub = await get_unsub_channels(context.bot, user.id)
    if unsub:
        names = ", ".join(ch["title"] for ch in unsub)
        await update.message.reply_text(
            f"Botdan foydalanish uchun quyidagi kanallarga a'zo bo'ling:\n\n{names}",
            reply_markup=sub_keyboard(unsub),
        )
        return

    name = esc(user.first_name or "Do'st")
    await update.message.reply_text(
        f"Salom, {name}\\! 👋\n\n"
        "🎥 Video yoki GIF → Telegram *krujok*\\!\n\n"
        "Faylni yuboring, men bir zumda tayyorlab beraman ✨\n\n"
        f"▪️ Max davomiylik: {MAX_DURATION_SECONDS}s \\(avtomat kesiladi\\)\n"
        f"▪️ Max hajm: {MAX_ACCEPT_MB} MB\n"
        f"▪️ Sifat: {CIRCLE_SIZE}×{CIRCLE_SIZE} HD\n\n"
        "/stats \\— statistika  |  /help \\— yordam",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_help(update: Update, context: CallbackContext):
    await track_user(update)
    channels = get_channels()
    ch_list  = "\n".join(f"▪️ {esc(ch['title'])} \\({esc(ch['username'])}\\)"
                         for ch in channels) or "Majburiy kanal yo'q"
    await update.message.reply_text(
        "📖 *Yordam*\n\n"
        "1\\. Video yoki GIF yuboring\n"
        "2\\. Ovoz saqlash / o'chirish ni tanlang\n"
        "3\\. Krujok tayyor\\!\n\n"
        f"⚠️ Max davomiylik: {MAX_DURATION_SECONDS}s\n"
        f"⚠️ Max hajm: {MAX_ACCEPT_MB} MB\n\n"
        f"📢 *Majburiy kanallar:*\n{ch_list}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def cmd_stats(update: Update, context: CallbackContext):
    await track_user(update)
    uid = str(update.effective_user.id)
    db  = _load_db()
    u   = db["users"].get(uid, {})
    await update.message.reply_text(
        "📊 *Sizning statistikangiz*\n\n"
        f"🎬 Konvertatsiyalar: *{esc(str(u.get('total_conversions', 0)))}*\n"
        f"📅 Qo'shilgan: {esc(u.get('created_at', '?')[:10])}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ══════════════════════════════════════════════
#  CHECKSUB callback
# ══════════════════════════════════════════════
async def cb_checksub(update: Update, context: CallbackContext):
    q = update.callback_query
    await q.answer()
    await track_user(update)

    unsub = await get_unsub_channels(context.bot, q.from_user.id)
    if unsub:
        names = "\n".join(f"▪️ {ch['title']}" for ch in unsub)
        await safe_edit(
            q.message,
            f"❌ Hali quyidagilarga a'zo emassiz:\n\n{names}\n\nA'zo bo'lib, qayta bosing\\.",
            reply_markup=sub_keyboard(unsub),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await safe_edit(
            q.message,
            "✅ A'zolik tasdiqlandi\\! Endi video yoki GIF yuboring\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )


# ══════════════════════════════════════════════
#  MEDIA HANDLER
# ══════════════════════════════════════════════
async def handle_media(update: Update, context: CallbackContext):
    await track_user(update)
    user    = update.effective_user
    message = update.message

    if is_banned(user.id):
        await message.reply_text("🚫 Siz bloklangansiz.")
        return

    unsub = await get_unsub_channels(context.bot, user.id)
    if unsub:
        await message.reply_text(
            "Botdan foydalanish uchun kanallarga a'zo bo'ling.",
            reply_markup=sub_keyboard(unsub),
        )
        return

    ok, wait = check_rate_limit(user.id)
    if not ok:
        await message.reply_text(
            f"⏳ *{esc(str(wait))}* soniyadan so'ng urinib ko'ring\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    media = message.video or message.animation
    if not media:
        await message.reply_text("Iltimos, video yoki GIF yuboring.")
        return

    media_type = "video" if message.video else "gif"
    file_id    = media.file_id
    file_uid   = media.file_unique_id
    media_size = getattr(media, "file_size", None) or 0
    media_dur  = getattr(media, "duration", 0) or 0

    # Hajm tekshiruvi (oldindan)
    if media_size and media_size > MAX_ACCEPT_BYTES:
        await message.reply_text(
            f"❌ Fayl juda katta \\({esc(fmt_size(media_size))}\\)\\.\n"
            f"Maksimal: *{MAX_ACCEPT_MB} MB*",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    # Admin notify (fon vazifasi — kutmaymiz)
    asyncio.create_task(notify_admins(context.bot, user, {
        "type": media_type,
        "duration": media_dur,
        "size": media_size,
        "chat_id": message.chat_id,
        "message_id": message.message_id,
    }))

    status  = await message.reply_text("⏳ Yuklab olinmoqda\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    in_path, _ = get_paths(user.id)

    try:
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)
        try:
            tg_file = await context.bot.get_file(file_id)
        except BadRequest as e:
            err = str(e).lower()
            if "too big" in err or "too large" in err:
                await safe_edit(status,
                    f"❌ Telegram API {TG_API_LIMIT_MB}MB dan katta fayllarni "
                    "yuklab bermaydi\\.",
                    parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await safe_edit(status, "❌ Yuklab bo'lmadi\\. Qayta urinib ko'ring\\.",
                                parse_mode=ParseMode.MARKDOWN_V2)
            return

        if tg_file.file_size and tg_file.file_size > MAX_ACCEPT_BYTES:
            await safe_edit(status,
                f"❌ Fayl katta \\({esc(fmt_size(tg_file.file_size))}\\)\\. "
                f"Max: *{MAX_ACCEPT_MB} MB*",
                parse_mode=ParseMode.MARKDOWN_V2)
            return

        await tg_file.download_to_drive(str(in_path))

    except TimedOut:
        await safe_edit(status, "❌ Yuklash vaqti tugadi\\. Qayta urinib ko'ring\\.",
                        parse_mode=ParseMode.MARKDOWN_V2)
        cleanup(in_path)
        return
    except TelegramError as e:
        logger.error(f"Download xato [{user.id}]: {e}")
        await safe_edit(status, "❌ Yuklab bo'lmadi\\.", parse_mode=ParseMode.MARKDOWN_V2)
        cleanup(in_path)
        return

    # Probe
    try:
        probe    = await asyncio.get_event_loop().run_in_executor(None, ffmpeg.probe, str(in_path))
        duration = float(probe["format"].get("duration", 0))
    except Exception:
        duration = float(media_dur)

    dur_text = fmt_dur(duration) if duration else "?"
    trimmed  = duration > MAX_DURATION_SECONDS

    caption = (
        f"🎬 Davomiylik: *{esc(dur_text)}*"
        + (f"\n✂️ Boshidan {MAX_DURATION_SECONDS}s olinadi" if trimmed else "")
        + "\n\n*Ovoz bilan yuboraymi?*"
    )

    short_key = _make_key(file_uid)
    context.user_data[f"m_{short_key}"] = {
        "in_path": str(in_path), "chat_id": message.chat_id,
        "status_id": status.message_id,
    }

    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔊 Ovoz bilan", callback_data=f"conv:a:{short_key}"),
            InlineKeyboardButton("🔇 Ovozsiz",    callback_data=f"conv:m:{short_key}"),
        ],
        [InlineKeyboardButton("❌ Bekor", callback_data=f"conv:c:{short_key}")],
    ])
    await safe_edit(status, caption, reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2)


# ══════════════════════════════════════════════
#  PROCESS CALLBACK
# ══════════════════════════════════════════════
async def cb_process(update: Update, context: CallbackContext):
    q = update.callback_query
    await q.answer()
    await track_user(update)

    user = q.from_user
    if is_banned(user.id):
        await q.answer("🚫 Siz bloklangansiz.", show_alert=True)
        return

    unsub = await get_unsub_channels(context.bot, user.id)
    if unsub:
        await q.answer("Kanalga a'zo bo'ling!", show_alert=True)
        return

    parts = q.data.split(":", 2)
    if len(parts) != 3:
        return
    _, choice, short_key = parts

    data = context.user_data.pop(f"m_{short_key}", None)
    if not data:
        await safe_edit(q.message, "⚠️ Ma'lumot topilmadi\\. Mediani qayta yuboring\\.",
                        parse_mode=ParseMode.MARKDOWN_V2)
        return

    in_path, chat_id, status_id = data["in_path"], data["chat_id"], data["status_id"]

    if choice == "c":
        await safe_edit(q.message, "❌ Bekor qilindi\\.", parse_mode=ParseMode.MARKDOWN_V2)
        cleanup(in_path)
        return

    mute = (choice == "m")
    _, out_path = get_paths(user.id)

    await safe_edit(q.message, "⚙️ Konvertatsiya boshlandi\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.RECORD_VIDEO_NOTE)

    global _conv_semaphore
    if _conv_semaphore is None:
        _conv_semaphore = asyncio.Semaphore(MAX_WORKERS)

    try:
        async with _conv_semaphore:
            await convert_async(str(in_path), str(out_path), mute)
    except RuntimeError as e:
        await safe_edit(q.message, f"❌ {esc(str(e)[:100])}", parse_mode=ParseMode.MARKDOWN_V2)
        cleanup(in_path, out_path)
        return
    except ffmpeg.Error as e:
        logger.error(f"FFmpeg [{user.id}]: {(e.stderr or b'').decode(errors='replace')[-300:]}")
        await safe_edit(q.message, "❌ Konvertatsiyada xatolik\\. Boshqa video yuboring\\.",
                        parse_mode=ParseMode.MARKDOWN_V2)
        cleanup(in_path, out_path)
        return
    except Exception as e:
        logger.error(f"Konvertatsiya [{user.id}]: {e}", exc_info=True)
        await safe_edit(q.message, "❌ Xatolik yuz berdi\\.", parse_mode=ParseMode.MARKDOWN_V2)
        cleanup(in_path, out_path)
        return

    out_size = out_path.stat().st_size if out_path.exists() else 0
    if out_size > MAX_SEND_BYTES:
        await safe_edit(q.message,
            f"❌ Natija fayl katta \\({esc(fmt_size(out_size))}\\)\\. Qisqaroq video yuboring\\.",
            parse_mode=ParseMode.MARKDOWN_V2)
        cleanup(in_path, out_path)
        return

    await safe_edit(q.message, "📤 Yuborilmoqda\\.\\.\\.", parse_mode=ParseMode.MARKDOWN_V2)
    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.UPLOAD_VIDEO_NOTE)

    try:
        with out_path.open("rb") as f:
            await context.bot.send_video_note(chat_id=chat_id, video_note=f, length=CIRCLE_SIZE)
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=status_id)
        except Exception:
            pass
        total = await increment_conversion(user.id)
        logger.info(f"✅ [{user.id}] mute={mute} jami={total}")
    except TimedOut:
        await safe_edit(q.message,
            "⚠️ Yuborish cho'zildi\\. Chatni tekshiring\\.", parse_mode=ParseMode.MARKDOWN_V2)
    except TelegramError as e:
        logger.error(f"Yuborish [{user.id}]: {e}")
        await safe_edit(q.message, "❌ Yuborishda xatolik\\.", parse_mode=ParseMode.MARKDOWN_V2)
    finally:
        cleanup(in_path, out_path)


# ══════════════════════════════════════════════════════════════
#  ELITE ADMIN PANEL
# ══════════════════════════════════════════════════════════════

def admin_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Statistika",    callback_data="adm:stats"),
            InlineKeyboardButton("👥 Foydalanuvchilar", callback_data="adm:users"),
        ],
        [
            InlineKeyboardButton("📢 Kanallar",      callback_data="adm:channels"),
            InlineKeyboardButton("📣 Broadcast",     callback_data="adm:broadcast"),
        ],
        [
            InlineKeyboardButton("🔔 Notify: ON/OFF", callback_data="adm:toggle_notify"),
            InlineKeyboardButton("🧹 Tmp tozalash",  callback_data="adm:cleantmp"),
        ],
    ])


@admin_only
async def cmd_admin(update: Update, context: CallbackContext):
    await track_user(update)
    db    = _load_db()
    users = db["users"]
    total_conv = db["stats"].get("total_conversions", 0)
    today      = datetime.now(timezone.utc).date().isoformat()
    today_conv = db["stats"].get("daily", {}).get(today, 0)
    notify_on  = db["settings"].get("notify_admin", True)

    text = (
        "🛡 *Elite Admin Panel*\n\n"
        f"👥 Foydalanuvchilar: *{esc(str(len(users)))}*\n"
        f"🎬 Bugungi konvertatsiya: *{esc(str(today_conv))}*\n"
        f"🔢 Jami konvertatsiya: *{esc(str(total_conv))}*\n"
        f"🔔 Admin notify: *{notify_on and '✅ Yoqiq' or '❌ Ochiq'}*\n"
        f"📢 Kanallar soni: *{esc(str(len(get_channels())))}*"
    )
    await update.message.reply_text(text, reply_markup=admin_main_kb(),
                                    parse_mode=ParseMode.MARKDOWN_V2)


# ── Admin callback dispatcher ──────────────────
@admin_only
async def cb_admin(update: Update, context: CallbackContext):
    q      = update.callback_query
    await q.answer()
    parts  = q.data.split(":", 1)
    action = parts[1] if len(parts) > 1 else ""
    db     = _load_db()

    # ── STATISTIKA ────────────────────────────
    if action == "stats":
        users      = list(db["users"].values())
        today      = datetime.now(timezone.utc).date().isoformat()
        week_start = (datetime.now(timezone.utc) - timedelta(days=7)).date().isoformat()
        total_conv = db["stats"].get("total_conversions", 0)
        today_conv = db["stats"].get("daily", {}).get(today, 0)
        week_conv  = sum(v for d, v in db["stats"].get("daily", {}).items() if d >= week_start)
        banned_cnt = sum(1 for u in users if u.get("banned"))
        active_today = sum(1 for u in users if u.get("last_seen", "")[:10] == today)

        # So'nggi 7 kunlik grafik
        chart_lines = []
        for i in range(6, -1, -1):
            d   = (datetime.now(timezone.utc) - timedelta(days=i)).date().isoformat()
            cnt = db["stats"].get("daily", {}).get(d, 0)
            bar = "█" * min(cnt, 15) + (f" {cnt}" if cnt else " 0")
            chart_lines.append(f"`{d[5:]}` {bar}")

        text = (
            "📊 *Batafsil statistika*\n\n"
            f"👥 Jami foydalanuvchi: *{esc(str(len(users)))}*\n"
            f"🟢 Bugun faol: *{esc(str(active_today))}*\n"
            f"🚫 Banlangan: *{esc(str(banned_cnt))}*\n\n"
            f"🎬 Bugungi konvertatsiya: *{esc(str(today_conv))}*\n"
            f"📅 Haftalik: *{esc(str(week_conv))}*\n"
            f"🔢 Jami: *{esc(str(total_conv))}*\n\n"
            "📈 *So'nggi 7 kun:*\n" + "\n".join(chart_lines)
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Orqaga", callback_data="adm:main")]])
        await safe_edit(q.message, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2)

    # ── FOYDALANUVCHILAR ──────────────────────
    elif action == "users":
        users  = sorted(db["users"].values(), key=lambda x: x.get("last_seen", ""), reverse=True)
        page   = context.user_data.get("usr_page", 0)
        pp     = 8   # page size
        chunk  = users[page * pp: (page + 1) * pp]
        total  = len(users)
        lines  = []
        for u in chunk:
            uname  = f"@{u['username']}" if u.get("username") else "—"
            conv   = u.get("total_conversions", 0)
            banned = " 🚫" if u.get("banned") else ""
            lines.append(
                f"`{u['id']}`{banned} {esc(u.get('first_name','?'))} "
                f"{esc(uname)} • {conv} ta"
            )
        text = (
            f"👥 *Foydalanuvchilar* \\({esc(str(total))} ta\\)\n"
            f"Sahifa {esc(str(page+1))}/{esc(str((total-1)//pp+1))}\n\n"
            + "\n".join(lines)
        )
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀️", callback_data="adm:users_prev"))
        if (page + 1) * pp < total:
            nav.append(InlineKeyboardButton("▶️", callback_data="adm:users_next"))
        kb = InlineKeyboardMarkup(
            [nav] if nav else [] +
            [[InlineKeyboardButton("◀️ Orqaga", callback_data="adm:main")]]
        )
        await safe_edit(q.message, text, reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2)

    elif action == "users_prev":
        context.user_data["usr_page"] = max(0, context.user_data.get("usr_page", 0) - 1)
        context.user_data.setdefault("usr_page", 0)
        # Re-dispatch
        q.data = "adm:users"
        return await cb_admin(update, context)

    elif action == "users_next":
        context.user_data["usr_page"] = context.user_data.get("usr_page", 0) + 1
        q.data = "adm:users"
        return await cb_admin(update, context)

    # ── KANALLAR ──────────────────────────────
    elif action == "channels":
        channels = get_channels()
        if channels:
            lines = [f"{i+1}\\. *{esc(ch['title'])}* \\- {esc(ch['username'])}"
                     for i, ch in enumerate(channels)]
            body = "\n".join(lines)
        else:
            body = "_Hali hech qanday kanal yo'q_"

        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Kanal qo'shish", callback_data="adm:ch_add")],
            [InlineKeyboardButton("➖ Kanal o'chirish", callback_data="adm:ch_remove")],
            [InlineKeyboardButton("◀️ Orqaga", callback_data="adm:main")],
        ])
        await safe_edit(
            q.message,
            f"📢 *Majburiy kanallar* \\({esc(str(len(channels)))} ta\\)\n\n{body}",
            reply_markup=kb, parse_mode=ParseMode.MARKDOWN_V2,
        )

    elif action == "ch_add":
        await safe_edit(
            q.message,
            "➕ *Kanal qo'shish*\n\n"
            "Quyidagi formatda yozing:\n"
            "`@username | https://t\\.me/username | Kanal nomi`\n\n"
            "Misol:\n"
            "`@uzdevnet | https://t.me/uzdevnet | UzDevNet`\n\n"
            "Bekor qilish: /cancel",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return ADM_ADD_CHANNEL

    elif action == "ch_remove":
        channels = get_channels()
        if not channels:
            await safe_edit(q.message, "❌ O'chiriladigan kanal yo'q\\.",
                            parse_mode=ParseMode.MARKDOWN_V2)
            return ConversationHandler.END
        buttons = [
            [InlineKeyboardButton(f"❌ {ch['title']} ({ch['username']})",
                                  callback_data=f"adm:ch_del:{ch['username']}")]
            for ch in channels
        ]
        buttons.append([InlineKeyboardButton("◀️ Orqaga", callback_data="adm:channels")])
        await safe_edit(
            q.message,
            "🗑 *Qaysi kanalni o'chirish kerak?*",
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    elif action.startswith("ch_del:"):
        username = action.split("ch_del:", 1)[1]
        removed  = await remove_channel(username)
        msg = f"✅ {esc(username)} o'chirildi\\." if removed else f"❌ {esc(username)} topilmadi\\."
        await safe_edit(q.message, msg, parse_mode=ParseMode.MARKDOWN_V2)

    # ── BROADCAST ─────────────────────────────
    elif action == "broadcast":
        await safe_edit(
            q.message,
            "📣 *Broadcast*\n\nHammaga yuboriladigan xabar matnini yozing\\.\n\nBekor: /cancel",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return ADM_BROADCAST

    # ── TOGGLE NOTIFY ─────────────────────────
    elif action == "toggle_notify":
        async with _db_lock:
            db2 = _load_db()
            cur = db2["settings"].get("notify_admin", True)
            db2["settings"]["notify_admin"] = not cur
            _write_db_sync(db2)
            state = "✅ Yoqiq" if not cur else "❌ O'chiq"
        await q.answer(f"Admin notify: {state}", show_alert=True)
        # Panelni yangilash
        q.data = "adm:main"
        return await cb_admin_main(update, context)

    # ── CLEANTMP ──────────────────────────────
    elif action == "cleantmp":
        removed = 0
        for f in TEMP_DIR.iterdir():
            try:
                f.unlink()
                removed += 1
            except Exception:
                pass
        await q.answer(f"{removed} ta fayl o'chirildi.", show_alert=True)

    # ── MAIN (orqaga) ─────────────────────────
    elif action == "main":
        return await cb_admin_main(update, context)

    return ConversationHandler.END


async def cb_admin_main(update: Update, context: CallbackContext):
    """Admin bosh sahifasini qayta chizadi."""
    q  = update.callback_query
    db = _load_db()
    users      = db["users"]
    total_conv = db["stats"].get("total_conversions", 0)
    today      = datetime.now(timezone.utc).date().isoformat()
    today_conv = db["stats"].get("daily", {}).get(today, 0)
    notify_on  = db["settings"].get("notify_admin", True)

    text = (
        "🛡 *Elite Admin Panel*\n\n"
        f"👥 Foydalanuvchilar: *{esc(str(len(users)))}*\n"
        f"🎬 Bugungi konvertatsiya: *{esc(str(today_conv))}*\n"
        f"🔢 Jami konvertatsiya: *{esc(str(total_conv))}*\n"
        f"\U0001f514 Admin notify: *{'ON' if notify_on else 'OFF'}*\n"
        f"📢 Kanallar soni: *{esc(str(len(get_channels())))}*"
    )
    await safe_edit(q.message, text, reply_markup=admin_main_kb(),
                    parse_mode=ParseMode.MARKDOWN_V2)
    return ConversationHandler.END


# ── Admin notify tugmalari: ✉️ xabar / 🚫 ban ──
async def cb_adm_msg(update: Update, context: CallbackContext):
    """✉️ Xabar yuborish tugmasi — foydalanuvchi ID ni saqlaydi."""
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return ConversationHandler.END
    await q.answer()

    target_id = int(q.data.split(":", 1)[1])
    context.user_data["msg_target"] = target_id
    db   = _load_db()
    u    = db["users"].get(str(target_id), {})
    name = u.get("first_name", "?")

    await q.message.reply_text(
        f"✉️ *{esc(name)}* \\(`{target_id}`\\) ga xabar yozing:\n\nBekor: /cancel",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return ADM_MSG_USER_TEXT


async def cb_adm_ban(update: Update, context: CallbackContext):
    """🚫 Ban / Unban tugmasi."""
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("Ruxsat yo'q.", show_alert=True)
        return
    await q.answer()

    target_id = int(q.data.split(":", 1)[1])
    async with _db_lock:
        db  = _load_db()
        uid = str(target_id)
        if uid not in db["users"]:
            await q.answer("Foydalanuvchi topilmadi.", show_alert=True)
            return
        cur = db["users"][uid].get("banned", False)
        db["users"][uid]["banned"] = not cur
        _write_db_sync(db)
        state = "🚫 Banlandi" if not cur else "✅ Ban olib tashlandi"

    await q.answer(f"{state}", show_alert=True)
    # Xabarni yangilash
    old_text = q.message.text or ""
    await safe_edit(
        q.message,
        old_text + f"\n\n_{esc(state)}_",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✉️ Xabar yuborish", callback_data=f"adm_msg:{target_id}"),
            InlineKeyboardButton(
                "✅ Unban" if not cur else "🚫 Ban again",
                callback_data=f"adm_ban:{target_id}"
            ),
        ]]),
    )


# ── Conversation handlers ──────────────────────
@admin_only
async def adm_add_channel_text(update: Update, context: CallbackContext):
    """Kanal qo'shish: '@username | link | Nomi' formatini parse qiladi."""
    text = update.message.text.strip()
    parts = [p.strip() for p in text.split("|")]
    if len(parts) != 3:
        await update.message.reply_text(
            "❌ Format noto'g'ri\\. Misol:\n"
            "`@uzdevnet | https://t.me/uzdevnet | UzDevNet`\n\n"
            "Qayta yozing yoki /cancel",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return ADM_ADD_CHANNEL

    username, link, title = parts
    if not username.startswith("@"):
        username = "@" + username

    await add_channel(username, link, title)
    await update.message.reply_text(
        f"✅ Kanal qo'shildi: *{esc(title)}* \\({esc(username)}\\)",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return ConversationHandler.END


@admin_only
async def broadcast_receive(update: Update, context: CallbackContext):
    text   = update.message.text
    db     = _load_db()
    users  = [u for u in db["users"].values() if not u.get("banned")]
    sent = failed = blocked = 0

    status = await update.message.reply_text(
        f"📣 {len(users)} ta foydalanuvchiga yuborilmoqda\\.\\.\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    for u in users:
        try:
            await context.bot.send_message(chat_id=u["id"], text=text)
            sent += 1
        except Forbidden:
            blocked += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)

    await status.edit_text(
        f"✅ *Broadcast yakunlandi*\n\n"
        f"📤 Yuborildi: *{esc(str(sent))}*\n"
        f"🚫 Bloklangan: *{esc(str(blocked))}*\n"
        f"❌ Xatolik: *{esc(str(failed))}*",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return ConversationHandler.END


@admin_only
async def adm_msg_user_text(update: Update, context: CallbackContext):
    target_id = context.user_data.get("msg_target")
    if not target_id:
        await update.message.reply_text("❌ Xato: foydalanuvchi ID topilmadi.")
        return ConversationHandler.END
    try:
        await context.bot.send_message(chat_id=target_id, text=update.message.text)
        await update.message.reply_text(f"✅ Xabar yuborildi → `{target_id}`",
                                        parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        await update.message.reply_text(f"❌ Yuborib bo'lmadi: {esc(str(e))}",
                                        parse_mode=ParseMode.MARKDOWN_V2)
    context.user_data.pop("msg_target", None)
    return ConversationHandler.END


@admin_only
async def cancel_conv(update: Update, context: CallbackContext):
    await update.message.reply_text("❌ Bekor qilindi\\.", parse_mode=ParseMode.MARKDOWN_V2)
    return ConversationHandler.END


# ══════════════════════════════════════════════
#  ERROR HANDLER
# ══════════════════════════════════════════════
async def error_handler(update: object, context: CallbackContext):
    logger.error("Xatolik:", exc_info=context.error)
    if isinstance(context.error, TimedOut):
        return
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Kutilmagan xatolik\\. Qayta urinib ko'ring\\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
        except Exception:
            pass


# ══════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════
async def post_init(app: Application) -> None:
    global _conv_semaphore
    _conv_semaphore = asyncio.Semaphore(MAX_WORKERS)

    await app.bot.set_my_commands([
        BotCommand("start", "Botni ishga tushirish"),
        BotCommand("help",  "Yordam"),
        BotCommand("stats", "Statistikam"),
        BotCommand("admin", "Admin panel"),
    ])
    logger.info(f"Bot tayyor | workers={MAX_WORKERS}")


def main():
    logger.info("Elite Bot v3.0 ishga tushmoqda...")

    app = (
        Application.builder()
        .token(API_TOKEN)
        .post_init(post_init)
        .concurrent_updates(True)
        .build()
    )

    # Asosiy komandalar
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))

    # Checksub
    app.add_handler(CallbackQueryHandler(cb_checksub, pattern=r"^checksub$"))

    # Admin notify tugmalari (ConversationHandler dan tashqarida — alohida)
    adm_msg_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(cb_adm_msg, pattern=r"^adm_msg:\d+$")],
        states={
            ADM_MSG_USER_TEXT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_msg_user_text)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        per_user=True, allow_reentry=True,
    )
    app.add_handler(adm_msg_conv)
    app.add_handler(CallbackQueryHandler(cb_adm_ban, pattern=r"^adm_ban:\d+$"))

    # Admin asosiy panel
    admin_conv = ConversationHandler(
        entry_points=[
            CommandHandler("admin", cmd_admin),
            CallbackQueryHandler(cb_admin, pattern=r"^adm:"),
        ],
        states={
            ADM_BROADCAST:  [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_receive)],
            ADM_ADD_CHANNEL:[MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_channel_text)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conv)],
        allow_reentry=True, per_user=True,
    )
    app.add_handler(admin_conv)

    # Media
    app.add_handler(MessageHandler(filters.VIDEO | filters.ANIMATION, handle_media))
    app.add_handler(CallbackQueryHandler(cb_process, pattern=r"^conv:"))

    app.add_error_handler(error_handler)

    logger.info("Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
