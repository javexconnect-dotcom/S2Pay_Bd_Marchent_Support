import asyncio
import hashlib
import hmac
import html
import json
import os
import sqlite3
import time
from pathlib import Path
from urllib.parse import parse_qsl

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup,
    Message, WebAppInfo
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().lstrip("-").isdigit()
}
MINI_APP_URL = os.getenv("MINI_APP_URL", "").rstrip("/")
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = Path(os.getenv("DATABASE_PATH", "./data/s2pay.sqlite3"))
MAX_MB = int(os.getenv("MAX_ATTACHMENT_MB", "10"))

BASE = Path(__file__).resolve().parent
STATIC = BASE / "app" / "static"
ASSET = BASE / "assets" / "s2pay_logo.png"
UPLOADS = BASE / "data" / "uploads"
UPLOADS.mkdir(parents=True, exist_ok=True)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="S2Pay Request Desk")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
router = Router()
bot: Bot | None = None


def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS settings(
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS group_configs(
        chat_id INTEGER PRIMARY KEY,
        role TEXT NOT NULL CHECK(role IN ('CLIENT','BUYER')),
        title TEXT,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS requests(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id INTEGER NOT NULL,
        client_name TEXT,
        client_group_id INTEGER NOT NULL,
        buyer_group_id INTEGER NOT NULL,
        project_name TEXT NOT NULL,
        operator TEXT NOT NULL,
        account_type TEXT NOT NULL,
        account TEXT NOT NULL,
        monthly_limit TEXT NOT NULL,
        daily_cash_in TEXT NOT NULL,
        daily_cash_out TEXT NOT NULL,
        open_time TEXT NOT NULL,
        transaction_per_minute TEXT NOT NULL,
        screenshot_path TEXT,
        status TEXT NOT NULL DEFAULT 'PROCESSING',
        buyer_message_id INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS interactions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        request_id INTEGER NOT NULL,
        sender_id INTEGER NOT NULL,
        sender_role TEXT NOT NULL,
        kind TEXT NOT NULL,
        text TEXT,
        file_path TEXT,
        created_at TEXT NOT NULL
    );
    """)
    c.commit()
    c.close()


def get_request(rid):
    c = db()
    r = c.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
    c.close()
    return r


def setting(key):
    c = db()
    r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    c.close()
    return r["value"] if r else None


def set_setting(key, value):
    c = db()
    c.execute("""
        INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at
    """, (key, str(value), now()))
    c.commit()
    c.close()


def group_config(chat_id, role=None):
    c = db()
    if role:
        r = c.execute(
            "SELECT * FROM group_configs WHERE chat_id=? AND role=? AND enabled=1",
            (chat_id, role)
        ).fetchone()
    else:
        r = c.execute(
            "SELECT * FROM group_configs WHERE chat_id=? AND enabled=1",
            (chat_id,)
        ).fetchone()
    c.close()
    return r


def configure_group(chat_id, role, title):
    c = db()
    ts = now()
    c.execute("""
        INSERT INTO group_configs(chat_id,role,title,enabled,created_at,updated_at)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(chat_id) DO UPDATE SET
            role=excluded.role,title=excluded.title,enabled=1,updated_at=excluded.updated_at
    """, (chat_id, role, title or "", 1, ts, ts))
    c.commit()
    c.close()


def set_active_pair(client_chat_id=None, buyer_chat_id=None):
    if client_chat_id is not None:
        set_setting("active_client_group", client_chat_id)
    if buyer_chat_id is not None:
        set_setting("active_buyer_group", buyer_chat_id)


def active_groups():
    client_id = setting("active_client_group")
    buyer_id = setting("active_buyer_group")
    return (int(client_id) if client_id else None, int(buyer_id) if buyer_id else None)


def esc(v):
    return html.escape(str(v or ""))


def status_emoji(status):
    return {"PROCESSING": "🟡", "SUCCESS": "🟢", "FAILED": "🔴"}.get(status, "⚪")


def card(r):
    return f"""╔════════════════════════════╗
   ⚡ <b>S2Pay • NEW REQUEST</b>
╚════════════════════════════╝

🆔 <b>Request:</b> <code>#{r["id"]:04d}</code>
📁 <b>Project:</b> <b>{esc(r["project_name"])}</b>
👤 <b>Client:</b> {esc(r["client_name"])}

▰ <b>PROJECT INFORMATION</b>
• Operator: <b>{esc(r["operator"])}</b>
• Account Type: <b>{esc(r["account_type"])}</b>
• Account: <code>{esc(r["account"])}</code>
• Monthly Limit: <b>{esc(r["monthly_limit"])}</b>
• Daily Cash In: <b>{esc(r["daily_cash_in"])}</b>
• Daily Cash Out: <b>{esc(r["daily_cash_out"])}</b>
• Open Time: <b>{esc(r["open_time"])}</b>
• Transaction/Minute: <b>{esc(r["transaction_per_minute"])}</b>

{status_emoji(r["status"])} <b>Status: {esc(r["status"])}</b>
━━━━━━━━━━━━━━━━━━━━
<i>S2Pay • Verification Request</i>"""


def buyer_kb(rid, status):
    if status != "PROCESSING":
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="📋 View", callback_data=f"view:{rid}")
        ]])
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📱 Apps", callback_data=f"apps:{rid}"),
            InlineKeyboardButton(text="🔐 OTP", callback_data=f"otp:{rid}")
        ],
        [InlineKeyboardButton(text="💬 Message", callback_data=f"msg:{rid}")],
        [
            InlineKeyboardButton(text="📋 View", callback_data=f"view:{rid}"),
            InlineKeyboardButton(text="✅ Success", callback_data=f"success:{rid}"),
            InlineKeyboardButton(text="❌ Failed", callback_data=f"failed:{rid}")
        ]
    ])


def web_button():
    if not MINI_APP_URL:
        return None
    return InlineKeyboardButton(
        text="🚀 Open S2Pay",
        web_app=WebAppInfo(url=MINI_APP_URL)
    )


def validate_tma(raw):
    try:
        p = dict(parse_qsl(raw, keep_blank_values=True))
        received = p.pop("hash")
        check = "\n".join(f"{k}={p[k]}" for k in sorted(p))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calculated, received):
            return None
        if time.time() - int(p.get("auth_date", "0")) > 86400:
            return None
        return json.loads(p.get("user", "{}"))
    except Exception:
        return None


def auth(authorization):
    if not authorization or not authorization.startswith("tma "):
        raise HTTPException(401, "Missing Telegram authorization")
    user = validate_tma(authorization[4:])
    if not user:
        raise HTTPException(401, "Invalid or expired Telegram authorization")
    return user


def open_request(r):
    return bool(r and r["status"] == "PROCESSING")


def allowed_file(filename):
    return Path(filename or "").suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".pdf"}


async def save_upload(upload, prefix, rid=None):
    if not upload:
        return None
    data = await upload.read()
    if len(data) > MAX_MB * 1024 * 1024:
        raise HTTPException(413, "Attachment too large")
    if not allowed_file(upload.filename):
        raise HTTPException(415, "Unsupported attachment type")
    suffix = Path(upload.filename or "file.bin").suffix.lower()
    name = f"{prefix}_{rid or int(time.time()*1000)}_{int(time.time()*1000)}{suffix}"
    path = UPLOADS / name
    path.write_bytes(data)
    return str(path)


def save_interaction(rid, sender_id, role, kind, text="", file_path=None):
    c = db()
    c.execute("""
        INSERT INTO interactions(request_id,sender_id,sender_role,kind,text,file_path,created_at)
        VALUES(?,?,?,?,?,?,?)
    """, (rid, sender_id, role, kind, text, file_path, now()))
    c.commit()
    c.close()


async def send_buyer_card(rid):
    r = get_request(rid)
    if not r:
        return
    target = int(r["buyer_group_id"])
    kb = buyer_kb(rid, r["status"])
    if r["screenshot_path"] and Path(r["screenshot_path"]).exists():
        msg = await bot.send_photo(
            target, FSInputFile(r["screenshot_path"]),
            caption=card(r), reply_markup=kb
        )
    elif ASSET.exists():
        msg = await bot.send_photo(
            target, FSInputFile(ASSET),
            caption=card(r), reply_markup=kb
        )
    else:
        msg = await bot.send_message(target, card(r), reply_markup=kb)
    c = db()
    c.execute(
        "UPDATE requests SET buyer_message_id=?,updated_at=? WHERE id=?",
        (msg.message_id, now(), rid)
    )
    c.commit()
    c.close()


async def send_to_client(r, text, attachment=None):
    if attachment and Path(attachment).exists():
        await bot.send_document(int(r["client_id"]), FSInputFile(attachment), caption=text)
    else:
        await bot.send_message(int(r["client_id"]), text)


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/health")
async def health():
    return {"ok": True, "service": "s2pay"}


@app.get("/api/my-requests")
async def my_requests(authorization: str = Header(default="")):
    user = auth(authorization)
    c = db()
    rows = c.execute("""
        SELECT id,status,project_name,operator,account_type,created_at
        FROM requests WHERE client_id=? ORDER BY id DESC LIMIT 50
    """, (int(user["id"]),)).fetchall()
    c.close()
    return [dict(r) for r in rows]


@app.post("/api/request")
async def create_request(
    project_name: str = Form(...),
    operator: str = Form(...),
    account_type: str = Form(...),
    account: str = Form(...),
    monthly_limit: str = Form(...),
    daily_cash_in: str = Form(...),
    daily_cash_out: str = Form(...),
    open_time: str = Form(...),
    transaction_per_minute: str = Form(...),
    screenshot: UploadFile | None = File(default=None),
    authorization: str = Header(default="")
):
    user = auth(authorization)
    required = [
        project_name, operator, account_type, account,
        monthly_limit, daily_cash_in, daily_cash_out, open_time,
        transaction_per_minute
    ]
    if any(not str(x).strip() for x in required):
        raise HTTPException(422, "All fields are required")

    active_client, active_buyer = active_groups()
    if not active_client or not active_buyer:
        raise HTTPException(503, "Admin must configure an active Client Group and Buyer Group first")

    # The Mini App is authenticated to Telegram, but group membership is not
    # inferred from the web app. The active Client Group is an admin-selected
    # routing target for this deployment.
    path = await save_upload(screenshot, "request")

    name = ((user.get("first_name") or "") + " " + (user.get("last_name") or "")).strip()
    name = name or str(user["id"])
    ts = now()

    c = db()
    cur = c.execute("""
        INSERT INTO requests(
            client_id,client_name,client_group_id,buyer_group_id,project_name,
            operator,account_type,account,monthly_limit,daily_cash_in,
            daily_cash_out,open_time,transaction_per_minute,screenshot_path,
            created_at,updated_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        int(user["id"]), name, active_client, active_buyer, project_name.strip(),
        operator.strip(), account_type.strip(), account.strip(),
        monthly_limit.strip(), daily_cash_in.strip(), daily_cash_out.strip(),
        open_time.strip(), transaction_per_minute.strip(), path, ts, ts
    ))
    rid = cur.lastrowid
    c.commit()
    c.close()

    try:
        await send_buyer_card(rid)
    except Exception:
        c = db()
        c.execute("UPDATE requests SET status='FAILED',updated_at=? WHERE id=?", (now(), rid))
        c.commit()
        c.close()
        raise HTTPException(502, "Could not deliver request to Buyer Group")

    return {"ok": True, "request_id": rid}


@app.post("/api/reply/{rid}")
async def client_reply(
    rid: int,
    text: str = Form(default=""),
    attachment: UploadFile | None = File(default=None),
    authorization: str = Header(default="")
):
    user = auth(authorization)
    r = get_request(rid)
    if not r or r["client_id"] != int(user["id"]):
        raise HTTPException(403, "Not authorized")
    if not open_request(r):
        raise HTTPException(409, "Request is closed")

    clean = text.strip()
    path = await save_upload(attachment, "reply", rid)
    if not clean and not path:
        raise HTTPException(422, "Reply cannot be empty")

    save_interaction(rid, int(user["id"]), "CLIENT", "REPLY", clean, path)

    body = f"📩 <b>CLIENT RESPONSE — #{rid:04d}</b>"
    if clean:
        body += f"\n\n{esc(clean)}"
    body += "\n\n🟡 Status: <b>PROCESSING</b>"

    if path:
        await bot.send_document(int(r["buyer_group_id"]), FSInputFile(path), caption=body)
    else:
        await bot.send_message(int(r["buyer_group_id"]), body)

    return {"ok": True}


@router.message(CommandStart())
async def start(m: Message):
    btn = web_button()
    rows = [[btn]] if btn else []
    await m.answer(
        "⚡ <b>S2Pay Request Desk</b>\n\n"
        "Use the button below to open the Client Request Desk.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    )


@router.message(Command("chatid"))
async def chatid(m: Message):
    await m.answer(f"🆔 Chat ID: <code>{m.chat.id}</code>")


@router.message(Command("setup"))
async def setup(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return await m.answer("⛔ Admin only.")
    if m.chat.type not in {"group", "supergroup"}:
        return await m.answer("Run /setup inside the target group.")
    await m.answer(
        "⚙️ <b>Group Setup</b>\n\nChoose this group's role:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="👤 Client Group", callback_data="role:CLIENT"),
            InlineKeyboardButton(text="🛒 Buyer Group", callback_data="role:BUYER")
        ]])
    )


