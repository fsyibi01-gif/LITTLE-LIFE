import asyncio
import html
import logging
import os
import re
import sqlite3
import threading
import time
from urllib.parse import quote
from datetime import datetime, timedelta, timezone

WIB = timezone(timedelta(hours=7))

from aiohttp import web

# Telethon: monitor public source channel using the user's Telegram account
from telethon import TelegramClient, events
from telethon.sessions import StringSession

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeAllGroupChats,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

CHANNEL_USERNAME = "@FWBTANGSEL"

# Force Subscribe
FORCE_SUB_CHANNEL = "@FWBTANGSEL"
FORCE_SUB_GROUP = "@FWBTANGSEL_ID"

TRAKTEER_USERNAME = "fwbtangsel"

TRAKTEER_WEBHOOK_TOKEN = os.getenv("TRAKTEER_WEBHOOK_TOKEN", "").strip()

WEBHOOK_PORT = int(os.getenv("PORT", "8080"))

DB_FILE = os.getenv("DB_FILE", "fwb_tangsel_fresh_20261108.db")

# Telethon auto-copy configuration
API_ID = os.getenv("API_ID", "").strip()
API_HASH = os.getenv("API_HASH", "").strip()
TELETHON_SESSION = os.getenv("TELETHON_SESSION", "").strip()
SOURCE_CHANNEL = os.getenv("SOURCE_CHANNEL", "fwbess_id").strip()
DEST_CHANNEL = os.getenv("DEST_CHANNEL", "@FWBTANGSEL").strip()
AUTO_COPY_HASHTAG = "#fwbgirl"
REGISTRATION_PRICE = 10000
REGISTRATION_BONUS_COINS = 300

# Rp10 = 1 coin
RUPIAH_PER_COIN = 10

# Harga 1 menfess setelah free habis
MENFESS_COST = 100

# Free menfess per hari
FREE_MENFESS = 1

# Zona waktu Indonesia Barat
datetime.now(WIB)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

logger = logging.getLogger("FWB-TANGSEL")


# ============================================================
# GLOBAL
# ============================================================

BOT_APP = None
BOT_USERNAME = ""

# Event loop milik Telegram
BOT_LOOP = None

# Lock database
db_lock = threading.Lock()

# Force-sub performance cache. Menu biasa memakai cache singkat;
# tombol "CEK LAGI" selalu meminta pengecekan terbaru.
FORCE_SUB_CACHE_TTL = 20.0
force_sub_cache = {}
force_sub_inflight = {}


# ============================================================
# TIME
# ============================================================

def now_wib():
    return datetime.now(WIB)


def today_key():
    return now_wib().strftime("%Y-%m-%d")


def now_text():
    return now_wib().strftime("%Y-%m-%d %H:%M:%S")


def reset_time_text():
    return "00:01 WIB"


# ============================================================
# DATABASE
# ============================================================

def get_db():
    # Buat folder database bila DB_FILE memakai path seperti /data/fwb_tangsel.db.
    db_parent = os.path.dirname(os.path.abspath(DB_FILE))
    if db_parent:
        os.makedirs(db_parent, exist_ok=True)

    conn = sqlite3.connect(
        DB_FILE,
        check_same_thread=False,
        timeout=30
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        # ----------------------------------------------------
        # USERS
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                balance INTEGER DEFAULT 0,
                total_menfess INTEGER DEFAULT 0,
                free_used INTEGER DEFAULT 0,
                joined_at TEXT,
                referral_code TEXT,
                referred_by INTEGER DEFAULT NULL,
                is_banned INTEGER DEFAULT 0,
                free_date TEXT DEFAULT '',
                is_registered INTEGER DEFAULT 0,
                registered_at TEXT DEFAULT NULL
            )
        """)

        # Kolom status registrasi untuk database yang sudah ada.
        cur.execute("PRAGMA table_info(users)")
        user_columns = {row[1] for row in cur.fetchall()}
        if "is_registered" not in user_columns:
            cur.execute("ALTER TABLE users ADD COLUMN is_registered INTEGER DEFAULT 0")
        if "registered_at" not in user_columns:
            cur.execute("ALTER TABLE users ADD COLUMN registered_at TEXT DEFAULT NULL")

        # ----------------------------------------------------
        # TRANSACTIONS
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                transaction_id TEXT PRIMARY KEY,
                user_id INTEGER,
                amount_rupiah INTEGER,
                coins INTEGER,
                created_at TEXT
            )
        """)

        # ----------------------------------------------------
        # MENFESS
        # ----------------------------------------------------

        cur.execute("""
            CREATE TABLE IF NOT EXISTS menfess (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                gender TEXT,
                message_id INTEGER,
                text TEXT,
                created_at TEXT
            )
        """)

    # ========================================================
    # MIGRASI DATABASE LAMA
    # ========================================================

    cur.execute("PRAGMA table_info(users)")
    columns = [row[1] for row in cur.fetchall()]

    if "free_reset_date" not in columns:
        cur.execute("""
            ALTER TABLE users
            ADD COLUMN free_reset_date TEXT DEFAULT NULL
        """)

        logger.info(
            "Kolom free_reset_date berhasil ditambahkan."
        )

    conn.commit()
    conn.close()

    logger.info(
        "SQLite database siap: %s",
        DB_FILE
    )


# ============================================================
# USER
# ============================================================

def create_user(user):

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT user_id
            FROM users
            WHERE user_id = ?
            """,
            (user.id,)
        )

        exists = cur.fetchone()

        if not exists:

            cur.execute(
                """
                INSERT INTO users (
                    user_id,
                    first_name,
                    username,
                    balance,
                    total_menfess,
                    free_used,
                    joined_at,
                    referral_code,
                    referred_by,
                    is_banned,
                    free_date
                )
                VALUES (?, ?, ?, 0, 0, 0, ?, ?, NULL, 0, ?)
                """,
                (
                    user.id,
                    user.first_name or "",
                    user.username or "",
                    now_text(),
                    f"fwb{user.id}",
                    today_key()
                )
            )

        else:

            cur.execute(
                """
                UPDATE users
                SET first_name = ?,
                    username = ?
                WHERE user_id = ?
                """,
                (
                    user.first_name or "",
                    user.username or "",
                    user.id
                )
            )

        conn.commit()
        conn.close()


def get_user(user_id):

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT *
            FROM users
            WHERE user_id = ?
            """,
            (user_id,)
        )

        row = cur.fetchone()

        conn.close()

        return row


