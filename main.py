#!/usr/bin/env python3

import os, sys, subprocess, threading, time, shutil, zipfile
import tarfile, sqlite3, ast, importlib, importlib.util
import html as html_lib, logging
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

BOT_TOKEN      = "9207682928:AAFAdNQjYfiDDzhg_y4907EiDsuhb_5ZnfE"
ADMIN_ID       = 9197380910
CHANNEL_URL    = "https://t.me/Aiflex_Devloper"

def install_requirements():
    pkgs = {"pyTelegramBotAPI": "telebot", "requests": "requests", "psutil": "psutil"}
    for pkg, mod in pkgs.items():
        try:
            __import__(mod)
        except ImportError:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pkg])

install_requirements()

import psutil
import telebot
from telebot.types import (InlineKeyboardMarkup, InlineKeyboardButton,
                            ReplyKeyboardMarkup, KeyboardButton)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

CPU_THRESHOLD        = float(os.environ.get("CPU_THRESHOLD", "90.0"))
MEMORY_THRESHOLD     = float(os.environ.get("MEMORY_THRESHOLD", "90.0"))
MAX_RUNNING_PROCESSES = int(os.environ.get("MAX_RUNNING_PROCESSES", "10"))
MAX_FILES_PER_USER   = int(os.environ.get("MAX_FILES_PER_USER", "3"))

BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
DATA_DIR   = os.path.join(BASE_DIR, "data")
DB_PATH    = os.path.join(DATA_DIR, "metadata.db")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
LOGS_DIR   = os.path.join(DATA_DIR, "logs")
TEMP_DIR   = os.path.join(DATA_DIR, "temp")

for _d in [DATA_DIR, UPLOADS_DIR, LOGS_DIR, TEMP_DIR]:
    os.makedirs(_d, exist_ok=True)

START_TIME = datetime.now(timezone.utc)

conn    = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
db_lock = threading.Lock()