@router.callback_query(F.data.startswith("role:"))
async def role(q: CallbackQuery):
    if q.from_user.id not in ADMIN_IDS:
        return await q.answer("Admin only.", show_alert=True)
    role_name = q.data.split(":", 1)[1]
    configure_group(q.message.chat.id, role_name, q.message.chat.title or "")
    await q.answer("Saved")
    await q.message.answer(
        f"✅ Configured as <b>{role_name}</b>.\n"
        f"Chat ID: <code>{q.message.chat.id}</code>\n\n"
        f"Now use /use{role_name.lower()} here if you want this to be the active {role_name} Group."
    )


@router.message(Command("useclient"))
async def useclient(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    if not group_config(m.chat.id, "CLIENT"):
        return await m.answer("This group is not configured as Client Group.")
    set_active_pair(client_chat_id=m.chat.id)
    await m.answer(f"✅ Active Client Group set to <code>{m.chat.id}</code>.")


@router.message(Command("usebuyer"))
async def usebuyer(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    if not group_config(m.chat.id, "BUYER"):
        return await m.answer("This group is not configured as Buyer Group.")
    set_active_pair(buyer_chat_id=m.chat.id)
    await m.answer(f"✅ Active Buyer Group set to <code>{m.chat.id}</code>.")


@router.message(Command("config"))
async def config(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    client_id, buyer_id = active_groups()
    await m.answer(
        "⚙️ <b>S2Pay Configuration</b>\n\n"
        f"Client Group: <code>{client_id or 'NOT SET'}</code>\n"
        f"Buyer Group: <code>{buyer_id or 'NOT SET'}</code>"
    )


async def ensure_buyer(q):
    if not group_config(q.message.chat.id, "BUYER"):
        await q.answer("Buyer Group only.", show_alert=True)
        return False
    rid = int(q.data.split(":")[1])
    r = get_request(rid)
    if not open_request(r) or int(r["buyer_group_id"]) != q.message.chat.id:
        await q.answer("Request is closed or unavailable.", show_alert=True)
        return False
    return r


@router.callback_query(F.data.startswith("apps:"))
async def apps(q: CallbackQuery):
    r = await ensure_buyer(q)
    if not r:
        return
    await q.answer()
    await send_to_client(
        r,
        f"📱 <b>Apps — Request #{r['id']:04d}</b>\n\n"
        "Please provide the requested app/account screenshot for verification."
    )
    save_interaction(r["id"], q.from_user.id, "BUYER", "APPS", "Apps")


@router.callback_query(F.data.startswith("otp:"))
async def otp(q: CallbackQuery):
    r = await ensure_buyer(q)
    if not r:
        return
    await q.answer()
    # Safety boundary: S2Pay does not relay, store, or request OTP codes.
    await q.message.answer(
        f"🔐 <b>OTP — Request #{r['id']:04d}</b>\n\n"
        "For security, S2Pay does not collect or relay OTP codes. "
        "Complete OTP verification through the official verification channel."
    )
    save_interaction(r["id"], q.from_user.id, "BUYER", "OTP", "OTP action selected")


@router.callback_query(F.data.startswith("msg:"))
async def message_action(q: CallbackQuery):
    r = await ensure_buyer(q)
    if not r:
        return
    await q.answer()
    await q.message.answer(
        f"💬 <b>Message for Client — #{r['id']:04d}</b>\n\n"
        "Reply to this message with the problem or clarification you want sent to the client."
    )


@router.message(F.reply_to_message)
async def buyer_message_reply(m: Message):
    if not group_config(m.chat.id, "BUYER"):
        return
    replied = m.reply_to_message
    source = replied.text or replied.caption or ""
    import re
    match = re.search(r"#(\d+)", source)
    if not match:
        return
    rid = int(match.group(1))
    r = get_request(rid)
    if not open_request(r) or int(r["buyer_group_id"]) != m.chat.id:
        return

    text = (m.text or m.caption or "").strip()
    if not text:
        return await m.reply("Please send a text message.")
    await send_to_client(
        r,
        f"💬 <b>Buyer Message — Request #{rid:04d}</b>\n\n{esc(text)}"
    )
    save_interaction(rid, m.from_user.id, "BUYER", "MESSAGE", text)
    await m.reply("✅ Sent to client.")


@router.callback_query(F.data.startswith("view:"))
async def view(q: CallbackQuery):
    rid = int(q.data.split(":")[1])
    r = get_request(rid)
    await q.answer()
    if not r or int(r["buyer_group_id"]) != q.message.chat.id:
        return
    await q.message.answer(card(r))


async def finish(q: CallbackQuery, status):
    if not group_config(q.message.chat.id, "BUYER"):
        return await q.answer("Buyer Group only.", show_alert=True)
    if q.from_user.id not in ADMIN_IDS:
        return await q.answer("Only an authorized admin can close requests.", show_alert=True)

    rid = int(q.data.split(":")[1])
    r = get_request(rid)
    if not r or int(r["buyer_group_id"]) != q.message.chat.id:
        return await q.answer("Wrong Buyer Group.", show_alert=True)
    if r["status"] != "PROCESSING":
        return await q.answer("Request already closed.", show_alert=True)

    c = db()
    c.execute(
        "UPDATE requests SET status=?,updated_at=? WHERE id=?",
        (status, now(), rid)
    )
    c.commit()
    c.close()

    await q.answer()
    await q.message.edit_reply_markup(reply_markup=buyer_kb(rid, status))
    await q.message.answer(
        f"{status_emoji(status)} <b>Request #{rid:04d} → {status}</b>\n\n"
        "Verification workflow closed. Buyer-side action buttons are now disabled."
    )
    try:
        await bot.send_message(
            int(r["client_id"]),
            f"{status_emoji(status)} <b>Request #{rid:04d}</b>\n\n"
            f"Final status: <b>{status}</b>\n"
            "This verification request is now closed."
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("success:"))
async def success(q: CallbackQuery):
    await finish(q, "SUCCESS")


@router.callback_query(F.data.startswith("failed:"))
async def failed(q: CallbackQuery):
    await finish(q, "FAILED")


@router.message(Command("status"))
async def stats(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    c = db()
    total = c.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    processing = c.execute("SELECT COUNT(*) FROM requests WHERE status='PROCESSING'").fetchone()[0]
    success_count = c.execute("SELECT COUNT(*) FROM requests WHERE status='SUCCESS'").fetchone()[0]
    failed_count = c.execute("SELECT COUNT(*) FROM requests WHERE status='FAILED'").fetchone()[0]
    c.close()
    await m.answer(
        f"📊 <b>S2Pay Stats</b>\n\nTotal: {total}\n"
        f"🟡 Processing: {processing}\n🟢 Success: {success_count}\n🔴 Failed: {failed_count}"
    )


async def bot_main():
    global bot
    init_db()
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not configured")
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await dp.start_polling(bot)


@app.on_event("startup")
async def startup():
    asyncio.create_task(bot_main())


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