# ============================================================
# REGISTRATION
# ============================================================
def is_user_registered(user_id):
    row = get_user(user_id)
    return bool(row and row["is_registered"])


def registration_keyboard(user_id):
    supporter_message = quote(f"fwbreg {user_id}")
    url = (
        f"https://trakteer.id/{TRAKTEER_USERNAME}/tip"
        f"?quantity=10&step=2&supporter_message={supporter_message}"
    )
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💳 BAYAR REGISTRASI Rp10.000", url=url)],
        [InlineKeyboardButton("🔄 Cek status registrasi", callback_data="check_registration")]
    ])


async def send_registration_prompt(target, user):
    await target.reply_text(
        "🔐 <b>REGISTRASI WAJIB FWB TANGSEL</b>\n\n"
        "Untuk menggunakan bot, pengguna baru wajib membayar biaya registrasi "
        "<b>Rp10.000</b>.\n\n"
        "Setelah pembayaran terverifikasi oleh Trakteer, akun otomatis aktif "
        f"dan mendapat bonus <b>{REGISTRATION_BONUS_COINS} coin</b> "
        "(3 kali kirim menfess).\n\n"
        "Tekan tombol pembayaran di bawah. Setelah membayar, tekan Cek status registrasi.",
        parse_mode=ParseMode.HTML,
        reply_markup=registration_keyboard(user.id)
    )


def mark_user_registered(user_id):
    with db_lock:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE users SET is_registered=1, registered_at=? WHERE user_id=? AND is_registered=0",
            (now_text(), user_id)
        )
        changed = cur.rowcount > 0
        if changed:
            cur.execute("UPDATE users SET balance=balance+? WHERE user_id=?", (REGISTRATION_BONUS_COINS, user_id))
        conn.commit()
        conn.close()
        return changed


# ============================================================
# FORCE SUBSCRIBE
# ============================================================

async def check_membership(bot, user_id, chat_id):
    """Return True when the user is a member/admin/creator of chat_id."""
    try:
        member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning(
            "Force sub check gagal | chat=%s | user=%s | error=%s",
            chat_id, user_id, e
        )
        return False


async def _perform_force_sub_check(bot, user_id):
    """Cek channel + grup secara paralel dan simpan hasil ke cache."""
    channel_task = asyncio.create_task(
        check_membership(bot, user_id, FORCE_SUB_CHANNEL)
    )
    group_task = asyncio.create_task(
        check_membership(bot, user_id, FORCE_SUB_GROUP)
    )

    channel_ok, group_ok = await asyncio.gather(
        channel_task,
        group_task
    )

    result = bool(channel_ok and group_ok)
    force_sub_cache[user_id] = (time.monotonic(), result)
    return result


async def check_force_sub(bot, user_id, force_refresh=False):
    """
    Cek membership channel + grup.

    Menu biasa menggunakan cache 20 detik. Beberapa update untuk user
    yang sama pada saat bersamaan akan berbagi satu pengecekan Telegram.
    """
    now = time.monotonic()

    if not force_refresh:
        cached = force_sub_cache.get(user_id)
        if cached:
            cached_at, cached_result = cached
            if now - cached_at < FORCE_SUB_CACHE_TTL:
                return cached_result

    task = force_sub_inflight.get(user_id)

    if task is None or task.done():
        task = asyncio.create_task(
            _perform_force_sub_check(bot, user_id)
        )
        force_sub_inflight[user_id] = task

    try:
        return await asyncio.shield(task)
    finally:
        if task.done() and force_sub_inflight.get(user_id) is task:
            force_sub_inflight.pop(user_id, None)


def force_sub_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "📢 JOIN CHANNEL",
            url="https://t.me/FWBTANGSEL"
        )],
        [InlineKeyboardButton(
            "👥 JOIN GRUP",
            url="https://t.me/FWBTANGSEL_ID"
        )],
        [InlineKeyboardButton(
            "✅ SUDAH JOIN / CEK LAGI",
            callback_data="check_force_sub"
        )]
    ])


def force_sub_text():
    return """
🔒 <b>AKSES TERKUNCI</b>

Sebelum menggunakan <b>FWB TANGSEL</b>,
kamu wajib bergabung ke:

📢 <b>Channel FWB TANGSEL</b>
👥 <b>Grup FWB TANGSEL</b>

Silakan join keduanya terlebih dahulu.

Setelah itu tekan:

<b>✅ SUDAH JOIN / CEK LAGI</b>

⚠️ Jika kamu keluar dari salah satu channel/grup,
akses bot akan otomatis terkunci kembali.
"""


async def force_sub_guard(update, context, force_refresh=False):
    """Gate terpusat: False berarti akses ditahan."""
    user = update.effective_user
    if not user:
        return False

    if await check_force_sub(context.bot, user.id, force_refresh=force_refresh):
        return True

    if update.callback_query:
        await update.callback_query.answer(
            "❌ Kamu belum join channel dan grup.",
            show_alert=True
        )
        # Pesan force-sub yang sama tidak perlu diedit ulang.
        # Ini mencegah 400 Bad Request saat isi pesan tidak berubah.
    elif update.message:
        await update.message.reply_text(
            force_sub_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=force_sub_keyboard()
        )

    return False


# ============================================================
# DAILY FREE MENFESS
# ============================================================

def reset_daily_free_if_needed(user_id):
    conn = get_db()
    cur = conn.cursor()

    today = today_key()

    cur.execute("""
        SELECT free_reset_date
        FROM users
        WHERE user_id = ?
    """, (user_id,))

    row = cur.fetchone()

    if row:
        last_reset = row["free_reset_date"]

        if last_reset != today:
            cur.execute("""
                UPDATE users
                SET free_used = 0,
                    free_reset_date = ?
                WHERE user_id = ?
            """, (today, user_id))

    conn.commit()
    conn.close()


