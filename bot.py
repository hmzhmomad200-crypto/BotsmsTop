#!/usr/bin/env python3
# Telegram OTP Bot - with Change Number & Change Service buttons

import os
import sqlite3
import json
import logging
import asyncio
import re
import random
import string
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes, MessageHandler, filters
import requests
from bs4 import BeautifulSoup

# ========== CONFIGURATION ==========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
IVASMS_COOKIES = os.getenv("IVASMS_COOKIES", "")
NUMBERS_FILE = "numbers.txt"
COUPONS_FILE = "coupons.json"
DB_FILE = "data/bot.db"
NUMBER_EXPIRE_MINUTES = 10
CAPTCHA_EXPIRE_MINUTES = 5

# ========== DATABASE FUNCTIONS (unchanged, included in full version) ==========
# ... (جميع دوال قاعدة البيانات كما هي سابقًا، لضمان الطول سأضعها مختصرة هنا، ولكن في الملف النهائي ستكون كاملة)

def init_db():
    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, language TEXT DEFAULT 'ar', captcha_code TEXT, captcha_expiry TIMESTAMP, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS active_numbers (user_id INTEGER PRIMARY KEY, number TEXT, service TEXT, country TEXT, expires_at TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS used_numbers_temp (number TEXT PRIMARY KEY, expires_at TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS otp_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, number TEXT, service TEXT, country TEXT, otp TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def add_user(user_id, username):
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT OR IGNORE INTO users (user_id, username) VALUES (?, ?)', (user_id, username))
    conn.commit()
    conn.close()

def get_user_lang(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT language FROM users WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    return row['language'] if row else 'ar'

def set_language(user_id, lang):
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE users SET language = ? WHERE user_id = ?', (lang, user_id))
    conn.commit()
    conn.close()

def set_captcha(user_id, code):
    conn = get_db()
    c = conn.cursor()
    expiry = (datetime.now() + timedelta(minutes=CAPTCHA_EXPIRE_MINUTES)).isoformat()
    c.execute('UPDATE users SET captcha_code = ?, captcha_expiry = ? WHERE user_id = ?', (code, expiry, user_id))
    conn.commit()
    conn.close()

def verify_captcha(user_id, code):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT captcha_code, captcha_expiry FROM users WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    if row and row['captcha_code'] == code and datetime.now() < datetime.fromisoformat(row['captcha_expiry']):
        return True
    return False

def is_captcha_solved(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT captcha_code FROM users WHERE user_id = ? AND captcha_code IS NULL', (user_id,))
    row = c.fetchone()
    conn.close()
    return row is not None

def set_captcha_solved(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE users SET captcha_code = NULL, captcha_expiry = NULL WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def set_active_number(user_id, number, service, country, expire_minutes=NUMBER_EXPIRE_MINUTES):
    conn = get_db()
    c = conn.cursor()
    expires_at = (datetime.now() + timedelta(minutes=expire_minutes)).isoformat()
    c.execute('REPLACE INTO active_numbers (user_id, number, service, country, expires_at) VALUES (?, ?, ?, ?, ?)',
              (user_id, number, service, country, expires_at))
    conn.commit()
    conn.close()

def get_active_number(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT number, service, country, expires_at FROM active_numbers WHERE user_id = ?', (user_id,))
    row = c.fetchone()
    conn.close()
    if row and datetime.now() < datetime.fromisoformat(row['expires_at']):
        return row['number'], row['service'], row['country'], datetime.fromisoformat(row['expires_at'])
    else:
        clear_active_number(user_id)
        return None, None, None, None

def clear_active_number(user_id):
    conn = get_db()
    c = conn.cursor()
    c.execute('DELETE FROM active_numbers WHERE user_id = ?', (user_id,))
    conn.commit()
    conn.close()

def add_used_number_temp(number, hours=24):
    conn = get_db()
    c = conn.cursor()
    expires_at = (datetime.now() + timedelta(hours=hours)).isoformat()
    c.execute('REPLACE INTO used_numbers_temp (number, expires_at) VALUES (?, ?)', (number, expires_at))
    conn.commit()
    conn.close()

def is_number_temp_used(number):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT expires_at FROM used_numbers_temp WHERE number = ?', (number,))
    row = c.fetchone()
    conn.close()
    if row and datetime.now() < datetime.fromisoformat(row['expires_at']):
        return True
    return False

def add_otp_log(user_id, number, service, country, otp):
    conn = get_db()
    c = conn.cursor()
    c.execute('INSERT INTO otp_logs (user_id, number, service, country, otp) VALUES (?, ?, ?, ?, ?)',
              (user_id, number, service, country, otp))
    conn.commit()
    conn.close()

def get_user_otp_history(user_id, limit=10):
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT number, service, country, otp, timestamp FROM otp_logs WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?',
              (user_id, limit))
    rows = c.fetchall()
    conn.close()
    return rows

def get_all_active_numbers():
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT user_id, number, service, country FROM active_numbers')
    rows = c.fetchall()
    conn.close()
    return rows

def load_coupons():
    try:
        with open(COUPONS_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_coupons(coupons):
    with open(COUPONS_FILE, 'w') as f:
        json.dump(coupons, f)

def use_coupon(code, user_id):
    coupons = load_coupons()
    if code in coupons and coupons[code] > 0:
        coupons[code] -= 1
        if coupons[code] == 0:
            del coupons[code]
        save_coupons(coupons)
        return True
    return False

def add_coupon(code, uses):
    coupons = load_coupons()
    coupons[code] = uses
    save_coupons(coupons)

def is_admin(user_id):
    return user_id in ADMIN_IDS

def load_numbers():
    try:
        with open(NUMBERS_FILE, 'r') as f:
            lines = [line.strip() for line in f if line.strip()]
            return [line.split(',') for line in lines if len(line.split(',')) == 3]
    except:
        return []

def save_numbers(numbers_list):
    with open(NUMBERS_FILE, 'w') as f:
        for num, serv, country in numbers_list:
            f.write(f"{num},{serv},{country}\n")

def remove_number_from_file(number):
    numbers = load_numbers()
    new_numbers = [n for n in numbers if n[0] != number]
    save_numbers(new_numbers)

async def is_subscribed(user_id, bot):
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except:
        return False

def fetch_otp_for_number(target_phone):
    if not IVASMS_COOKIES:
        return None
    session = requests.Session()
    session.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
    for cookie_pair in IVASMS_COOKIES.split(';'):
        if '=' in cookie_pair:
            name, value = cookie_pair.strip().split('=', 1)
            session.cookies.set(name, value)
    try:
        resp = session.get('https://www.ivasms.com/portal/sms/received', timeout=15)
        if resp.status_code != 200:
            return None
        soup = BeautifulSoup(resp.text, 'html.parser')
        rows = soup.select('table tr')
        for row in rows:
            cells = row.find_all('td')
            if len(cells) >= 3:
                phone = re.sub(r'\s+', '', cells[0].get_text(strip=True))
                msg = cells[1].get_text(strip=True)
                if target_phone in phone or phone in target_phone:
                    otp_match = re.search(r'\b\d{4,6}\b', msg)
                    if otp_match:
                        return otp_match.group(0)
        return None
    except Exception as e:
        logger.error(f"Scraper error: {e}")
        return None

# Translations (shortened for readability)
texts = {
    'ar': {
        'number_assigned': "✅ تم اختيار الرقم!\n\n🌍 {country}\n📱 {service}\n📞 `{number}`\n\n⏳ ينتهي خلال {minutes} دقيقة",
        'choose_country': "🌍 اختر الدولة:",
        'choose_service': "📱 اختر الخدمة:",
        'no_numbers': "⚠️ لا توجد أرقام متاحة",
        'main_menu': "القائمة الرئيسية:",
        'get_number': "📞 احصل على رقم",
        'my_number': "📱 رقمي",
        'release': "🔓 تحرير الرقم",
        'renew': "🔄 تجديد",
        'history': "📜 سجل OTP",
        'services': "📋 الخدمات",
        'language': "🌐 اللغة",
        'coupon': "🎫 كوبون",
        'active_number_info': "📞 رقمك: `{number}`\n📱 {service}\n🌍 {country}\n⏳ متبقي: {time_left}",
        'released': "✅ تم تحرير الرقم",
        'renew_success': "✅ تم تجديد الرقم {minutes} دقيقة",
        'history_empty': "📭 لا يوجد سجل",
        'history_format': "📜 **آخر {limit} أكواد:**\n\n",
        'history_line': "• `{otp}` – {service} – {number} ({country})\n   _{time}_\n",
        'services_list': "📋 **الخدمات المتاحة:**\n",
        'lang_changed': "✅ تم تغيير اللغة",
        'coupon_instruction': "🎫 أرسل كود الكوبون:",
        'coupon_used': "✅ تم تفعيل الكوبون! تم تمديد رقمك {minutes} دقيقة.",
        'coupon_invalid': "❌ كوبون غير صالح",
        'export_sent': "✅ تم تصدير بياناتك",
        'admin_panel': "🔧 لوحة الأدمن",
        'admin_stats': "📊 الإحصائيات",
        'admin_upload': "📂 رفع ملف أرقام",
        'admin_list': "👥 قائمة الأدمن",
        'admin_broadcast': "📢 إرسال جماعي",
        'admin_export': "💾 تصدير كل البيانات",
        'admin_coupon': "🎫 إضافة كوبون",
        'file_upload_instruction': "📂 أرسل ملف txt يحتوي على قائمة الأرقام (رقم واحد في كل سطر).\n\nبعد إرسال الملف، سيُطلب منك إدخال الخدمة والدولة.",
        'ask_service': "✅ تم استلام {count} رقم.\n\nالآن أرسل اسم الخدمة (مثال: WhatsApp, Telegram, Facebook):",
        'ask_country': "✅ الخدمة: {service}\n\nالآن أرسل اسم الدولة (مثال: Venezuela, Ghana, Iraq):",
        'file_added': "✅ تم إضافة {count} رقم بنجاح!\n📱 الخدمة: {service}\n🌍 الدولة: {country}",
        'file_invalid': "❌ الملف لا يحتوي على أرقام صالحة.",
        'broadcast_instruction': "📢 أرسل الرسالة التي تريد بثها:",
        'broadcast_done': "✅ تم الإرسال إلى {count} مستخدم",
        'coupon_added': "✅ تم إضافة الكوبون `{code}` بعدد {uses} استخدامات",
    },
    'en': {
        'number_assigned': "✅ Number assigned!\n\n🌍 {country}\n📱 {service}\n📞 `{number}`\n\n⏳ Expires in {minutes} minutes",
        'choose_country': "🌍 Choose country:",
        'choose_service': "📱 Choose service:",
        'no_numbers': "⚠️ No numbers available",
        'main_menu': "Main Menu:",
        'get_number': "📞 Get Number",
        'my_number': "📱 My Number",
        'release': "🔓 Release Number",
        'renew': "🔄 Renew",
        'history': "📜 OTP History",
        'services': "📋 Services",
        'language': "🌐 Language",
        'coupon': "🎫 Coupon",
        'active_number_info': "📞 Your number: `{number}`\n📱 {service}\n🌍 {country}\n⏳ Time left: {time_left}",
        'released': "✅ Number released",
        'renew_success': "✅ Number renewed for {minutes} minutes",
        'history_empty': "📭 No history",
        'history_format': "📜 **Last {limit} OTPs:**\n\n",
        'history_line': "• `{otp}` – {service} – {number} ({country})\n   _{time}_\n",
        'services_list': "📋 **Available services:**\n",
        'lang_changed': "✅ Language changed",
        'coupon_instruction': "🎫 Send coupon code:",
        'coupon_used': "✅ Coupon redeemed! Number extended by {minutes} minutes.",
        'coupon_invalid': "❌ Invalid coupon",
        'export_sent': "✅ Data exported",
        'admin_panel': "🔧 Admin Panel",
        'admin_stats': "📊 Statistics",
        'admin_upload': "📂 Upload Numbers File",
        'admin_list': "👥 Admin List",
        'admin_broadcast': "📢 Broadcast",
        'admin_export': "💾 Export All Data",
        'admin_coupon': "🎫 Add Coupon",
        'file_upload_instruction': "📂 Send a txt file containing numbers (one per line).\n\nAfter that, you will be asked for service and country.",
        'ask_service': "✅ Received {count} numbers.\n\nNow send the service name (e.g., WhatsApp, Telegram):",
        'ask_country': "✅ Service: {service}\n\nNow send the country name (e.g., Venezuela, Ghana):",
        'file_added': "✅ Added {count} numbers!\n📱 Service: {service}\n🌍 Country: {country}",
        'file_invalid': "❌ File contains no valid numbers.",
        'broadcast_instruction': "📢 Send the message to broadcast:",
        'broadcast_done': "✅ Sent to {count} users",
        'coupon_added': "✅ Coupon `{code}` added with {uses} uses",
    }
}

def get_text(user_id, key, **kwargs):
    lang = get_user_lang(user_id)
    txt = texts.get(lang, texts['ar']).get(key, key)
    return txt.format(**kwargs) if kwargs else txt

# ========== BOT HANDLERS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or user.first_name
    add_user(user_id, username)
    if not is_captcha_solved(user_id):
        code = ''.join(random.choices(string.digits, k=6))
        set_captcha(user_id, code)
        await update.message.reply_text(get_text(user_id, 'start_captcha', code=code), parse_mode='Markdown')
        return
    await show_main_menu(update.message, user_id)

async def show_main_menu(message, user_id):
    keyboard = [
        [InlineKeyboardButton(get_text(user_id, 'get_number'), callback_data="get_number")],
        [InlineKeyboardButton(get_text(user_id, 'my_number'), callback_data="my_number")],
        [InlineKeyboardButton(get_text(user_id, 'release'), callback_data="release")],
        [InlineKeyboardButton(get_text(user_id, 'renew'), callback_data="renew")],
        [InlineKeyboardButton(get_text(user_id, 'history'), callback_data="history")],
        [InlineKeyboardButton(get_text(user_id, 'services'), callback_data="services")],
        [InlineKeyboardButton(get_text(user_id, 'language'), callback_data="language")],
        [InlineKeyboardButton(get_text(user_id, 'coupon'), callback_data="coupon")],
    ]
    if is_admin(user_id):
        keyboard.append([InlineKeyboardButton("🔧 لوحة الأدمن", callback_data="admin_panel")])
    await message.reply_text(get_text(user_id, 'main_menu'), reply_markup=InlineKeyboardMarkup(keyboard))

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "get_number":
        numbers = load_numbers()
        if not numbers:
            await query.edit_message_text(get_text(user_id, 'no_numbers'))
            return
        countries = {c for _, _, c in numbers}
        keyboard = [[InlineKeyboardButton(c, callback_data=f"country_{c}")] for c in countries]
        keyboard.append([InlineKeyboardButton("🔙", callback_data="main_menu")])
        await query.edit_message_text(get_text(user_id, 'choose_country'), reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("country_"):
        country = data.split('_', 1)[1]
        numbers = load_numbers()
        services = {s for _, s, c in numbers if c == country}
        keyboard = [[InlineKeyboardButton(s, callback_data=f"service_{country}_{s}")] for s in services]
        keyboard.append([InlineKeyboardButton("🔙", callback_data="get_number")])
        await query.edit_message_text(get_text(user_id, 'choose_service'), reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("service_"):
        parts = data.split('_', 2)
        country = parts[1]
        service = parts[2]
        numbers = load_numbers()
        used = set()
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT number FROM active_numbers')
        used.update(r[0] for r in c.fetchall())
        c.execute('SELECT number FROM used_numbers_temp')
        used.update(r[0] for r in c.fetchall())
        conn.close()
        selected = None
        for num, s, ctry in numbers:
            if ctry == country and s == service and num not in used:
                selected = num
                break
        if not selected:
            await query.edit_message_text(get_text(user_id, 'no_numbers'))
            return
        set_active_number(user_id, selected, service, country)
        remove_number_from_file(selected)
        add_used_number_temp(selected, hours=24)
        # Display assigned number with change buttons
        await query.edit_message_text(
            get_text(user_id, 'number_assigned', country=country, service=service, number=selected, minutes=NUMBER_EXPIRE_MINUTES),
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 تغيير الرقم", callback_data=f"change_num_{user_id}")],
                [InlineKeyboardButton("🔄 تغيير الخدمة", callback_data=f"change_svc_{country}_{service}_{user_id}")]
            ])
        )

    elif data.startswith("change_num_"):
        # تغيير الرقم: تحرير الرقم الحالي ثم عرض قائمة الدول
        clear_active_number(user_id)
        numbers = load_numbers()
        countries = {c for _, _, c in numbers}
        keyboard = [[InlineKeyboardButton(c, callback_data=f"country_{c}")] for c in countries]
        keyboard.append([InlineKeyboardButton("🔙", callback_data="main_menu")])
        await query.edit_message_text(get_text(user_id, 'choose_country'), reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("change_svc_"):
        # تغيير الخدمة مع الحفاظ على نفس الدولة
        parts = data.split('_')
        # format: change_svc_{country}_{old_service}_{user_id}
        country = parts[2]
        # old_service = parts[3]  (not needed)
        clear_active_number(user_id)
        numbers = load_numbers()
        services = {s for _, s, c in numbers if c == country}
        keyboard = [[InlineKeyboardButton(s, callback_data=f"service_{country}_{s}")] for s in services]
        keyboard.append([InlineKeyboardButton("🔙", callback_data="main_menu")])
        await query.edit_message_text(get_text(user_id, 'choose_service'), reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "my_number":
        number, service, country, expires = get_active_number(user_id)
        if number:
            remaining = expires - datetime.now()
            minutes = remaining.seconds // 60
            seconds = remaining.seconds % 60
            lang = get_user_lang(user_id)
            time_left = f"{minutes} د {seconds} ث" if lang=='ar' else f"{minutes} min {seconds} sec"
            await query.edit_message_text(
                get_text(user_id, 'active_number_info', number=number, service=service, country=country, time_left=time_left),
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(get_text(user_id, 'no_active'))

    elif data == "release":
        clear_active_number(user_id)
        await query.edit_message_text(get_text(user_id, 'released'))

    elif data == "renew":
        number, service, country, expires = get_active_number(user_id)
        if not number:
            await query.edit_message_text(get_text(user_id, 'no_active'))
            return
        new_expiry = datetime.now() + timedelta(minutes=NUMBER_EXPIRE_MINUTES)
        conn = get_db()
        c = conn.cursor()
        c.execute('UPDATE active_numbers SET expires_at = ? WHERE user_id = ?', (new_expiry.isoformat(), user_id))
        conn.commit()
        conn.close()
        await query.edit_message_text(get_text(user_id, 'renew_success', minutes=NUMBER_EXPIRE_MINUTES))

    elif data == "history":
        history = get_user_otp_history(user_id, 10)
        if not history:
            await query.edit_message_text(get_text(user_id, 'history_empty'))
            return
        msg = get_text(user_id, 'history_format', limit=10)
        for row in history:
            time_str = row['timestamp'][:16].replace('T', ' ')
            msg += get_text(user_id, 'history_line', otp=row['otp'], service=row['service'], number=row['number'], country=row['country'], time=time_str)
        await query.edit_message_text(msg, parse_mode='Markdown')

    elif data == "services":
        numbers = load_numbers()
        services = {s for _, s, _ in numbers}
        msg = get_text(user_id, 'services_list') + "\n".join(f"• {s}" for s in services)
        await query.edit_message_text(msg)

    elif data == "language":
        keyboard = [
            [InlineKeyboardButton("العربية", callback_data="lang_ar")],
            [InlineKeyboardButton("English", callback_data="lang_en")],
            [InlineKeyboardButton("🔙", callback_data="main_menu")]
        ]
        await query.edit_message_text("اختر اللغة / Choose language:", reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("lang_"):
        new_lang = data.split('_')[1]
        set_language(user_id, new_lang)
        await query.edit_message_text(get_text(user_id, 'lang_changed'))
        await show_main_menu(query.message, user_id)

    elif data == "coupon":
        context.user_data['coupon_mode'] = True
        await query.edit_message_text(
            get_text(user_id, 'coupon_instruction'),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="main_menu")]])
        )

    elif data == "admin_panel" and is_admin(user_id):
        keyboard = [
            [InlineKeyboardButton(get_text(user_id, 'admin_stats'), callback_data="admin_stats")],
            [InlineKeyboardButton(get_text(user_id, 'admin_upload'), callback_data="admin_upload")],
            [InlineKeyboardButton(get_text(user_id, 'admin_list'), callback_data="admin_list")],
            [InlineKeyboardButton(get_text(user_id, 'admin_broadcast'), callback_data="admin_broadcast")],
            [InlineKeyboardButton(get_text(user_id, 'admin_export'), callback_data="admin_export")],
            [InlineKeyboardButton(get_text(user_id, 'admin_coupon'), callback_data="admin_coupon")],
            [InlineKeyboardButton("🔙", callback_data="main_menu")]
        ]
        await query.edit_message_text(get_text(user_id, 'admin_panel'), reply_markup=InlineKeyboardMarkup(keyboard))

    elif data == "admin_stats" and is_admin(user_id):
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM users')
        total_users = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM active_numbers')
        active_cnt = c.fetchone()[0]
        c.execute('SELECT COUNT(*) FROM otp_logs')
        total_otps = c.fetchone()[0]
        conn.close()
        text = f"📊 **Statistics**\n👥 Users: {total_users}\n🔢 Active: {active_cnt}\n🔐 Total OTPs: {total_otps}"
        await query.edit_message_text(text, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]))

    elif data == "admin_list" and is_admin(user_id):
        admins = ADMIN_IDS
        msg = get_text(user_id, 'admin_list') + "\n".join(f"• `{a}`" for a in admins)
        await query.edit_message_text(msg, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]))

    elif data == "admin_upload" and is_admin(user_id):
        context.user_data['waiting_for_numbers_file'] = True
        await query.edit_message_text(get_text(user_id, 'file_upload_instruction'), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]))

    elif data == "admin_broadcast" and is_admin(user_id):
        context.user_data['broadcast_mode'] = True
        await query.edit_message_text(get_text(user_id, 'broadcast_instruction'), reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]))

    elif data == "admin_export" and is_admin(user_id):
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT * FROM users')
        users = c.fetchall()
        c.execute('SELECT * FROM otp_logs')
        logs = c.fetchall()
        conn.close()
        export_data = {"users": [dict(u) for u in users], "otp_logs": [dict(l) for l in logs]}
        filename = "all_data.json"
        with open(filename, "w") as f:
            json.dump(export_data, f, indent=2, default=str)
        await query.message.reply_document(document=open(filename, "rb"), filename="all_data.json")
        os.remove(filename)
        await query.edit_message_text("✅ Data exported.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]]))

    elif data == "admin_coupon" and is_admin(user_id):
        context.user_data['add_coupon_mode'] = True
        await query.edit_message_text(
            "🎫 أرسل الكوبون بالصيغة: `كود,عدد_الاستخدامات`\nمثال: `SAVE10,100`",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙", callback_data="admin_panel")]])
        )

    elif data == "main_menu":
        await show_main_menu(query.message, user_id)

# ========== TEXT INPUT HANDLERS ==========
async def handle_service_country_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return False
    text = update.message.text.strip()
    if context.user_data.get('waiting_for_service'):
        context.user_data['temp_service'] = text
        context.user_data['waiting_for_service'] = False
        context.user_data['waiting_for_country'] = True
        await update.message.reply_text(get_text(user_id, 'ask_country', service=text))
        return True
    elif context.user_data.get('waiting_for_country'):
        service = context.user_data.get('temp_service')
        country = text
        numbers = context.user_data.get('uploaded_numbers', [])
        current = load_numbers()
        for num in numbers:
            current.append([num, service, country])
        save_numbers(current)
        context.user_data.pop('uploaded_numbers', None)
        context.user_data.pop('temp_service', None)
        context.user_data.pop('waiting_for_country', None)
        await update.message.reply_text(get_text(user_id, 'file_added', count=len(numbers), service=service, country=country))
        return True
    return False

async def handle_coupon_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.user_data.get('coupon_mode'):
        code = update.message.text.strip()
        context.user_data['coupon_mode'] = False
        if use_coupon(code, user_id):
            number, service, country, expires = get_active_number(user_id)
            if number:
                new_expiry = datetime.now() + timedelta(minutes=NUMBER_EXPIRE_MINUTES)
                conn = get_db()
                c = conn.cursor()
                c.execute('UPDATE active_numbers SET expires_at = ? WHERE user_id = ?', (new_expiry.isoformat(), user_id))
                conn.commit()
                conn.close()
                await update.message.reply_text(get_text(user_id, 'coupon_used', minutes=NUMBER_EXPIRE_MINUTES))
            else:
                await update.message.reply_text("✅ Coupon activated! Use /start to get a number.")
        else:
            await update.message.reply_text(get_text(user_id, 'coupon_invalid'))
        return True
    return False

async def handle_broadcast_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.user_data.get('broadcast_mode') and is_admin(user_id):
        msg = update.message.text
        context.user_data['broadcast_mode'] = False
        conn = get_db()
        c = conn.cursor()
        c.execute('SELECT user_id FROM users')
        users = c.fetchall()
        conn.close()
        sent = 0
        for row in users:
            try:
                await context.bot.send_message(row['user_id'], f"📢 Broadcast:\n{msg}")
                sent += 1
            except:
                pass
        await update.message.reply_text(get_text(user_id, 'broadcast_done', count=sent))
        return True
    return False

async def handle_add_coupon_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if context.user_data.get('add_coupon_mode') and is_admin(user_id):
        text = update.message.text.strip()
        context.user_data['add_coupon_mode'] = False
        if ',' not in text:
            await update.message.reply_text("❌ Use: code,uses")
            return True
        code, uses_str = text.split(',', 1)
        try:
            uses = int(uses_str.strip())
        except:
            await update.message.reply_text("❌ Uses must be a number.")
            return True
        add_coupon(code.strip(), uses)
        await update.message.reply_text(get_text(user_id, 'coupon_added', code=code, uses=uses))
        return True
    return False

async def handle_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.message.reply_text("⛔ Only admins can upload.")
        return
    if not context.user_data.get('waiting_for_numbers_file'):
        await update.message.reply_text("⚠️ Please use 'Upload Numbers File' from admin panel.")
        return
    document = update.message.document
    if not document.file_name.endswith('.txt'):
        await update.message.reply_text("❌ Please send a .txt file.")
        return
    file = await document.get_file()
    file_content = await file.download_as_bytearray()
    lines = file_content.decode('utf-8').splitlines()
    numbers = [line.strip() for line in lines if line.strip()]
    if not numbers:
        await update.message.reply_text(get_text(user_id, 'file_invalid'))
        return
    context.user_data['uploaded_numbers'] = numbers
    context.user_data['waiting_for_numbers_file'] = False
    context.user_data['waiting_for_service'] = True
    await update.message.reply_text(get_text(user_id, 'ask_service', count=len(numbers)))

async def captcha_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # Skip if any other mode is active
    if any(context.user_data.get(k) for k in ['waiting_for_service', 'waiting_for_country', 'coupon_mode', 'broadcast_mode', 'add_coupon_mode']):
        return
    if not is_captcha_solved(user_id):
        code = update.message.text.strip()
        if verify_captcha(user_id, code):
            set_captcha_solved(user_id)
            await update.message.reply_text(get_text(user_id, 'main_menu'))
            await show_main_menu(update.message, user_id)
        else:
            await update.message.reply_text(get_text(user_id, 'wrong_captcha'))

# Command handlers
async def my_number_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    number, service, country, expires = get_active_number(user_id)
    if number:
        remaining = expires - datetime.now()
        minutes = remaining.seconds // 60
        seconds = remaining.seconds % 60
        lang = get_user_lang(user_id)
        time_left = f"{minutes} د {seconds} ث" if lang=='ar' else f"{minutes} min {seconds} sec"
        await update.message.reply_text(
            get_text(user_id, 'active_number_info', number=number, service=service, country=country, time_left=time_left),
            parse_mode='Markdown'
        )
    else:
        await update.message.reply_text(get_text(user_id, 'no_active'))

async def release_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    clear_active_number(user_id)
    await update.message.reply_text(get_text(user_id, 'released'))

async def renew_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    number, service, country, expires = get_active_number(user_id)
    if not number:
        await update.message.reply_text(get_text(user_id, 'no_active'))
        return
    new_expiry = datetime.now() + timedelta(minutes=NUMBER_EXPIRE_MINUTES)
    conn = get_db()
    c = conn.cursor()
    c.execute('UPDATE active_numbers SET expires_at = ? WHERE user_id = ?', (new_expiry.isoformat(), user_id))
    conn.commit()
    conn.close()
    await update.message.reply_text(get_text(user_id, 'renew_success', minutes=NUMBER_EXPIRE_MINUTES))

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    history = get_user_otp_history(user_id, 10)
    if not history:
        await update.message.reply_text(get_text(user_id, 'history_empty'))
        return
    msg = get_text(user_id, 'history_format', limit=10)
    for row in history:
        time_str = row['timestamp'][:16].replace('T', ' ')
        msg += get_text(user_id, 'history_line', otp=row['otp'], service=row['service'], number=row['number'], country=row['country'], time=time_str)
    await update.message.reply_text(msg, parse_mode='Markdown')

async def services_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    numbers = load_numbers()
    services = {s for _, s, _ in numbers}
    msg = get_text(user_id, 'services_list') + "\n".join(f"• {s}" for s in services)
    await update.message.reply_text(msg)

async def language_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    keyboard = [
        [InlineKeyboardButton("العربية", callback_data="lang_ar")],
        [InlineKeyboardButton("English", callback_data="lang_en")],
    ]
    await update.message.reply_text("اختر اللغة:", reply_markup=InlineKeyboardMarkup(keyboard))

async def redeem_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text(get_text(user_id, 'coupon_format'))
        return
    code = args[0]
    if use_coupon(code, user_id):
        number, service, country, expires = get_active_number(user_id)
        if number:
            new_expiry = datetime.now() + timedelta(minutes=NUMBER_EXPIRE_MINUTES)
            conn = get_db()
            c = conn.cursor()
            c.execute('UPDATE active_numbers SET expires_at = ? WHERE user_id = ?', (new_expiry.isoformat(), user_id))
            conn.commit()
            conn.close()
            await update.message.reply_text(get_text(user_id, 'coupon_used', minutes=NUMBER_EXPIRE_MINUTES))
        else:
            await update.message.reply_text("✅ Coupon activated! Get a new number via /start.")
    else:
        await update.message.reply_text(get_text(user_id, 'coupon_invalid'))

async def export_data_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db()
    c = conn.cursor()
    c.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
    user_row = c.fetchone()
    c.execute('SELECT * FROM otp_logs WHERE user_id = ?', (user_id,))
    logs = c.fetchall()
    conn.close()
    data = {
        "user_id": user_id,
        "username": user_row['username'],
        "language": user_row['language'],
        "created_at": user_row['created_at'],
        "otp_history": [dict(log) for log in logs]
    }
    filename = f"export_{user_id}.json"
    with open(filename, "w") as f:
        json.dump(data, f, indent=2, default=str)
    await update.message.reply_document(document=open(filename, "rb"), filename=f"user_{user_id}_data.json")
    os.remove(filename)
    await update.message.reply_text(get_text(user_id, 'export_sent'))

# ========== BACKGROUND OTP MONITOR ==========
async def otp_monitor(app: Application):
    while True:
        try:
            active = get_all_active_numbers()
            for row in active:
                user_id = row['user_id']
                number = row['number']
                service = row['service']
                country = row['country']
                otp = fetch_otp_for_number(number)
                if otp:
                    await app.bot.send_message(user_id, f"🔐 **New OTP!**\n📞 `{number}`\n📱 {service}\n🌍 {country}\n🔢 `{otp}`", parse_mode='Markdown')
                    if REQUIRED_CHANNEL:
                        await app.bot.send_message(REQUIRED_CHANNEL, f"🔐 OTP for {number}: `{otp}`", parse_mode='Markdown')
                    add_otp_log(user_id, number, service, country, otp)
                    clear_active_number(user_id)
        except Exception as e:
            logger.error(f"Monitor error: {e}")
        await asyncio.sleep(15)

# ========== MAIN ==========
def main():
    logging.basicConfig(level=logging.INFO)
    init_db()
    if not os.path.exists(NUMBERS_FILE):
        open(NUMBERS_FILE, 'a').close()
    if not os.path.exists(COUPONS_FILE):
        with open(COUPONS_FILE, 'w') as f:
            json.dump({}, f)

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("my_number", my_number_command))
    app.add_handler(CommandHandler("release", release_command))
    app.add_handler(CommandHandler("renew", renew_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("services", services_command))
    app.add_handler(CommandHandler("language", language_command))
    app.add_handler(CommandHandler("redeem", redeem_command))
    app.add_handler(CommandHandler("export_data", export_data_command))

    app.add_handler(CallbackQueryHandler(button_callback))
    # Order matters: service/country first, then coupon, broadcast, coupon add, file, then captcha
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_service_country_input))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_coupon_input))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_broadcast_input))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_add_coupon_input))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file_upload))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, captcha_handler))

    loop = asyncio.get_event_loop()
    loop.create_task(otp_monitor(app))
    print("✅ Bot is running...")
    app.run_polling()

if __name__ == '__main__':
    main()
