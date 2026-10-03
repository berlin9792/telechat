"""
Telegram Relay Bot with Web Dashboard
Production-ready for Render.com deployment
"""

import os
import sys
import json
import logging
import asyncio
import threading
import tempfile
from datetime import datetime
from functools import wraps
from io import BytesIO

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, flash, Response
)
from flask_socketio import SocketIO
from werkzeug.utils import secure_filename

from telegram import Update, Bot
from telegram.ext import (
    ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
)

import database as db

# ============= CONFIG from ENV =============
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-to-random-secret")
PORT = int(os.environ.get("PORT", "10000"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
# ===========================================

if not BOT_TOKEN or OWNER_ID == 0:
    print("❌ ERROR: BOT_TOKEN and OWNER_ID environment variables are required!")
    sys.exit(1)

# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Flask app
TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(TEMPLATE_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
app.secret_key = SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50 MB upload limit

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading',
                    logger=False, engineio_logger=False)

# Global bot event loop
BOT_LOOP = None


# =============================================
# TELEGRAM FILE URL BUILDER
# =============================================
def get_telegram_file_url(file_path):
    """Build direct Telegram file URL."""
    if not file_path:
        return None
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"


async def resolve_file_url(bot, file_id):
    """Get direct URL to a Telegram file."""
    try:
        file = await bot.get_file(file_id)
        return get_telegram_file_url(file.file_path)
    except Exception as e:
        logger.error(f"Error getting file URL: {e}")
        return None


# =============================================
# TELEGRAM BOT HANDLERS
# =============================================

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle messages from users."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    msg = update.effective_message

    if db.is_blocked(chat_id):
        try:
            await msg.reply_text("⚠️ You have been blocked by the admin.")
        except Exception:
            pass
        return

    db.get_or_create_user(chat_id, user.username, user.full_name)

    # Profile photo (lightweight, just URL)
    try:
        photos = await context.bot.get_user_profile_photos(chat_id, limit=1)
        if photos.total_count > 0:
            photo = photos.photos[0][-1]
            pp_url = await resolve_file_url(context.bot, photo.file_id)
            if pp_url:
                db.save_user_profile_photo(chat_id, pp_url)
    except Exception:
        pass

    # Determine message type
    message_type = 'text'
    content = msg.text or msg.caption or ''
    file_id = None
    file_url = None
    file_name = None
    thumbnail_url = None

    try:
        if msg.photo:
            message_type = 'photo'
            photo = msg.photo[-1]
            file_id = photo.file_id
            file_url = await resolve_file_url(context.bot, file_id)

        elif msg.video:
            message_type = 'video'
            file_id = msg.video.file_id
            file_url = await resolve_file_url(context.bot, file_id)
            file_name = msg.video.file_name
            if msg.video.thumbnail:
                thumbnail_url = await resolve_file_url(context.bot, msg.video.thumbnail.file_id)

        elif msg.document:
            message_type = 'document'
            file_id = msg.document.file_id
            file_url = await resolve_file_url(context.bot, file_id)
            file_name = msg.document.file_name

        elif msg.voice:
            message_type = 'voice'
            file_id = msg.voice.file_id
            file_url = await resolve_file_url(context.bot, file_id)

        elif msg.audio:
            message_type = 'audio'
            file_id = msg.audio.file_id
            file_url = await resolve_file_url(context.bot, file_id)
            file_name = msg.audio.file_name or msg.audio.title

        elif msg.video_note:
            message_type = 'video_note'
            file_id = msg.video_note.file_id
            file_url = await resolve_file_url(context.bot, file_id)

        elif msg.sticker:
            message_type = 'sticker'
            file_id = msg.sticker.file_id
            file_url = await resolve_file_url(context.bot, file_id)

        elif msg.animation:
            message_type = 'animation'
            file_id = msg.animation.file_id
            file_url = await resolve_file_url(context.bot, file_id)

        elif msg.contact:
            content = f"📱 Contact: {msg.contact.first_name} - {msg.contact.phone_number}"

        elif msg.location:
            content = f"📍 Location: {msg.location.latitude}, {msg.location.longitude}"

    except Exception as e:
        logger.error(f"Error processing media: {e}")

    msg_id = db.save_message(
        user_id=chat_id,
        direction='incoming',
        message_type=message_type,
        content=content,
        file_id=file_id,
        file_name=file_name,
        file_url=file_url,
        thumbnail_url=thumbnail_url,
        telegram_message_id=msg.message_id
    )

    # Real-time notification
    try:
        socketio.emit('new_message', {
            'user_id': chat_id,
            'username': user.username,
            'full_name': user.full_name,
            'direction': 'incoming',
            'message_type': message_type,
            'content': content,
            'file_url': file_url,
            'file_name': file_name,
            'thumbnail_url': thumbnail_url,
            'timestamp': datetime.now().isoformat(),
            'msg_id': msg_id
        })
    except Exception as e:
        logger.error(f"Socket emit error: {e}")

    # Also forward to owner on Telegram as backup
    try:
        await context.bot.forward_message(
            chat_id=OWNER_ID,
            from_chat_id=chat_id,
            message_id=msg.message_id
        )
    except Exception as e:
        logger.error(f"Owner forward error: {e}")


async def handle_owner_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner's messages - redirect to dashboard."""
    msg = update.effective_message
    if msg.text and msg.text.startswith('/'):
        return
    url = WEBHOOK_URL if WEBHOOK_URL else f"http://localhost:{PORT}"
    await msg.reply_text(f"🌐 Reply via dashboard: {url}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id == OWNER_ID:
        url = WEBHOOK_URL if WEBHOOK_URL else f"http://localhost:{PORT}"
        await update.message.reply_text(
            f"👑 Welcome Admin!\n\n🌐 Dashboard: {url}\n🔑 Login with your admin password"
        )
    else:
        db.get_or_create_user(user.id, user.username, user.full_name)
        await update.message.reply_text(
            "👋 Welcome! Send me any message, media, or file. The admin will respond soon."
        )


# =============================================
# FLASK ROUTES
# =============================================

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('logged_in'):
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'service': 'telegram-relay-bot'})


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form.get('password', '')
        if password == ADMIN_PASSWORD:
            session['logged_in'] = True
            session.permanent = True
            return redirect(url_for('dashboard'))
        flash('Wrong password!', 'error')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def dashboard():
    users = db.get_all_users()
    stats = db.get_total_stats()
    return render_template('dashboard.html', users=users, stats=stats)


@app.route('/chat/<int:user_id>')
@login_required
def chat(user_id):
    user = db.get_user(user_id)
    if not user:
        flash('User not found!', 'error')
        return redirect(url_for('dashboard'))
    messages = db.get_messages(user_id)
    db.mark_as_read(user_id)
    all_users = db.get_all_users()
    return render_template('chat.html', user=user, messages=messages, all_users=all_users)


def run_async_in_bot_loop(coro):
    """Submit a coroutine to the running bot's event loop."""
    global BOT_LOOP
    if BOT_LOOP is None:
        raise RuntimeError("Bot loop is not running yet")
    future = asyncio.run_coroutine_threadsafe(coro, BOT_LOOP)
    return future.result(timeout=60)


@app.route('/api/send_message', methods=['POST'])
@login_required
def api_send_message():
    try:
        user_id = int(request.form.get('user_id'))
        content = request.form.get('content', '').strip()
        file = request.files.get('file')

        if not content and not (file and file.filename):
            return jsonify({'status': 'error', 'message': 'Empty message'}), 400

        async def do_send():
            bot = Bot(token=BOT_TOKEN)
            async with bot:
                if file and file.filename:
                    filename = secure_filename(file.filename)
                    ext = os.path.splitext(filename)[1].lower()
                    file_bytes = file.read()
                    bio = BytesIO(file_bytes)
                    bio.name = filename

                    if ext in ('.jpg', '.jpeg', '.png', '.webp'):
                        sent = await bot.send_photo(
                            chat_id=user_id, photo=bio,
                            caption=content if content else None
                        )
                        return 'photo', sent

                    elif ext in ('.mp4', '.mov', '.avi', '.mkv'):
                        sent = await bot.send_video(
                            chat_id=user_id, video=bio,
                            caption=content if content else None
                        )
                        return 'video', sent

                    elif ext in ('.mp3', '.ogg', '.wav', '.m4a', '.flac'):
                        sent = await bot.send_audio(
                            chat_id=user_id, audio=bio,
                            caption=content if content else None
                        )
                        return 'audio', sent

                    elif ext == '.gif':
                        sent = await bot.send_animation(
                            chat_id=user_id, animation=bio,
                            caption=content if content else None
                        )
                        return 'animation', sent

                    else:
                        sent = await bot.send_document(
                            chat_id=user_id, document=bio,
                            caption=content if content else None,
                            filename=filename
                        )
                        return 'document', sent
                else:
                    sent = await bot.send_message(chat_id=user_id, text=content)
                    return 'text', sent

        # Run in bot's event loop
        msg_type, sent_msg = run_async_in_bot_loop(do_send())

        # Save to DB
        file_url = None
        file_name = None
        if file and file.filename:
            file_name = secure_filename(file.filename)
            # Try to get file_id from sent message
            try:
                if msg_type == 'photo' and sent_msg.photo:
                    async def get_url():
                        bot = Bot(token=BOT_TOKEN)
                        async with bot:
                            return await resolve_file_url(bot, sent_msg.photo[-1].file_id)
                    file_url = run_async_in_bot_loop(get_url())
                elif msg_type == 'video' and sent_msg.video:
                    async def get_url():
                        bot = Bot(token=BOT_TOKEN)
                        async with bot:
                            return await resolve_file_url(bot, sent_msg.video.file_id)
                    file_url = run_async_in_bot_loop(get_url())
                elif msg_type == 'document' and sent_msg.document:
                    async def get_url():
                        bot = Bot(token=BOT_TOKEN)
                        async with bot:
                            return await resolve_file_url(bot, sent_msg.document.file_id)
                    file_url = run_async_in_bot_loop(get_url())
            except Exception as e:
                logger.error(f"File URL error: {e}")

        msg_id = db.save_message(
            user_id=user_id,
            direction='outgoing',
            message_type=msg_type,
            content=content,
            file_name=file_name,
            file_url=file_url
        )

        socketio.emit('new_message', {
            'user_id': user_id,
            'direction': 'outgoing',
            'message_type': msg_type,
            'content': content,
            'file_url': file_url,
            'file_name': file_name,
            'timestamp': datetime.now().isoformat(),
            'msg_id': msg_id
        })

        return jsonify({'status': 'ok', 'msg_id': msg_id})

    except Exception as e:
        logger.exception("Send error")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/block/<int:user_id>', methods=['POST'])
@login_required
def api_block(user_id):
    db.block_user(user_id)
    return jsonify({'status': 'ok'})


@app.route('/api/unblock/<int:user_id>', methods=['POST'])
@login_required
def api_unblock(user_id):
    db.unblock_user(user_id)
    return jsonify({'status': 'ok'})


@app.route('/api/delete_chat/<int:user_id>', methods=['POST'])
@login_required
def api_delete_chat(user_id):
    db.delete_chat(user_id)
    return jsonify({'status': 'ok'})


@app.route('/api/search')
@login_required
def api_search():
    query = request.args.get('q', '')
    users = db.search_users(query)
    return jsonify(users)


@app.route('/api/users')
@login_required
def api_users():
    return jsonify(db.get_all_users())


@app.route('/api/messages/<int:user_id>')
@login_required
def api_messages(user_id):
    messages = db.get_messages(user_id)
    db.mark_as_read(user_id)
    return jsonify(messages)


# =============================================
# TEMPLATE & STATIC FILE GENERATION
# =============================================

LOGIN_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Admin Login</title>
<link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
</head>
<body class="login-body">
<div class="login-container">
    <div class="login-card">
        <div class="login-icon"><i class="fab fa-telegram"></i></div>
        <h1>Relay Bot Dashboard</h1>
        <p class="login-subtitle">Enter admin password to continue</p>
        {% with messages = get_flashed_messages(with_categories=true) %}
            {% for category, message in messages %}
                <div class="alert alert-{{ category }}">{{ message }}</div>
            {% endfor %}
        {% endwith %}
        <form method="POST">
            <div class="input-group">
                <i class="fas fa-lock"></i>
                <input type="password" name="password" placeholder="Password" required autofocus>
            </div>
            <button type="submit" class="btn-login">
                <i class="fas fa-sign-in-alt"></i> Login
            </button>
        </form>
    </div>
</div>
</body>
</html>'''

DASHBOARD_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Dashboard</title>
<link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
</head>
<body>
<div class="app-container">
    <div class="sidebar">
        <div class="sidebar-header">
            <div class="logo"><i class="fab fa-telegram"></i><span>Relay Bot</span></div>
            <a href="{{ url_for('logout') }}" class="logout-btn" title="Logout">
                <i class="fas fa-sign-out-alt"></i>
            </a>
        </div>
        <div class="stats-bar">
            <div class="stat-item"><span class="stat-num">{{ stats.total_users }}</span><span class="stat-label">Users</span></div>
            <div class="stat-item"><span class="stat-num">{{ stats.total_messages }}</span><span class="stat-label">Messages</span></div>
            <div class="stat-item"><span class="stat-num">{{ stats.total_unread }}</span><span class="stat-label">Unread</span></div>
            <div class="stat-item"><span class="stat-num">{{ stats.blocked_users }}</span><span class="stat-label">Blocked</span></div>
        </div>
        <div class="search-box">
            <i class="fas fa-search"></i>
            <input type="text" id="searchInput" placeholder="Search users..." oninput="searchUsers(this.value)">
        </div>
        <div class="user-list" id="userList">
            {% for user in users %}
            <a href="{{ url_for('chat', user_id=user.user_id) }}" class="user-item {% if user.is_blocked %}blocked{% endif %}">
                <div class="user-avatar">
                    {% if user.profile_photo %}
                    <img src="{{ user.profile_photo }}" alt="" onerror="this.style.display='none'">
                    {% else %}
                    <div class="avatar-placeholder">{{ (user.full_name or '?')[0] }}</div>
                    {% endif %}
                    {% if user.unread_count > 0 %}<span class="unread-badge">{{ user.unread_count }}</span>{% endif %}
                </div>
                <div class="user-info">
                    <div class="user-name">{{ user.full_name or 'Unknown' }}{% if user.is_blocked %} <i class="fas fa-ban blocked-icon"></i>{% endif %}</div>
                    <div class="user-last-msg">
                        {% if user.last_type and user.last_type != 'text' %}
                            <i class="fas fa-paperclip"></i> {{ user.last_type }}
                        {% elif user.last_content %}{{ user.last_content[:40] }}
                        {% else %}No messages yet{% endif %}
                    </div>
                </div>
            </a>
            {% endfor %}
            {% if not users %}
            <div class="empty-state">
                <i class="fas fa-inbox"></i>
                <p>No users yet. Wait for someone to message your bot!</p>
            </div>
            {% endif %}
        </div>
    </div>
    <div class="main-content">
        <div class="welcome-screen">
            <div class="welcome-icon"><i class="fab fa-telegram"></i></div>
            <h2>Welcome to Relay Bot Dashboard</h2>
            <p>Select a user from the sidebar to start chatting</p>
            <div class="welcome-features">
                <div class="feature"><i class="fas fa-comments"></i><span>Reply to users</span></div>
                <div class="feature"><i class="fas fa-paperclip"></i><span>Send media</span></div>
                <div class="feature"><i class="fas fa-ban"></i><span>Block/Unblock</span></div>
            </div>
        </div>
    </div>
</div>
<script>
const socket = io();
socket.on('new_message', function(data) {
    if (data.direction === 'incoming') location.reload();
});
function searchUsers(query) {
    if (!query) { location.reload(); return; }
    fetch('/api/search?q=' + encodeURIComponent(query))
        .then(r => r.json())
        .then(users => renderUsers(users));
}
function renderUsers(users) {
    const list = document.getElementById('userList');
    list.innerHTML = '';
    users.forEach(u => {
        const initial = (u.full_name || '?')[0];
        const blocked = u.is_blocked ? 'blocked' : '';
        const blockedIcon = u.is_blocked ? '<i class="fas fa-ban blocked-icon"></i>' : '';
        const badge = u.unread_count > 0 ? `<span class="unread-badge">${u.unread_count}</span>` : '';
        const avatar = u.profile_photo
            ? `<img src="${u.profile_photo}" onerror="this.style.display='none'">`
            : `<div class="avatar-placeholder">${initial}</div>`;
        let lastMsg = 'No messages';
        if (u.last_type && u.last_type !== 'text') lastMsg = '<i class="fas fa-paperclip"></i> ' + u.last_type;
        else if (u.last_content) lastMsg = u.last_content.substring(0, 40);
        list.innerHTML += `
            <a href="/chat/${u.user_id}" class="user-item ${blocked}">
                <div class="user-avatar">${avatar}${badge}</div>
                <div class="user-info">
                    <div class="user-name">${u.full_name || 'Unknown'} ${blockedIcon}</div>
                    <div class="user-last-msg">${lastMsg}</div>
                </div>
            </a>`;
    });
}
</script>
</body>
</html>'''

CHAT_HTML = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Chat - {{ user.full_name }}</title>
<link rel="stylesheet" href="{{ url_for('static', filename='style.css') }}">
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
<script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.7.2/socket.io.min.js"></script>
</head>
<body>
<div class="app-container">
    <div class="sidebar" id="sidebar">
        <div class="sidebar-header">
            <div class="logo"><i class="fab fa-telegram"></i><span>Relay Bot</span></div>
            <a href="{{ url_for('logout') }}" class="logout-btn"><i class="fas fa-sign-out-alt"></i></a>
        </div>
        <div class="search-box">
            <i class="fas fa-search"></i>
            <input type="text" id="searchInput" placeholder="Search users..." oninput="searchUsers(this.value)">
        </div>
        <div class="user-list" id="userList">
            {% for u in all_users %}
            <a href="{{ url_for('chat', user_id=u.user_id) }}" 
               class="user-item {% if u.user_id == user.user_id %}active{% endif %} {% if u.is_blocked %}blocked{% endif %}">
                <div class="user-avatar">
                    {% if u.profile_photo %}<img src="{{ u.profile_photo }}" onerror="this.style.display='none'">
                    {% else %}<div class="avatar-placeholder">{{ (u.full_name or '?')[0] }}</div>{% endif %}
                    {% if u.unread_count > 0 %}<span class="unread-badge">{{ u.unread_count }}</span>{% endif %}
                </div>
                <div class="user-info">
                    <div class="user-name">{{ u.full_name or 'Unknown' }}</div>
                    <div class="user-last-msg">{% if u.last_content %}{{ u.last_content[:30] }}{% else %}...{% endif %}</div>
                </div>
            </a>
            {% endfor %}
        </div>
    </div>
    <div class="chat-area">
        <div class="chat-header">
            <button class="mobile-menu-btn" onclick="toggleSidebar()"><i class="fas fa-bars"></i></button>
            <div class="chat-header-user">
                <div class="user-avatar small">
                    {% if user.profile_photo %}<img src="{{ user.profile_photo }}" onerror="this.style.display='none'">
                    {% else %}<div class="avatar-placeholder">{{ (user.full_name or '?')[0] }}</div>{% endif %}
                </div>
                <div class="chat-header-info">
                    <div class="chat-header-name">
                        {{ user.full_name or 'Unknown' }}
                        {% if user.is_blocked %}<span class="badge-blocked">BLOCKED</span>{% endif %}
                    </div>
                    <div class="chat-header-meta">@{{ user.username or 'no_username' }} · ID: {{ user.user_id }}</div>
                </div>
            </div>
            <div class="chat-header-actions">
                {% if user.is_blocked %}
                <button class="btn-action btn-unblock" onclick="unblockUser({{ user.user_id }})">
                    <i class="fas fa-check-circle"></i><span>Unblock</span>
                </button>
                {% else %}
                <button class="btn-action btn-block" onclick="blockUser({{ user.user_id }})">
                    <i class="fas fa-ban"></i><span>Block</span>
                </button>
                {% endif %}
                <button class="btn-action btn-delete" onclick="deleteChat({{ user.user_id }})">
                    <i class="fas fa-trash"></i><span>Delete</span>
                </button>
            </div>
        </div>
        <div class="messages-container" id="messagesContainer">
            {% for msg in messages %}
            <div class="message {{ msg.direction }}">
                <div class="message-bubble">
                    {% if msg.message_type == 'photo' and msg.file_url %}
                    <div class="message-media">
                        <img src="{{ msg.file_url }}" alt="Photo" onclick="openMedia(this.src)" loading="lazy">
                    </div>
                    {% endif %}
                    {% if msg.message_type == 'video' and msg.file_url %}
                    <div class="message-media"><video controls preload="metadata"><source src="{{ msg.file_url }}"></video></div>
                    {% endif %}
                    {% if msg.message_type == 'audio' and msg.file_url %}
                    <div class="message-media"><audio controls><source src="{{ msg.file_url }}"></audio></div>
                    {% endif %}
                    {% if msg.message_type == 'voice' and msg.file_url %}
                    <div class="message-media voice-msg">
                        <i class="fas fa-microphone"></i>
                        <audio controls><source src="{{ msg.file_url }}"></audio>
                    </div>
                    {% endif %}
                    {% if msg.message_type == 'document' and msg.file_url %}
                    <div class="message-file">
                        <i class="fas fa-file"></i>
                        <a href="{{ msg.file_url }}" target="_blank" download>{{ msg.file_name or 'Download File' }}</a>
                    </div>
                    {% endif %}
                    {% if msg.message_type == 'sticker' and msg.file_url %}
                    <div class="message-sticker"><img src="{{ msg.file_url }}" alt="Sticker"></div>
                    {% endif %}
                    {% if msg.message_type == 'animation' and msg.file_url %}
                    <div class="message-media"><video autoplay loop muted playsinline><source src="{{ msg.file_url }}"></video></div>
                    {% endif %}
                    {% if msg.message_type == 'video_note' and msg.file_url %}
                    <div class="message-media video-note"><video controls><source src="{{ msg.file_url }}"></video></div>
                    {% endif %}
                    {% if msg.content %}<div class="message-text">{{ msg.content }}</div>{% endif %}
                    <div class="message-time">
                        {{ msg.timestamp[11:16] if msg.timestamp else '' }}
                        {% if msg.direction == 'outgoing' %}<i class="fas fa-check-double"></i>{% endif %}
                    </div>
                </div>
            </div>
            {% endfor %}
            {% if not messages %}
            <div class="empty-chat"><i class="far fa-comments"></i><p>No messages yet. Start the conversation!</p></div>
            {% endif %}
        </div>
        <div class="input-area" {% if user.is_blocked %}style="opacity:0.5"{% endif %}>
            <form id="messageForm" onsubmit="sendMessage(event)" enctype="multipart/form-data">
                <input type="hidden" name="user_id" value="{{ user.user_id }}">
                <label for="fileInput" class="btn-attach" title="Attach"><i class="fas fa-paperclip"></i></label>
                <input type="file" id="fileInput" name="file" 
                       accept="image/*,video/*,audio/*,.pdf,.doc,.docx,.zip,.rar,.txt"
                       onchange="fileSelected(this)" style="display:none">
                <div class="input-wrapper">
                    <div id="filePreview" class="file-preview" style="display:none">
                        <span id="fileName"></span>
                        <button type="button" onclick="removeFile()"><i class="fas fa-times"></i></button>
                    </div>
                    <textarea id="messageInput" name="content" placeholder="Type a message..." rows="1"
                              onkeydown="handleKeyDown(event)" oninput="autoResize(this)"
                              {% if user.is_blocked %}disabled{% endif %}></textarea>
                </div>
                <button type="submit" class="btn-send" {% if user.is_blocked %}disabled{% endif %}>
                    <i class="fas fa-paper-plane"></i>
                </button>
            </form>
        </div>
    </div>
</div>
<div class="media-modal" id="mediaModal" onclick="closeMedia()">
    <div class="media-modal-content">
        <img id="modalImage" src="" alt="">
        <button class="modal-close" onclick="closeMedia()"><i class="fas fa-times"></i></button>
    </div>
</div>
<script>
const currentUserId = {{ user.user_id }};
const socket = io();
function scrollToBottom() {
    const c = document.getElementById('messagesContainer');
    c.scrollTop = c.scrollHeight;
}
scrollToBottom();
socket.on('new_message', function(data) {
    if (data.user_id === currentUserId) location.reload();
});
function sendMessage(e) {
    e.preventDefault();
    const form = document.getElementById('messageForm');
    const formData = new FormData(form);
    const input = document.getElementById('messageInput');
    const fileInput = document.getElementById('fileInput');
    if (!input.value.trim() && !fileInput.files.length) return;
    const btn = document.querySelector('.btn-send');
    btn.disabled = true;
    btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
    fetch('/api/send_message', { method: 'POST', body: formData })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'ok') {
                input.value = ''; fileInput.value = ''; removeFile(); autoResize(input);
                location.reload();
            } else alert('Error: ' + (data.message || 'Unknown'));
        })
        .catch(err => alert('Error: ' + err))
        .finally(() => {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-paper-plane"></i>';
        });
}
function fileSelected(input) {
    if (input.files.length) {
        document.getElementById('filePreview').style.display = 'flex';
        document.getElementById('fileName').textContent = input.files[0].name;
    }
}
function removeFile() {
    document.getElementById('fileInput').value = '';
    document.getElementById('filePreview').style.display = 'none';
}
function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMessage(e); }
}
function autoResize(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}
function openMedia(src) {
    document.getElementById('modalImage').src = src;
    document.getElementById('mediaModal').classList.add('active');
}
function closeMedia() { document.getElementById('mediaModal').classList.remove('active'); }
function blockUser(id) {
    if (!confirm('Block this user?')) return;
    fetch('/api/block/' + id, { method: 'POST' }).then(() => location.reload());
}
function unblockUser(id) {
    if (!confirm('Unblock this user?')) return;
    fetch('/api/unblock/' + id, { method: 'POST' }).then(() => location.reload());
}
function deleteChat(id) {
    if (!confirm('Delete all messages? Cannot be undone.')) return;
    fetch('/api/delete_chat/' + id, { method: 'POST' }).then(() => location.reload());
}
function toggleSidebar() { document.getElementById('sidebar').classList.toggle('open'); }
function searchUsers(query) {
    if (!query) { location.reload(); return; }
    fetch('/api/search?q=' + encodeURIComponent(query))
        .then(r => r.json())
        .then(users => {
            const list = document.getElementById('userList');
            list.innerHTML = '';
            users.forEach(u => {
                const initial = (u.full_name || '?')[0];
                const active = u.user_id === currentUserId ? 'active' : '';
                const blocked = u.is_blocked ? 'blocked' : '';
                const badge = u.unread_count > 0 ? `<span class="unread-badge">${u.unread_count}</span>` : '';
                const avatar = u.profile_photo
                    ? `<img src="${u.profile_photo}" onerror="this.style.display='none'">`
                    : `<div class="avatar-placeholder">${initial}</div>`;
                list.innerHTML += `
                    <a href="/chat/${u.user_id}" class="user-item ${active} ${blocked}">
                        <div class="user-avatar">${avatar}${badge}</div>
                        <div class="user-info">
                            <div class="user-name">${u.full_name || 'Unknown'}</div>
                            <div class="user-last-msg">${u.last_content ? u.last_content.substring(0, 30) : '...'}</div>
                        </div>
                    </a>`;
            });
        });
}
</script>
</body>
</html>'''

STYLE_CSS = '''
:root {
    --bg-primary: #0a0a0f; --bg-secondary: #111118; --bg-tertiary: #1a1a24;
    --bg-card: #16161f; --accent: #6c5ce7; --accent-hover: #7d6ff0;
    --accent-light: rgba(108, 92, 231, 0.15); --text-primary: #e8e8ed;
    --text-secondary: #8888a0; --text-muted: #555566; --border: #2a2a3a;
    --incoming-bg: #1e1e2e; --outgoing-bg: #2d2b55;
    --danger: #e74c3c; --success: #2ecc71;
    --shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
    --radius: 12px; --radius-sm: 8px;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Inter', -apple-system, sans-serif; background: var(--bg-primary); color: var(--text-primary); height: 100vh; overflow: hidden; }
a { color: inherit; text-decoration: none; }
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

.login-body { display: flex; align-items: center; justify-content: center; min-height: 100vh; background: linear-gradient(135deg, #0a0a1a 0%, #1a1030 50%, #0a0a1a 100%); }
.login-container { width: 100%; max-width: 420px; padding: 20px; }
.login-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 20px; padding: 50px 40px; text-align: center; box-shadow: var(--shadow); }
.login-icon { font-size: 64px; color: var(--accent); margin-bottom: 20px; }
.login-card h1 { font-size: 24px; font-weight: 700; margin-bottom: 8px; }
.login-subtitle { color: var(--text-secondary); margin-bottom: 30px; font-size: 14px; }
.input-group { position: relative; margin-bottom: 20px; }
.input-group i { position: absolute; left: 16px; top: 50%; transform: translateY(-50%); color: var(--text-muted); }
.input-group input { width: 100%; padding: 14px 16px 14px 46px; background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: var(--radius); color: var(--text-primary); font-size: 15px; outline: none; transition: border-color 0.3s; }
.input-group input:focus { border-color: var(--accent); }
.btn-login { width: 100%; padding: 14px; background: var(--accent); color: white; border: none; border-radius: var(--radius); font-size: 16px; font-weight: 600; cursor: pointer; transition: background 0.3s, transform 0.1s; }
.btn-login:hover { background: var(--accent-hover); }
.btn-login:active { transform: scale(0.98); }
.alert { padding: 10px; border-radius: var(--radius-sm); margin-bottom: 16px; font-size: 13px; }
.alert-error { background: rgba(231, 76, 60, 0.15); color: var(--danger); border: 1px solid rgba(231, 76, 60, 0.3); }

.app-container { display: flex; height: 100vh; overflow: hidden; }
.sidebar { width: 360px; min-width: 360px; background: var(--bg-secondary); border-right: 1px solid var(--border); display: flex; flex-direction: column; height: 100vh; }
.sidebar-header { padding: 16px 20px; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid var(--border); }
.logo { display: flex; align-items: center; gap: 10px; font-size: 20px; font-weight: 700; }
.logo i { font-size: 28px; color: var(--accent); }
.logout-btn { padding: 8px 12px; border-radius: var(--radius-sm); color: var(--text-secondary); transition: all 0.3s; }
.logout-btn:hover { background: var(--bg-tertiary); color: var(--danger); }

.stats-bar { display: flex; padding: 12px 16px; gap: 8px; border-bottom: 1px solid var(--border); }
.stat-item { flex: 1; text-align: center; padding: 8px 4px; background: var(--bg-tertiary); border-radius: var(--radius-sm); }
.stat-num { display: block; font-size: 18px; font-weight: 700; color: var(--accent); }
.stat-label { font-size: 10px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }

.search-box { padding: 12px 16px; position: relative; border-bottom: 1px solid var(--border); }
.search-box i { position: absolute; left: 28px; top: 50%; transform: translateY(-50%); color: var(--text-muted); font-size: 14px; }
.search-box input { width: 100%; padding: 10px 12px 10px 38px; background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-primary); font-size: 14px; outline: none; transition: border-color 0.3s; }
.search-box input:focus { border-color: var(--accent); }

.user-list { flex: 1; overflow-y: auto; padding: 8px; }
.user-item { display: flex; align-items: center; padding: 12px 14px; border-radius: var(--radius); transition: background 0.2s; gap: 12px; margin-bottom: 2px; }
.user-item:hover { background: var(--bg-tertiary); }
.user-item.active { background: var(--accent-light); border: 1px solid rgba(108, 92, 231, 0.3); }
.user-item.blocked { opacity: 0.5; }

.user-avatar { position: relative; width: 48px; height: 48px; min-width: 48px; border-radius: 50%; overflow: hidden; background: var(--bg-tertiary); }
.user-avatar.small { width: 40px; height: 40px; min-width: 40px; }
.user-avatar img { width: 100%; height: 100%; object-fit: cover; }
.avatar-placeholder { width: 100%; height: 100%; background: linear-gradient(135deg, var(--accent), #a855f7); display: flex; align-items: center; justify-content: center; font-size: 20px; font-weight: 700; color: white; text-transform: uppercase; }
.user-avatar.small .avatar-placeholder { font-size: 16px; }
.unread-badge { position: absolute; top: -2px; right: -2px; background: var(--accent); color: white; font-size: 11px; font-weight: 700; min-width: 20px; height: 20px; border-radius: 10px; display: flex; align-items: center; justify-content: center; padding: 0 5px; border: 2px solid var(--bg-secondary); }

.user-info { flex: 1; min-width: 0; }
.user-name { font-size: 14px; font-weight: 600; margin-bottom: 3px; display: flex; align-items: center; gap: 6px; }
.blocked-icon { color: var(--danger); font-size: 12px; }
.user-last-msg { font-size: 13px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.empty-state { text-align: center; padding: 60px 20px; color: var(--text-muted); }
.empty-state i { font-size: 48px; margin-bottom: 16px; color: var(--border); }

.main-content { flex: 1; display: flex; align-items: center; justify-content: center; }
.welcome-screen { text-align: center; padding: 40px; }
.welcome-icon { font-size: 80px; color: var(--accent); margin-bottom: 24px; opacity: 0.6; }
.welcome-screen h2 { font-size: 28px; margin-bottom: 10px; font-weight: 700; }
.welcome-screen p { color: var(--text-secondary); font-size: 16px; margin-bottom: 40px; }
.welcome-features { display: flex; gap: 30px; justify-content: center; }
.feature { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 20px 24px; background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius); }
.feature i { font-size: 24px; color: var(--accent); }
.feature span { font-size: 13px; color: var(--text-secondary); }

.chat-area { flex: 1; display: flex; flex-direction: column; height: 100vh; background: var(--bg-primary); }
.chat-header { padding: 12px 20px; background: var(--bg-secondary); border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 12px; }
.mobile-menu-btn { display: none; background: none; border: none; color: var(--text-primary); font-size: 20px; cursor: pointer; padding: 8px; }
.chat-header-user { display: flex; align-items: center; gap: 12px; flex: 1; }
.chat-header-name { font-size: 16px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.badge-blocked { font-size: 10px; padding: 2px 8px; background: rgba(231, 76, 60, 0.2); color: var(--danger); border-radius: 4px; font-weight: 700; }
.chat-header-meta { font-size: 12px; color: var(--text-secondary); }
.chat-header-actions { display: flex; gap: 8px; }
.btn-action { padding: 8px 14px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: transparent; color: var(--text-secondary); cursor: pointer; font-size: 13px; display: flex; align-items: center; gap: 6px; transition: all 0.3s; }
.btn-action:hover { background: var(--bg-tertiary); }
.btn-block:hover { border-color: var(--danger); color: var(--danger); }
.btn-unblock:hover { border-color: var(--success); color: var(--success); }
.btn-delete:hover { border-color: var(--danger); color: var(--danger); }

.messages-container { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 6px; background: radial-gradient(ellipse at top left, rgba(108, 92, 231, 0.03), transparent 50%), radial-gradient(ellipse at bottom right, rgba(168, 85, 247, 0.03), transparent 50%), var(--bg-primary); }
.message { display: flex; max-width: 70%; }
.message.incoming { align-self: flex-start; }
.message.outgoing { align-self: flex-end; }
.message-bubble { padding: 10px 14px; border-radius: 16px; max-width: 100%; word-wrap: break-word; position: relative; }
.incoming .message-bubble { background: var(--incoming-bg); border-bottom-left-radius: 4px; border: 1px solid var(--border); }
.outgoing .message-bubble { background: var(--outgoing-bg); border-bottom-right-radius: 4px; border: 1px solid rgba(108, 92, 231, 0.3); }
.message-text { font-size: 14px; line-height: 1.5; white-space: pre-wrap; }
.message-time { font-size: 11px; color: var(--text-muted); margin-top: 4px; text-align: right; display: flex; align-items: center; justify-content: flex-end; gap: 4px; }
.outgoing .message-time i { color: var(--accent); font-size: 12px; }

.message-media { margin-bottom: 6px; border-radius: var(--radius-sm); overflow: hidden; max-width: 320px; }
.message-media img { max-width: 100%; display: block; cursor: pointer; border-radius: var(--radius-sm); transition: opacity 0.2s; }
.message-media img:hover { opacity: 0.9; }
.message-media video, .message-media audio { max-width: 100%; display: block; border-radius: var(--radius-sm); }
.message-media audio { min-width: 250px; }
.voice-msg { display: flex; align-items: center; gap: 10px; }
.voice-msg i { color: var(--accent); font-size: 20px; }
.video-note { width: 200px; height: 200px; border-radius: 50% !important; overflow: hidden; }
.video-note video { width: 100%; height: 100%; object-fit: cover; }
.message-sticker { max-width: 180px; }
.message-sticker img { max-width: 100%; }
.message-file { display: flex; align-items: center; gap: 10px; padding: 10px 14px; background: var(--bg-tertiary); border-radius: var(--radius-sm); margin-bottom: 6px; }
.message-file i { font-size: 24px; color: var(--accent); }
.message-file a { color: var(--accent); font-size: 14px; word-break: break-all; }
.message-file a:hover { text-decoration: underline; }
.empty-chat { text-align: center; padding: 80px 20px; color: var(--text-muted); margin: auto; }
.empty-chat i { font-size: 64px; margin-bottom: 16px; color: var(--border); }

.input-area { padding: 12px 20px; background: var(--bg-secondary); border-top: 1px solid var(--border); }
.input-area form { display: flex; align-items: flex-end; gap: 10px; }
.btn-attach { padding: 12px; color: var(--text-secondary); cursor: pointer; border-radius: var(--radius-sm); transition: all 0.3s; font-size: 18px; display: flex; align-items: center; }
.btn-attach:hover { color: var(--accent); background: var(--accent-light); }
.input-wrapper { flex: 1; position: relative; }
.file-preview { display: flex; align-items: center; gap: 8px; padding: 6px 12px; background: var(--accent-light); border: 1px solid rgba(108, 92, 231, 0.3); border-radius: var(--radius-sm) var(--radius-sm) 0 0; font-size: 13px; color: var(--accent); }
.file-preview button { background: none; border: none; color: var(--text-secondary); cursor: pointer; padding: 2px; font-size: 14px; }
.file-preview button:hover { color: var(--danger); }
#messageInput { width: 100%; padding: 12px 16px; background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: var(--radius); color: var(--text-primary); font-size: 14px; font-family: inherit; outline: none; resize: none; max-height: 120px; line-height: 1.4; transition: border-color 0.3s; }
#messageInput:focus { border-color: var(--accent); }
#messageInput::placeholder { color: var(--text-muted); }
.btn-send { padding: 12px 16px; background: var(--accent); color: white; border: none; border-radius: var(--radius); cursor: pointer; font-size: 16px; transition: all 0.3s; display: flex; align-items: center; min-height: 44px; }
.btn-send:hover:not(:disabled) { background: var(--accent-hover); transform: scale(1.05); }
.btn-send:disabled { opacity: 0.5; cursor: not-allowed; }

.media-modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0, 0, 0, 0.9); z-index: 1000; align-items: center; justify-content: center; }
.media-modal.active { display: flex; }
.media-modal-content { position: relative; max-width: 90vw; max-height: 90vh; }
.media-modal-content img { max-width: 90vw; max-height: 90vh; object-fit: contain; border-radius: var(--radius); }
.modal-close { position: absolute; top: -40px; right: 0; background: none; border: none; color: white; font-size: 24px; cursor: pointer; padding: 8px; }

@media (max-width: 768px) {
    .sidebar { position: fixed; left: -360px; top: 0; height: 100vh; z-index: 100; transition: left 0.3s; }
    .sidebar.open { left: 0; box-shadow: 10px 0 30px rgba(0, 0, 0, 0.5); }
    .mobile-menu-btn { display: block; }
    .message { max-width: 85%; }
    .chat-header-actions span { display: none; }
    .welcome-features { flex-direction: column; align-items: center; }
    .stats-bar { flex-wrap: wrap; }
}
@media (max-width: 480px) {
    .chat-header-actions { gap: 4px; }
    .btn-action { padding: 8px; }
    .btn-action span { display: none; }
    .message { max-width: 90%; }
}
@keyframes fadeIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
.message { animation: fadeIn 0.3s ease; }
.user-item { animation: fadeIn 0.2s ease; }
'''


def write_templates():
    with open(os.path.join(TEMPLATE_DIR, 'login.html'), 'w', encoding='utf-8') as f:
        f.write(LOGIN_HTML)
    with open(os.path.join(TEMPLATE_DIR, 'dashboard.html'), 'w', encoding='utf-8') as f:
        f.write(DASHBOARD_HTML)
    with open(os.path.join(TEMPLATE_DIR, 'chat.html'), 'w', encoding='utf-8') as f:
        f.write(CHAT_HTML)
    with open(os.path.join(STATIC_DIR, 'style.css'), 'w', encoding='utf-8') as f:
        f.write(STYLE_CSS)


# =============================================
# BOT RUNNER
# =============================================

def run_bot():
    """Run Telegram bot in background thread with its own event loop."""
    global BOT_LOOP
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    BOT_LOOP = loop

    application = ApplicationBuilder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))

    owner_filter = filters.User(user_id=OWNER_ID) & (~filters.COMMAND)
    application.add_handler(MessageHandler(owner_filter, handle_owner_message))
    application.add_handler(MessageHandler(
        (~filters.User(user_id=OWNER_ID)) & (~filters.COMMAND),
        handle_user_message
    ))

    logger.info("🤖 Telegram bot starting...")
    try:
        application.run_polling(
            drop_pending_updates=True,
            close_loop=False,
            stop_signals=None  # important when running in a thread
        )
    except Exception as e:
        logger.exception(f"Bot crashed: {e}")


# =============================================
# MAIN
# =============================================

def main():
    db.init_db()
    write_templates()

    logger.info("=" * 60)
    logger.info("🚀 Starting Telegram Relay Bot")
    logger.info(f"📡 Port: {PORT}")
    logger.info(f"👑 Owner ID: {OWNER_ID}")
    if WEBHOOK_URL:
        logger.info(f"🌐 Public URL: {WEBHOOK_URL}")
    logger.info("=" * 60)

    # Start bot in background thread
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # Give bot a moment to initialize its loop
    import time
    time.sleep(2)

    # Start Flask web server
    socketio.run(
        app,
        host='0.0.0.0',
        port=PORT,
        debug=False,
        allow_unsafe_werkzeug=True
    )


if __name__ == "__main__":
    main()