def get_free_remaining(user_id):

    reset_daily_free_if_needed(user_id)

    row = get_user(user_id)

    if not row:
        return FREE_MENFESS

    remaining = FREE_MENFESS - row["free_used"]

    return max(0, remaining)


def use_free_menfess(user_id):

    reset_daily_free_if_needed(user_id)

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            UPDATE users
            SET free_used = free_used + 1
            WHERE user_id = ?
            AND free_used < ?
            """,
            (
                user_id,
                FREE_MENFESS
            )
        )

        changed = cur.rowcount

        conn.commit()
        conn.close()

        return changed > 0


def refund_free_menfess(user_id):

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            UPDATE users
            SET free_used =
                CASE
                    WHEN free_used > 0
                    THEN free_used - 1
                    ELSE 0
                END
            WHERE user_id = ?
            """,
            (user_id,)
        )

        conn.commit()
        conn.close()


# ============================================================
# BALANCE
# ============================================================

def add_balance(user_id, coins):

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            UPDATE users
            SET balance = balance + ?
            WHERE user_id = ?
            """,
            (
                coins,
                user_id
            )
        )

        conn.commit()
        conn.close()


def subtract_balance(user_id, coins):

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            UPDATE users
            SET balance = balance - ?
            WHERE user_id = ?
            AND balance >= ?
            """,
            (
                coins,
                user_id,
                coins
            )
        )

        changed = cur.rowcount

        conn.commit()
        conn.close()

        return changed > 0


# ============================================================
# MENFESS DATABASE
# ============================================================

def increase_menfess(user_id):

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            UPDATE users
            SET total_menfess = total_menfess + 1
            WHERE user_id = ?
            """,
            (user_id,)
        )

        conn.commit()
        conn.close()


def save_menfess(
    user_id,
    gender,
    message_id,
    text
):

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            INSERT INTO menfess (
                user_id,
                gender,
                message_id,
                text,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_id,
                gender,
                message_id,
                text,
                now_text()
            )
        )

        conn.commit()
        conn.close()


# ============================================================
# TRANSACTIONS
# ============================================================

def transaction_exists(transaction_id):

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        cur.execute(
            """
            SELECT transaction_id
            FROM transactions
            WHERE transaction_id = ?
            """,
            (transaction_id,)
        )

        exists = cur.fetchone()

        conn.close()

        return exists is not None


def save_transaction(
    transaction_id,
    user_id,
    amount_rupiah,
    coins
):

    with db_lock:

        conn = get_db()
        cur = conn.cursor()

        try:

            cur.execute(
                """
                INSERT INTO transactions (
                    transaction_id,
                    user_id,
                    amount_rupiah,
                    coins,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    transaction_id,
                    user_id,
                    amount_rupiah,
                    coins,
                    now_text()
                )
            )

            conn.commit()

            return True

        except sqlite3.IntegrityError:

            return False

        finally:

            conn.close()


# ============================================================
# HTML HELPER
# ============================================================

def esc(text):

    if text is None:
        return ""

    return html.escape(
        str(text),
        quote=False
    )


# ============================================================
# TEXT
# ============================================================

START_TEXT = """
👋 <b>Hai, {name}!</b>

🤖 <b>FWB TANGSEL FESS</b> adalah bot auto post pesan yang kamu kirim akan masuk ke channel <b>@FWBTANGSEL</b>.

Silakan baca Rules dan Help terlebih dahulu.

Pilih menu di bawah untuk melihat informasi.
"""


RULES_TEXT = """
📚 <b>RULES @FWBTANGSEL</b>

1. DILARANG SARA / RUSUH / SPAM / MENGANCAM YANG MERUGIKAN ORANG LAIN.

2. DILARANG MENGEMIS DANA / PULSA / REFERRAL DLL.

3. DILARANG PROMOSI GROUP / LPM / BOT LAIN MENGGUNAKAN FITUR BASE UNTUK MENDAPATKAN KEUNTUNGAN MATERIL.

4. DILARANG MENYEBARKAN AIB ORANG LAIN ATAU FAKE PROMOTE MENGGUNAKAN USERNAME ORANG LAIN ATAU SHARE NOMOR WHATSAPP.

5. HARUS BERUSIA MINIMAL 18 TAHUN.

6. DILARANG MENANYAKAN ATAU MENGIRIM MENFESS TENTANG PROSTITUSI SEPERTI OPEN BO / CC / LC / VCS / JUAL KONTEN VIP/VVIP / SEXTAPE / FFM / MMF / 3SOME DAN SEJENISNYA.

7. OWNER SELALU BENAR.

━━━━━━━━━━━━━━

⚠️ <b>MELANGGAR = AUTO BANNED</b>

💸 <b>BIAYA UNBAN = 30RB</b>
"""


COMMANDS_TEXT = """
💡 <b>COMMANDS FWB TANGSEL</b>

/start
Menampilkan menu utama.

/status
Melihat profile, saldo dan statistik.

/topup
Melihat cara top up coin.

━━━━━━━━━━━━━━

📨 <b>CARA KIRIM MENFESS</b>

Kirim <b>satu pesan</b> yang di dalamnya terdapat:

<code>#fwbgirl</code>

atau

<code>#fwbboy</code>

Hashtag boleh berada di depan, tengah, atau belakang pesan.

Contoh:

<code>#fwbgirl ada yang mau nongkrong malam ini?</code>

atau:

<code>ada yang mau nongkrong #fwbgirl</code>

atau:

<code>ada yang mau nongkrong malam ini? #fwbgirl</code>

🎁 Kamu mendapatkan <b>1x menfess gratis setiap hari</b>.

⏰ Reset gratis setiap hari pukul <b>00:01 WIB</b>.

💰 Setelah jatah gratis habis, pengiriman berikutnya menggunakan coin.
"""


HELP_TEXT = """
❓ <b>HELP FWB TANGSEL</b>

Untuk mengirim menfess, cukup kirim satu pesan yang mengandung:

<code>#fwbgirl</code>

atau

<code>#fwbboy</code>

Hashtag boleh diletakkan di mana saja.

Bot akan otomatis mendeteksi hashtag tersebut dan menghapus hashtag dari isi pesan sebelum diposting.

🎁 <b>1x gratis setiap hari.</b>

⏰ Reset setiap hari pukul <b>00:01 WIB</b>.

💰 Setelah gratis habis, harga pengiriman adalah <b>100 coin</b>.

⚠️ Pastikan membaca Rules sebelum mengirim.
"""


