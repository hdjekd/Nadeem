import os
import json
import sqlite3
import time
import hashlib
import threading
import re
from datetime import datetime, timedelta
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

# جدول الطلبات
c.execute('''CREATE TABLE IF NOT EXISTS approvals
             (request_id TEXT PRIMARY KEY, 
              status TEXT, 
              timestamp INTEGER,
              username TEXT,
              device_name TEXT,
              device_info TEXT,
              ip_address TEXT)''')

# جدول الإعدادات
c.execute('''CREATE TABLE IF NOT EXISTS settings
             (key TEXT PRIMARY KEY, 
              value TEXT)''')

# جدول كلمات المرور
c.execute('''CREATE TABLE IF NOT EXISTS passwords
             (id INTEGER PRIMARY KEY AUTOINCREMENT,
              password_hash TEXT,
              updated_at INTEGER,
              updated_by TEXT)''')

# جدول سجل الدخول
c.execute('''CREATE TABLE IF NOT EXISTS access_logs
             (id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT,
              device_name TEXT,
              ip_address TEXT,
              status TEXT,
              timestamp INTEGER)''')

# جدول الأجهزة المحظورة
c.execute('''CREATE TABLE IF NOT EXISTS banned_devices
             (device_name TEXT PRIMARY KEY,
              username TEXT,
              banned_at INTEGER,
              banned_until INTEGER,
              ban_type TEXT,
              ban_duration TEXT,
              reason TEXT)''')

# جدول الأجهزة النشطة
c.execute('''CREATE TABLE IF NOT EXISTS active_devices
             (device_name TEXT PRIMARY KEY,
              username TEXT,
              device_info TEXT,
              last_active INTEGER,
              first_active INTEGER)''')

conn.commit()

# ===================== دوال مساعدة =====================
def escape_markdown(text):
    """هروب الأحرف الخاصة في Markdown"""
    if not text:
        return ""
    escape_chars = r'_*[]()~`>#+-=|{}.!'
    return ''.join(f'\\{char}' if char in escape_chars else char for char in str(text))

def validate_device_name(device_name):
    """التحقق من صحة اسم الجهاز"""
    if not device_name or len(device_name) > 255:
        return False
    return True

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

# ===================== دوال إدارة الأجهزة =====================
def add_active_device(device_name, username, device_info):
    """إضافة جهاز إلى قائمة الأجهزة النشطة"""
    if not validate_device_name(device_name):
        return False
    now = int(time.time())
    c.execute("""INSERT OR REPLACE INTO active_devices 
                 (device_name, username, device_info, last_active, first_active) 
                 VALUES (?, ?, ?, ?, COALESCE((SELECT first_active FROM active_devices WHERE device_name = ?), ?))""",
              (device_name, username, device_info, now, device_name, now))
    conn.commit()
    return True

def remove_active_device(device_name):
    """إزالة جهاز من قائمة الأجهزة النشطة"""
    c.execute("DELETE FROM active_devices WHERE device_name = ?", (device_name,))
    conn.commit()

def get_active_devices():
    """الحصول على قائمة الأجهزة النشطة"""
    c.execute("SELECT device_name, username, device_info, last_active, first_active FROM active_devices ORDER BY last_active DESC")
    return c.fetchall()

# ===================== دوال الحظر المتطورة =====================
def calculate_ban_until(unit, value):
    """
    حساب وقت انتهاء الحظر
    unit: 'minutes', 'hours', 'days'
    value: العدد
    """
    now = int(time.time())
    if unit == 'minutes':
        return now + (value * 60)
    elif unit == 'hours':
        return now + (value * 3600)
    elif unit == 'days':
        return now + (value * 86400)
    return 0

def format_duration(seconds):
    """تنسيق المدة المتبقية بشكل مقروء"""
    if seconds <= 0:
        return "انتهى"
    
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    
    if days > 0:
        return f"{days} يوم"
    elif hours > 0:
        return f"{hours} ساعة"
    else:
        return f"{minutes} دقيقة"

