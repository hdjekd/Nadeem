import os
import json
import sqlite3
import time
import hashlib
import threading
import requests
from datetime import datetime
from flask import Flask, request, jsonify
from flask_cors import CORS
import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, CallbackQueryHandler, MessageHandler, Filters

# ===================== الإعدادات =====================
BOT_TOKEN = '8506676223:AAHcbApXCWpLjR7aQrdbK8LlWsr54SE1qa4'
ADMIN_CHAT_ID = '8311254462'
app = Flask(__name__)
CORS(app)

# ===================== قاعدة البيانات =====================
conn = sqlite3.connect('tomb_bot.db', check_same_thread=False)
c = conn.cursor()

c.execute('''CREATE TABLE IF NOT EXISTS approvals
             (request_id TEXT PRIMARY KEY, 
              status TEXT, 
              timestamp INTEGER,
              username TEXT,
              device_name TEXT,
              device_info TEXT,
              ip_address TEXT)''')

c.execute('''CREATE TABLE IF NOT EXISTS settings
             (key TEXT PRIMARY KEY, 
              value TEXT)''')

c.execute('''CREATE TABLE IF NOT EXISTS passwords
             (id INTEGER PRIMARY KEY AUTOINCREMENT,
              password_hash TEXT,
              updated_at INTEGER,
              updated_by TEXT)''')

c.execute('''CREATE TABLE IF NOT EXISTS access_logs
             (id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT,
              device_name TEXT,
              ip_address TEXT,
              status TEXT,
              timestamp INTEGER)''')

conn.commit()

# ===================== دوال الإعدادات =====================
def get_setting(key, default=""):
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    if row:
        return row[0]
    return default

def set_setting(key, value):
    c.execute("INSERT OR REPLACE INTO settings VALUES (?, ?)", (key, value))
    conn.commit()

def get_app_password():
    c.execute("SELECT password_hash FROM passwords ORDER BY updated_at DESC LIMIT 1")
    row = c.fetchone()
    if row:
        return row[0]
    default_hash = hashlib.sha256("123456".encode()).hexdigest()
    c.execute("INSERT INTO passwords (password_hash, updated_at, updated_by) VALUES (?, ?, ?)", 
              (default_hash, int(time.time()), "system"))
    conn.commit()
    return default_hash

# ✅ تم تغيير اسم الدالة من verify_password إلى check_password لتجنب التضارب
def check_password(password):
    """التحقق من كلمة المرور"""
    current_hash = get_app_password()
    input_hash = hashlib.sha256(password.encode()).hexdigest()
    return input_hash == current_hash

def update_password(new_password, updated_by="bot"):
    new_hash = hashlib.sha256(new_password.encode()).hexdigest()
    c.execute("INSERT INTO passwords (password_hash, updated_at, updated_by) VALUES (?, ?, ?)", 
              (new_hash, int(time.time()), updated_by))
    conn.commit()
    return True

def log_access(username, device_name, ip_address, status):
    c.execute("""INSERT INTO access_logs 
                 (username, device_name, ip_address, status, timestamp) 
                 VALUES (?, ?, ?, ?, ?)""",
              (username, device_name, ip_address, status, int(time.time())))
    conn.commit()