def topup_text(user_id):

    return f"""
💰 <b>TOP UP COIN FWB TANGSEL</b>

1 coin = Rp10

Contoh:

100 coin = Rp1.000
500 coin = Rp5.000
1.000 coin = Rp10.000

━━━━━━━━━━━━━━

📌 <b>Code Top Up kamu:</b>

<code>fwb {user_id}</code>

Saat melakukan pembayaran melalui Trakteer, masukkan code tersebut pada pesan dukungan.

Setelah pembayaran terdeteksi oleh webhook, coin akan otomatis masuk ke akun Telegram kamu.

⚠️ Jangan menghapus code tersebut dari pesan dukungan.
"""


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👤 Profile",
                callback_data="profile"
            ),
            InlineKeyboardButton(
                "📚 Rules",
                callback_data="rules"
            )
        ],
        [
            InlineKeyboardButton(
                "💡 Commands",
                callback_data="commands"
            ),
            InlineKeyboardButton(
                "❓ Help",
                callback_data="help"
            )
        ],
        [
            InlineKeyboardButton(
                "🔗 Referral",
                callback_data="referral"
            ),
            InlineKeyboardButton(
                "💰 Top Up",
                callback_data="topup"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ Close",
                callback_data="close"
            )
        ]
    ])


def back_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔙 Kembali",
                callback_data="home"
            ),
            InlineKeyboardButton(
                "❌ Tutup",
                callback_data="close"
            )
        ]
    ])


def profile_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💰 Cara Top Up Coin",
                callback_data="topup"
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 Kembali",
                callback_data="home"
            ),
            InlineKeyboardButton(
                "❌ Tutup",
                callback_data="close"
            )
        ]
    ])


def topup_keyboard(user_id):
    """Buat link Trakteer personal yang membawa Telegram user ID."""
    supporter_message = quote(f"fwb {user_id}")

    topup_url = (
        f"https://trakteer.id/{TRAKTEER_USERNAME}/tip"
        f"?quantity=10&step=2&supporter_message={supporter_message}"
    )

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💰 Buka Trakteer",
                url=topup_url
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 Kembali",
                callback_data="home"
            ),
            InlineKeyboardButton(
                "❌ Tutup",
                callback_data="close"
            )
        ]
    ])


# ============================================================
# START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    create_user(user)

    if not await force_sub_guard(update, context):
        return

    if not is_user_registered(user.id):
        await send_registration_prompt(update.message, user)
        return

    text = START_TEXT.format(name=esc(user.first_name or "User"))
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard())


# ============================================================
# STATUS / PROFILE
# ============================================================

def build_profile_text(user_id):

    reset_daily_free_if_needed(user_id)

    row = get_user(user_id)

    if not row:
        return "❌ User tidak ditemukan."

    username = (
        f"@{esc(row['username'])}"
        if row["username"]
        else "-"
    )

    free_remaining = get_free_remaining(user_id)

    return f"""
👤 <b>Profile User</b>

📛 Nama: {esc(row['first_name'])}
🆔 User ID: <code>{row['user_id']}</code>
🌐 Username: {username}
💰 Saldo: {row['balance']} coin
🔰 Status: terdaftar

📊 <b>User Stats</b>

• Menfess Gratis: {FREE_MENFESS - free_remaining}/{FREE_MENFESS}
• Sisa Gratis: {free_remaining}
• Semua Menfess: {row['total_menfess']}
• Bergabung: {esc(row['joined_at'])}

━━━━━━━━━━━━━━

🔑 <b>Code Top Up:</b>

<code>fwb {row['user_id']}</code>
"""


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    create_user(user)

    if not await force_sub_guard(update, context):
        return
    if not is_user_registered(user.id):
        await send_registration_prompt(update.message, user)
        return

    await update.message.reply_text(
        build_profile_text(user.id),
        parse_mode=ParseMode.HTML,
        reply_markup=profile_keyboard()
    )


# ============================================================
# TOPUP
# ============================================================

async def topup_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    create_user(user)

    if not await force_sub_guard(update, context):
        return
    if not is_user_registered(user.id):
        await send_registration_prompt(update.message, user)
        return

    await update.message.reply_text(
        topup_text(user.id),
        parse_mode=ParseMode.HTML,
        reply_markup=topup_keyboard(user.id)
    )


# ============================================================
# TOPUP
# ============================================================