def init_db():
    with db_lock:
        cur = conn.cursor()
        cur.execute('''CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, username TEXT, filename TEXT,
            orig_name TEXT, path TEXT, uploaded_at TEXT,
            file_type TEXT, pid INTEGER, status TEXT DEFAULT 'Stopped'
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER, started_at TEXT, finished_at TEXT,
            pid INTEGER, log_path TEXT, exit_code INTEGER
        )''')
        cur.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT,
            joined_at TEXT, last_seen TEXT
        )''')
        conn.commit()

init_db()

def add_file_record(user_id, username, filename, orig_name, path, file_type):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO files (user_id,username,filename,orig_name,path,uploaded_at,file_type) VALUES (?,?,?,?,?,?,?)",
            (user_id, username, filename, orig_name, path, datetime.now(timezone.utc).isoformat(), file_type)
        )
        conn.commit()
        return cur.lastrowid

def list_user_files(user_id):
    cur = conn.cursor()
    cur.execute("SELECT id,filename,orig_name,uploaded_at,file_type,status,pid FROM files WHERE user_id=? ORDER BY id DESC", (user_id,))
    return cur.fetchall()

def get_file_record(file_id):
    cur = conn.cursor()
    cur.execute("SELECT * FROM files WHERE id=?", (file_id,))
    return cur.fetchone()

def remove_file_record(file_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute("DELETE FROM files WHERE id=?", (file_id,))
        conn.commit()

def record_run_start(file_id, pid, log_path):
    with db_lock:
        cur = conn.cursor()
        cur.execute("INSERT INTO runs (file_id,started_at,pid,log_path) VALUES (?,?,?,?)",
                    (file_id, datetime.now(timezone.utc).isoformat(), pid, log_path))
        conn.commit()
        return cur.lastrowid

def record_run_finish(run_id, exit_code):
    with db_lock:
        cur = conn.cursor()
        cur.execute("UPDATE runs SET finished_at=?,exit_code=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), exit_code, run_id))
        conn.commit()

def update_file_status(file_id, pid, status):
    with db_lock:
        cur = conn.cursor()
        cur.execute("UPDATE files SET pid=?,status=? WHERE id=?", (pid, status, file_id))
        conn.commit()

processes = {}
proc_lock = threading.Lock()

def get_system_load():
    try:
        return float(psutil.cpu_percent(interval=0.1)), float(psutil.virtual_memory().percent), len(processes)
    except Exception:
        return 0.0, 0.0, 0

def should_stop_due_to_load():
    cpu, mem, cnt = get_system_load()
    if cnt >= MAX_RUNNING_PROCESSES:
        return True, f"Too many running processes ({cnt}/{MAX_RUNNING_PROCESSES})"
    if cpu >= CPU_THRESHOLD:
        return True, f"High CPU load ({cpu}%)"
    if mem >= MEMORY_THRESHOLD:
        return True, f"High memory usage ({mem}%)"
    return False, None

def get_file_type(filename):
    if not filename: return "unknown"
    n = filename.lower()
    if n.endswith(".py"):   return "python"
    if n.endswith(".js"):   return "javascript"
    if n.endswith(".zip"):  return "zip"
    if any(n.endswith(e) for e in [".tar", ".tar.gz", ".tgz"]): return "archive"
    return "unknown"

def extract_archive(file_path, extract_dir):
    try:
        if file_path.lower().endswith(".zip"):
            with zipfile.ZipFile(file_path, 'r') as z: z.extractall(extract_dir)
        elif file_path.lower().endswith((".tar.gz", ".tgz")):
            with tarfile.open(file_path, 'r:gz') as t: t.extractall(extract_dir)
        elif file_path.lower().endswith(".tar"):
            with tarfile.open(file_path, 'r') as t: t.extractall(extract_dir)
        else:
            return False, "Unsupported archive format"
        return True, None
    except Exception as e:
        return False, str(e)

def find_main_file(directory):
    priority = ["main.py","bot.py","app.py","server.py","index.py","script.py",
                "main.js","bot.js","app.js","server.js","index.js","script.js"]
    for f in priority:
        p = os.path.join(directory, f)
        if os.path.isfile(p): return p
    for root, _, files in os.walk(directory):
        for f in priority:
            if f in files: return os.path.join(root, f)
    for root, _, files in os.walk(directory):
        for f in files:
            if f.endswith((".py", ".js")): return os.path.join(root, f)
    return None

def install_requirements_from_file(req_path, chat_id, file_name):
    if not os.path.exists(req_path): return False, "requirements.txt not found"
    with open(req_path) as f:
        reqs = [l.strip() for l in f if l.strip() and not l.startswith('#')]
    if not reqs: return True, "No requirements"
    ok, fail, failed = 0, 0, []
    for pkg in reqs:
        try:
            r = subprocess.run([sys.executable, "-m", "pip", "install", pkg],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0: ok += 1
            else: fail += 1; failed.append(pkg)
        except Exception as e:
            fail += 1; failed.append(f"{pkg} ({e})")
    msg = f"Installed {ok} packages"
    if fail: msg += f", failed {fail}: {', '.join(failed[:5])}"
    return fail == 0, msg

def extract_imports(file_path):
    imports = set()
    try:
        with open(file_path, encoding='utf-8') as f: content = f.read()
        tree = ast.parse(content)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names: imports.add(a.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0: imports.add(node.module.split('.')[0])
    except Exception as e:
        logger.error(f"Import parse error: {e}")
    return imports

def install_missing_imports(imports, chat_id, file_name):
    pip_map = {'telebot':'pyTelegramBotAPI','PIL':'Pillow','cv2':'opencv-python',
               'Crypto':'pycryptodome','bs4':'beautifulsoup4'}
    missing = [m for m in imports if not importlib.util.find_spec(m)]
    if not missing: return True, "All imports available"
    ok, fail, failed = 0, 0, []
    for mod in missing:
        pkg = pip_map.get(mod, mod)
        try:
            r = subprocess.run([sys.executable, "-m", "pip", "install", pkg],
                               capture_output=True, text=True, timeout=120)
            if r.returncode == 0: ok += 1
            else: fail += 1; failed.append(mod)
        except Exception as e:
            fail += 1; failed.append(mod)
    msg = f"Installed {ok} modules"
    if fail: msg += f", failed {fail}: {', '.join(failed)}"
    return fail == 0, msg

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

def main_menu_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("📢 Updates Channel"))
    kb.add(KeyboardButton("📤 Upload File"), KeyboardButton("📁 My Files"))
    kb.add(KeyboardButton("⚡ Bot Speed"), KeyboardButton("📊 Statistics"))
    kb.add(KeyboardButton("📞 Contact Owner"))
    return kb

def admin_panel_kb():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("👥 Total Users",       callback_data="admin_total_users"),
        InlineKeyboardButton("📁 Total Files",       callback_data="admin_total_files")
    )
    kb.add(
        InlineKeyboardButton("👥 View Users",        callback_data="admin_view_users"),
        InlineKeyboardButton("🛑 Stop Process",      callback_data="admin_stop_process")
    )
    kb.add(InlineKeyboardButton("📢 Broadcast",      callback_data="admin_broadcast"))
    kb.add(InlineKeyboardButton("❌ Exit Admin",     callback_data="admin_exit"))
    return kb

def file_actions_kb(file_id, is_running=False):
    kb = InlineKeyboardMarkup()
    if is_running:
        kb.row(InlineKeyboardButton("⏹ Stop",    callback_data=f"stop:{file_id}"),
               InlineKeyboardButton("🔁 Restart", callback_data=f"restart:{file_id}"))
    else:
        kb.row(InlineKeyboardButton("▶️ Start",   callback_data=f"start:{file_id}"),
               InlineKeyboardButton("🔁 Restart", callback_data=f"restart:{file_id}"))
    kb.row(
        InlineKeyboardButton("🗑 Delete",   callback_data=f"delete:{file_id}"),
        InlineKeyboardButton("📄 Logs",     callback_data=f"logs:{file_id}"),
        InlineKeyboardButton("⬇️ Download", callback_data=f"download:{file_id}")
    )
    kb.row(InlineKeyboardButton("⬅️ Back", callback_data="back_to_files"))
    return kb

@bot.message_handler(commands=['start', 'help'])
def start_handler(message):
    user    = message.from_user
    user_id = user.id
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO users (user_id,username,joined_at,last_seen) VALUES (?,?,?,?)",
            (user_id, user.username or "", datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat())
        )
        conn.commit()

    file_count = len(list_user_files(user_id))
    bot.send_message(message.chat.id, f"""
🔥 <b>24x7 Danger Hosting Bot</b>

👋 Welcome <b>{html_lib.escape(user.first_name or 'User')}</b>
🆔 Your ID: <code>{user_id}</code>
📂 Files: {file_count}/{MAX_FILES_PER_USER}

🤖 <b>Features:</b>
• Host Python & JS scripts 24/7
• Auto-install dependencies
• Real-time logs

👇 Use buttons below to get started!
    """, reply_markup=main_menu_kb())

@bot.message_handler(commands=['admin'])
def admin_command(message):
    if message.from_user.id != ADMIN_ID:
        bot.reply_to(message, "⛔ Not authorized.")
        return
    msg = bot.reply_to(message, "🔐 Enter the admin password:")
    bot.register_next_step_handler(msg, check_admin_password)

def check_admin_password(message):
    if message.text == "primesamiul170#":
        bot.send_message(message.chat.id, "✅ Access granted. Welcome to Admin Panel.",
                         reply_markup=admin_panel_kb())
    else:
        bot.reply_to(message, "❌ Incorrect password.")

@bot.callback_query_handler(func=lambda c: c.data.startswith("admin_"))
def admin_panel_callback(call):
    if call.from_user.id != ADMIN_ID:
        bot.answer_callback_query(call.id, "Not authorized.")
        return
    data    = call.data
    chat_id = call.message.chat.id

    if data == "admin_total_users":
        cur = conn.cursor(); cur.execute("SELECT COUNT(*) FROM users")
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, f"👥 Total users: {cur.fetchone()[0] or 0}")

    elif data == "admin_total_files":
        cur = conn.cursor(); cur.execute("SELECT COUNT(*) FROM files")
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, f"📁 Total files: {cur.fetchone()[0] or 0}")

    elif data == "admin_view_users":
        admin_view_users(chat_id, 0, call.message.message_id)
        bot.answer_callback_query(call.id)

    elif data == "admin_stop_process":
        admin_stop_process_list(chat_id, call.message.message_id)
        bot.answer_callback_query(call.id)

    elif data == "admin_broadcast":
        msg = bot.send_message(chat_id, "📢 Send the broadcast message.\n(/cancel to abort)")
        bot.register_next_step_handler(msg, broadcast_send)
        bot.answer_callback_query(call.id)

    elif data == "admin_exit":
        bot.answer_callback_query(call.id)
        bot.send_message(chat_id, "👋 Exited admin panel.", reply_markup=main_menu_kb())

    elif data.startswith("admin_users_page:"):
        page = int(data.split(":")[1])
        admin_view_users(chat_id, page, call.message.message_id)
        bot.answer_callback_query(call.id)

    elif data.startswith("admin_user_files:"):
        admin_view_user_files(chat_id, int(data.split(":")[1]), 0, call.message.message_id)
        bot.answer_callback_query(call.id)

    elif data.startswith("admin_user_files_page:"):
        _, uid, pg = data.split(":")
        admin_view_user_files(chat_id, int(uid), int(pg), call.message.message_id)
        bot.answer_callback_query(call.id)

    elif data.startswith("admin_download_file:"):
        admin_download_file(chat_id, int(data.split(":")[1]), call.id)

    elif data.startswith("admin_stop_file:"):
        stop_file_process(int(data.split(":")[1]))
        bot.answer_callback_query(call.id, "✅ Stopped.")
        admin_stop_process_list(chat_id, call.message.message_id)

    elif data == "admin_back":
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=admin_panel_kb())
        bot.answer_callback_query(call.id)

    else:
        bot.answer_callback_query(call.id, "Unknown action")

def admin_view_users(chat_id, page=0, message_id=None):
    cur = conn.cursor(); cur.execute("SELECT user_id,username,joined_at FROM users ORDER BY user_id")
    users = cur.fetchall(); per_page = 5
    total_pages = max(1, (len(users) + per_page - 1) // per_page)
    page_users  = users[page*per_page:(page+1)*per_page]
    text = f"👥 <b>Users (Page {page+1}/{total_pages})</b>\n\n"
    for u in page_users:
        text += f"👤 <b>{html_lib.escape(u['username'] or 'None')}</b> (ID: <code>{u['user_id']}</code>)\n"
    kb = InlineKeyboardMarkup()
    for u in page_users:
        kb.add(InlineKeyboardButton(f"📂 {html_lib.escape(u['username'] or str(u['user_id']))}",
                                    callback_data=f"admin_user_files:{u['user_id']}"))
    if page > 0:              kb.add(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin_users_page:{page-1}"))
    if page < total_pages-1:  kb.add(InlineKeyboardButton("Next ➡️", callback_data=f"admin_users_page:{page+1}"))
    kb.add(InlineKeyboardButton("🔙 Back", callback_data="admin_back"))
    _edit_or_send(chat_id, message_id, text, kb)

def admin_view_user_files(chat_id, user_id, page=0, message_id=None):
    cur = conn.cursor()
    cur.execute("SELECT id,orig_name,file_type,status FROM files WHERE user_id=? ORDER BY id DESC", (user_id,))
    files = cur.fetchall(); per_page = 5
    total_pages = max(1, (len(files) + per_page - 1) // per_page)
    page_files  = files[page*per_page:(page+1)*per_page]
    cur.execute("SELECT username FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone(); username = row["username"] if row else "Unknown"
    text = f"📂 <b>{html_lib.escape(username)} (ID {user_id})</b> — Page {page+1}/{total_pages}\n\n"
    for f in page_files:
        text += f"📄 <b>{html_lib.escape(f['orig_name'])}</b> | {f['file_type']} | {f['status']}\n"
    kb = InlineKeyboardMarkup()
    for f in page_files:
        kb.add(InlineKeyboardButton(f"⬇️ {html_lib.escape(f['orig_name'])}", callback_data=f"admin_download_file:{f['id']}"))
    if page > 0:              kb.add(InlineKeyboardButton("⬅️ Prev", callback_data=f"admin_user_files_page:{user_id}:{page-1}"))
    if page < total_pages-1:  kb.add(InlineKeyboardButton("Next ➡️", callback_data=f"admin_user_files_page:{user_id}:{page+1}"))
    kb.add(InlineKeyboardButton("🔙 Users", callback_data="admin_view_users"))
    _edit_or_send(chat_id, message_id, text, kb)

def admin_download_file(chat_id, file_id, callback_id=None):
    rec = get_file_record(file_id)
    if not rec:
        if callback_id: bot.answer_callback_query(callback_id, "File not found.")
        return
    if callback_id: bot.answer_callback_query(callback_id, "Sending...")
    fp = rec["path"]
    if os.path.isdir(fp):
        zp = os.path.join(TEMP_DIR, f"adl_{file_id}.zip")
        shutil.make_archive(zp.replace('.zip',''), 'zip', fp)
        with open(zp,'rb') as f: bot.send_document(chat_id, f, caption=f"📦 {rec['orig_name']}")
        os.remove(zp)
    elif os.path.exists(fp):
        with open(fp,'rb') as f: bot.send_document(chat_id, f, caption=f"📄 {rec['orig_name']}")
    else:
        bot.send_message(chat_id, "❌ File not found on disk.")

def admin_stop_process_list(chat_id, message_id=None):
    cur = conn.cursor()
    cur.execute("SELECT id,orig_name,user_id FROM files WHERE status='Running'")
    running = cur.fetchall()
    if not running:
        _edit_or_send(chat_id, message_id, "✅ No running processes.", None)
        return
    text = "🛑 <b>Running Processes</b>\n\nSelect to stop:\n"
    kb   = InlineKeyboardMarkup()
    for f in running:
        kb.add(InlineKeyboardButton(f"🛑 {html_lib.escape(f['orig_name'])} (User {f['user_id']})",
                                    callback_data=f"admin_stop_file:{f['id']}"))
    kb.add(InlineKeyboardButton("🔙 Back", callback_data="admin_back"))
    _edit_or_send(chat_id, message_id, text, kb)

def broadcast_send(message):
    if message.text == "/cancel":
        bot.send_message(message.chat.id, "❌ Cancelled."); return
    cur = conn.cursor(); cur.execute("SELECT user_id FROM users")
    users = cur.fetchall()
    bot.send_message(message.chat.id, f"📢 Broadcasting to {len(users)} users...")
    ok = fail = 0
    for row in users:
        try:
            bot.send_message(row[0], f"📢 <b>Announcement</b>\n\n{message.text}")
            ok += 1
        except: fail += 1
        time.sleep(0.05)
    bot.send_message(message.chat.id, f"✅ Done. Success: {ok} | Failed: {fail}")

def _edit_or_send(chat_id, message_id, text, kb):
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb); return
        except: pass
    bot.send_message(chat_id, text, reply_markup=kb)

@bot.callback_query_handler(func=lambda c: True)
def user_callback_handler(call):
    data, chat_id, user_id = call.data, call.message.chat.id, call.from_user.id

    if data == "back_to_files":
        try: bot.delete_message(chat_id, call.message.message_id)
        except: pass
        send_files_list(chat_id, user_id); return

    try:
        if data.startswith("manage:"):
            show_file_management(chat_id, int(data.split(":")[1]), user_id, call.message.message_id)

        elif data.startswith("start:"):
            bot.answer_callback_query(call.id, "Starting...")
            start_file_process(int(data.split(":")[1]), chat_id)
            time.sleep(1)
            show_file_management(chat_id, int(data.split(":")[1]), user_id, call.message.message_id)

        elif data.startswith("stop:"):
            bot.answer_callback_query(call.id, "Stopping...")
            stop_file_process(int(data.split(":")[1]))
            bot.send_message(chat_id, "⏹ Process stopped")
            time.sleep(1)
            show_file_management(chat_id, int(data.split(":")[1]), user_id, call.message.message_id)

        elif data.startswith("restart:"):
            bot.answer_callback_query(call.id, "Restarting...")
            fid = int(data.split(":")[1])
            stop_file_process(fid); time.sleep(2)
            start_file_process(fid, chat_id); time.sleep(1)
            show_file_management(chat_id, fid, user_id, call.message.message_id)

        elif data.startswith("delete:"):
            bot.answer_callback_query(call.id, "Deleting...")
            fid = int(data.split(":")[1])
            rec = get_file_record(fid)
            if rec:
                stop_file_process(fid)
                try:
                    fp = rec["path"]
                    shutil.rmtree(fp, ignore_errors=True) if os.path.isdir(fp) else os.remove(fp)
                except: pass
                remove_file_record(fid)
            bot.send_message(chat_id, "🗑 File deleted")
            send_files_list(chat_id, user_id)

        elif data.startswith("logs:"):
            bot.answer_callback_query(call.id, "Fetching logs...")
            fid  = int(data.split(":")[1])
            logs = get_file_logs(fid)
            rec  = get_file_record(fid)
            name = rec["orig_name"] if rec else "Unknown"
            if len(logs) > 4000: logs = "... (truncated)\n" + logs[-4000:]
            bot.send_message(chat_id, f"📄 <b>Logs: {html_lib.escape(name)}</b>\n\n<pre>{html_lib.escape(logs)}</pre>")

        elif data.startswith("download:"):
            bot.answer_callback_query(call.id, "Preparing...")
            fid = int(data.split(":")[1])
            rec = get_file_record(fid)
            if not rec: bot.send_message(chat_id, "❌ File not found."); return
            if user_id not in (rec["user_id"], ADMIN_ID):
                bot.send_message(chat_id, "❌ Access denied."); return
            fp = rec["path"]
            if os.path.isdir(fp):
                zp = os.path.join(TEMP_DIR, f"dl_{fid}_{user_id}.zip")
                shutil.make_archive(zp.replace('.zip',''), 'zip', fp)
                with open(zp,'rb') as f: bot.send_document(chat_id, f)
                os.remove(zp)
            elif os.path.exists(fp):
                with open(fp,'rb') as f: bot.send_document(chat_id, f)
            else:
                bot.send_message(chat_id, "❌ File not found on disk.")

    except Exception as e:
        bot.answer_callback_query(call.id, "Error processing request")
        logger.error(f"Callback error: {e}")

def send_files_list(chat_id, user_id):
    files = list_user_files(user_id)
    if not files:
        bot.send_message(chat_id, "📁 <b>Your Files</b>\n\nNo files uploaded yet.")
        return
    kb = InlineKeyboardMarkup()
    for fid, fname, orig, uploaded, ftype, status, pid in files:
        emoji = "🟢" if status == "Running" else "🔴"
        kb.add(InlineKeyboardButton(f"{emoji} {orig} ({ftype})", callback_data=f"manage:{fid}"))
    bot.send_message(chat_id, "📁 <b>Your Files</b>\n\nClick a file to manage it:", reply_markup=kb)

def show_file_management(chat_id, file_id, user_id, message_id=None):
    rec = get_file_record(file_id)
    if not rec: bot.send_message(chat_id, "❌ File not found"); return
    if rec["user_id"] != user_id: bot.send_message(chat_id, "❌ Access denied"); return
    with proc_lock: running = file_id in processes
    text = f"""
⚙️ <b>File Management</b>

📁 File: {html_lib.escape(rec['orig_name'])}
📊 Type: {rec['file_type']}
📈 Status: {"🟢 Running" if running else "🔴 Stopped"}{"" if not rec['pid'] else chr(10)+"PID: " + str(rec['pid'])}
⏰ Uploaded: {rec['uploaded_at'][:16]}
    """
    _edit_or_send(chat_id, message_id, text, file_actions_kb(file_id, running))

@bot.message_handler(func=lambda m: m.text == "📢 Updates Channel")
def updates_handler(message):
    bot.send_message(message.chat.id, f"📢 Join our channel: {CHANNEL_URL}")

@bot.message_handler(func=lambda m: m.text == "📞 Contact Owner")
def contact_handler(message):
    bot.send_message(message.chat.id, "📞 Contact: @Prime_x_Samiul")

@bot.message_handler(func=lambda m: m.text == "⚡ Bot Speed")
def speed_handler(message):
    cpu, mem, cnt = get_system_load()
    uptime = datetime.now(timezone.utc) - START_TIME
    h, r   = divmod(uptime.seconds, 3600); mn, _ = divmod(r, 60)
    bot.send_message(message.chat.id, f"""
⚡ <b>System Status</b>

• CPU Usage: {cpu:.1f}%
• Memory Usage: {mem:.1f}%
• Running Processes: {cnt}/{MAX_RUNNING_PROCESSES}
• Uptime: {uptime.days}d {h}h {mn}m
    """)

@bot.message_handler(func=lambda m: m.text == "📊 Statistics")
def stats_handler(message):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT user_id) FROM files"); uc = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM files");                fc = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM files WHERE status='Running'"); rc = cur.fetchone()[0] or 0
    cpu, mem, _ = get_system_load()
    bot.send_message(message.chat.id, f"""
📊 <b>Bot Statistics</b>

👥 Total Users: {uc}
📁 Total Files: {fc}
🚀 Running Files: {rc}
⚡ CPU Usage: {cpu:.1f}%
💾 Memory Usage: {mem:.1f}%
    """)

@bot.message_handler(func=lambda m: m.text == "📁 My Files")
def my_files_handler(message):
    send_files_list(message.chat.id, message.from_user.id)

@bot.message_handler(func=lambda m: m.text == "📤 Upload File")
def upload_handler(message):
    bot.send_message(message.chat.id,
        "📤 <b>Upload a File</b>\n\n"
        "Send me a Python (.py), JavaScript (.js), or ZIP archive.\n\n"
        "For ZIP files I will:\n"
        "• Extract the archive\n"
        "• Find the main file\n"
        "• Install requirements.txt\n"
        "• Auto-start the script"
    )

@bot.message_handler(content_types=['document'])
def document_handler(message):
    user    = message.from_user
    user_id = user.id
    if len(list_user_files(user_id)) >= MAX_FILES_PER_USER:
        bot.reply_to(message, f"❌ File limit reached ({MAX_FILES_PER_USER}). Delete some to upload new ones.")
        return
    try:
        info  = bot.get_file(message.document.file_id)
        data  = bot.download_file(info.file_path)
    except Exception as e:
        bot.reply_to(message, f"❌ Download failed: {e}"); return

    orig_name = message.document.file_name or "unknown"
    file_type = get_file_type(orig_name)
    user_dir  = os.path.join(UPLOADS_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    safe_name = f"{int(time.time())}_{orig_name}"
    file_path = os.path.join(user_dir, safe_name)
    try:
        with open(file_path, 'wb') as f: f.write(data)
    except Exception as e:
        bot.reply_to(message, f"❌ Save failed: {e}"); return

    final_path, extracted_dir = file_path, None

    if file_type in ["zip", "archive"]:
        bot.reply_to(message, "📦 Extracting archive...")
        extracted_dir = os.path.join(TEMP_DIR, f"ext_{user_id}_{int(time.time())}")
        os.makedirs(extracted_dir, exist_ok=True)
        ok, err = extract_archive(file_path, extracted_dir)
        if not ok:
            bot.reply_to(message, f"❌ Extract failed: {err}")
            try: os.remove(file_path)
            except: pass
            return
        main_file = find_main_file(extracted_dir)
        if not main_file:
            bot.reply_to(message, "❌ No main Python/JS file found in archive.")
            try: shutil.rmtree(extracted_dir, ignore_errors=True); os.remove(file_path)
            except: pass
            return
        final_path = extracted_dir
        file_type  = get_file_type(main_file)
        bot.reply_to(message, f"✅ Found main file: {os.path.basename(main_file)}")

    file_id = add_file_record(user_id, user.username, safe_name, orig_name, final_path, file_type)

    if file_type in ["python", "javascript"]:
        bot.reply_to(message, "⏳ <b>Starting your bot, please wait...</b>")
        start_file_process(file_id, message.chat.id)
    else:
        bot.reply_to(message, "✅ Uploaded! Use 📁 My Files to manage it.")

def start_file_process(file_id, chat_id):
    ok, reason = should_stop_due_to_load()
    if ok:
        bot.send_message(chat_id, f"⚠️ Cannot start: {reason}"); return

    rec = get_file_record(file_id)
    if not rec: bot.send_message(chat_id, "❌ File not found"); return

    fp, orig, ftype = rec["path"], rec["orig_name"], rec["file_type"]

    if os.path.isdir(fp):
        main_file = find_main_file(fp)
        if not main_file: bot.send_message(chat_id, "❌ No main file found"); return
        target, working_dir = main_file, os.path.dirname(main_file)
    else:
        if not os.path.exists(fp): bot.send_message(chat_id, f"❌ File missing: {fp}"); return
        target, working_dir = fp, os.path.dirname(fp)

    ext = os.path.splitext(target)[1].lower()

    if ext == ".py":
        req = os.path.join(working_dir, "requirements.txt")
        if os.path.exists(req):
            bot.send_message(chat_id, "📦 Installing requirements.txt...")
            ok2, msg = install_requirements_from_file(req, chat_id, orig)
            bot.send_message(chat_id, f"{'✅' if ok2 else '⚠️'} {msg}")
        bot.send_message(chat_id, "🔍 Checking imports...")
        imps = extract_imports(target)
        if imps:
            ok2, msg = install_missing_imports(imps, chat_id, orig)
            bot.send_message(chat_id, f"📦 Imports: {msg}")

    if ext == ".py":   cmd = [sys.executable, target]
    elif ext == ".js": cmd = ["node", target]
    else: bot.send_message(chat_id, f"❌ Unsupported: {ext}"); return

    log_path = os.path.join(LOGS_DIR, f"file_{file_id}_{int(time.time())}.log")
    try:
        with open(log_path, 'w') as lf:
            proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, cwd=working_dir, text=True)
        run_id = record_run_start(file_id, proc.pid, log_path)
        update_file_status(file_id, proc.pid, "Running")
        with proc_lock:
            processes[file_id] = {'process': proc, 'run_id': run_id,
                                   'log_path': log_path, 'started_at': datetime.now(timezone.utc).isoformat()}

        time.sleep(2)
        if proc.poll() is None:
            bot.send_message(chat_id,
                f"🚀 <b>Your Bot Is Live!</b>\n\n"
                f"📄 File: <b>{html_lib.escape(orig)}</b>\n"
                f"🆔 PID: <code>{proc.pid}</code>\n"
                f"✅ Status: Running 24/7\n\n"
                f"📁 My Files → Logs to see output.")
        else:
            exit_code = proc.returncode
            early_log = ""
            try:
                with open(log_path) as lf: early_log = lf.read()[-1500:]
            except: pass
            update_file_status(file_id, None, "Stopped")
            with proc_lock: processes.pop(file_id, None)
            record_run_finish(run_id, exit_code)
            bot.send_message(chat_id,
                f"❌ <b>Bot crashed on start!</b>\n\n"
                f"📄 File: <b>{html_lib.escape(orig)}</b>\n"
                f"🔴 Exit Code: <code>{exit_code}</code>\n\n"
                f"📋 Error:\n<pre>{html_lib.escape(early_log or 'No output')}</pre>")
            return

        def monitor():
            try: exit_code = proc.wait()
            except: exit_code = -1
            finally:
                update_file_status(file_id, None, "Stopped")
                record_run_finish(run_id, exit_code)
                with proc_lock: processes.pop(file_id, None)
                if exit_code not in (0, -15):
                    try:
                        crash_log = ""
                        try:
                            with open(log_path) as lf:
                                crash_log = ''.join(lf.readlines()[-20:])[-1000:]
                        except: pass
                        bot.send_message(chat_id,
                            f"⚠️ <b>{html_lib.escape(orig)}</b> crashed!\n"
                            f"🔴 Exit: <code>{exit_code}</code>\n\n"
                            f"<pre>{html_lib.escape(crash_log or 'No log')}</pre>")
                    except: pass

        threading.Thread(target=monitor, daemon=True).start()
    except Exception as e:
        bot.send_message(chat_id, f"❌ Failed to start: {e}")

def stop_file_process(file_id):
    with proc_lock:
        if file_id in processes:
            proc = processes[file_id]['process']
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try: proc.wait(timeout=5)
                    except subprocess.TimeoutExpired: proc.kill(); proc.wait()
            except Exception as e: logger.error(f"Stop error {file_id}: {e}")
            processes.pop(file_id, None)
    update_file_status(file_id, None, "Stopped")

def get_file_logs(file_id, lines=50):
    try:
        with proc_lock:
            if file_id in processes:
                lp = processes[file_id]['log_path']
                if os.path.exists(lp):
                    with open(lp) as f: c = f.readlines()
                    return ''.join(c[-lines:]) or "No logs yet"
        cur = conn.cursor()
        cur.execute("SELECT log_path FROM runs WHERE file_id=? ORDER BY started_at DESC LIMIT 1", (file_id,))
        row = cur.fetchone()
        if row and row[0] and os.path.exists(row[0]):
            with open(row[0]) as f: c = f.readlines()
            return ''.join(c[-lines:]) or "No logs"
        return "No log file found"
    except Exception as e:
        return f"Log error: {e}"

class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is active and running 24/7!")

    def log_message(self, format, *args):
        return

def run_http_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

def start_bot():
    logger.info("Starting bot...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=50)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)

if __name__ == "__main__":
    threading.Thread(target=run_http_server, daemon=True).start()
    start_bot()