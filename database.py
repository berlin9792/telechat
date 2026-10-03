import sqlite3
import os
from datetime import datetime

DATA_DIR = os.environ.get("DATA_DIR", "./data")
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.path.join(DATA_DIR, "relay_bot.db")


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT,
            first_seen TEXT,
            last_message TEXT,
            is_blocked INTEGER DEFAULT 0,
            unread_count INTEGER DEFAULT 0,
            profile_photo TEXT,
            is_typing INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            direction TEXT,
            message_type TEXT,
            content TEXT,
            file_id TEXT,
            file_name TEXT,
            file_url TEXT,
            thumbnail_url TEXT,
            telegram_message_id INTEGER,
            reply_to_id INTEGER,
            reply_to_content TEXT,
            reply_to_type TEXT,
            forwarded_from TEXT,
            is_edited INTEGER DEFAULT 0,
            is_deleted INTEGER DEFAULT 0,
            is_read INTEGER DEFAULT 0,
            timestamp TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        );

        CREATE TABLE IF NOT EXISTS message_map (
            owner_msg_id INTEGER PRIMARY KEY,
            user_id INTEGER,
            user_msg_id INTEGER,
            db_msg_id INTEGER
        );
        
        CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id);
        CREATE INDEX IF NOT EXISTS idx_messages_time ON messages(timestamp);
    """)
    conn.commit()
    conn.close()


def get_or_create_user(user_id, username=None, full_name=None):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    now = datetime.now().isoformat()

    if user is None:
        cursor.execute("""
            INSERT INTO users (user_id, username, full_name, first_seen, last_message, unread_count)
            VALUES (?, ?, ?, ?, ?, 1)
        """, (user_id, username, full_name, now, now))
    else:
        cursor.execute("""
            UPDATE users SET username = ?, full_name = ?, last_message = ?
            WHERE user_id = ?
        """, (username, full_name, now, user_id))
    conn.commit()
    conn.close()


def save_message(user_id, direction, message_type, content=None,
                 file_id=None, file_name=None, file_url=None,
                 thumbnail_url=None, telegram_message_id=None,
                 reply_to_id=None, reply_to_content=None, reply_to_type=None,
                 forwarded_from=None):
    conn = get_db()
    cursor = conn.cursor()
    now = datetime.now().isoformat()

    cursor.execute("""
        INSERT INTO messages 
        (user_id, direction, message_type, content, file_id, file_name, 
         file_url, thumbnail_url, telegram_message_id, reply_to_id,
         reply_to_content, reply_to_type, forwarded_from, timestamp, is_read)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (user_id, direction, message_type, content, file_id, file_name,
          file_url, thumbnail_url, telegram_message_id, reply_to_id,
          reply_to_content, reply_to_type, forwarded_from, now,
          1 if direction == 'outgoing' else 0))

    cursor.execute("UPDATE users SET last_message = ? WHERE user_id = ?", (now, user_id))
    if direction == 'incoming':
        cursor.execute("UPDATE users SET unread_count = unread_count + 1 WHERE user_id = ?", (user_id,))

    conn.commit()
    msg_id = cursor.lastrowid
    conn.close()
    return msg_id


def get_all_users():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.*, 
               (SELECT content FROM messages WHERE user_id = u.user_id AND is_deleted = 0 ORDER BY timestamp DESC LIMIT 1) as last_content,
               (SELECT message_type FROM messages WHERE user_id = u.user_id AND is_deleted = 0 ORDER BY timestamp DESC LIMIT 1) as last_type,
               (SELECT direction FROM messages WHERE user_id = u.user_id AND is_deleted = 0 ORDER BY timestamp DESC LIMIT 1) as last_direction
        FROM users u 
        ORDER BY u.last_message DESC
    """)
    users = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return users


def get_user(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_messages(user_id, limit=500):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT * FROM messages WHERE user_id = ? AND is_deleted = 0
        ORDER BY timestamp ASC LIMIT ?
    """, (user_id, limit))
    messages = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return messages


def get_message_by_id(msg_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM messages WHERE id = ?", (msg_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def edit_message(msg_id, new_content):
    conn = get_db()
    conn.execute("UPDATE messages SET content = ?, is_edited = 1 WHERE id = ?", (new_content, msg_id))
    conn.commit()
    conn.close()


def delete_message(msg_id):
    conn = get_db()
    conn.execute("UPDATE messages SET is_deleted = 1 WHERE id = ?", (msg_id,))
    conn.commit()
    conn.close()


def mark_as_read(user_id):
    conn = get_db()
    conn.execute("UPDATE messages SET is_read = 1 WHERE user_id = ? AND direction = 'incoming'", (user_id,))
    conn.execute("UPDATE users SET unread_count = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def block_user(user_id):
    conn = get_db()
    conn.execute("UPDATE users SET is_blocked = 1 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def unblock_user(user_id):
    conn = get_db()
    conn.execute("UPDATE users SET is_blocked = 0 WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def is_blocked(user_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT is_blocked FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return bool(row and row['is_blocked'])


def delete_chat(user_id):
    conn = get_db()
    conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def get_total_stats():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as c FROM users")
    total_users = cursor.fetchone()['c']
    cursor.execute("SELECT COUNT(*) as c FROM messages WHERE is_deleted = 0")
    total_messages = cursor.fetchone()['c']
    cursor.execute("SELECT COUNT(*) as c FROM users WHERE is_blocked = 1")
    blocked_users = cursor.fetchone()['c']
    cursor.execute("SELECT COALESCE(SUM(unread_count), 0) as c FROM users")
    total_unread = cursor.fetchone()['c']
    conn.close()
    return {
        'total_users': total_users,
        'total_messages': total_messages,
        'blocked_users': blocked_users,
        'total_unread': total_unread
    }


def search_users(query):
    conn = get_db()
    cursor = conn.cursor()
    q = f'%{query}%'
    cursor.execute("""
        SELECT u.*, 
               (SELECT content FROM messages WHERE user_id = u.user_id AND is_deleted = 0 ORDER BY timestamp DESC LIMIT 1) as last_content,
               (SELECT message_type FROM messages WHERE user_id = u.user_id AND is_deleted = 0 ORDER BY timestamp DESC LIMIT 1) as last_type
        FROM users u 
        WHERE u.full_name LIKE ? OR u.username LIKE ? OR CAST(u.user_id AS TEXT) LIKE ?
        ORDER BY u.last_message DESC
    """, (q, q, q))
    users = [dict(row) for row in cursor.fetchall()]
    conn.close()
    return users


def save_user_profile_photo(user_id, photo_url):
    conn = get_db()
    conn.execute("UPDATE users SET profile_photo = ? WHERE user_id = ?", (photo_url, user_id))
    conn.commit()
    conn.close()


def save_message_map(owner_msg_id, user_id, user_msg_id=None, db_msg_id=None):
    conn = get_db()
    conn.execute("""
        INSERT OR REPLACE INTO message_map (owner_msg_id, user_id, user_msg_id, db_msg_id)
        VALUES (?, ?, ?, ?)
    """, (owner_msg_id, user_id, user_msg_id, db_msg_id))
    conn.commit()
    conn.close()


def get_mapped_user(owner_msg_id):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM message_map WHERE owner_msg_id = ?", (owner_msg_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_db_msg_from_tg(user_id, tg_msg_id):
    """Get DB message id from telegram message id."""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id FROM messages 
        WHERE user_id = ? AND telegram_message_id = ? AND is_deleted = 0
        LIMIT 1
    """, (user_id, tg_msg_id))
    row = cursor.fetchone()
    conn.close()
    return row['id'] if row else None