# ============================================================
# CALLBACK
# ============================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    create_user(user)

    data = query.data

    if data == "check_registration":
        if is_user_registered(user.id):
            row = get_user(user.id)
            await query.edit_message_text(
                f"✅ Registrasi berhasil.\nBonus saldo: {REGISTRATION_BONUS_COINS} coin.\nSaldo sekarang: {row['balance']} coin.",
                reply_markup=main_keyboard()
            )
        else:
            await query.answer("Pembayaran belum terdeteksi. Coba lagi setelah webhook Trakteer masuk.", show_alert=True)
        return

    # Semua menu utama dikunci sampai registrasi lunas.
    if data in ("home", "profile", "rules", "commands", "help", "referral", "topup") and not is_user_registered(user.id):
        await send_registration_prompt(query.message, user)
        return

    # --------------------------------------------------------
    # FORCE SUB CHECK BUTTON
    # --------------------------------------------------------

    if data == "check_force_sub":

        if not await force_sub_guard(update, context, force_refresh=True):
            return

        await query.answer("✅ Verifikasi berhasil!")

        if not is_user_registered(user.id):
            await query.edit_message_text(
                "Silakan selesaikan registrasi terlebih dahulu.",
                reply_markup=registration_keyboard(user.id)
            )
            return

        text = START_TEXT.format(name=esc(user.first_name or "User"))
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=main_keyboard())
        return

    # --------------------------------------------------------
    # FORCE SUB GUARD FOR MENU ACTIONS
    # --------------------------------------------------------

    if data in (
        "home",
        "profile",
        "rules",
        "commands",
        "help",
        "referral",
        "topup"
    ):
        if not await force_sub_guard(update, context):
            return

    # --------------------------------------------------------
    # HOME
    # --------------------------------------------------------

    if data == "home":

        text = START_TEXT.format(
            name=esc(user.first_name or "User")
        )

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=main_keyboard()
        )

    # --------------------------------------------------------
    # PROFILE
    # --------------------------------------------------------

    elif data == "profile":

        await query.edit_message_text(
            build_profile_text(user.id),
            parse_mode=ParseMode.HTML,
            reply_markup=profile_keyboard()
        )

    # --------------------------------------------------------
    # RULES
    # --------------------------------------------------------

    elif data == "rules":

        await query.edit_message_text(
            RULES_TEXT,
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard()
        )

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

    elif data == "commands":

        await query.edit_message_text(
            COMMANDS_TEXT,
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard()
        )

    # --------------------------------------------------------
    # HELP
    # --------------------------------------------------------

    elif data == "help":

        await query.edit_message_text(
            HELP_TEXT,
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard()
        )

    # --------------------------------------------------------
    # TOPUP
    # --------------------------------------------------------

    elif data == "topup":

        await query.edit_message_text(
            topup_text(user.id),
            parse_mode=ParseMode.HTML,
            reply_markup=topup_keyboard(user.id)
        )

    # --------------------------------------------------------
    # REFERRAL
    # --------------------------------------------------------

    elif data == "referral":

        referral_link = (
            f"https://t.me/{BOT_USERNAME}"
            f"?start=ref_{user.id}"
        )

        text = f"""
🔗 <b>REFERRAL</b>

Kode referral kamu:

<code>fwbref{user.id}</code>

Link referral:

{esc(referral_link)}

Kirim link tersebut kepada teman kamu.
"""

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=back_keyboard()
        )

    # --------------------------------------------------------
    # DELETE MENFESS
    # --------------------------------------------------------

    elif data.startswith("delete_menfess:"):

        try:

            message_id = int(
                data.split(":", 1)[1]
            )

        except Exception:

            await query.answer(
                "ID pesan tidak valid.",
                show_alert=True
            )

            return

        try:

            with db_lock:
                conn = get_db()
                cur = conn.cursor()
                cur.execute(
                    "SELECT user_id FROM menfess WHERE message_id = ?",
                    (message_id,)
                )
                owner_row = cur.fetchone()
                conn.close()

            if not owner_row:
                await query.answer(
                    "Menfess tidak ditemukan di database.",
                    show_alert=True
                )
                return

            if owner_row["user_id"] != query.from_user.id:
                await query.answer(
                    "Kamu hanya bisa menghapus menfess milikmu sendiri.",
                    show_alert=True
                )
                return

            await context.bot.delete_message(
                chat_id=CHANNEL_USERNAME,
                message_id=message_id
            )

            await query.edit_message_text(
                "🗑 <b>Menfess berhasil dihapus.</b>",
                parse_mode=ParseMode.HTML
            )

        except Exception as e:

            logger.exception(
                "Gagal menghapus menfess"
            )

            await query.answer(
                "Gagal menghapus. Pastikan bot admin di channel.",
                show_alert=True
            )

    # --------------------------------------------------------
    # CLOSE
    # --------------------------------------------------------

    elif data == "close":

        try:

            await query.delete_message()

        except Exception:

            pass


# ============================================================
# DETECT MENFESS HASHTAG
# ============================================================

MENFESS_TAG_REGEX = re.compile(
    r"(?<!\w)#(fwbgirl|fwbboy)\b",
    re.IGNORECASE
)


def detect_menfess(text):

    matches = MENFESS_TAG_REGEX.findall(text)

    if not matches:
        return None

    genders = {
        match.lower()
        for match in matches
    }

    if len(genders) > 1:

        return "both"

    if "fwbgirl" in genders:
        return "girl"

    return "boy"


def remove_menfess_tag(text):

    cleaned = MENFESS_TAG_REGEX.sub(
        "",
        text
    )

    # Rapikan spasi
    cleaned = re.sub(
        r"[ \t]+",
        " ",
        cleaned
    )

    cleaned = re.sub(
        r"\n[ \t]+",
        "\n",
        cleaned
    )

    return cleaned.strip()


# ============================================================
# USERNAME VALIDATION
# ============================================================

def contains_other_username(text, user):

    usernames = re.findall(
        r"(?<!\w)@([A-Za-z0-9_]{5,32})",
        text
    )

    if not usernames:
        return False, None

    own_username = (
        user.username.lower()
        if user.username
        else None
    )

    for username in usernames:

        if not own_username:

            return True, username

        if username.lower() != own_username:

            return True, username

    return False, None


# ============================================================
# PROCESS MENFESS
# ============================================================

