import asyncio
import hashlib
import hmac
import html
import json
import os
import secrets
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
from aiogram.types import CallbackQuery, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message, WebAppInfo

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().lstrip("-").isdigit()}
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
app = FastAPI(title="S2Pay")
app.mount("/static", StaticFiles(directory=STATIC), name="static")
router = Router()
bot: Bot | None = None

DEFAULT_FIELDS = [
    ("project_name", "Project Name", "text", 1, 1),
    ("operator", "Operator", "select", 1, 2),
    ("account_type", "Account Type", "select", 1, 3),
    ("account", "Account", "text", 1, 4),
    ("monthly_limit", "Monthly Limit", "text", 1, 5),
    ("open_time", "Open Time", "text", 1, 6),
    ("daily_cash_in", "Daily Cash In", "text", 1, 7),
    ("daily_cash_out", "Daily Cash Out", "text", 1, 8),
    ("transaction_per_minute", "Transaction Per Minute", "text", 1, 9),
]
DEFAULT_OPTIONS = {
    "operator": [("Please Select", "", "#64748b"), ("bKash", "bKash", "#e91e63"), ("Nagad", "Nagad", "#f97316"), ("Rocket", "Rocket", "#7c3aed")],
    "account_type": [("Please Select", "", "#64748b"), ("Personal", "Personal", "#2563eb"), ("Agent", "Agent", "#16a34a"), ("Merchant", "Merchant", "#ea580c")],
}

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS group_configs(chat_id INTEGER PRIMARY KEY,role TEXT NOT NULL,title TEXT,enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS form_fields(id TEXT PRIMARY KEY,label TEXT NOT NULL,field_type TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,required INTEGER NOT NULL DEFAULT 1,sort_order INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS field_options(id INTEGER PRIMARY KEY AUTOINCREMENT,field_id TEXT NOT NULL,label TEXT NOT NULL,value TEXT NOT NULL,color TEXT NOT NULL DEFAULT '#64748b',sort_order INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS members(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,pin_hash TEXT NOT NULL UNIQUE,pin_hint TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS requests(id INTEGER PRIMARY KEY AUTOINCREMENT,client_id INTEGER NOT NULL,client_name TEXT,client_group_id INTEGER NOT NULL,buyer_group_id INTEGER NOT NULL,project_name TEXT NOT NULL DEFAULT '',operator TEXT NOT NULL DEFAULT '',account_type TEXT NOT NULL DEFAULT '',account TEXT NOT NULL DEFAULT '',monthly_limit TEXT NOT NULL DEFAULT '',daily_cash_in TEXT NOT NULL DEFAULT '',daily_cash_out TEXT NOT NULL DEFAULT '',open_time TEXT NOT NULL DEFAULT '',transaction_per_minute TEXT NOT NULL DEFAULT '',access_pin TEXT NOT NULL DEFAULT '',message TEXT NOT NULL DEFAULT '',field_values TEXT NOT NULL DEFAULT '{}',screenshot_path TEXT,status TEXT NOT NULL DEFAULT 'PROCESSING',buyer_message_id INTEGER,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS interactions(id INTEGER PRIMARY KEY AUTOINCREMENT,request_id INTEGER NOT NULL,sender_id INTEGER NOT NULL,sender_role TEXT NOT NULL,kind TEXT NOT NULL,text TEXT,file_path TEXT,created_at TEXT NOT NULL);
    """)
    ts = now()
    for fid, label, typ, enabled, order in DEFAULT_FIELDS:
        c.execute("INSERT OR IGNORE INTO form_fields(id,label,field_type,enabled,required,sort_order,updated_at) VALUES(?,?,?,?,?,?,?)", (fid,label,typ,enabled,1,order,ts))
    for fid, opts in DEFAULT_OPTIONS.items():
        if c.execute("SELECT COUNT(*) FROM field_options WHERE field_id=?", (fid,)).fetchone()[0] == 0:
            for i,(label,value,color) in enumerate(opts):
                c.execute("INSERT INTO field_options(field_id,label,value,color,sort_order) VALUES(?,?,?,?,?)", (fid,label,value,color,i))
    # Backward-compatible columns for an existing database.
    for col, typ, default in [("access_pin","TEXT","''"),("message","TEXT","''"),("field_values","TEXT","'{}'")]:
        try: c.execute(f"ALTER TABLE requests ADD COLUMN {col} {typ} NOT NULL DEFAULT {default}")
        except sqlite3.OperationalError: pass
    c.commit(); c.close()

def setting(key):
    c=db(); r=c.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone(); c.close(); return r["value"] if r else None

def set_setting(key,value):
    c=db(); c.execute("INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",(key,str(value),now())); c.commit(); c.close()

def active_groups():
    a,b=setting("active_client_group"),setting("active_buyer_group")
    return (int(a) if a else None,int(b) if b else None)

def group_config(chat_id,role=None):
    c=db(); q="SELECT * FROM group_configs WHERE chat_id=? AND enabled=1" + (" AND role=?" if role else "")
    r=c.execute(q,(chat_id,role) if role else (chat_id,)).fetchone(); c.close(); return r

def configure_group(chat_id,role,title):
    c=db(); ts=now(); c.execute("INSERT INTO group_configs(chat_id,role,title,enabled,created_at,updated_at) VALUES(?,?,?,?,?,?) ON CONFLICT(chat_id) DO UPDATE SET role=excluded.role,title=excluded.title,enabled=1,updated_at=excluded.updated_at",(chat_id,role,title or "",1,ts,ts)); c.commit(); c.close()

def set_active_pair(client_chat_id=None,buyer_chat_id=None):
    if client_chat_id is not None: set_setting("active_client_group",client_chat_id)
    if buyer_chat_id is not None: set_setting("active_buyer_group",buyer_chat_id)

def esc(v): return html.escape(str(v or ""))
def status_emoji(s): return {"PROCESSING":"🟡","SUCCESS":"🟢","FAILED":"🔴"}.get(s,"⚪")
def hash_pin(pin): return hashlib.sha256(pin.strip().encode()).hexdigest()

def auth(authorization):
    if not authorization or not authorization.startswith("tma "): raise HTTPException(401,"Open this app from Telegram.")
    try:
        p=dict(parse_qsl(authorization[4:],keep_blank_values=True)); received=p.pop("hash")
        check="\n".join(f"{k}={p[k]}" for k in sorted(p)); secret=hmac.new(b"WebAppData",BOT_TOKEN.encode(),hashlib.sha256).digest(); calc=hmac.new(secret,check.encode(),hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc,received) or time.time()-int(p.get("auth_date","0"))>86400: raise ValueError
        return json.loads(p.get("user","{}"))
    except Exception: raise HTTPException(401,"Invalid or expired Telegram authorization")

def admin_auth(authorization):
    u=auth(authorization)
    if int(u["id"]) not in ADMIN_IDS: raise HTTPException(403,"Admin only")
    return u

def form_config():
    c=db(); fields=c.execute("SELECT * FROM form_fields ORDER BY sort_order,id").fetchall(); out=[]
    for f in fields:
        opts=c.execute("SELECT label,value,color FROM field_options WHERE field_id=? ORDER BY sort_order,id",(f["id"],)).fetchall()
        out.append({"id":f["id"],"label":f["label"],"type":f["field_type"],"enabled":bool(f["enabled"]),"required":bool(f["required"]),"order":f["sort_order"],"options":[dict(x) for x in opts]})
    c.close(); return out

def allowed_file(name): return Path(name or "").suffix.lower() in {".jpg",".jpeg",".png",".webp",".pdf"}

async def save_upload(upload,prefix,rid=None):
    if not upload: return None
    data=await upload.read()
    if len(data)>MAX_MB*1024*1024: raise HTTPException(413,"Attachment too large")
    if not allowed_file(upload.filename): raise HTTPException(415,"Unsupported attachment type")
    path=UPLOADS/f"{prefix}_{rid or int(time.time()*1000)}_{secrets.token_hex(4)}{Path(upload.filename).suffix.lower()}"; path.write_bytes(data); return str(path)

def get_request(rid):
    c=db(); r=c.execute("SELECT * FROM requests WHERE id=?",(rid,)).fetchone(); c.close(); return r

def save_interaction(rid,sender,role,kind,text="",file_path=None):
    c=db(); c.execute("INSERT INTO interactions(request_id,sender_id,sender_role,kind,text,file_path,created_at) VALUES(?,?,?,?,?,?,?)",(rid,sender,role,kind,text,file_path,now())); c.commit(); c.close()

def card(r):
    vals=json.loads(r["field_values"] or "{}")
    lines=["╔════════════════════════════╗","   ⚡ <b>S2Pay • NEW REQUEST</b>","╚════════════════════════════╝",f"\n🆔 <b>Request:</b> <code>#{r['id']:04d}</code>",f"👤 <b>Client:</b> {esc(r['client_name'])}",f"🔑 <b>Access PIN:</b> <code>{esc(r['access_pin'])}</code>",""]
    for f in form_config():
        if f["enabled"]: lines.append(f"• {esc(f['label'])}: <b>{esc(vals.get(f['id'],'')) if vals.get(f['id'],'') else '—'}</b>")
    if r["message"]: lines += ["",f"💬 <b>Message:</b> {esc(r['message'])}"]
    lines += ["",f"{status_emoji(r['status'])} <b>Status: {esc(r['status'])}</b>","━━━━━━━━━━━━━━━━━━━━"]
    return "\n".join(lines)

def buyer_kb(rid,status):
    if status!="PROCESSING": return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📋 View",callback_data=f"view:{rid}")]])
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📱 Apps",callback_data=f"apps:{rid}"),InlineKeyboardButton(text="💬 Message",callback_data=f"msg:{rid}")],[InlineKeyboardButton(text="📋 View",callback_data=f"view:{rid}"),InlineKeyboardButton(text="✅ Success",callback_data=f"success:{rid}"),InlineKeyboardButton(text="❌ Failed",callback_data=f"failed:{rid}")]])

def web_button(url=None,text="🚀 Open S2Pay"):
    u=url or MINI_APP_URL
    return InlineKeyboardButton(text=text,web_app=WebAppInfo(url=u)) if u else None

async def send_buyer_card(rid):
    r=get_request(rid); target=int(r["buyer_group_id"]); kb=buyer_kb(rid,r["status"])
    if r["screenshot_path"] and Path(r["screenshot_path"]).exists(): msg=await bot.send_photo(target,FSInputFile(r["screenshot_path"]),caption=card(r),reply_markup=kb)
    elif ASSET.exists(): msg=await bot.send_photo(target,FSInputFile(ASSET),caption=card(r),reply_markup=kb)
    else: msg=await bot.send_message(target,card(r),reply_markup=kb)
    c=db(); c.execute("UPDATE requests SET buyer_message_id=?,updated_at=? WHERE id=?",(msg.message_id,now(),rid)); c.commit(); c.close()

@app.get("/")
async def index(): return FileResponse(STATIC/"index.html")
@app.get("/health")
async def health(): return {"ok":True,"service":"s2pay"}
@app.get("/admin")
async def admin_page(): return FileResponse(STATIC/"admin.html")
@app.get("/api/form-config")
async def api_form_config(authorization:str=Header(default="")): auth(authorization); return {"fields":form_config()}
@app.get("/api/admin/form-config")
async def admin_form_config(authorization:str=Header(default="")): admin_auth(authorization); return {"fields":form_config()}

@app.post("/api/admin/form-config")
async def save_form_config(payload:str=Form(...),authorization:str=Header(default="")):
    admin_auth(authorization)
    try: data=json.loads(payload)
    except Exception: raise HTTPException(400,"Invalid configuration")
    c=db(); ts=now()
    for i,f in enumerate(data.get("fields",[]),1):
        fid=str(f.get("id","")).strip(); label=str(f.get("label","")).strip(); typ=str(f.get("type","text"))
        if not fid or not label or fid=="access_pin": continue
        c.execute("INSERT INTO form_fields(id,label,field_type,enabled,required,sort_order,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label,field_type=excluded.field_type,enabled=excluded.enabled,required=excluded.required,sort_order=excluded.sort_order,updated_at=excluded.updated_at",(fid,label,typ,int(bool(f.get("enabled",True))),int(bool(f.get("required",False))),i,ts))
        c.execute("DELETE FROM field_options WHERE field_id=?",(fid,))
        for j,o in enumerate(f.get("options",[]) or []):
            label2=str(o.get("label","")).strip(); value=str(o.get("value",label2)); color=str(o.get("color","#64748b"))
            if label2: c.execute("INSERT INTO field_options(field_id,label,value,color,sort_order) VALUES(?,?,?,?,?)",(fid,label2,value,color,j))
    c.commit(); c.close(); return {"ok":True,"fields":form_config()}

@app.post("/api/admin/member")
async def add_member(name:str=Form(...),pin:str=Form(...),authorization:str=Header(default="")):
    admin_auth(authorization); name=name.strip(); pin=pin.strip()
    if not name or len(pin)<4: raise HTTPException(422,"Name and a PIN of at least 4 characters are required")
    c=db()
    try: c.execute("INSERT INTO members(name,pin_hash,pin_hint,enabled,created_at) VALUES(?,?,?,?,?)",(name,hash_pin(pin),pin[-2:],1,now())); c.commit()
    except sqlite3.IntegrityError: raise HTTPException(409,"That PIN already exists")
    finally: c.close()
    return {"ok":True}

@app.get("/api/admin/members")
async def members(authorization:str=Header(default="")):
    admin_auth(authorization); c=db(); rows=c.execute("SELECT id,name,pin_hint,enabled,created_at FROM members ORDER BY id DESC").fetchall(); c.close(); return [dict(r) for r in rows]

@app.post("/api/admin/member/{mid}/toggle")
async def toggle_member(mid:int,authorization:str=Header(default="")):
    admin_auth(authorization); c=db(); c.execute("UPDATE members SET enabled=CASE enabled WHEN 1 THEN 0 ELSE 1 END WHERE id=?",(mid,)); c.commit(); c.close(); return {"ok":True}

@app.post("/api/request")
async def create_request(values:str=Form(...),access_pin:str=Form(...),message:str=Form(default=""),screenshot:UploadFile|None=File(default=None),authorization:str=Header(default="")):
    user=auth(authorization)
    try: vals=json.loads(values)
    except Exception: raise HTTPException(422,"Invalid form data")
    pin=access_pin.strip(); c=db(); member=c.execute("SELECT * FROM members WHERE pin_hash=? AND enabled=1",(hash_pin(pin),)).fetchone(); c.close()
    if not member: raise HTTPException(403,"Invalid or inactive Access PIN")
    fields=form_config()
    for f in fields:
        if f["enabled"] and f["required"] and not str(vals.get(f["id"],"")).strip(): raise HTTPException(422,f"{f['label']} is required")
    active_client,active_buyer=active_groups()
    if not active_client or not active_buyer: raise HTTPException(503,"Admin must configure Client and Buyer Groups first")
    path=await save_upload(screenshot,"request")
    name=((user.get("first_name") or "")+" "+(user.get("last_name") or "")).strip() or str(user["id"])
    c=db(); cur=c.execute("INSERT INTO requests(client_id,client_name,client_group_id,buyer_group_id,project_name,operator,account_type,account,monthly_limit,daily_cash_in,daily_cash_out,open_time,transaction_per_minute,access_pin,message,field_values,screenshot_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(int(user["id"]),name,active_client,active_buyer,str(vals.get("project_name","")),str(vals.get("operator","")),str(vals.get("account_type","")),str(vals.get("account","")),str(vals.get("monthly_limit","")),str(vals.get("daily_cash_in","")),str(vals.get("daily_cash_out","")),str(vals.get("open_time","")),str(vals.get("transaction_per_minute","")),pin,message.strip(),json.dumps(vals,ensure_ascii=False),path,now(),now())); rid=cur.lastrowid; c.commit(); c.close()
    try: await send_buyer_card(rid)
    except Exception:
        c=db(); c.execute("UPDATE requests SET status='FAILED',updated_at=? WHERE id=?",(now(),rid)); c.commit(); c.close(); raise HTTPException(502,"Could not deliver request to Buyer Group")
    return {"ok":True,"request_id":rid}

@app.post("/api/reply/{rid}")
async def client_reply(rid:int,text:str=Form(default=""),attachment:UploadFile|None=File(default=None),authorization:str=Header(default="")):
    user=auth(authorization); r=get_request(rid)
    if not r or r["client_id"]!=int(user["id"]): raise HTTPException(403,"Not authorized")
    if r["status"]!="PROCESSING": raise HTTPException(409,"Request is closed")
    clean=text.strip(); path=await save_upload(attachment,"reply",rid)
    if not clean and not path: raise HTTPException(422,"Reply cannot be empty")
    save_interaction(rid,int(user["id"]),"CLIENT","REPLY",clean,path)
    body=f"📩 <b>CLIENT RESPONSE — #{rid:04d}</b>\n\n{esc(clean)}"
    if path: await bot.send_document(int(r["buyer_group_id"]),FSInputFile(path),caption=body)
    else: await bot.send_message(int(r["buyer_group_id"]),body)
    return {"ok":True}

@router.message(CommandStart())
async def start(m:Message):
    btn=web_button(); rows=[[btn]] if btn else []
    await m.answer("⚡ <b>S2Pay</b>\n\nUse the button below to open the form.",reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None)

@router.message(Command("formsettings"))
async def formsettings(m:Message):
    if m.from_user.id not in ADMIN_IDS: return
    btn=web_button((MINI_APP_URL+"/admin") if MINI_APP_URL else None,"⚙️ Form Settings")
    await m.answer("⚙️ <b>S2Pay Form Settings</b>\n\nEnable/disable fields, add fields, edit dropdown options/colors, and manage member Access PINs.",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn]]) if btn else None)

@router.message(Command("chatid"))
async def chatid(m:Message): await m.answer(f"🆔 Chat ID: <code>{m.chat.id}</code>")

@router.message(Command("setup"))
async def setup(m:Message):
    if m.from_user.id not in ADMIN_IDS: return await m.answer("⛔ Admin only.")
    if m.chat.type not in {"group","supergroup"}: return await m.answer("Run /setup inside the target group.")
    await m.answer("⚙️ <b>Group Setup</b>\n\nChoose this group's role:",reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👤 Client Group",callback_data="role:CLIENT"),InlineKeyboardButton(text="🛒 Buyer Group",callback_data="role:BUYER")]]))

@router.callback_query(F.data.startswith("role:"))
async def role(q:CallbackQuery):
    if q.from_user.id not in ADMIN_IDS: return await q.answer("Admin only.",show_alert=True)
    role_name=q.data.split(":",1)[1]; configure_group(q.message.chat.id,role_name,q.message.chat.title or ""); await q.answer("Saved"); await q.message.answer(f"✅ Configured as <b>{role_name}</b>.\nChat ID: <code>{q.message.chat.id}</code>\n\nUse /use{role_name.lower()} here to make it active.")

@router.message(Command("useclient"))
async def useclient(m:Message):
    if m.from_user.id in ADMIN_IDS and group_config(m.chat.id,"CLIENT"): set_active_pair(client_chat_id=m.chat.id); await m.answer(f"✅ Active Client Group: <code>{m.chat.id}</code>")

@router.message(Command("usebuyer"))
async def usebuyer(m:Message):
    if m.from_user.id in ADMIN_IDS and group_config(m.chat.id,"BUYER"): set_active_pair(buyer_chat_id=m.chat.id); await m.answer(f"✅ Active Buyer Group: <code>{m.chat.id}</code>")

async def ensure_buyer(q):
    if not group_config(q.message.chat.id,"BUYER"): await q.answer("Buyer Group only.",show_alert=True); return None
    rid=int(q.data.split(":")[1]); r=get_request(rid)
    if not r or int(r["buyer_group_id"])!=q.message.chat.id or r["status"]!="PROCESSING": await q.answer("Request is closed or unavailable.",show_alert=True); return None
    return r

@router.callback_query(F.data.startswith("apps:"))
async def apps(q:CallbackQuery):
    r=await ensure_buyer(q)
    if not r:return
    await q.answer(); await q.message.answer(f"📱 <b>Apps — Request #{r['id']:04d}</b>\n\nPlease handle app verification manually."); save_interaction(r["id"],q.from_user.id,"BUYER","APPS","Apps")

@router.callback_query(F.data.startswith("msg:"))
async def msg_action(q:CallbackQuery):
    r=await ensure_buyer(q)
    if not r:return
    await q.answer(); await q.message.answer(f"💬 <b>Message for Client — #{r['id']:04d}</b>\n\nReply to this message with the clarification you want sent to the client.")

@router.message(F.reply_to_message)
async def buyer_reply(m:Message):
    if not group_config(m.chat.id,"BUYER"): return
    src=m.reply_to_message.text or m.reply_to_message.caption or ""
    import re
    match=re.search(r"#(\d+)",src)
    if not match:return
    rid=int(match.group(1)); r=get_request(rid)
    if not r or r["buyer_group_id"]!=m.chat.id or r["status"]!="PROCESSING":return
    text=(m.text or m.caption or "").strip()
    if not text:return
    await bot.send_message(int(r["client_id"]),f"💬 <b>Buyer Message — Request #{rid:04d}</b>\n\n{esc(text)}"); save_interaction(rid,m.from_user.id,"BUYER","MESSAGE",text); await m.reply("✅ Sent to client.")

@router.callback_query(F.data.startswith("view:"))
async def view(q:CallbackQuery):
    rid=int(q.data.split(":")[1]); r=get_request(rid); await q.answer()
    if r and int(r["buyer_group_id"])==q.message.chat.id: await q.message.answer(card(r))

async def finish(q,status):
    if not group_config(q.message.chat.id,"BUYER") or q.from_user.id not in ADMIN_IDS: return await q.answer("Admin only.",show_alert=True)
    rid=int(q.data.split(":")[1]); r=get_request(rid)
    if not r or int(r["buyer_group_id"])!=q.message.chat.id or r["status"]!="PROCESSING": return await q.answer("Request unavailable.",show_alert=True)
    c=db(); c.execute("UPDATE requests SET status=?,updated_at=? WHERE id=?",(status,now(),rid)); c.commit(); c.close(); await q.answer(); await q.message.edit_reply_markup(reply_markup=buyer_kb(rid,status)); await bot.send_message(int(r["client_id"]),f"{status_emoji(status)} <b>Request #{rid:04d}</b>\n\nFinal status: <b>{status}</b>")

@router.callback_query(F.data.startswith("success:"))
async def success(q:CallbackQuery): await finish(q,"SUCCESS")
@router.callback_query(F.data.startswith("failed:"))
async def failed(q:CallbackQuery): await finish(q,"FAILED")

async def bot_main():
    global bot
    init_db()
    if not BOT_TOKEN: raise RuntimeError("BOT_TOKEN is not configured")
    bot=Bot(BOT_TOKEN,default=DefaultBotProperties(parse_mode=ParseMode.HTML)); dp=Dispatcher(); dp.include_router(router); await dp.start_polling(bot)

@app.on_event("startup")
async def startup(): init_db(); asyncio.create_task(bot_main())

if __name__=="__main__":
    import uvicorn
    uvicorn.run("main:app",host="0.0.0.0",port=PORT)
