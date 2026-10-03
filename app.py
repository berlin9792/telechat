"""
Telegram Relay Bot - Full Featured Messenger
Features: Swipe-to-Reply, Forward, Edit, Delete, Read Receipts, Typing Indicator
"""

import os
import sys
import json
import logging
import asyncio
import threading
from datetime import datetime
from functools import wraps
from io import BytesIO

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, jsonify, flash
)
from flask_socketio import SocketIO
from werkzeug.utils import secure_filename

from telegram import Update, Bot
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder, ContextTypes, MessageHandler, CommandHandler, filters
)

import database as db

# ============= CONFIG =============
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin123")
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me-secret-key-123")
PORT = int(os.environ.get("PORT", "10000"))
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").rstrip("/")
# ===================================

if not BOT_TOKEN or OWNER_ID == 0:
    print("❌ ERROR: BOT_TOKEN and OWNER_ID env variables required!")
    sys.exit(1)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(TEMPLATE_DIR, exist_ok=True)
os.makedirs(STATIC_DIR, exist_ok=True)

app = Flask(__name__, template_folder=TEMPLATE_DIR, static_folder=STATIC_DIR)
app.secret_key = SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading',
                    logger=False, engineio_logger=False)

BOT_LOOP = None


# =============================================
# HELPERS
# =============================================
def get_telegram_file_url(file_path):
    if not file_path:
        return None
    return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"


async def resolve_file_url(bot, file_id):
    try:
        file = await bot.get_file(file_id)
        return get_telegram_file_url(file.file_path)
    except Exception as e:
        logger.error(f"File URL error: {e}")
        return None


def run_async(coro):
    """Submit coroutine to bot loop."""
    global BOT_LOOP
    if BOT_LOOP is None:
        raise RuntimeError("Bot loop not ready")
    future = asyncio.run_coroutine_threadsafe(coro, BOT_LOOP)
    return future.result(timeout=120)


async def extract_media_info(bot, msg):
    """Extract media type and URL from a telegram message."""
    message_type = 'text'
    content = msg.text or msg.caption or ''
    file_id = None
    file_url = None
    file_name = None
    thumbnail_url = None

    if msg.photo:
        message_type = 'photo'
        file_id = msg.photo[-1].file_id
        file_url = await resolve_file_url(bot, file_id)
    elif msg.video:
        message_type = 'video'
        file_id = msg.video.file_id
        file_url = await resolve_file_url(bot, file_id)
        file_name = msg.video.file_name
        if msg.video.thumbnail:
            thumbnail_url = await resolve_file_url(bot, msg.video.thumbnail.file_id)
    elif msg.document:
        message_type = 'document'
        file_id = msg.document.file_id
        file_url = await resolve_file_url(bot, file_id)
        file_name = msg.document.file_name
    elif msg.voice:
        message_type = 'voice'
        file_id = msg.voice.file_id
        file_url = await resolve_file_url(bot, file_id)
    elif msg.audio:
        message_type = 'audio'
        file_id = msg.audio.file_id
        file_url = await resolve_file_url(bot, file_id)
        file_name = msg.audio.file_name or msg.audio.title
    elif msg.video_note:
        message_type = 'video_note'
        file_id = msg.video_note.file_id
        file_url = await resolve_file_url(bot, file_id)
    elif msg.sticker:
        message_type = 'sticker'
        file_id = msg.sticker.file_id
        file_url = await resolve_file_url(bot, file_id)
    elif msg.animation:
        message_type = 'animation'
        file_id = msg.animation.file_id
        file_url = await resolve_file_url(bot, file_id)
    elif msg.contact:
        content = f"📱 {msg.contact.first_name} - {msg.contact.phone_number}"
    elif msg.location:
        content = f"📍 {msg.location.latitude}, {msg.location.longitude}"

    return {
        'message_type': message_type,
        'content': content,
        'file_id': file_id,
        'file_url': file_url,
        'file_name': file_name,
        'thumbnail_url': thumbnail_url
    }


# =============================================
# TELEGRAM BOT HANDLERS
# =============================================

async def handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Incoming message from users."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    msg = update.effective_message

    if db.is_blocked(chat_id):
        try:
            await msg.reply_text("⚠️ You are blocked by admin.")
        except Exception:
            pass
        return

    db.get_or_create_user(chat_id, user.username, user.full_name)

    # Profile photo
    try:
        photos = await context.bot.get_user_profile_photos(chat_id, limit=1)
        if photos.total_count > 0:
            photo = photos.photos[0][-1]
            pp_url = await resolve_file_url(context.bot, photo.file_id)
            if pp_url:
                db.save_user_profile_photo(chat_id, pp_url)
    except Exception:
        pass

    media = await extract_media_info(context.bot, msg)

    # Handle reply
    reply_to_id = None
    reply_to_content = None
    reply_to_type = None
    if msg.reply_to_message:
        reply_tg_id = msg.reply_to_message.message_id
        reply_db_id = db.get_db_msg_from_tg(chat_id, reply_tg_id)
        if reply_db_id:
            replied_msg = db.get_message_by_id(reply_db_id)
            if replied_msg:
                reply_to_id = reply_db_id
                reply_to_content = replied_msg['content']
                reply_to_type = replied_msg['message_type']

    # Handle forward
    forwarded_from = None
    if msg.forward_origin:
        try:
            if hasattr(msg.forward_origin, 'sender_user'):
                forwarded_from = msg.forward_origin.sender_user.full_name
            elif hasattr(msg.forward_origin, 'sender_user_name'):
                forwarded_from = msg.forward_origin.sender_user_name
            elif hasattr(msg.forward_origin, 'chat'):
                forwarded_from = msg.forward_origin.chat.title
        except Exception:
            pass

    msg_id = db.save_message(
        user_id=chat_id,
        direction='incoming',
        message_type=media['message_type'],
        content=media['content'],
        file_id=media['file_id'],
        file_name=media['file_name'],
        file_url=media['file_url'],
        thumbnail_url=media['thumbnail_url'],
        telegram_message_id=msg.message_id,
        reply_to_id=reply_to_id,
        reply_to_content=reply_to_content,
        reply_to_type=reply_to_type,
        forwarded_from=forwarded_from
    )

    # Emit to dashboard
    try:
        socketio.emit('new_message', {
            'user_id': chat_id,
            'username': user.username,
            'full_name': user.full_name,
            'direction': 'incoming',
            'message_type': media['message_type'],
            'content': media['content'],
            'file_url': media['file_url'],
            'file_name': media['file_name'],
            'thumbnail_url': media['thumbnail_url'],
            'reply_to_id': reply_to_id,
            'reply_to_content': reply_to_content,
            'reply_to_type': reply_to_type,
            'forwarded_from': forwarded_from,
            'timestamp': datetime.now().isoformat(),
            'msg_id': msg_id
        })
    except Exception as e:
        logger.error(f"Socket error: {e}")

    # Forward to owner on Telegram
    try:
        copied = await context.bot.copy_message(
            chat_id=OWNER_ID,
            from_chat_id=chat_id,
            message_id=msg.message_id
        )
        info_text = (
            f"📩 *{user.full_name}* "
            f"(@{user.username or 'no_username'})\n"
            f"ID: `{user.id}`\n"
            f"💡 Reply karo direct message pe"
        )
        info_msg = await context.bot.send_message(
            chat_id=OWNER_ID, text=info_text, parse_mode="Markdown"
        )
        db.save_message_map(copied.message_id, chat_id, msg.message_id, msg_id)
        db.save_message_map(info_msg.message_id, chat_id, msg.message_id, msg_id)
    except Exception as e:
        logger.error(f"Owner forward error: {e}")