async def process_menfess(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    create_user(user)

    if not await force_sub_guard(update, context):
        return
    if not is_user_registered(user.id):
        await send_registration_prompt(update.message, user)
        return

    row = get_user(user.id)

    # --------------------------------------------------------
    # BAN
    # --------------------------------------------------------

    if row["is_banned"]:

        await update.message.reply_text(
            "🚫 Akun kamu telah dibanned."
        )

        return

    original_text = update.message.text or ""

    # --------------------------------------------------------
    # DETECT TAG
    # --------------------------------------------------------

    gender = detect_menfess(
        original_text
    )

    if gender is None:

        return

    if gender == "both":

        await update.message.reply_text(
            "⚠️ Gunakan hanya satu hashtag: "
            "#fwbgirl atau #fwbboy."
        )

        return

    # --------------------------------------------------------
    # HAPUS HASHTAG
    # --------------------------------------------------------

    text = remove_menfess_tag(
        original_text
    )

    if not text:

        await update.message.reply_text(
            "⚠️ Isi menfess tidak boleh kosong."
        )

        return

    # --------------------------------------------------------
    # USERNAME VALIDATION
    # --------------------------------------------------------

    bad_username, username = contains_other_username(
        text,
        user
    )

    if bad_username:

        await update.message.reply_text(
            "🚫 Menfess ditolak.\n\n"
            "Kalau ingin mencantumkan @username, "
            "kamu hanya boleh mencantumkan username "
            "Telegram milik kamu sendiri."
        )

        return

    # --------------------------------------------------------
    # PAYMENT
    # --------------------------------------------------------

    free_remaining = get_free_remaining(
        user.id
    )

    paid_with_coin = False
    used_free = False

    # --------------------------------------------------------
    # FREE
    # --------------------------------------------------------

    if free_remaining > 0:

        used = use_free_menfess(
            user.id
        )

        if not used:

            await update.message.reply_text(
                "⚠️ Gagal menggunakan free menfess. "
                "Coba lagi."
            )

            return

        used_free = True

    # --------------------------------------------------------
    # COIN
    # --------------------------------------------------------

    else:

        if not subtract_balance(
            user.id,
            MENFESS_COST
        ):

            current = get_user(
                user.id
            )

            await update.message.reply_text(
                f"""
💰 <b>Saldo coin tidak cukup.</b>

Harga menfess: <b>{MENFESS_COST} coin</b>
Saldo kamu: <b>{current['balance']} coin</b>

Silakan top up terlebih dahulu.
""",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "💰 Top Up",
                            callback_data="topup"
                        )
                    ]
                ])
            )

            return

        paid_with_coin = True

    # --------------------------------------------------------
    # FORMAT CHANNEL
    # --------------------------------------------------------

    tag = (
        "#fwbgirl"
        if gender == "girl"
        else "#fwbboy"
    )

    # Hanya tag + isi pesan
    channel_text = (
        f"{tag} {text}"
    )

    # --------------------------------------------------------
    # SEND CHANNEL
    # --------------------------------------------------------

    try:

        sent = await context.bot.send_message(
            chat_id=CHANNEL_USERNAME,
            text=channel_text,
            parse_mode=None
        )

    except Exception as e:

        logger.exception(
            "Gagal mengirim menfess ke channel"
        )

        # Refund coin
        if paid_with_coin:

            add_balance(
                user.id,
                MENFESS_COST
            )

        # Refund free
        elif used_free:

            refund_free_menfess(
                user.id
            )

        await update.message.reply_text(
            "❌ Menfess gagal dikirim ke channel.\n"
            "Jatah/coin kamu sudah dikembalikan.\n"
            "Silakan coba lagi."
        )

        return

    # --------------------------------------------------------
    # SAVE DATABASE
    # --------------------------------------------------------

    increase_menfess(
        user.id
    )

    save_menfess(
        user.id,
        gender,
        sent.message_id,
        text
    )

    # --------------------------------------------------------
    # MESSAGE LINK
    # --------------------------------------------------------

    clean_channel = (
        CHANNEL_USERNAME
        .replace("@", "")
    )

    message_link = (
        f"https://t.me/"
        f"{clean_channel}/"
        f"{sent.message_id}"
    )

    # --------------------------------------------------------
    # DAILY COUNTER
    # --------------------------------------------------------

    row = get_user(
        user.id
    )

    free_remaining_after = (
        get_free_remaining(
            user.id
        )
    )

    free_used_today = (
        FREE_MENFESS
        - free_remaining_after
    )

    # --------------------------------------------------------
    # SUCCESS MESSAGE
    # --------------------------------------------------------

    success_text = f"""
<b>pesan anda berhasil terkirim.</b>

hari ini kamu telah mengirim pesan
sebanyak <b>{free_used_today}/{FREE_MENFESS}</b>. kamu dapat mengirim
pesan sebanyak <b>{FREE_MENFESS - free_used_today}</b> kali dalam sehari

waktu reset setiap hari <b>00:01 WIB</b>
"""

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👀 LIHAT",
                url=message_link
            ),
            InlineKeyboardButton(
                "🗑 HAPUS",
                callback_data=f"delete_menfess:{sent.message_id}"
            )
        ]
    ])

    await update.message.reply_text(
        success_text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard
    )


# ============================================================
# MENFESS HANDLER
# ============================================================

async def menfess_message_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    text = update.message.text or ""

    if not MENFESS_TAG_REGEX.search(text):

        return

    await process_menfess(
        update,
        context
    )


# ============================================================
# TRAKTEER
# ============================================================

def extract_registration_id(message):
    if not message:
        return None
    match = re.search(r"\bfwbreg\s+(\d{5,15})\b", str(message), re.IGNORECASE)
    return int(match.group(1)) if match else None


def extract_telegram_id(message):

    if not message:
        return None

    patterns = [
        r"\bfwb\s+(\d{5,15})\b",
        r"\bfwb(\d{5,15})\b"
    ]

    for pattern in patterns:

        match = re.search(
            pattern,
            str(message),
            re.IGNORECASE
        )

        if match:

            try:

                return int(
                    match.group(1)
                )

            except Exception:

                return None

    return None


# ============================================================
# SEND TOPUP NOTIFICATION
# ============================================================

async def send_topup_notification(
    user_id,
    amount_rupiah,
    coins
):

    try:

        row = get_user(
            user_id
        )

        if not row:
            return

        await BOT_APP.bot.send_message(
            chat_id=user_id,
            text=f"""
✅ <b>TOP UP BERHASIL</b>

💰 Pembayaran: Rp{amount_rupiah:,}
🪙 Coin masuk: +{coins}
💳 Saldo sekarang: {row['balance']} coin

Terima kasih telah melakukan top up! 🎉
""".replace(",", "."),
            parse_mode=ParseMode.HTML
        )

    except Exception as e:

        logger.warning(
            "Notif topup gagal: %s",
            e
        )


# ============================================================
# TRAKTEER WEBHOOK
# ============================================================