def ban_device_advanced(device_name, username, unit, value, reason="محظور من قبل المطور"):
    """
    حظر جهاز مع تحديد الوحدة (دقائق/ساعات/أيام)
    unit: 'minutes', 'hours', 'days'
    value: العدد
    """
    if not validate_device_name(device_name):
        return False
    
    now = int(time.time())
    banned_until = calculate_ban_until(unit, value)
    ban_duration = f"{value} {unit}"
    
    c.execute("""INSERT OR REPLACE INTO banned_devices 
                 (device_name, username, banned_at, banned_until, ban_type, ban_duration, reason) 
                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
              (device_name, username, now, banned_until, "temporary", ban_duration, reason))
    conn.commit()
    
    # إزالة الجهاز من قائمة الأجهزة النشطة
    remove_active_device(device_name)
    return True

def ban_device_permanent(device_name, username, reason="محظور بشكل دائم"):
    """حظر جهاز بشكل دائم"""
    if not validate_device_name(device_name):
        return False
    
    c.execute("""INSERT OR REPLACE INTO banned_devices 
                 (device_name, username, banned_at, banned_until, ban_type, ban_duration, reason) 
                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
              (device_name, username, int(time.time()), 0, "permanent", "دائم", reason))
    conn.commit()
    
    remove_active_device(device_name)
    return True

def unban_device(device_name):
    """رفع الحظر عن جهاز"""
    c.execute("DELETE FROM banned_devices WHERE device_name = ?", (device_name,))
    conn.commit()

def is_device_banned(device_name):
    """التحقق من حظر الجهاز"""
    c.execute("SELECT banned_until, ban_type FROM banned_devices WHERE device_name = ?", (device_name,))
    row = c.fetchone()
    if row:
        banned_until, ban_type = row
        if ban_type == "temporary" and banned_until < int(time.time()):
            # انتهت مدة الحظر المؤقت
            unban_device(device_name)
            return False
        return True
    return False

def get_banned_devices():
    """الحصول على قائمة الأجهزة المحظورة"""
    c.execute("SELECT device_name, username, banned_at, banned_until, ban_type, ban_duration, reason FROM banned_devices ORDER BY banned_at DESC")
    return c.fetchall()

def get_device_ban_info(device_name):
    """الحصول على معلومات حظر جهاز"""
    c.execute("SELECT banned_until, ban_type, ban_duration, reason FROM banned_devices WHERE device_name = ?", (device_name,))
    row = c.fetchone()
    if row:
        banned_until, ban_type, ban_duration, reason = row
        now = int(time.time())
        remaining_seconds = 0
        if ban_type == "temporary" and banned_until > now:
            remaining_seconds = banned_until - now
        return {
            "is_banned": True,
            "ban_type": ban_type,
            "ban_duration": ban_duration,
            "reason": reason,
            "remaining_seconds": remaining_seconds,
            "remaining_text": format_duration(remaining_seconds),
            "expires_at": banned_until
        }
    return {"is_banned": False}

def get_access_stats():
    total = c.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
    pending = c.execute("SELECT COUNT(*) FROM approvals WHERE status='pending'").fetchone()[0]
    approved = c.execute("SELECT COUNT(*) FROM approvals WHERE status='approved'").fetchone()[0]
    denied = c.execute("SELECT COUNT(*) FROM approvals WHERE status='denied'").fetchone()[0]
    banned = c.execute("SELECT COUNT(*) FROM banned_devices").fetchone()[0]
    active = c.execute("SELECT COUNT(*) FROM active_devices").fetchone()[0]
    
    recent = c.execute("""SELECT username, device_name, status, timestamp 
                          FROM approvals ORDER BY timestamp DESC LIMIT 10""").fetchall()
    
    return {
        "total": total,
        "pending": pending,
        "approved": approved,
        "denied": denied,
        "banned": banned,
        "active": active,
        "recent": recent
    }

# ===================== إعدادات البوت =====================
bot = telegram.Bot(token=BOT_TOKEN)
pending_requests = {}

CUSTOM_LOGO = get_setting("custom_logo", "𓆩♛✦𓆪 TOMB OF MAKROTEC 𓆩♛✦𓆪")
WELCOME_MESSAGE = get_setting("welcome_message", "✨ مرحباً بك في نظام حماية تطبيق Tomb ✨")