async def handle_owner_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner replies on Telegram directly."""
    msg = update.effective_message
    if msg.text and msg.text.startswith('/'):
        return

    if not msg.reply_to_message:
        url = WEBHOOK_URL if WEBHOOK_URL else f"http://localhost:{PORT}"
        await msg.reply_text(
            f"ℹ️ User ke message par **Reply** karke jawab bhejo.\n\n"
            f"🌐 Dashboard: {url}"
        )
        return

    replied_id = msg.reply_to_message.message_id
    mapping = db.get_mapped_user(replied_id)

    if not mapping:
        await msg.reply_text("⚠️ Message ka record nahi mila. Dashboard use karein.")
        return

    target_user_id = mapping['user_id']

    if db.is_blocked(target_user_id):
        await msg.reply_text("⚠️ User blocked hai.")
        return

    try:
        # Reply-to info
        reply_to_id = mapping.get('db_msg_id')
        reply_to_content = None
        reply_to_type = None
        if reply_to_id:
            orig = db.get_message_by_id(reply_to_id)
            if orig:
                reply_to_content = orig['content']
                reply_to_type = orig['message_type']

        # Send to user with reply
        sent = await context.bot.copy_message(
            chat_id=target_user_id,
            from_chat_id=OWNER_ID,
            message_id=msg.message_id,
            reply_to_message_id=mapping.get('user_msg_id') if reply_to_id else None,
            allow_sending_without_reply=True
        )

        media = await extract_media_info(context.bot, msg)

        msg_db_id = db.save_message(
            user_id=target_user_id,
            direction='outgoing',
            message_type=media['message_type'],
            content=media['content'],
            file_id=media['file_id'],
            file_name=media['file_name'],
            file_url=media['file_url'],
            thumbnail_url=media['thumbnail_url'],
            telegram_message_id=sent.message_id,
            reply_to_id=reply_to_id,
            reply_to_content=reply_to_content,
            reply_to_type=reply_to_type
        )

        socketio.emit('new_message', {
            'user_id': target_user_id,
            'direction': 'outgoing',
            'message_type': media['message_type'],
            'content': media['content'],
            'file_url': media['file_url'],
            'file_name': media['file_name'],
            'thumbnail_url': media['thumbnail_url'],
            'reply_to_id': reply_to_id,
            'reply_to_content': reply_to_content,
            'reply_to_type': reply_to_type,
            'timestamp': datetime.now().isoformat(),
            'msg_id': msg_db_id
        })

        await msg.set_reaction("👍")
    except Exception as e:
        logger.error(f"Reply error: {e}")
        await msg.reply_text(f"❌ Error: {e}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id == OWNER_ID:
        url = WEBHOOK_URL if WEBHOOK_URL else f"http://localhost:{PORT}"
        await update.message.reply_text(
            f"👑 Welcome Admin!\n\n🌐 Dashboard: {url}"
        )
    else:
        db.get_or_create_user(user.id, user.username, user.full_name)
        await update.message.reply_text(
            "👋 Welcome! Send any message and admin will respond."
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
    return jsonify({'status': 'ok'})


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        if request.form.get('password', '') == ADMIN_PASSWORD:
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


@app.route('/api/send_message', methods=['POST'])
@login_required
def api_send_message():
    try:
        user_id = int(request.form.get('user_id'))
        content = request.form.get('content', '').strip()
        reply_to_id = request.form.get('reply_to_id')
        file = request.files.get('file')

        if not content and not (file and file.filename):
            return jsonify({'status': 'error', 'message': 'Empty'}), 400

        reply_to_id = int(reply_to_id) if reply_to_id and reply_to_id != 'null' else None
        reply_tg_id = None
        reply_to_content = None
        reply_to_type = None

        if reply_to_id:
            orig = db.get_message_by_id(reply_to_id)
            if orig:
                reply_tg_id = orig.get('telegram_message_id')
                reply_to_content = orig['content']
                reply_to_type = orig['message_type']

        async def do_send():
            bot = Bot(token=BOT_TOKEN)
            async with bot:
                kwargs = {}
                if reply_tg_id:
                    kwargs['reply_to_message_id'] = reply_tg_id
                    kwargs['allow_sending_without_reply'] = True

                if file and file.filename:
                    filename = secure_filename(file.filename)
                    ext = os.path.splitext(filename)[1].lower()
                    file_bytes = file.read()
                    bio = BytesIO(file_bytes)
                    bio.name = filename

                    if ext in ('.jpg', '.jpeg', '.png', '.webp'):
                        sent = await bot.send_photo(
                            chat_id=user_id, photo=bio,
                            caption=content if content else None,
                            **kwargs
                        )
                        return 'photo', sent
                    elif ext in ('.mp4', '.mov', '.avi', '.mkv'):
                        sent = await bot.send_video(
                            chat_id=user_id, video=bio,
                            caption=content if content else None,
                            **kwargs
                        )
                        return 'video', sent
                    elif ext in ('.mp3', '.ogg', '.wav', '.m4a', '.flac'):
                        sent = await bot.send_audio(
                            chat_id=user_id, audio=bio,
                            caption=content if content else None,
                            **kwargs
                        )
                        return 'audio', sent
                    elif ext == '.gif':
                        sent = await bot.send_animation(
                            chat_id=user_id, animation=bio,
                            caption=content if content else None,
                            **kwargs
                        )
                        return 'animation', sent
                    else:
                        sent = await bot.send_document(
                            chat_id=user_id, document=bio,
                            caption=content if content else None,
                            filename=filename, **kwargs
                        )
                        return 'document', sent
                else:
                    sent = await bot.send_message(
                        chat_id=user_id, text=content, **kwargs
                    )
                    return 'text', sent

        msg_type, sent_msg = run_async(do_send())

        file_url = None
        file_name = None
        if file and file.filename:
            file_name = secure_filename(file.filename)
            try:
                async def get_url():
                    bot = Bot(token=BOT_TOKEN)
                    async with bot:
                        if msg_type == 'photo' and sent_msg.photo:
                            return await resolve_file_url(bot, sent_msg.photo[-1].file_id)
                        elif msg_type == 'video' and sent_msg.video:
                            return await resolve_file_url(bot, sent_msg.video.file_id)
                        elif msg_type == 'audio' and sent_msg.audio:
                            return await resolve_file_url(bot, sent_msg.audio.file_id)
                        elif msg_type == 'document' and sent_msg.document:
                            return await resolve_file_url(bot, sent_msg.document.file_id)
                        elif msg_type == 'animation' and sent_msg.animation:
                            return await resolve_file_url(bot, sent_msg.animation.file_id)
                        return None
                file_url = run_async(get_url())
            except Exception as e:
                logger.error(f"URL error: {e}")

        msg_id = db.save_message(
            user_id=user_id,
            direction='outgoing',
            message_type=msg_type,
            content=content,
            file_name=file_name,
            file_url=file_url,
            telegram_message_id=sent_msg.message_id,
            reply_to_id=reply_to_id,
            reply_to_content=reply_to_content,
            reply_to_type=reply_to_type
        )

        socketio.emit('new_message', {
            'user_id': user_id,
            'direction': 'outgoing',
            'message_type': msg_type,
            'content': content,
            'file_url': file_url,
            'file_name': file_name,
            'reply_to_id': reply_to_id,
            'reply_to_content': reply_to_content,
            'reply_to_type': reply_to_type,
            'timestamp': datetime.now().isoformat(),
            'msg_id': msg_id
        })

        return jsonify({'status': 'ok', 'msg_id': msg_id})

    except Exception as e:
        logger.exception("Send error")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/forward_message', methods=['POST'])
@login_required
def api_forward_message():
    try:
        msg_id = int(request.json.get('msg_id'))
        target_user_id = int(request.json.get('target_user_id'))
        orig_msg = db.get_message_by_id(msg_id)
        if not orig_msg:
            return jsonify({'status': 'error', 'message': 'Not found'}), 404

        async def do_forward():
            bot = Bot(token=BOT_TOKEN)
            async with bot:
                if orig_msg.get('telegram_message_id') and orig_msg['direction'] == 'incoming':
                    sent = await bot.forward_message(
                        chat_id=target_user_id,
                        from_chat_id=orig_msg['user_id'],
                        message_id=orig_msg['telegram_message_id']
                    )
                    return sent
                else:
                    # Resend as new
                    if orig_msg['message_type'] == 'text':
                        return await bot.send_message(
                            chat_id=target_user_id,
                            text=orig_msg['content']
                        )
                    elif orig_msg['file_id']:
                        if orig_msg['message_type'] == 'photo':
                            return await bot.send_photo(
                                chat_id=target_user_id, photo=orig_msg['file_id'],
                                caption=orig_msg['content']
                            )
                        elif orig_msg['message_type'] == 'video':
                            return await bot.send_video(
                                chat_id=target_user_id, video=orig_msg['file_id'],
                                caption=orig_msg['content']
                            )
                        elif orig_msg['message_type'] == 'document':
                            return await bot.send_document(
                                chat_id=target_user_id, document=orig_msg['file_id'],
                                caption=orig_msg['content']
                            )
                return None

        sent = run_async(do_forward())
        if not sent:
            return jsonify({'status': 'error', 'message': 'Forward failed'}), 500

        new_msg_id = db.save_message(
            user_id=target_user_id,
            direction='outgoing',
            message_type=orig_msg['message_type'],
            content=orig_msg['content'],
            file_url=orig_msg['file_url'],
            file_name=orig_msg['file_name'],
            telegram_message_id=sent.message_id,
            forwarded_from='Forwarded'
        )

        socketio.emit('new_message', {
            'user_id': target_user_id,
            'direction': 'outgoing',
            'message_type': orig_msg['message_type'],
            'content': orig_msg['content'],
            'file_url': orig_msg['file_url'],
            'file_name': orig_msg['file_name'],
            'forwarded_from': 'Forwarded',
            'timestamp': datetime.now().isoformat(),
            'msg_id': new_msg_id
        })

        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.exception("Forward error")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/edit_message', methods=['POST'])
@login_required
def api_edit_message():
    try:
        msg_id = int(request.json.get('msg_id'))
        new_content = request.json.get('content', '').strip()
        orig = db.get_message_by_id(msg_id)
        if not orig or orig['direction'] != 'outgoing':
            return jsonify({'status': 'error', 'message': 'Cannot edit'}), 400

        async def do_edit():
            bot = Bot(token=BOT_TOKEN)
            async with bot:
                try:
                    if orig['message_type'] == 'text':
                        await bot.edit_message_text(
                            chat_id=orig['user_id'],
                            message_id=orig['telegram_message_id'],
                            text=new_content
                        )
                    else:
                        await bot.edit_message_caption(
                            chat_id=orig['user_id'],
                            message_id=orig['telegram_message_id'],
                            caption=new_content
                        )
                except Exception as e:
                    logger.error(f"TG edit error: {e}")

        run_async(do_edit())
        db.edit_message(msg_id, new_content)

        socketio.emit('message_edited', {
            'user_id': orig['user_id'],
            'msg_id': msg_id,
            'new_content': new_content
        })

        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.exception("Edit error")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/delete_message', methods=['POST'])
@login_required
def api_delete_message():
    try:
        msg_id = int(request.json.get('msg_id'))
        orig = db.get_message_by_id(msg_id)
        if not orig:
            return jsonify({'status': 'error', 'message': 'Not found'}), 404

        if orig['direction'] == 'outgoing' and orig.get('telegram_message_id'):
            async def do_delete():
                bot = Bot(token=BOT_TOKEN)
                async with bot:
                    try:
                        await bot.delete_message(
                            chat_id=orig['user_id'],
                            message_id=orig['telegram_message_id']
                        )
                    except Exception as e:
                        logger.error(f"TG delete error: {e}")
            run_async(do_delete())

        db.delete_message(msg_id)

        socketio.emit('message_deleted', {
            'user_id': orig['user_id'],
            'msg_id': msg_id
        })
        return jsonify({'status': 'ok'})
    except Exception as e:
        logger.exception("Delete error")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/typing', methods=['POST'])
@login_required
def api_typing():
    try:
        user_id = int(request.json.get('user_id'))

        async def send_typing():
            bot = Bot(token=BOT_TOKEN)
            async with bot:
                await bot.send_chat_action(chat_id=user_id, action=ChatAction.TYPING)

        run_async(send_typing())
        return jsonify({'status': 'ok'})
    except Exception as e:
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
    return jsonify(db.search_users(request.args.get('q', '')))


@app.route('/api/users')
@login_required
def api_users():
    return jsonify(db.get_all_users())


@app.route('/api/message/<int:msg_id>')
@login_required
def api_message(msg_id):
    msg = db.get_message_by_id(msg_id)
    return jsonify(msg) if msg else ('Not found', 404)


# =============================================
# TEMPLATES
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
        <p class="login-subtitle">Enter admin password</p>
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
            <a href="{{ url_for('logout') }}" class="logout-btn"><i class="fas fa-sign-out-alt"></i></a>
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
                    {% if user.profile_photo %}<img src="{{ user.profile_photo }}" onerror="this.style.display='none'">
                    {% else %}<div class="avatar-placeholder">{{ (user.full_name or '?')[0] }}</div>{% endif %}
                    {% if user.unread_count > 0 %}<span class="unread-badge">{{ user.unread_count }}</span>{% endif %}
                </div>
                <div class="user-info">
                    <div class="user-name">{{ user.full_name or 'Unknown' }}{% if user.is_blocked %} <i class="fas fa-ban blocked-icon"></i>{% endif %}</div>
                    <div class="user-last-msg">
                        {% if user.last_direction == 'outgoing' %}<i class="fas fa-check"></i> {% endif %}
                        {% if user.last_type and user.last_type != 'text' %}<i class="fas fa-paperclip"></i> {{ user.last_type }}
                        {% elif user.last_content %}{{ user.last_content[:40] }}
                        {% else %}No messages{% endif %}
                    </div>
                </div>
            </a>
            {% endfor %}
            {% if not users %}
            <div class="empty-state"><i class="fas fa-inbox"></i><p>No users yet</p></div>
            {% endif %}
        </div>
    </div>
    <div class="main-content">
        <div class="welcome-screen">
            <div class="welcome-icon"><i class="fab fa-telegram"></i></div>
            <h2>Welcome to Relay Bot</h2>
            <p>Select a user to start chatting</p>
            <div class="welcome-features">
                <div class="feature"><i class="fas fa-reply"></i><span>Swipe to Reply</span></div>
                <div class="feature"><i class="fas fa-share"></i><span>Forward Messages</span></div>
                <div class="feature"><i class="fas fa-edit"></i><span>Edit & Delete</span></div>
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
        .then(r => r.json()).then(renderUsers);
}
function renderUsers(users) {
    const list = document.getElementById('userList');
    list.innerHTML = '';
    users.forEach(u => {
        const initial = (u.full_name || '?')[0];
        const blocked = u.is_blocked ? 'blocked' : '';
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
                    <div class="user-name">${u.full_name || 'Unknown'}</div>
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
            <input type="text" id="searchInput" placeholder="Search..." oninput="searchUsers(this.value)">
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
                    <div class="chat-header-meta" id="userStatus">@{{ user.username or 'no_username' }} · ID: {{ user.user_id }}</div>
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
            <div class="message {{ msg.direction }}" data-msg-id="{{ msg.id }}" data-content="{{ msg.content|e }}" data-type="{{ msg.message_type }}">
                <div class="message-bubble">
                    {% if msg.forwarded_from %}
                    <div class="forwarded-tag"><i class="fas fa-share"></i> Forwarded from {{ msg.forwarded_from }}</div>
                    {% endif %}
                    {% if msg.reply_to_id %}
                    <div class="reply-preview" onclick="scrollToMsg({{ msg.reply_to_id }})">
                        <div class="reply-bar"></div>
                        <div class="reply-content">
                            <div class="reply-sender">Reply</div>
                            <div class="reply-text">
                                {% if msg.reply_to_type and msg.reply_to_type != 'text' %}
                                <i class="fas fa-paperclip"></i> {{ msg.reply_to_type }}
                                {% else %}{{ msg.reply_to_content[:60] if msg.reply_to_content else '...' }}{% endif %}
                            </div>
                        </div>
                    </div>
                    {% endif %}
                    {% if msg.message_type == 'photo' and msg.file_url %}
                    <div class="message-media"><img src="{{ msg.file_url }}" onclick="openMedia(this.src)" loading="lazy"></div>
                    {% endif %}
                    {% if msg.message_type == 'video' and msg.file_url %}
                    <div class="message-media"><video controls preload="metadata"><source src="{{ msg.file_url }}"></video></div>
                    {% endif %}
                    {% if msg.message_type == 'audio' and msg.file_url %}
                    <div class="message-media"><audio controls><source src="{{ msg.file_url }}"></audio></div>
                    {% endif %}
                    {% if msg.message_type == 'voice' and msg.file_url %}
                    <div class="message-media voice-msg"><i class="fas fa-microphone"></i><audio controls><source src="{{ msg.file_url }}"></audio></div>
                    {% endif %}
                    {% if msg.message_type == 'document' and msg.file_url %}
                    <div class="message-file"><i class="fas fa-file"></i><a href="{{ msg.file_url }}" target="_blank" download>{{ msg.file_name or 'Download' }}</a></div>
                    {% endif %}
                    {% if msg.message_type == 'sticker' and msg.file_url %}
                    <div class="message-sticker"><img src="{{ msg.file_url }}"></div>
                    {% endif %}
                    {% if msg.message_type == 'animation' and msg.file_url %}
                    <div class="message-media"><video autoplay loop muted playsinline><source src="{{ msg.file_url }}"></video></div>
                    {% endif %}
                    {% if msg.message_type == 'video_note' and msg.file_url %}
                    <div class="message-media video-note"><video controls><source src="{{ msg.file_url }}"></video></div>
                    {% endif %}
                    {% if msg.content %}<div class="message-text">{{ msg.content }}</div>{% endif %}
                    <div class="message-time">
                        {% if msg.is_edited %}<span class="edited-tag">edited</span>{% endif %}
                        {{ msg.timestamp[11:16] if msg.timestamp else '' }}
                        {% if msg.direction == 'outgoing' %}<i class="fas fa-check-double"></i>{% endif %}
                    </div>
                </div>
            </div>
            {% endfor %}
            {% if not messages %}
            <div class="empty-chat"><i class="far fa-comments"></i><p>No messages yet</p></div>
            {% endif %}
        </div>
        
        <!-- Reply/Edit Preview -->
        <div class="reply-bar-container" id="replyBar" style="display:none">
            <div class="reply-bar-content">
                <i class="fas fa-reply reply-icon"></i>
                <div class="reply-bar-info">
                    <div class="reply-bar-title" id="replyTitle">Replying to</div>
                    <div class="reply-bar-text" id="replyText"></div>
                </div>
                <button class="reply-close" onclick="cancelReply()"><i class="fas fa-times"></i></button>
            </div>
        </div>

        <div class="input-area" {% if user.is_blocked %}style="opacity:0.5"{% endif %}>
            <form id="messageForm" onsubmit="sendMessage(event)" enctype="multipart/form-data">
                <input type="hidden" name="user_id" value="{{ user.user_id }}">
                <input type="hidden" name="reply_to_id" id="replyToId" value="">
                <label for="fileInput" class="btn-attach"><i class="fas fa-paperclip"></i></label>
                <input type="file" id="fileInput" name="file" accept="*/*" onchange="fileSelected(this)" style="display:none">
                <div class="input-wrapper">
                    <div id="filePreview" class="file-preview" style="display:none">
                        <span id="fileName"></span>
                        <button type="button" onclick="removeFile()"><i class="fas fa-times"></i></button>
                    </div>
                    <textarea id="messageInput" name="content" placeholder="Message..." rows="1"
                              onkeydown="handleKeyDown(event)" oninput="handleInput(this)"
                              {% if user.is_blocked %}disabled{% endif %}></textarea>
                </div>
                <button type="submit" class="btn-send" {% if user.is_blocked %}disabled{% endif %}>
                    <i class="fas fa-paper-plane"></i>
                </button>
            </form>
        </div>
    </div>
</div>

<!-- Media Modal -->
<div class="media-modal" id="mediaModal" onclick="closeMedia()">
    <div class="media-modal-content">
        <img id="modalImage" src="">
        <button class="modal-close" onclick="closeMedia()"><i class="fas fa-times"></i></button>
    </div>
</div>

<!-- Context Menu -->
<div class="context-menu" id="contextMenu">
    <div class="context-item" onclick="replyToSelected()"><i class="fas fa-reply"></i> Reply</div>
    <div class="context-item" onclick="copySelected()"><i class="fas fa-copy"></i> Copy</div>
    <div class="context-item" onclick="forwardSelected()"><i class="fas fa-share"></i> Forward</div>
    <div class="context-item outgoing-only" onclick="editSelected()"><i class="fas fa-edit"></i> Edit</div>
    <div class="context-item danger" onclick="deleteSelected()"><i class="fas fa-trash"></i> Delete</div>
</div>

<!-- Forward Modal -->
<div class="forward-modal" id="forwardModal">
    <div class="forward-content">
        <div class="forward-header">
            <h3><i class="fas fa-share"></i> Forward to</h3>
            <button onclick="closeForward()"><i class="fas fa-times"></i></button>
        </div>
        <div class="forward-search">
            <input type="text" id="forwardSearch" placeholder="Search user..." oninput="filterForwardList(this.value)">
        </div>
        <div class="forward-list" id="forwardList"></div>
    </div>
</div>

<!-- Edit Modal -->
<div class="edit-modal" id="editModal">
    <div class="edit-content">
        <div class="edit-header">
            <h3><i class="fas fa-edit"></i> Edit Message</h3>
            <button onclick="closeEdit()"><i class="fas fa-times"></i></button>
        </div>
        <textarea id="editInput" placeholder="Message..."></textarea>
        <div class="edit-actions">
            <button class="btn-cancel" onclick="closeEdit()">Cancel</button>
            <button class="btn-save" onclick="saveEdit()">Save</button>
        </div>
    </div>
</div>

<script>
const currentUserId = {{ user.user_id }};
const socket = io();
let selectedMsgId = null;
let selectedMsgEl = null;
let replyToMsgId = null;
let typingTimer = null;

function scrollToBottom(smooth = false) {
    const c = document.getElementById('messagesContainer');
    c.scrollTo({ top: c.scrollHeight, behavior: smooth ? 'smooth' : 'auto' });
}
scrollToBottom();

// Socket events
socket.on('new_message', function(data) {
    if (data.user_id === currentUserId) location.reload();
});
socket.on('message_edited', function(data) {
    if (data.user_id === currentUserId) {
        const el = document.querySelector(`[data-msg-id="${data.msg_id}"] .message-text`);
        if (el) el.textContent = data.new_content;
    }
});
socket.on('message_deleted', function(data) {
    if (data.user_id === currentUserId) {
        const el = document.querySelector(`[data-msg-id="${data.msg_id}"]`);
        if (el) el.remove();
    }
});

// Send message
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
                input.value = '';
                fileInput.value = '';
                removeFile();
                cancelReply();
                autoResize(input);
                location.reload();
            } else alert('Error: ' + data.message);
        })
        .catch(err => alert('Error: ' + err))
        .finally(() => {
            btn.disabled = false;
            btn.innerHTML = '<i class="fas fa-paper-plane"></i>';
        });
}

// File
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

// Keyboard
function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage(e);
    } else if (e.key === 'Escape') {
        cancelReply();
    }
}
function handleInput(el) {
    autoResize(el);
    // Typing indicator
    clearTimeout(typingTimer);
    fetch('/api/typing', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ user_id: currentUserId })
    });
    typingTimer = setTimeout(() => {}, 3000);
}
function autoResize(el) {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 120) + 'px';
}

// Media modal
function openMedia(src) {
    document.getElementById('modalImage').src = src;
    document.getElementById('mediaModal').classList.add('active');
}
function closeMedia() { document.getElementById('mediaModal').classList.remove('active'); }

// Block/Delete
function blockUser(id) {
    if (!confirm('Block this user?')) return;
    fetch('/api/block/' + id, { method: 'POST' }).then(() => location.reload());
}
function unblockUser(id) {
    if (!confirm('Unblock?')) return;
    fetch('/api/unblock/' + id, { method: 'POST' }).then(() => location.reload());
}
function deleteChat(id) {
    if (!confirm('Delete all messages?')) return;
    fetch('/api/delete_chat/' + id, { method: 'POST' }).then(() => location.href = '/');
}
function toggleSidebar() { document.getElementById('sidebar').classList.toggle('open'); }

// Search
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

// =========== SWIPE TO REPLY ===========
let touchStartX = 0, touchStartY = 0, swipingEl = null;
document.querySelectorAll('.message').forEach(msg => {
    msg.addEventListener('touchstart', e => {
        touchStartX = e.touches[0].clientX;
        touchStartY = e.touches[0].clientY;
        swipingEl = msg;
    }, { passive: true });
    
    msg.addEventListener('touchmove', e => {
        if (!swipingEl) return;
        const dx = e.touches[0].clientX - touchStartX;
        const dy = Math.abs(e.touches[0].clientY - touchStartY);
        if (dy > 30) { swipingEl = null; return; }
        if (dx > 10 && dx < 100) {
            swipingEl.style.transform = `translateX(${dx}px)`;
            swipingEl.style.transition = 'none';
        }
    }, { passive: true });
    
    msg.addEventListener('touchend', e => {
        if (!swipingEl) return;
        const dx = e.changedTouches[0].clientX - touchStartX;
        swipingEl.style.transition = 'transform 0.3s';
        swipingEl.style.transform = 'translateX(0)';
        if (dx > 60) {
            selectedMsgId = parseInt(swipingEl.dataset.msgId);
            selectedMsgEl = swipingEl;
            replyToSelected();
        }
        swipingEl = null;
    }, { passive: true });
});

// =========== DESKTOP: Right-click & Hover Actions ===========
document.querySelectorAll('.message').forEach(msg => {
    msg.addEventListener('contextmenu', e => {
        e.preventDefault();
        selectedMsgId = parseInt(msg.dataset.msgId);
        selectedMsgEl = msg;
        showContextMenu(e.clientX, e.clientY, msg.classList.contains('outgoing'));
    });
    
    // Double-click to reply
    msg.addEventListener('dblclick', e => {
        selectedMsgId = parseInt(msg.dataset.msgId);
        selectedMsgEl = msg;
        replyToSelected();
    });
});

function showContextMenu(x, y, isOutgoing) {
    const menu = document.getElementById('contextMenu');
    menu.style.display = 'block';
    menu.querySelectorAll('.outgoing-only').forEach(el => {
        el.style.display = isOutgoing ? 'flex' : 'none';
    });
    // Position menu
    const rect = menu.getBoundingClientRect();
    const maxX = window.innerWidth - rect.width - 10;
    const maxY = window.innerHeight - rect.height - 10;
    menu.style.left = Math.min(x, maxX) + 'px';
    menu.style.top = Math.min(y, maxY) + 'px';
}
document.addEventListener('click', () => {
    document.getElementById('contextMenu').style.display = 'none';
});

// =========== REPLY ===========
function replyToSelected() {
    if (!selectedMsgId || !selectedMsgEl) return;
    const content = selectedMsgEl.dataset.content;
    const type = selectedMsgEl.dataset.type;
    replyToMsgId = selectedMsgId;
    document.getElementById('replyToId').value = selectedMsgId;
    document.getElementById('replyBar').style.display = 'block';
    document.getElementById('replyTitle').textContent = selectedMsgEl.classList.contains('outgoing') ? 'Replying to You' : 'Replying to User';
    document.getElementById('replyText').textContent = (type !== 'text') ? `📎 ${type}` : (content.substring(0, 80) || '...');
    document.getElementById('messageInput').focus();
}
function cancelReply() {
    replyToMsgId = null;
    document.getElementById('replyToId').value = '';
    document.getElementById('replyBar').style.display = 'none';
}
function scrollToMsg(msgId) {
    const el = document.querySelector(`[data-msg-id="${msgId}"]`);
    if (el) {
        el.scrollIntoView({ behavior: 'smooth', block: 'center' });
        el.classList.add('highlight');
        setTimeout(() => el.classList.remove('highlight'), 1500);
    }
}

// =========== COPY ===========
function copySelected() {
    if (!selectedMsgEl) return;
    const text = selectedMsgEl.dataset.content;
    if (text) {
        navigator.clipboard.writeText(text).then(() => {
            showToast('Copied!');
        });
    }
}

// =========== FORWARD ===========
function forwardSelected() {
    if (!selectedMsgId) return;
    const modal = document.getElementById('forwardModal');
    modal.classList.add('active');
    fetch('/api/users').then(r => r.json()).then(users => {
        const list = document.getElementById('forwardList');
        list.innerHTML = '';
        users.forEach(u => {
            if (u.is_blocked) return;
            const initial = (u.full_name || '?')[0];
            const avatar = u.profile_photo
                ? `<img src="${u.profile_photo}" onerror="this.style.display='none'">`
                : `<div class="avatar-placeholder">${initial}</div>`;
            list.innerHTML += `
                <div class="forward-item" onclick="doForward(${u.user_id})" data-name="${u.full_name || ''}">
                    <div class="user-avatar small">${avatar}</div>
                    <div class="forward-name">${u.full_name || 'Unknown'}</div>
                </div>`;
        });
    });
}
function filterForwardList(q) {
    document.querySelectorAll('.forward-item').forEach(el => {
        el.style.display = el.dataset.name.toLowerCase().includes(q.toLowerCase()) ? 'flex' : 'none';
    });
}
function closeForward() { document.getElementById('forwardModal').classList.remove('active'); }
function doForward(targetId) {
    fetch('/api/forward_message', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ msg_id: selectedMsgId, target_user_id: targetId })
    })
    .then(r => r.json())
    .then(data => {
        closeForward();
        if (data.status === 'ok') showToast('Forwarded!');
        else alert('Error: ' + data.message);
    });
}

// =========== EDIT ===========
function editSelected() {
    if (!selectedMsgEl || !selectedMsgEl.classList.contains('outgoing')) return;
    document.getElementById('editModal').classList.add('active');
    document.getElementById('editInput').value = selectedMsgEl.dataset.content;
    document.getElementById('editInput').focus();
}
function closeEdit() { document.getElementById('editModal').classList.remove('active'); }
function saveEdit() {
    const newContent = document.getElementById('editInput').value.trim();
    if (!newContent) return;
    fetch('/api/edit_message', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ msg_id: selectedMsgId, content: newContent })
    })
    .then(r => r.json())
    .then(data => {
        closeEdit();
        if (data.status === 'ok') showToast('Edited!');
        else alert('Error: ' + data.message);
    });
}

// =========== DELETE ===========
function deleteSelected() {
    if (!selectedMsgId) return;
    if (!confirm('Delete this message?')) return;
    fetch('/api/delete_message', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ msg_id: selectedMsgId })
    })
    .then(r => r.json())
    .then(data => {
        if (data.status === 'ok') showToast('Deleted!');
        else alert('Error: ' + data.message);
    });
}

// =========== TOAST ===========
function showToast(msg) {
    const t = document.createElement('div');
    t.className = 'toast';
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => t.classList.add('show'), 10);
    setTimeout(() => {
        t.classList.remove('show');
        setTimeout(() => t.remove(), 300);
    }, 2000);
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
    --danger: #e74c3c; --success: #2ecc71; --warning: #f39c12;
    --shadow: 0 4px 20px rgba(0, 0, 0, 0.4);
    --radius: 12px; --radius-sm: 8px;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body { font-family: 'Inter', -apple-system, sans-serif; background: var(--bg-primary); color: var(--text-primary); height: 100vh; overflow: hidden; -webkit-tap-highlight-color: transparent; }
a { color: inherit; text-decoration: none; }
::-webkit-scrollbar { width: 6px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px; }
::-webkit-scrollbar-thumb:hover { background: var(--text-muted); }

/* LOGIN */
.login-body { display: flex; align-items: center; justify-content: center; min-height: 100vh; background: linear-gradient(135deg, #0a0a1a 0%, #1a1030 50%, #0a0a1a 100%); }
.login-container { width: 100%; max-width: 420px; padding: 20px; }
.login-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 20px; padding: 50px 40px; text-align: center; box-shadow: var(--shadow); }
.login-icon { font-size: 64px; color: var(--accent); margin-bottom: 20px; }
.login-card h1 { font-size: 24px; font-weight: 700; margin-bottom: 8px; }
.login-subtitle { color: var(--text-secondary); margin-bottom: 30px; font-size: 14px; }
.input-group { position: relative; margin-bottom: 20px; }
.input-group i { position: absolute; left: 16px; top: 50%; transform: translateY(-50%); color: var(--text-muted); }
.input-group input { width: 100%; padding: 14px 16px 14px 46px; background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: var(--radius); color: var(--text-primary); font-size: 15px; outline: none; }
.input-group input:focus { border-color: var(--accent); }
.btn-login { width: 100%; padding: 14px; background: var(--accent); color: white; border: none; border-radius: var(--radius); font-size: 16px; font-weight: 600; cursor: pointer; }
.btn-login:hover { background: var(--accent-hover); }
.alert { padding: 10px; border-radius: var(--radius-sm); margin-bottom: 16px; font-size: 13px; }
.alert-error { background: rgba(231, 76, 60, 0.15); color: var(--danger); border: 1px solid rgba(231, 76, 60, 0.3); }

/* APP */
.app-container { display: flex; height: 100vh; overflow: hidden; }
.sidebar { width: 360px; min-width: 360px; background: var(--bg-secondary); border-right: 1px solid var(--border); display: flex; flex-direction: column; height: 100vh; }
.sidebar-header { padding: 16px 20px; display: flex; align-items: center; justify-content: space-between; border-bottom: 1px solid var(--border); }
.logo { display: flex; align-items: center; gap: 10px; font-size: 20px; font-weight: 700; }
.logo i { font-size: 28px; color: var(--accent); }
.logout-btn { padding: 8px 12px; border-radius: var(--radius-sm); color: var(--text-secondary); }
.logout-btn:hover { background: var(--bg-tertiary); color: var(--danger); }

.stats-bar { display: flex; padding: 12px 16px; gap: 8px; border-bottom: 1px solid var(--border); }
.stat-item { flex: 1; text-align: center; padding: 8px 4px; background: var(--bg-tertiary); border-radius: var(--radius-sm); }
.stat-num { display: block; font-size: 18px; font-weight: 700; color: var(--accent); }
.stat-label { font-size: 10px; color: var(--text-muted); text-transform: uppercase; }

.search-box { padding: 12px 16px; position: relative; border-bottom: 1px solid var(--border); }
.search-box i { position: absolute; left: 28px; top: 50%; transform: translateY(-50%); color: var(--text-muted); font-size: 14px; }
.search-box input { width: 100%; padding: 10px 12px 10px 38px; background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-primary); font-size: 14px; outline: none; }
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
.welcome-features { display: flex; gap: 30px; justify-content: center; flex-wrap: wrap; }
.feature { display: flex; flex-direction: column; align-items: center; gap: 10px; padding: 20px 24px; background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius); }
.feature i { font-size: 24px; color: var(--accent); }
.feature span { font-size: 13px; color: var(--text-secondary); }

/* CHAT */
.chat-area { flex: 1; display: flex; flex-direction: column; height: 100vh; background: var(--bg-primary); }
.chat-header { padding: 12px 20px; background: var(--bg-secondary); border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 12px; }
.mobile-menu-btn { display: none; background: none; border: none; color: var(--text-primary); font-size: 20px; cursor: pointer; padding: 8px; }
.chat-header-user { display: flex; align-items: center; gap: 12px; flex: 1; }
.chat-header-name { font-size: 16px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
.badge-blocked { font-size: 10px; padding: 2px 8px; background: rgba(231, 76, 60, 0.2); color: var(--danger); border-radius: 4px; font-weight: 700; }
.chat-header-meta { font-size: 12px; color: var(--text-secondary); }
.chat-header-actions { display: flex; gap: 8px; }
.btn-action { padding: 8px 14px; border: 1px solid var(--border); border-radius: var(--radius-sm); background: transparent; color: var(--text-secondary); cursor: pointer; font-size: 13px; display: flex; align-items: center; gap: 6px; }
.btn-action:hover { background: var(--bg-tertiary); }
.btn-block:hover { border-color: var(--danger); color: var(--danger); }
.btn-unblock:hover { border-color: var(--success); color: var(--success); }
.btn-delete:hover { border-color: var(--danger); color: var(--danger); }

/* MESSAGES */
.messages-container { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 6px; background: radial-gradient(ellipse at top left, rgba(108, 92, 231, 0.03), transparent 50%), radial-gradient(ellipse at bottom right, rgba(168, 85, 247, 0.03), transparent 50%), var(--bg-primary); }
.message { display: flex; max-width: 70%; transition: transform 0.1s, background 0.3s; position: relative; cursor: pointer; }
.message.incoming { align-self: flex-start; }
.message.outgoing { align-self: flex-end; }
.message.highlight .message-bubble { animation: highlight 1.5s ease; }
@keyframes highlight {
    0%, 100% { background: inherit; }
    50% { background: var(--accent-light); }
}

.message-bubble { padding: 10px 14px; border-radius: 16px; max-width: 100%; word-wrap: break-word; position: relative; }
.incoming .message-bubble { background: var(--incoming-bg); border-bottom-left-radius: 4px; border: 1px solid var(--border); }
.outgoing .message-bubble { background: var(--outgoing-bg); border-bottom-right-radius: 4px; border: 1px solid rgba(108, 92, 231, 0.3); }
.message-text { font-size: 14px; line-height: 1.5; white-space: pre-wrap; }
.message-time { font-size: 11px; color: var(--text-muted); margin-top: 4px; text-align: right; display: flex; align-items: center; justify-content: flex-end; gap: 4px; }
.outgoing .message-time i { color: var(--accent); font-size: 12px; }
.edited-tag { font-style: italic; opacity: 0.7; margin-right: 4px; }

/* Forwarded tag */
.forwarded-tag { font-size: 12px; color: var(--accent); margin-bottom: 6px; font-weight: 500; display: flex; align-items: center; gap: 4px; padding: 4px 8px; background: var(--accent-light); border-radius: 6px; }

/* Reply preview inside message */
.reply-preview { display: flex; align-items: stretch; gap: 8px; padding: 6px 10px; background: rgba(255,255,255,0.05); border-radius: 6px; margin-bottom: 6px; cursor: pointer; min-height: 40px; }
.reply-preview:hover { background: rgba(255,255,255,0.08); }
.reply-bar { width: 3px; background: var(--accent); border-radius: 2px; }
.reply-content { flex: 1; min-width: 0; }
.reply-sender { font-size: 12px; color: var(--accent); font-weight: 600; }
.reply-text { font-size: 12px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* Media */
.message-media { margin-bottom: 6px; border-radius: var(--radius-sm); overflow: hidden; max-width: 320px; }
.message-media img { max-width: 100%; display: block; cursor: pointer; border-radius: var(--radius-sm); }
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

/* REPLY BAR (above input) */
.reply-bar-container { background: var(--bg-secondary); border-top: 1px solid var(--border); padding: 10px 20px; }
.reply-bar-content { display: flex; align-items: center; gap: 12px; padding: 8px 12px; background: var(--accent-light); border-left: 3px solid var(--accent); border-radius: 6px; }
.reply-icon { color: var(--accent); font-size: 18px; }
.reply-bar-info { flex: 1; min-width: 0; }
.reply-bar-title { font-size: 12px; color: var(--accent); font-weight: 600; }
.reply-bar-text { font-size: 13px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.reply-close { background: none; border: none; color: var(--text-secondary); cursor: pointer; padding: 4px 8px; font-size: 16px; }
.reply-close:hover { color: var(--danger); }

/* INPUT */
.input-area { padding: 12px 20px; background: var(--bg-secondary); border-top: 1px solid var(--border); }
.input-area form { display: flex; align-items: flex-end; gap: 10px; }
.btn-attach { padding: 12px; color: var(--text-secondary); cursor: pointer; border-radius: var(--radius-sm); font-size: 18px; display: flex; align-items: center; }
.btn-attach:hover { color: var(--accent); background: var(--accent-light); }
.input-wrapper { flex: 1; position: relative; }
.file-preview { display: flex; align-items: center; gap: 8px; padding: 6px 12px; background: var(--accent-light); border: 1px solid rgba(108, 92, 231, 0.3); border-radius: var(--radius-sm) var(--radius-sm) 0 0; font-size: 13px; color: var(--accent); }
.file-preview button { background: none; border: none; color: var(--text-secondary); cursor: pointer; padding: 2px; font-size: 14px; }
.file-preview button:hover { color: var(--danger); }
#messageInput { width: 100%; padding: 12px 16px; background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: var(--radius); color: var(--text-primary); font-size: 14px; font-family: inherit; outline: none; resize: none; max-height: 120px; line-height: 1.4; }
#messageInput:focus { border-color: var(--accent); }
#messageInput::placeholder { color: var(--text-muted); }
.btn-send { padding: 12px 16px; background: var(--accent); color: white; border: none; border-radius: var(--radius); cursor: pointer; font-size: 16px; display: flex; align-items: center; min-height: 44px; }
.btn-send:hover:not(:disabled) { background: var(--accent-hover); transform: scale(1.05); }
.btn-send:disabled { opacity: 0.5; cursor: not-allowed; }

/* MEDIA MODAL */
.media-modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0, 0, 0, 0.95); z-index: 1000; align-items: center; justify-content: center; }
.media-modal.active { display: flex; }
.media-modal-content { position: relative; max-width: 90vw; max-height: 90vh; }
.media-modal-content img { max-width: 90vw; max-height: 90vh; object-fit: contain; border-radius: var(--radius); }
.modal-close { position: absolute; top: -40px; right: 0; background: none; border: none; color: white; font-size: 24px; cursor: pointer; padding: 8px; }

/* CONTEXT MENU */
.context-menu { display: none; position: fixed; background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-sm); box-shadow: var(--shadow); z-index: 1000; min-width: 180px; padding: 6px; }
.context-item { padding: 10px 14px; cursor: pointer; display: flex; align-items: center; gap: 10px; border-radius: var(--radius-sm); font-size: 14px; color: var(--text-primary); }
.context-item:hover { background: var(--bg-tertiary); }
.context-item.danger { color: var(--danger); }
.context-item.danger:hover { background: rgba(231, 76, 60, 0.1); }
.context-item i { width: 18px; color: var(--text-secondary); }
.context-item.danger i { color: var(--danger); }

/* FORWARD MODAL */
.forward-modal, .edit-modal { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.7); z-index: 999; align-items: center; justify-content: center; padding: 20px; }
.forward-modal.active, .edit-modal.active { display: flex; }
.forward-content, .edit-content { background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius); max-width: 420px; width: 100%; max-height: 80vh; display: flex; flex-direction: column; overflow: hidden; }
.forward-header, .edit-header { padding: 16px 20px; display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid var(--border); }
.forward-header h3, .edit-header h3 { font-size: 16px; display: flex; align-items: center; gap: 8px; }
.forward-header button, .edit-header button { background: none; border: none; color: var(--text-secondary); cursor: pointer; font-size: 18px; }
.forward-search { padding: 12px 20px; border-bottom: 1px solid var(--border); }
.forward-search input { width: 100%; padding: 10px 14px; background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-primary); outline: none; font-size: 14px; }
.forward-search input:focus { border-color: var(--accent); }
.forward-list { flex: 1; overflow-y: auto; padding: 8px; }
.forward-item { display: flex; align-items: center; gap: 12px; padding: 10px 14px; border-radius: var(--radius-sm); cursor: pointer; }
.forward-item:hover { background: var(--bg-tertiary); }
.forward-name { font-size: 14px; font-weight: 500; }

/* EDIT MODAL */
.edit-content { max-width: 480px; }
#editInput { width: calc(100% - 40px); margin: 20px; padding: 12px; background: var(--bg-tertiary); border: 1px solid var(--border); border-radius: var(--radius-sm); color: var(--text-primary); outline: none; font-size: 14px; min-height: 100px; resize: vertical; font-family: inherit; }
#editInput:focus { border-color: var(--accent); }
.edit-actions { display: flex; justify-content: flex-end; gap: 10px; padding: 0 20px 20px; }
.btn-cancel, .btn-save { padding: 10px 20px; border-radius: var(--radius-sm); border: none; cursor: pointer; font-size: 14px; font-weight: 600; }
.btn-cancel { background: var(--bg-tertiary); color: var(--text-primary); }
.btn-cancel:hover { background: var(--border); }
.btn-save { background: var(--accent); color: white; }
.btn-save:hover { background: var(--accent-hover); }

/* TOAST */
.toast { position: fixed; bottom: 30px; left: 50%; transform: translateX(-50%) translateY(100px); background: var(--accent); color: white; padding: 10px 20px; border-radius: var(--radius); font-size: 14px; font-weight: 500; z-index: 2000; opacity: 0; transition: all 0.3s; box-shadow: var(--shadow); }
.toast.show { transform: translateX(-50%) translateY(0); opacity: 1; }

/* RESPONSIVE */
@media (max-width: 768px) {
    .sidebar { position: fixed; left: -360px; top: 0; height: 100vh; z-index: 100; transition: left 0.3s; }
    .sidebar.open { left: 0; box-shadow: 10px 0 30px rgba(0, 0, 0, 0.5); }
    .mobile-menu-btn { display: block; }
    .message { max-width: 85%; }
    .chat-header-actions span { display: none; }
    .welcome-features { flex-direction: column; align-items: center; }
}
@media (max-width: 480px) {
    .chat-header-actions { gap: 4px; }
    .btn-action { padding: 8px; }
    .btn-action span { display: none; }
    .message { max-width: 90%; }
    .message-media { max-width: 240px; }
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
            stop_signals=None
        )
    except Exception as e:
        logger.exception(f"Bot crashed: {e}")


def main():
    db.init_db()
    write_templates()

    logger.info("=" * 60)
    logger.info("🚀 Telegram Relay Bot Starting")
    logger.info(f"📡 Port: {PORT}")
    logger.info(f"👑 Owner ID: {OWNER_ID}")
    if WEBHOOK_URL:
        logger.info(f"🌐 URL: {WEBHOOK_URL}")
    logger.info("=" * 60)

    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    import time
    time.sleep(2)

    socketio.run(
        app, host='0.0.0.0', port=PORT,
        debug=False, allow_unsafe_werkzeug=True
    )


if __name__ == "__main__":
    main()