async def trakteer_webhook(
    request
):

    try:

        # ----------------------------------------------------
        # TOKEN
        # ----------------------------------------------------

        received_token = request.headers.get(
            "X-Webhook-Token"
        )

        if (
            TRAKTEER_WEBHOOK_TOKEN
            and received_token != TRAKTEER_WEBHOOK_TOKEN
        ):

            logger.warning(
                "Webhook ditolak: token salah"
            )

            return web.json_response(
                {
                    "ok": False,
                    "error": "invalid token"
                },
                status=401
            )

        # ----------------------------------------------------
        # JSON
        # ----------------------------------------------------

        data = await request.json()

        logger.info(
            "Webhook Trakteer diterima: %s",
            data
        )

        # ----------------------------------------------------
        # EVENT
        # ----------------------------------------------------

        event_type = data.get(
            "type"
        )

        if event_type != "tip":

            return web.json_response({
                "ok": True,
                "message": "event ignored"
            })

        # ----------------------------------------------------
        # TRANSACTION ID
        # ----------------------------------------------------

        transaction_id = str(
            data.get(
                "transaction_id",
                ""
            )
        )

        if not transaction_id:

            return web.json_response(
                {
                    "ok": False,
                    "error": "missing transaction_id"
                },
                status=400
            )

        # ----------------------------------------------------
        # ANTI DOUBLE
        # ----------------------------------------------------

        if transaction_exists(
            transaction_id
        ):

            return web.json_response({
                "ok": True,
                "message": "transaction already processed"
            })

        # ----------------------------------------------------
        # SUPPORTER MESSAGE
        # ----------------------------------------------------

        supporter_message = (
            data.get(
                "supporter_message"
            )
            or ""
        )

        registration_user_id = extract_registration_id(supporter_message)
        if registration_user_id:
            row = get_user(registration_user_id)
            if not row:
                return web.json_response({"ok": True, "message": "registration user not found"})
            try:
                registration_amount = int(float(data.get("price", 0)))
            except Exception:
                registration_amount = 0
            if registration_amount < REGISTRATION_PRICE:
                return web.json_response({"ok": True, "message": "registration amount below Rp10000"})
            saved = save_transaction(transaction_id, registration_user_id, registration_amount, REGISTRATION_BONUS_COINS)
            if not saved:
                return web.json_response({"ok": True, "message": "transaction already processed"})
            activated = mark_user_registered(registration_user_id)
            if activated and BOT_LOOP is not None:
                try:
                    asyncio.run_coroutine_threadsafe(
                        BOT_APP.bot.send_message(
                            chat_id=registration_user_id,
                            text=("✅ <b>REGISTRASI BERHASIL</b>\n\n"
                                  f"Pembayaran: Rp{registration_amount:,}.\n"
                                  f"Bonus registrasi: +{REGISTRATION_BONUS_COINS} coin.\n"
                                  "Akun kamu sekarang aktif dan bisa mengirim menfess."),
                            parse_mode=ParseMode.HTML
                        ), BOT_LOOP
                    )
                except Exception as notify_error:
                    logger.warning("Notif registrasi gagal: %s", notify_error)
            return web.json_response({"ok": True, "message": "registration activated" if activated else "already registered"})

        telegram_id = extract_telegram_id(
            supporter_message
        )

        if not telegram_id:

            logger.warning(
                "ID Telegram tidak ditemukan: %s",
                supporter_message
            )

            return web.json_response({
                "ok": True,
                "message": "telegram id not found"
            })

        # ----------------------------------------------------
        # USER
        # ----------------------------------------------------

        row = get_user(
            telegram_id
        )

        if not row:

            logger.warning(
                "User belum terdaftar: %s",
                telegram_id
            )

            return web.json_response({
                "ok": True,
                "message": "user not registered"
            })

        # ----------------------------------------------------
        # AMOUNT
        # ----------------------------------------------------

        price = data.get(
            "price",
            0
        )

        try:

            amount_rupiah = int(
                float(price)
            )

        except Exception:

            amount_rupiah = 0

        coins = (
            amount_rupiah
            // RUPIAH_PER_COIN
        )

        if coins <= 0:

            return web.json_response({
                "ok": True,
                "message": "amount too small"
            })

        # ----------------------------------------------------
        # SAVE TRANSACTION
        # ----------------------------------------------------

        saved = save_transaction(
            transaction_id,
            telegram_id,
            amount_rupiah,
            coins
        )

        if not saved:

            return web.json_response({
                "ok": True,
                "message": "already processed"
            })

        # ----------------------------------------------------
        # ADD COIN
        # ----------------------------------------------------

        add_balance(
            telegram_id,
            coins
        )

        new_balance = get_user(
            telegram_id
        )["balance"]

        logger.info(
            "TOPUP BERHASIL | user=%s | Rp%s | +%s coin",
            telegram_id,
            amount_rupiah,
            coins
        )

        # ----------------------------------------------------
        # NOTIFICATION
        # ----------------------------------------------------

        if BOT_LOOP is not None:

            try:

                future = asyncio.run_coroutine_threadsafe(
                    send_topup_notification(
                        telegram_id,
                        amount_rupiah,
                        coins
                    ),
                    BOT_LOOP
                )

                # Tidak perlu menunggu
                logger.info(
                    "Notifikasi topup dijadwalkan."
                )

            except Exception as e:

                logger.warning(
                    "Gagal menjadwalkan notif topup: %s",
                    e
                )

        # ----------------------------------------------------
        # RESPONSE
        # ----------------------------------------------------

        return web.json_response({
            "ok": True,
            "message": "topup success",
            "user_id": telegram_id,
            "coins": coins,
            "balance": new_balance
        })

    except Exception as e:

        logger.exception(
            "ERROR WEBHOOK"
        )

        return web.json_response(
            {
                "ok": False,
                "error": "internal server error"
            },
            status=500
        )


# ============================================================
# HEALTH CHECK
# ============================================================

async def health(
    request
):

    return web.json_response({
        "status": "online",
        "bot": "FWB TANGSEL",
        "database": DB_FILE,
        "time": now_text()
    })


# ============================================================
# WEBHOOK SERVER
# ============================================================

def run_webhook_server():

    loop = asyncio.new_event_loop()

    asyncio.set_event_loop(
        loop
    )

    app = web.Application()

    app.router.add_get(
        "/",
        health
    )

    app.router.add_post(
        "/trakteer/webhook",
        trakteer_webhook
    )

    runner = web.AppRunner(
        app
    )

    async def start_server():

        await runner.setup()

        site = web.TCPSite(
            runner,
            "0.0.0.0",
            WEBHOOK_PORT
        )

        await site.start()

        logger.info(
            "Webhook server aktif di port %s",
            WEBHOOK_PORT
        )

    loop.run_until_complete(
        start_server()
    )

    loop.run_forever()


# ============================================================
# POST INIT
# ============================================================