def get_access_stats():
    total = c.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
    pending = c.execute("SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]
    approved = c.execute("SELECT COUNT(*) FROM approvals WHERE status='approved'").fetchone()[0]
    denied = c.execute("SELECT COUNT(*) FROM approvals WHERE status='denied'").fetchone()[0]
    
    recent = c.execute("""SELECT username, device_name, status, timestamp 
                          FROM approvals ORDER BY timestamp DESC LIMIT 10""").fetchall()
    
    return {
        "total": total,
        "pending": pending,
        "approved": approved,
        "denied": denied,
        "recent": recent
    }

# ===================== إعدادات البوت =====================
bot = telegram.Bot(token=BOT_TOKEN)
pending_requests = {}

CUSTOM_LOGO = get_setting("custom_logo", "𓆩♛✦𓆪 TOMB OF MAKROTEC 𓆩♛✦𓆪")
WELCOME_MESSAGE = get_setting("welcome_message", "🔐 طلب فتح التطبيق")

def send_approval_request(request_id, app_name="Tomb", username="Unknown", 
                          device_name="Unknown", device_info="", ip_address="Unknown"):
    
    custom_logo = get_setting("custom_logo", CUSTOM_LOGO)
    welcome_msg = get_setting("welcome_message", WELCOME_MESSAGE)
    
    message_text = f"""
{custom_logo}

🔐 *{welcome_msg}*

👤 *المستخدم:* `{username}`
📱 *الجهاز:* `{device_name}`
ℹ️ *المعلومات:* `{device_info}`
🌐 *IP:* `{ip_address}`
🕐 *الوقت:* `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`

⚠️ *هل تسمح بالدخول؟*
"""
    
    keyboard = [
        [
            InlineKeyboardButton("✅ موافقة", callback_data=f"approve_{request_id}"),
            InlineKeyboardButton("❌ رفض", callback_data=f"deny_{request_id}")
        ],
        [
            InlineKeyboardButton("📊 معلومات الجهاز", callback_data=f"info_{request_id}")
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=message_text,
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    except Exception as e:
        print(f"Error sending message: {e}")
        bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=message_text.replace('*', '').replace('`', ''),
            reply_markup=reply_markup
        )
    
    pending_requests[request_id] = {
        "status": "pending", 
        "timestamp": time.time(),
        "username": username,
        "device_name": device_name
    }
    
    c.execute("""INSERT OR REPLACE INTO approvals 
                 (request_id, status, timestamp, username, device_name, device_info, ip_address) 
                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
              (request_id, "pending", int(time.time()), username, device_name, device_info, ip_address))
    conn.commit()

# ===================== دوال البوت =====================
def handle_callback(update, context):
    query = update.callback_query
    query.answer()
    
    data = query.data
    
    if data.startswith("approve_"):
        request_id = data[8:]
        status = "approved"
        response_text = "✅ **تمت الموافقة بنجاح**\n\nيمكن للمستخدم الآن الدخول إلى التطبيق."
        
        if request_id in pending_requests:
            pending_requests[request_id]["status"] = status
        
        c.execute("UPDATE approvals SET status = ? WHERE request_id = ?", (status, request_id))
        conn.commit()
        
        c.execute("SELECT username, device_name, ip_address FROM approvals WHERE request_id = ?", (request_id,))
        row = c.fetchone()
        if row:
            log_access(row[0], row[1], row[2], "approved")
        
        bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"✅ تمت الموافقة على طلب `{request_id[:8]}`",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        
    elif data.startswith("deny_"):
        request_id = data[5:]
        status = "denied"
        response_text = "❌ **تم رفض الطلب**\n\nلم يتم السماح للمستخدم بالدخول."
        
        if request_id in pending_requests:
            pending_requests[request_id]["status"] = status
        
        c.execute("UPDATE approvals SET status = ? WHERE request_id = ?", (status, request_id))
        conn.commit()
        
        c.execute("SELECT username, device_name, ip_address FROM approvals WHERE request_id = ?", (request_id,))
        row = c.fetchone()
        if row:
            log_access(row[0], row[1], row[2], "denied")
        
        bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"❌ تم رفض طلب `{request_id[:8]}`",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        
    elif data.startswith("info_"):
        request_id = data[5:]
        c.execute("""SELECT username, device_name, device_info, ip_address, timestamp 
                     FROM approvals WHERE request_id = ?""", (request_id,))
        row = c.fetchone()
        
        if row:
            info_text = f"""
📱 *معلومات الجهاز*

👤 *المستخدم:* {row[0]}
📱 *اسم الجهاز:* {row[1]}
ℹ️ *تفاصيل:* {row[2]}
🌐 *IP:* {row[3]}
🕐 *الوقت:* {datetime.fromtimestamp(row[4]).strftime('%Y-%m-%d %H:%M:%S')}
"""
            bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=info_text,
                parse_mode=telegram.ParseMode.MARKDOWN
            )
        else:
            bot.send_message(chat_id=ADMIN_CHAT_ID, text="❌ لم يتم العثور على الطلب")
        return
    
    else:
        return
    
    try:
        query.edit_message_text(
            text=f"{response_text}\n\n{get_setting('custom_logo', CUSTOM_LOGO)}",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
    except:
        pass

def handle_message(update, context):
    message = update.message
    chat_id = message.chat_id
    text = message.text
    
    if str(chat_id) != str(ADMIN_CHAT_ID):
        bot.send_message(chat_id=chat_id, text="⚠️ أنت غير مصرح لك باستخدام هذا البوت")
        return
    
    if text.startswith('/'):
        if text == '/start':
            welcome = f"""
{get_setting('custom_logo', CUSTOM_LOGO)}

✨ *مرحباً بك في نظام حماية تطبيق Tomb* ✨

📌 *الأوامر المتاحة:*

🔹 `/status` - عرض حالة النظام
🔹 `/stats` - إحصائيات الطلبات
🔹 `/pending` - الطلبات المعلقة
🔹 `/approved` - الطلبات المقبولة
🔹 `/denied` - الطلبات المرفوضة
🔹 `/logs` - سجل الدخول الأخير
🔹 `/setlogo <النص>` - تغيير الشعار
🔹 `/setwelcome <النص>` - تغيير رسالة الترحيب
🔹 `/getsettings` - عرض الإعدادات
🔹 `/setpass <كلمة المرور>` - تغيير كلمة مرور التطبيق
🔹 `/clear` - مسح جميع الطلبات

💡 *أمثلة:* 
/setlogo 𓆩♛✦𓆪
/setwelcome 🔐 طلب دخول جديد
/setpass MyNewPass123
"""
            bot.send_message(chat_id=chat_id, text=welcome, parse_mode=telegram.ParseMode.MARKDOWN)
        
        elif text == '/status':
            stats = get_access_stats()
            status_text = f"""
📊 *إحصائيات النظام*

📝 *إجمالي الطلبات:* {stats['total']}
⏳ *قيد الانتظار:* {stats['pending']}
✅ *تمت الموافقة:* {stats['approved']}
❌ *تم الرفض:* {stats['denied']}

🔄 *آخر تحديث:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
            bot.send_message(chat_id=chat_id, text=status_text, parse_mode=telegram.ParseMode.MARKDOWN)
        
        elif text == '/stats':
            stats = get_access_stats()
            if stats['recent']:
                stats_text = "📋 *آخر 10 طلبات:*\n\n"
                for req in stats['recent']:
                    time_str = datetime.fromtimestamp(req[3]).strftime('%H:%M:%S')
                    emoji = "✅" if req[2] == "approved" else "❌" if req[2] == "denied" else "⏳"
                    stats_text += f"{emoji} *{req[0]}* - {req[1]} - {time_str}\n"
                bot.send_message(chat_id=chat_id, text=stats_text, parse_mode=telegram.ParseMode.MARKDOWN)
            else:
                bot.send_message(chat_id=chat_id, text="📭 لا توجد طلبات بعد")
        
        elif text == '/pending':
            pending_reqs = c.execute(
                "SELECT request_id, username, device_name, timestamp FROM approvals WHERE status='pending' ORDER BY timestamp DESC"
            ).fetchall()
            
            if pending_reqs:
                text_msg = "⏳ *الطلبات المعلقة:*\n\n"
                for req in pending_reqs:
                    time_str = datetime.fromtimestamp(req[3]).strftime('%H:%M:%S')
                    text_msg += f"🆔 `{req[0][:8]}` - {req[1]} - {req[2]} - {time_str}\n"
                bot.send_message(chat_id=chat_id, text=text_msg, parse_mode=telegram.ParseMode.MARKDOWN)
            else:
                bot.send_message(chat_id=chat_id, text="✅ لا توجد طلبات معلقة")
        
        elif text == '/approved':
            approved_reqs = c.execute(
                "SELECT request_id, username, device_name, timestamp FROM approvals WHERE status='approved' ORDER BY timestamp DESC LIMIT 20"
            ).fetchall()
            
            if approved_reqs:
                text_msg = "✅ *الطلبات المقبولة:*\n\n"
                for req in approved_reqs:
                    time_str = datetime.fromtimestamp(req[3]).strftime('%Y-%m-%d %H:%M')
                    text_msg += f"👤 {req[1]} - {req[2]} - {time_str}\n"
                bot.send_message(chat_id=chat_id, text=text_msg, parse_mode=telegram.ParseMode.MARKDOWN)
            else:
                bot.send_message(chat_id=chat_id, text="📭 لا توجد طلبات مقبولة")
        
        elif text == '/denied':
            denied_reqs = c.execute(
                "SELECT request_id, username, device_name, timestamp FROM approvals WHERE status='denied' ORDER BY timestamp DESC LIMIT 20"
            ).fetchall()
            
            if denied_reqs:
                text_msg = "❌ *الطلبات المرفوضة:*\n\n"
                for req in denied_reqs:
                    time_str = datetime.fromtimestamp(req[3]).strftime('%Y-%m-%d %H:%M')
                    text_msg += f"👤 {req[1]} - {req[2]} - {time_str}\n"
                bot.send_message(chat_id=chat_id, text=text_msg, parse_mode=telegram.ParseMode.MARKDOWN)
            else:
                bot.send_message(chat_id=chat_id, text="📭 لا توجد طلبات مرفوضة")
        
        elif text == '/logs':
            logs = c.execute(
                "SELECT username, device_name, status, timestamp FROM access_logs ORDER BY timestamp DESC LIMIT 20"
            ).fetchall()
            
            if logs:
                log_text = "📋 *سجل الدخول الأخير:*\n\n"
                for log in logs:
                    time_str = datetime.fromtimestamp(log[3]).strftime('%Y-%m-%d %H:%M')
                    emoji = "✅" if log[2] == "approved" else "❌"
                    log_text += f"{emoji} {log[0]} - {log[1]} - {time_str}\n"
                bot.send_message(chat_id=chat_id, text=log_text, parse_mode=telegram.ParseMode.MARKDOWN)
            else:
                bot.send_message(chat_id=chat_id, text="📭 لا يوجد سجل")
        
        elif text == '/clear':
            c.execute("DELETE FROM approvals WHERE status != 'pending'")
            c.execute("DELETE FROM access_logs")
            conn.commit()
            bot.send_message(chat_id=chat_id, text="🗑️ تم مسح جميع الطلبات المنتهية وسجل الدخول")
        
        elif text.startswith('/setpass'):
            new_password = text.replace('/setpass', '').strip()
            if new_password and len(new_password) >= 4:
                if update_password(new_password, "bot"):
                    bot.send_message(
                        chat_id=chat_id,
                        text=f"✅ تم تغيير كلمة مرور التطبيق بنجاح!\n\n🔑 كلمة المرور الجديدة: `{new_password}`",
                        parse_mode=telegram.ParseMode.MARKDOWN
                    )
                else:
                    bot.send_message(chat_id=chat_id, text="❌ فشل في تغيير كلمة المرور")
            else:
                bot.send_message(
                    chat_id=chat_id,
                    text="❌ كلمة المرور يجب أن تكون 4 أحرف على الأقل\nمثال: /setpass MyNewPass123"
                )
        
        elif text.startswith('/setlogo'):
            new_logo = text.replace('/setlogo', '').strip()
            if new_logo:
                set_setting("custom_logo", new_logo)
                bot.send_message(chat_id=chat_id, text=f"✅ تم تغيير الشعار إلى:\n\n{new_logo}")
            else:
                bot.send_message(chat_id=chat_id, text="❌ الرجاء إدخال الشعار الجديد\nمثال: /setlogo 𓆩♛✦𓆪")
        
        elif text.startswith('/setwelcome'):
            new_welcome = text.replace('/setwelcome', '').strip()
            if new_welcome:
                set_setting("welcome_message", new_welcome)
                bot.send_message(chat_id=chat_id, text=f"✅ تم تغيير رسالة الترحيب إلى:\n\n{new_welcome}")
            else:
                bot.send_message(chat_id=chat_id, text="❌ الرجاء إدخال الرسالة الجديدة\nمثال: /setwelcome 🔐 طلب جديد")
        
        elif text == '/getsettings':
            logo = get_setting("custom_logo", CUSTOM_LOGO)
            welcome = get_setting("welcome_message", WELCOME_MESSAGE)
            
            settings_text = f"""
⚙️ *الإعدادات الحالية*

🏷️ *الشعار:* 
{logo}

📝 *رسالة الترحيب:* 
{welcome}

🔑 *كلمة المرور:* {'●' * 8}
"""
            bot.send_message(chat_id=chat_id, text=settings_text, parse_mode=telegram.ParseMode.MARKDOWN)

def run_bot():
    try:
        updater = Updater(BOT_TOKEN, use_context=True)
        dp = updater.dispatcher
        
        dp.add_handler(CallbackQueryHandler(handle_callback))
        dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
        dp.add_handler(CommandHandler("start", handle_message))
        dp.add_handler(CommandHandler("status", handle_message))
        dp.add_handler(CommandHandler("stats", handle_message))
        dp.add_handler(CommandHandler("pending", handle_message))
        dp.add_handler(CommandHandler("approved", handle_message))
        dp.add_handler(CommandHandler("denied", handle_message))
        dp.add_handler(CommandHandler("logs", handle_message))
        dp.add_handler(CommandHandler("clear", handle_message))
        dp.add_handler(CommandHandler("setpass", handle_message))
        dp.add_handler(CommandHandler("setlogo", handle_message))
        dp.add_handler(CommandHandler("setwelcome", handle_message))
        dp.add_handler(CommandHandler("getsettings", handle_message))
        
        updater.start_polling()
        updater.idle()
    except Exception as e:
        print(f"Bot error: {e}")

bot_thread = threading.Thread(target=run_bot)
bot_thread.daemon = True
bot_thread.start()

# ===================== API للتطبيق =====================
@app.route('/request_access', methods=['POST'])
def request_access():
    try:
        data = request.json
        request_id = data.get('request_id')
        app_name = data.get('app_name', 'Tomb')
        username = data.get('username', 'Unknown')
        device_name = data.get('device_name', 'Unknown')
        device_info = data.get('device_info', 'Unknown')
        
        if not request_id:
            return jsonify({"error": "missing request_id"}), 400
        
        ip_address = request.headers.get('X-Forwarded-For', request.remote_addr)
        
        send_approval_request(
            request_id=request_id,
            app_name=app_name,
            username=username,
            device_name=device_name,
            device_info=device_info,
            ip_address=ip_address
        )
        
        return jsonify({"status": "sent", "request_id": request_id})
    
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/check_status/<request_id>', methods=['GET'])
def check_status(request_id):
    try:
        if request_id in pending_requests:
            status = pending_requests[request_id]["status"]
            if status != "pending":
                del pending_requests[request_id]
            return jsonify({"status": status})
        
        c.execute("SELECT status FROM approvals WHERE request_id = ?", (request_id,))
        row = c.fetchone()
        if row:
            return jsonify({"status": row[0]})
        
        return jsonify({"status": "pending"})
    
    except Exception as e:
        return jsonify({"status": "pending", "error": str(e)}), 500

# ✅ تم إصلاح الدالة: الآن تستخدم check_password بدلاً من verify_password
@app.route('/verify_password', methods=['POST'])
def verify_password():
    """التحقق من كلمة المرور"""
    try:
        data = request.json
        password = data.get('password', '')
        
        if check_password(password):  # ← استخدم check_password
            return jsonify({"valid": True})
        return jsonify({"valid": False})
    
    except Exception as e:
        return jsonify({"valid": False, "error": str(e)}), 500

# ✅ تم إصلاح الدالة: الآن تستخدم check_password بدلاً من verify_password
@app.route('/change_password', methods=['POST'])
def change_password():
    """تغيير كلمة المرور"""
    try:
        data = request.json
        old_password = data.get('old_password', '')
        new_password = data.get('new_password', '')
        
        if not check_password(old_password):  # ← استخدم check_password
            return jsonify({"success": False, "error": "كلمة المرور الحالية غير صحيحة"})
        
        if len(new_password) < 4:
            return jsonify({"success": False, "error": "كلمة المرور الجديدة قصيرة جداً"})
        
        if update_password(new_password, "app"):
            return jsonify({"success": True})
        
        return jsonify({"success": False, "error": "فشل في تحديث كلمة المرور"})
    
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

# ✅ تم إصلاح الدالة: الآن تستخدم check_password بدلاً من verify_password
@app.route('/update_settings', methods=['POST'])
def update_settings():
    """تحديث إعدادات البوت من التطبيق"""
    try:
        data = request.json
        password = data.get('password', '')
        
        if not check_password(password):  # ← استخدم check_password
            return jsonify({"success": False, "error": "كلمة المرور غير صحيحة"})
        
        if 'logo' in data:
            set_setting("custom_logo", data['logo'])
        if 'welcome_message' in data:
            set_setting("welcome_message", data['welcome_message'])
        
        return jsonify({"success": True})
    
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/get_settings', methods=['GET'])
def get_settings():
    try:
        return jsonify({
            "logo": get_setting("custom_logo", CUSTOM_LOGO),
            "welcome_message": get_setting("welcome_message", WELCOME_MESSAGE)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/get_stats', methods=['GET'])
def get_stats():
    try:
        stats = get_access_stats()
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "ok",
        "bot": "running",
        "version": "3.0",
        "timestamp": int(time.time())
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