# ===================== دوال البوت =====================
def send_main_menu(chat_id):
    """إرسال القائمة الرئيسية بالأزرار"""
    logo = get_setting("custom_logo", CUSTOM_LOGO)
    welcome = get_setting("welcome_message", WELCOME_MESSAGE)
    
    keyboard = [
        [
            InlineKeyboardButton("📋 الطلبات المعلقة", callback_data="menu_pending"),
            InlineKeyboardButton("📊 الإحصائيات", callback_data="menu_stats")
        ],
        [
            InlineKeyboardButton("✅ الطلبات المقبولة", callback_data="menu_approved"),
            InlineKeyboardButton("❌ الطلبات المرفوضة", callback_data="menu_denied")
        ],
        [
            InlineKeyboardButton("📜 سجل الطلبات", callback_data="menu_logs"),
            InlineKeyboardButton("⚙️ الإعدادات", callback_data="menu_settings")
        ],
        [
            InlineKeyboardButton("📱 الأجهزة النشطة", callback_data="menu_active_devices"),
            InlineKeyboardButton("🚫 الأجهزة المحظورة", callback_data="menu_banned_devices")
        ],
        [
            InlineKeyboardButton("🔒 حظر جهاز", callback_data="menu_ban_device"),
            InlineKeyboardButton("🔓 رفع حظر", callback_data="menu_unban_device")
        ],
        [
            InlineKeyboardButton("🗑️ مسح الطلبات", callback_data="menu_clear_requests")
        ]
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    try:
        bot.send_message(
            chat_id=chat_id,
            text=f"{logo}\n\n{welcome}\n\n📌 **لوحة التحكم الرئيسية**\n\nاختر أحد الخيارات أدناه:",
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    except:
        bot.send_message(
            chat_id=chat_id,
            text=f"{logo}\n\n{welcome}\n\n📌 لوحة التحكم الرئيسية\n\nاختر أحد الخيارات أدناه:",
            reply_markup=reply_markup
        )

def send_approval_request(request_id, app_name="Tomb", username="Unknown", 
                          device_name="Unknown", device_info="", ip_address="Unknown"):
    
    custom_logo = get_setting("custom_logo", CUSTOM_LOGO)
    welcome_msg = get_setting("welcome_message", WELCOME_MESSAGE)
    
    # إضافة الجهاز إلى قائمة الأجهزة النشطة
    add_active_device(device_name, username, device_info)
    
    message_text = f"""
{custom_logo}

🔐 *{welcome_msg}*

👤 *المستخدم:* `{escape_markdown(username)}`
📱 *الجهاز:* `{escape_markdown(device_name)}`
ℹ️ *المعلومات:* `{escape_markdown(device_info)}`
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
            InlineKeyboardButton("📊 معلومات الجهاز", callback_data=f"info_{request_id}"),
            InlineKeyboardButton("🔒 حظر هذا الجهاز", callback_data=f"ban_this_{request_id}")
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
    chat_id = query.message.chat_id
    
    # ========== القائمة الرئيسية ==========
    if data == "menu_pending":
        pending_reqs = c.execute(
            "SELECT request_id, username, device_name, timestamp FROM approvals WHERE status='pending' ORDER BY timestamp DESC"
        ).fetchall()
        
        if pending_reqs:
            text_msg = "⏳ *الطلبات المعلقة:*\n\n"
            for req in pending_reqs:
                time_str = datetime.fromtimestamp(req[3]).strftime('%H:%M:%S')
                text_msg += f"🆔 `{req[0][:8]}` - {escape_markdown(req[1])} - {escape_markdown(req[2])} - {time_str}\n"
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text=text_msg,
                parse_mode=telegram.ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text="✅ لا توجد طلبات معلقة",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    
    elif data == "menu_stats":
        stats = get_access_stats()
        text_msg = f"""
📊 *إحصائيات النظام*

📝 *إجمالي الطلبات:* {stats['total']}
⏳ *قيد الانتظار:* {stats['pending']}
✅ *تمت الموافقة:* {stats['approved']}
❌ *تم الرفض:* {stats['denied']}
🚫 *الأجهزة المحظورة:* {stats['banned']}
📱 *الأجهزة النشطة:* {stats['active']}

🔄 *آخر تحديث:* {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
        query.edit_message_text(
            text=text_msg,
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data == "menu_approved":
        approved_reqs = c.execute(
            "SELECT username, device_name, timestamp FROM approvals WHERE status='approved' ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
        
        if approved_reqs:
            text_msg = "✅ *الطلبات المقبولة:*\n\n"
            for req in approved_reqs:
                time_str = datetime.fromtimestamp(req[2]).strftime('%Y-%m-%d %H:%M')
                text_msg += f"👤 {escape_markdown(req[0])} - {escape_markdown(req[1])} - {time_str}\n"
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text=text_msg,
                parse_mode=telegram.ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text="📭 لا توجد طلبات مقبولة",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    
    elif data == "menu_denied":
        denied_reqs = c.execute(
            "SELECT username, device_name, timestamp FROM approvals WHERE status='denied' ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
        
        if denied_reqs:
            text_msg = "❌ *الطلبات المرفوضة:*\n\n"
            for req in denied_reqs:
                time_str = datetime.fromtimestamp(req[2]).strftime('%Y-%m-%d %H:%M')
                text_msg += f"👤 {escape_markdown(req[0])} - {escape_markdown(req[1])} - {time_str}\n"
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text=text_msg,
                parse_mode=telegram.ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text="📭 لا توجد طلبات مرفوضة",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    
    elif data == "menu_logs":
        logs = c.execute(
            "SELECT username, device_name, status, timestamp FROM access_logs ORDER BY timestamp DESC LIMIT 20"
        ).fetchall()
        
        if logs:
            log_text = "📋 *سجل الدخول الأخير:*\n\n"
            for log in logs:
                time_str = datetime.fromtimestamp(log[3]).strftime('%Y-%m-%d %H:%M')
                emoji = "✅" if log[2] == "approved" else "❌"
                log_text += f"{emoji} {escape_markdown(log[0])} - {escape_markdown(log[1])} - {time_str}\n"
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text=log_text,
                parse_mode=telegram.ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text="📭 لا يوجد سجل",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    
    elif data == "menu_active_devices":
        devices = get_active_devices()
        
        if devices:
            text_msg = "📱 *الأجهزة النشطة:*\n\n"
            for dev in devices:
                device_name, username, device_info, last_active, first_active = dev
                last_active_str = datetime.fromtimestamp(last_active).strftime('%Y-%m-%d %H:%M')
                text_msg += f"📱 **{escape_markdown(device_name)}**\n   👤 {escape_markdown(username)}\n   🕐 آخر ظهور: {last_active_str}\n\n"
            keyboard = [
                [InlineKeyboardButton("🔒 حظر جهاز", callback_data="menu_ban_device")],
                [InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]
            ]
            query.edit_message_text(
                text=text_msg,
                parse_mode=telegram.ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text="📭 لا توجد أجهزة نشطة",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    
    elif data == "menu_banned_devices":
        devices = get_banned_devices()
        
        if devices:
            text_msg = "🚫 *الأجهزة المحظورة:*\n\n"
            for dev in devices:
                device_name, username, banned_at, banned_until, ban_type, ban_duration, reason = dev
                banned_at_str = datetime.fromtimestamp(banned_at).strftime('%Y-%m-%d %H:%M')
                if ban_type == "temporary":
                    remaining_seconds = max(0, banned_until - int(time.time()))
                    expiry = format_duration(remaining_seconds)
                else:
                    expiry = "🔒 دائم"
                text_msg += f"📱 **{escape_markdown(device_name)}**\n   👤 {escape_markdown(username)}\n   🗓️ حظر في: {banned_at_str}\n   ⏱️ المدة: {ban_duration}\n   ⏰ متبقي: {expiry}\n   📝 السبب: {reason}\n\n"
            keyboard = [
                [InlineKeyboardButton("🔓 رفع حظر جهاز", callback_data="menu_unban_device")],
                [InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]
            ]
            query.edit_message_text(
                text=text_msg,
                parse_mode=telegram.ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            query.edit_message_text(
                text="✅ لا توجد أجهزة محظورة",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    
    # ========== قائمة الحظر المتطورة ==========
    elif data == "menu_ban_device":
        keyboard = [
            [InlineKeyboardButton("🔒 حظر دائم", callback_data="ban_permanent")],
            [InlineKeyboardButton("⏱️ حظر بالدقائق", callback_data="ban_unit_minutes")],
            [InlineKeyboardButton("⏰ حظر بالساعات", callback_data="ban_unit_hours")],
            [InlineKeyboardButton("📅 حظر بالأيام", callback_data="ban_unit_days")],
            [InlineKeyboardButton("🔙 العودة", callback_data="back_to_main")]
        ]
        query.edit_message_text(
            text="🔒 **حظر جهاز**\n\nاختر نوع الحظر:",
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data == "ban_unit_minutes":
        context.user_data['ban_unit'] = 'minutes'
        context.user_data['ban_unit_text'] = 'دقائق'
        query.edit_message_text(
            "⏱️ **حظر بالدقائق**\n\n📝 أرسل عدد الدقائق:\n(مثال: 5, 30, 60, 120)",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_ban_value'] = True
    
    elif data == "ban_unit_hours":
        context.user_data['ban_unit'] = 'hours'
        context.user_data['ban_unit_text'] = 'ساعات'
        query.edit_message_text(
            "⏰ **حظر بالساعات**\n\n📝 أرسل عدد الساعات:\n(مثال: 1, 6, 12, 24, 48)",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_ban_value'] = True
    
    elif data == "ban_unit_days":
        context.user_data['ban_unit'] = 'days'
        context.user_data['ban_unit_text'] = 'أيام'
        query.edit_message_text(
            "📅 **حظر بالأيام**\n\n📝 أرسل عدد الأيام:\n(مثال: 1, 3, 7, 15, 30)",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_ban_value'] = True
    
    elif data == "ban_permanent":
        context.user_data['ban_type'] = "permanent"
        query.edit_message_text(
            "🔒 **حظر دائم**\n\n📝 أرسل اسم الجهاز الذي تريد حظره:",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_ban_device'] = True
    
    elif data == "menu_unban_device":
        query.edit_message_text(
            "🔓 **رفع الحظر عن جهاز**\n\n📝 أرسل اسم الجهاز الذي تريد رفع الحظر عنه:",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_unban_device'] = True
    
    elif data == "menu_clear_requests":
        c.execute("DELETE FROM approvals WHERE status != 'pending'")
        conn.commit()
        keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
        query.edit_message_text(
            text="🗑️ تم مسح جميع الطلبات المنتهية بنجاح",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data == "menu_settings":
        logo = get_setting("custom_logo", CUSTOM_LOGO)
        welcome = get_setting("welcome_message", WELCOME_MESSAGE)
        
        text_msg = f"""
⚙️ *الإعدادات الحالية*

🏷️ *الشعار:* 
{logo[:50]}...

📝 *رسالة الترحيب:* 
{welcome[:50]}...

🔑 *كلمة المرور:* {'●' * 8}
"""
        keyboard = [
            [InlineKeyboardButton("📝 تغيير الشعار", callback_data="change_logo")],
            [InlineKeyboardButton("💬 تغيير رسالة الترحيب", callback_data="change_welcome")],
            [InlineKeyboardButton("🔑 تغيير كلمة المرور", callback_data="change_password")],
            [InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]
        ]
        query.edit_message_text(
            text=text_msg,
            parse_mode=telegram.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    
    elif data == "change_logo":
        query.edit_message_text(
            "📝 **تغيير الشعار**\n\nأرسل الشعار الجديد:",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_logo'] = True
    
    elif data == "change_welcome":
        query.edit_message_text(
            "💬 **تغيير رسالة الترحيب**\n\nأرسل الرسالة الجديدة:",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_welcome'] = True
    
    elif data == "change_password":
        query.edit_message_text(
            "🔑 **تغيير كلمة المرور**\n\nأرسل كلمة المرور الجديدة (4 أحرف على الأقل):",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_new_password'] = True
    
    elif data == "back_to_main":
        try:
            query.delete_message()
        except:
            pass
        send_main_menu(chat_id)
    
    # ========== معالجة الطلبات (تم إصلاحها) ==========
    elif data.startswith("approve_"):
        request_id = data[8:]
        status = "approved"
        
        if request_id in pending_requests:
            pending_requests[request_id]["status"] = status
        
        c.execute("UPDATE approvals SET status = ? WHERE request_id = ?", (status, request_id))
        conn.commit()
        
        c.execute("SELECT username, device_name, ip_address FROM approvals WHERE request_id = ?", (request_id,))
        row = c.fetchone()
        if row:
            log_access(row[0], row[1], row[2], "approved")
        
        # ✅ إرسال رسالة منفصلة للتأكيد (مثل الكود القديم)
        bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"✅ تمت الموافقة على طلب `{request_id[:8]}`",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        
        # تعديل الرسالة الأصلية لإظهار النتيجة
        try:
            query.edit_message_text(
                text=f"✅ **تمت الموافقة بنجاح**\n\nيمكن للمستخدم الآن الدخول إلى التطبيق.\n\n{get_setting('custom_logo', CUSTOM_LOGO)}",
                parse_mode=telegram.ParseMode.MARKDOWN
            )
        except:
            pass
    
    elif data.startswith("deny_"):
        request_id = data[5:]
        status = "denied"
        
        if request_id in pending_requests:
            pending_requests[request_id]["status"] = status
        
        c.execute("UPDATE approvals SET status = ? WHERE request_id = ?", (status, request_id))
        conn.commit()
        
        c.execute("SELECT username, device_name, ip_address FROM approvals WHERE request_id = ?", (request_id,))
        row = c.fetchone()
        if row:
            log_access(row[0], row[1], row[2], "denied")
        
        # ✅ إرسال رسالة منفصلة للتأكيد (مثل الكود القديم)
        bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"❌ تم رفض طلب `{request_id[:8]}`",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        
        # تعديل الرسالة الأصلية لإظهار النتيجة
        try:
            query.edit_message_text(
                text=f"❌ **تم رفض الطلب**\n\nلم يتم السماح للمستخدم بالدخول.\n\n{get_setting('custom_logo', CUSTOM_LOGO)}",
                parse_mode=telegram.ParseMode.MARKDOWN
            )
        except:
            pass
    
    elif data.startswith("ban_this_"):
        request_id = data[9:]
        c.execute("SELECT device_name, username FROM approvals WHERE request_id = ?", (request_id,))
        row = c.fetchone()
        if row:
            device_name, username = row
            context.user_data['ban_device_name'] = device_name
            context.user_data['ban_username'] = username
            
            keyboard = [
                [InlineKeyboardButton("🔒 حظر دائم", callback_data="ban_this_permanent")],
                [InlineKeyboardButton("⏱️ حظر بالدقائق", callback_data="ban_this_minutes")],
                [InlineKeyboardButton("⏰ حظر بالساعات", callback_data="ban_this_hours")],
                [InlineKeyboardButton("📅 حظر بالأيام", callback_data="ban_this_days")],
                [InlineKeyboardButton("🔙 إلغاء", callback_data="back_to_main")]
            ]
            query.edit_message_text(
                text=f"🔒 **حظر جهاز:** `{escape_markdown(device_name)}`\n\nاختر نوع الحظر:",
                parse_mode=telegram.ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
    
    elif data == "ban_this_minutes":
        context.user_data['ban_unit'] = 'minutes'
        context.user_data['ban_unit_text'] = 'دقائق'
        context.user_data['ban_from_request'] = True
        query.edit_message_text(
            "⏱️ **حظر بالدقائق**\n\n📝 أرسل عدد الدقائق:\n(مثال: 5, 30, 60, 120)",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_ban_value'] = True
    
    elif data == "ban_this_hours":
        context.user_data['ban_unit'] = 'hours'
        context.user_data['ban_unit_text'] = 'ساعات'
        context.user_data['ban_from_request'] = True
        query.edit_message_text(
            "⏰ **حظر بالساعات**\n\n📝 أرسل عدد الساعات:\n(مثال: 1, 6, 12, 24, 48)",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_ban_value'] = True
    
    elif data == "ban_this_days":
        context.user_data['ban_unit'] = 'days'
        context.user_data['ban_unit_text'] = 'أيام'
        context.user_data['ban_from_request'] = True
        query.edit_message_text(
            "📅 **حظر بالأيام**\n\n📝 أرسل عدد الأيام:\n(مثال: 1, 3, 7, 15, 30)",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        context.user_data['waiting_for_ban_value'] = True
    
    elif data == "ban_this_permanent":
        device_name = context.user_data.get('ban_device_name')
        username = context.user_data.get('ban_username')
        
        if device_name:
            ban_device_permanent(device_name, username, "محظور بشكل دائم من قبل المطور")
            
            keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
            try:
                query.edit_message_text(
                    text=f"✅ **تم حظر الجهاز بشكل دائم!**\n\n📱 {escape_markdown(device_name)}",
                    parse_mode=telegram.ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except:
                query.edit_message_text(
                    text=f"✅ تم حظر الجهاز بشكل دائم!\n\n📱 {device_name}",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
    
    elif data.startswith("info_"):
        request_id = data[5:]
        c.execute("""SELECT username, device_name, device_info, ip_address, timestamp 
                     FROM approvals WHERE request_id = ?""", (request_id,))
        row = c.fetchone()
        
        if row:
            info_text = f"""
📱 *معلومات الجهاز*

👤 *المستخدم:* {escape_markdown(row[0])}
📱 *اسم الجهاز:* {escape_markdown(row[1])}
ℹ️ *تفاصيل:* {escape_markdown(row[2])}
🌐 *IP:* {row[3]}
🕐 *الوقت:* {datetime.fromtimestamp(row[4]).strftime('%Y-%m-%d %H:%M:%S')}
"""
            keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="back_to_main")]]
            query.edit_message_text(
                text=info_text,
                parse_mode=telegram.ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            query.edit_message_text("❌ لم يتم العثور على الطلب")

def handle_message(update, context):
    message = update.message
    chat_id = message.chat_id
    text = message.text.strip()
    
    if str(chat_id) != str(ADMIN_CHAT_ID):
        bot.send_message(chat_id=chat_id, text="⚠️ أنت غير مصرح لك باستخدام هذا البوت")
        return
    
    # معالجة انتظار قيمة الحظر (دقائق/ساعات/أيام)
    if context.user_data.get('waiting_for_ban_value'):
        try:
            value = int(text)
            if value <= 0:
                bot.send_message(chat_id=chat_id, text="❌ الرجاء إدخال رقم أكبر من 0")
                return
            
            ban_unit = context.user_data.get('ban_unit')
            ban_unit_text = context.user_data.get('ban_unit_text')
            ban_from_request = context.user_data.get('ban_from_request', False)
            device_name = context.user_data.get('ban_device_name') if ban_from_request else None
            
            if ban_from_request and device_name:
                # حظر من داخل الطلب
                username = context.user_data.get('ban_username', 'Unknown')
                ban_device_advanced(device_name, username, ban_unit, value, f"محظور لمدة {value} {ban_unit_text}")
                
                duration_text = ""
                if ban_unit == 'minutes':
                    duration_text = f"{value} دقيقة"
                elif ban_unit == 'hours':
                    duration_text = f"{value} ساعة"
                else:
                    duration_text = f"{value} يوم"
                
                keyboard = [[InlineKeyboardButton("🔙 العودة للقائمة", callback_data="back_to_main")]]
                try:
                    bot.send_message(
                        chat_id=chat_id,
                        text=f"✅ **تم حظر الجهاز بنجاح!**\n\n📱 {escape_markdown(device_name)}\n⏰ المدة: {duration_text}",
                        parse_mode=telegram.ParseMode.MARKDOWN,
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                except:
                    bot.send_message(
                        chat_id=chat_id,
                        text=f"✅ تم حظر الجهاز بنجاح!\n\n📱 {device_name}\n⏰ المدة: {duration_text}",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                
                # تنظيف البيانات
                context.user_data.pop('waiting_for_ban_value', None)
                context.user_data.pop('ban_unit', None)
                context.user_data.pop('ban_unit_text', None)
                context.user_data.pop('ban_from_request', None)
                context.user_data.pop('ban_device_name', None)
                context.user_data.pop('ban_username', None)
                
            else:
                # حظر من القائمة الرئيسية - نطلب اسم الجهاز بعد تحديد المدة
                context.user_data['ban_value'] = value
                context.user_data.pop('waiting_for_ban_value')
                context.user_data['waiting_for_ban_device'] = True
                bot.send_message(
                    chat_id=chat_id,
                    text=f"⏰ المدة: {value} {ban_unit_text}\n\n📝 أرسل اسم الجهاز الذي تريد حظره:",
                    parse_mode=telegram.ParseMode.MARKDOWN
                )
            return
            
        except ValueError:
            bot.send_message(chat_id=chat_id, text="❌ الرجاء إدخال رقم صحيح")
            return
    
    # معالجة انتظار اسم الجهاز للحظر
    if context.user_data.get('waiting_for_ban_device'):
        device_name = text.strip()
        
        if not validate_device_name(device_name):
            bot.send_message(chat_id=chat_id, text="❌ اسم الجهاز غير صالح (طويل جداً أو فارغ)")
            return
        
        ban_type = context.user_data.get('ban_type')
        ban_unit = context.user_data.get('ban_unit')
        ban_value = context.user_data.get('ban_value')
        
        # البحث عن اسم المستخدم من قاعدة البيانات
        c.execute("SELECT username FROM approvals WHERE device_name = ? ORDER BY timestamp DESC LIMIT 1", (device_name,))
        row = c.fetchone()
        username = row[0] if row else "Unknown"
        
        if ban_type == "permanent":
            ban_device_permanent(device_name, username, "محظور بشكل دائم من قبل المطور")
            bot.send_message(
                chat_id=chat_id,
                text=f"✅ تم حظر الجهاز `{escape_markdown(device_name)}` بشكل دائم",
                parse_mode=telegram.ParseMode.MARKDOWN
            )
        elif ban_unit and ban_value:
            unit_text = ""
            if ban_unit == 'minutes':
                unit_text = f"{ban_value} دقيقة"
            elif ban_unit == 'hours':
                unit_text = f"{ban_value} ساعة"
            else:
                unit_text = f"{ban_value} يوم"
            
            ban_device_advanced(device_name, username, ban_unit, ban_value, f"محظور لمدة {unit_text}")
            bot.send_message(
                chat_id=chat_id,
                text=f"✅ تم حظر الجهاز `{escape_markdown(device_name)}` لمدة {unit_text}",
                parse_mode=telegram.ParseMode.MARKDOWN
            )
        
        # تنظيف البيانات
        context.user_data.pop('waiting_for_ban_device', None)
        context.user_data.pop('ban_type', None)
        context.user_data.pop('ban_unit', None)
        context.user_data.pop('ban_value', None)
        context.user_data.pop('ban_unit_text', None)
        
        send_main_menu(chat_id)
        return
    
    # معالجة رفع الحظر
    if context.user_data.get('waiting_for_unban_device'):
        device_name = text.strip()
        unban_device(device_name)
        context.user_data.pop('waiting_for_unban_device')
        bot.send_message(
            chat_id=chat_id,
            text=f"✅ تم رفع الحظر عن الجهاز `{escape_markdown(device_name)}`",
            parse_mode=telegram.ParseMode.MARKDOWN
        )
        send_main_menu(chat_id)
        return
    
    # معالجة تغيير الشعار
    if context.user_data.get('waiting_for_logo'):
        new_logo = text.strip()
        set_setting("custom_logo", new_logo)
        context.user_data.pop('waiting_for_logo')
        bot.send_message(chat_id=chat_id, text=f"✅ تم تغيير الشعار بنجاح!")
        send_main_menu(chat_id)
        return
    
    # معالجة تغيير رسالة الترحيب
    if context.user_data.get('waiting_for_welcome'):
        new_welcome = text.strip()
        set_setting("welcome_message", new_welcome)
        context.user_data.pop('waiting_for_welcome')
        bot.send_message(chat_id=chat_id, text=f"✅ تم تغيير رسالة الترحيب بنجاح!")
        send_main_menu(chat_id)
        return
    
    # معالجة تغيير كلمة المرور
    if context.user_data.get('waiting_for_new_password'):
        new_password = text.strip()
        if len(new_password) >= 4:
            if update_password(new_password, "bot"):
                bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ تم تغيير كلمة مرور التطبيق بنجاح!\n\n🔑 كلمة المرور الجديدة: `{escape_markdown(new_password)}`",
                    parse_mode=telegram.ParseMode.MARKDOWN
                )
            else:
                bot.send_message(chat_id=chat_id, text="❌ فشل في تغيير كلمة المرور")
        else:
            bot.send_message(chat_id=chat_id, text="❌ كلمة المرور يجب أن تكون 4 أحرف على الأقل")
        context.user_data.pop('waiting_for_new_password')
        send_main_menu(chat_id)
        return
    
    # إذا كان الأمر /start
    if text == '/start':
        send_main_menu(chat_id)
    else:
        send_main_menu(chat_id)

def run_bot():
    try:
        updater = Updater(BOT_TOKEN, use_context=True)
        dp = updater.dispatcher
        
        dp.add_handler(CallbackQueryHandler(handle_callback))
        dp.add_handler(MessageHandler(Filters.text & ~Filters.command, handle_message))
        dp.add_handler(CommandHandler("start", handle_message))
        
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
        
        # التحقق من حظر الجهاز
        if is_device_banned(device_name):
            ban_info = get_device_ban_info(device_name)
            return jsonify({
                "status": "banned",
                "message": "جهازك محظور من قبل المطور",
                "ban_type": ban_info['ban_type'],
                "ban_duration": ban_info['ban_duration'],
                "reason": ban_info['reason'],
                "remaining_text": ban_info['remaining_text']
            })
        
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

@app.route('/verify_password', methods=['POST'])
def verify_password():
    """التحقق من كلمة المرور"""
    try:
        data = request.json
        password = data.get('password', '')
        
        if check_password(password):
            return jsonify({"valid": True})
        return jsonify({"valid": False})
    
    except Exception as e:
        return jsonify({"valid": False, "error": str(e)}), 500

@app.route('/check_device_status', methods=['POST'])
def check_device_status():
    """التحقق من حالة الجهاز (محظور أم لا)"""
    try:
        data = request.json
        device_name = data.get('device_name', '')
        
        if is_device_banned(device_name):
            ban_info = get_device_ban_info(device_name)
            return jsonify({
                "banned": True,
                "ban_type": ban_info['ban_type'],
                "ban_duration": ban_info['ban_duration'],
                "reason": ban_info['reason'],
                "remaining_text": ban_info['remaining_text']
            })
        return jsonify({"banned": False})
    
    except Exception as e:
        return jsonify({"banned": False, "error": str(e)}), 500

@app.route('/change_password', methods=['POST'])
def change_password():
    """تغيير كلمة المرور"""
    try:
        data = request.json
        old_password = data.get('old_password', '')
        new_password = data.get('new_password', '')
        
        if not check_password(old_password):
            return jsonify({"success": False, "error": "كلمة المرور الحالية غير صحيحة"})
        
        if len(new_password) < 4:
            return jsonify({"success": False, "error": "كلمة المرور الجديدة قصيرة جداً"})
        
        if update_password(new_password, "app"):
            return jsonify({"success": True})
        
        return jsonify({"success": False, "error": "فشل في تحديث كلمة المرور"})
    
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/update_settings', methods=['POST'])
def update_settings():
    """تحديث إعدادات البوت من التطبيق"""
    try:
        data = request.json
        password = data.get('password', '')
        
        if not check_password(password):
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
        "version": "5.1",
        "timestamp": int(time.time())
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