async def post_init(
    application
):

    global BOT_LOOP, BOT_USERNAME

    BOT_LOOP = asyncio.get_running_loop()

    me = await application.bot.get_me()
    BOT_USERNAME = me.username or ""

    # Command menu hanya tersedia di private chat.
    private_commands = [
        BotCommand("start", "🚀 Mulai bot"),
        BotCommand("status", "👤 Profile / status akun"),
        BotCommand("topup", "💰 Top up coin"),
    ]

    await application.bot.set_my_commands(
        private_commands,
        scope=BotCommandScopeAllPrivateChats()
    )

    # Hapus command menu dari semua grup.
    await application.bot.set_my_commands(
        [],
        scope=BotCommandScopeAllGroupChats()
    )

    logger.info(
        "Telegram event loop siap. Bot username: @%s",
        BOT_USERNAME
    )


# ============================================================
# TELETHON AUTO-COPY WORKER
# ============================================================

def run_telethon_worker():
    """Run in a dedicated thread so PTB polling and webhook keep their loops."""
    if not (API_ID and API_HASH and TELETHON_SESSION):
        logger.warning(
            "Telethon auto-copy belum aktif: isi API_ID, API_HASH, dan TELETHON_SESSION di Railway Variables."
        )
        return

    if not SOURCE_CHANNEL or not DEST_CHANNEL:
        logger.warning("Telethon auto-copy belum aktif: SOURCE_CHANNEL/DEST_CHANNEL kosong.")
        return

    async def telethon_main():
        client = TelegramClient(
            StringSession(TELETHON_SESSION),
            int(API_ID),
            API_HASH,
        )
        await client.start()

        source = await client.get_entity(SOURCE_CHANNEL)
        destination = await client.get_entity(DEST_CHANNEL)

        logger.info(
            "Telethon aktif. Memantau %s untuk %s; tujuan %s",
            SOURCE_CHANNEL, AUTO_COPY_HASHTAG, DEST_CHANNEL
        )

        @client.on(events.NewMessage(chats=source))
        async def copy_matching_post(event):
            message = event.message
            text_content = message.message or ""

            # Hanya postingan yang mengandung hashtag #fwbgirl (case-insensitive).
            if not re.search(r"(?<!\w)#fwbgirl\b", text_content, flags=re.IGNORECASE):
                return

            try:
                if message.media:
                    # Mengirim ulang media dari akun yang berhak mengakses sumber;
                    # bukan forward, sehingga tidak menampilkan atribusi sumber.
                    await client.send_file(
                        destination,
                        message.media,
                        caption=text_content or None,
                        formatting_entities=message.entities,
                    )
                elif text_content.strip():
                    await client.send_message(
                        destination,
                        text_content,
                        formatting_entities=message.entities,
                        link_preview=bool(message.web_preview),
                    )
                else:
                    logger.info("Postingan %s mengandung hashtag tetapi formatnya tidak didukung.", message.id)
                    return

                logger.info(
                    "Auto-copy berhasil: %s/%s -> %s",
                    SOURCE_CHANNEL, message.id, DEST_CHANNEL
                )
            except Exception:
                logger.exception("Gagal auto-copy postingan %s dari %s", message.id, SOURCE_CHANNEL)

        await client.run_until_disconnected()

    try:
        asyncio.run(telethon_main())
    except Exception:
        logger.exception("Telethon worker berhenti karena error.")


# ============================================================
# MAIN
# ============================================================

def main():

    global BOT_APP

    if not BOT_TOKEN or BOT_TOKEN == "PASTE_NEW_BOT_TOKEN_HERE":
        raise RuntimeError(
            "BOT_TOKEN belum diset. Isi BOT_TOKEN baru atau environment variable BOT_TOKEN."
        )

    # --------------------------------------------------------
    # DATABASE
    # --------------------------------------------------------

    init_db()

    # --------------------------------------------------------
    # WEBHOOK THREAD
    # --------------------------------------------------------

    webhook_thread = threading.Thread(
        target=run_webhook_server,
        daemon=True
    )

    webhook_thread.start()

    # --------------------------------------------------------
    # TELETHON AUTO-COPY (separate thread/event loop)
    # --------------------------------------------------------

    telethon_thread = threading.Thread(
        target=run_telethon_worker,
        name="telethon-auto-copy",
        daemon=True,
    )
    telethon_thread.start()

    # --------------------------------------------------------
    # BOT
    # --------------------------------------------------------

    BOT_APP = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(64)
        .build()
    )

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

    BOT_APP.add_handler(
        CommandHandler(
            "start",
            start_command,
            filters=filters.ChatType.PRIVATE
        )
    )

    BOT_APP.add_handler(
        CommandHandler(
            "status",
            status_command,
            filters=filters.ChatType.PRIVATE
        )
    )

    BOT_APP.add_handler(
        CommandHandler(
            "topup",
            topup_command,
            filters=filters.ChatType.PRIVATE
        )
    )

    # --------------------------------------------------------
    # CALLBACK
    # --------------------------------------------------------

    BOT_APP.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    # --------------------------------------------------------
    # MENFESS
    #
    # Satu pesan langsung.
    # #fwbgirl / #fwbboy boleh di mana saja.
    # --------------------------------------------------------

    BOT_APP.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND
            & filters.ChatType.PRIVATE,
            menfess_message_handler
        )
    )

    # --------------------------------------------------------
    # RUN
    # --------------------------------------------------------

    logger.info(
        "========================================"
    )

    logger.info(
        "FWB TANGSEL BOT ONLINE"
    )

    logger.info(
        "Database: %s",
        DB_FILE
    )

    logger.info(
        "Free menfess: %s / hari",
        FREE_MENFESS
    )

    logger.info(
        "Harga setelah free: %s coin",
        MENFESS_COST
    )

    logger.info(
        "Reset free: 00:01 WIB"
    )

    logger.info(
        "Webhook port: %s",
        WEBHOOK_PORT
    )

    logger.info(
        "Auto-copy source=%s hashtag=%s destination=%s",
        SOURCE_CHANNEL, AUTO_COPY_HASHTAG, DEST_CHANNEL
    )

    logger.info(
        "========================================"
    )

    BOT_APP.run_polling(
        drop_pending_updates=False
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    main()