import os
import json
import urllib.parse
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
import socketio
import psycopg2
from psycopg2.extras import RealDictCursor

# Инициализируем FastAPI
app = FastAPI()

# Разрешаем CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Настройка Socket.IO сервера
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
socket_app = socketio.ASGIApp(sio, other_asgi_app=app)

DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

# --- РОУТЫ ДЛЯ СТАТИКИ И СТРАНИЦ ---

@app.get("/", response_class=HTMLResponse)
async def get_chat_page():
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Ошибка загрузки index.html: {str(e)}"

@app.get("/admin", response_class=HTMLResponse)
async def get_admin_page():
    try:
        with open("admin.html", "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Ошибка загрузки admin.html: {str(e)}"

# --- ОБРАБОТЧИКИ СОБЫТИЙ SOCKET.IO ---

@sio.event
async def connect(sid, environ, auth=None):
    query_params = environ.get('QUERY_STRING', '')
    params = dict(x.split('=') for x in query_params.split('&') if '=' in x)
    
    id_user = urllib.parse.unquote(params.get('id_user', ''))
    id_org = urllib.parse.unquote(params.get('id_org', ''))
    username = urllib.parse.unquote(params.get('username', ''))
    user_role = urllib.parse.unquote(params.get('user_role', 'user'))

    if not id_user or not id_org:
        return False 

    await sio.save_session(sid, {
        'id_user': id_user,
        'id_org': id_org,
        'username': username,
        'role': user_role
    })
    print(f"Пользователь {username} ({id_user}) подключился")

@sio.event
async def join_room_pool(sid, data):
    room_id = data.get('room_id')
    if not room_id:
        return
    rooms = sio.rooms(sid)
    for room in list(rooms):
        if room != sid:
            await sio.leave_room(sid, room)
    await sio.enter_room(sid, f"room_{room_id}")

@sio.event
async def get_rooms_again(sid):
    session = await sio.get_session(sid)
    id_org = session['id_org']
    id_user = session['id_user']
    
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        query = """
            SELECT DISTINCT r.id_room, r.name, r.type, r.id_org 
            FROM rooms r
            LEFT JOIN room_participants rp ON r.id_room = rp.id_room
            WHERE r.id_org = %s::uuid AND (r.type = 'group' OR rp.id_user = %s)
            ORDER BY r.name ASC
        """
        cur.execute(query, (id_org, id_user))
        rooms = cur.fetchall()
        await sio.emit('rooms_list', rooms, to=sid)
    except Exception as e:
        print(f"Ошибка комнат: {e}")
    finally:
        cur.close()
        conn.close()

@sio.event
async def get_users_list(sid):
    session = await sio.get_session(sid)
    id_org = session['id_org']
    
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute('SELECT id_user, username, role FROM users WHERE id_org = %s::uuid AND is_active = true ORDER BY username ASC', (id_org,))
        users = cur.fetchall()
        await sio.emit('users_list', users, to=sid)
    except Exception as e:
        print(f"Ошибка пользователей: {e}")
    finally:
        cur.close()
        conn.close()

@sio.event
async def get_room_history(sid, data):
    room_id = data.get('room_id')
    if not room_id:
        return

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        query = """
            SELECT m.id_message, m.id_room, m.id_user_from, u.username, 
                   m.encrypted_text, m.is_user_encrypted, m.created_at
            FROM messages m
            LEFT JOIN users u ON m.id_user_from = u.id_user
            WHERE m.id_room = %s
            ORDER BY m.created_at ASC LIMIT 50
        """
        cur.execute(query, (room_id,))
        messages = cur.fetchall()
        for m in messages:
            if m['created_at']:
                m['created_at'] = m['created_at'].isoformat()
        await sio.emit('room_history', {'messages': messages}, to=sid)
    except Exception as e:
        print(f"Ошибка истории: {e}")
    finally:
        cur.close()
        conn.close()

@sio.event
async def send_message(sid, data):
    room_id = data.get('room_id')
    text = data.get('text')
    is_secret = data.get('is_secret', False)
    if not room_id or not text:
        return

    session = await sio.get_session(sid)
    id_user = session['id_user']
    username = session['username']

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        query = """
            INSERT INTO messages (id_room, id_user_from, encrypted_text, is_user_encrypted)
            VALUES (%s, %s, %s, %s)
            RETURNING id_message, id_room, id_user_from, encrypted_text, is_user_encrypted, created_at
        """
        cur.execute(query, (room_id, id_user, text, is_secret))
        new_msg = cur.fetchone()
        conn.commit()

        new_msg['username'] = username
        if new_msg['created_at']:
            new_msg['created_at'] = new_msg['created_at'].isoformat()

        await sio.emit('new_message', new_msg, room=f"room_{room_id}")
    except Exception as e:
        print(f"Ошибка отправки: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()

@sio.event
async def get_room_participants(sid, data):
    room_id = data.get('room_id')
    if not room_id:
        return

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        query = """
            SELECT rp.id_user, u.username 
            FROM room_participants rp
            INNER JOIN users u ON rp.id_user = u.id_user
            WHERE rp.id_room = %s
        """
        cur.execute(query, (room_id,))
        participants = cur.fetchall()
        await sio.emit('room_participants_list', {'participants': participants}, to=sid)
    except Exception as e:
        print(f"Ошибка участников: {e}")
    finally:
        cur.close()
        conn.close()

@sio.event
async def add_user_to_room(sid, data):
    room_id = data.get('room_id')
    user_id = data.get('user_id')
    if not room_id or not user_id:
        return

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        query = """
            INSERT INTO room_participants (id_room, id_user)
            VALUES (%s, %s)
            ON CONFLICT (id_room, id_user) DO NOTHING
        """
        cur.execute(query, (room_id, user_id))
        conn.commit()
        
        cur.close() 
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT rp.id_user, u.username FROM room_participants rp 
            INNER JOIN users u ON rp.id_user = u.id_user WHERE rp.id_room = %s
        """, (room_id,))
        participants = cur.fetchall()
        await sio.emit('room_participants_list', {'participants': participants}, room=f"room_{room_id}")
    except Exception as e:
        print(f"Ошибка добавления: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()

@sio.event
async def create_group_chat(sid, data):
    group_name = data.get('group_name')
    if not group_name:
        return
    session = await sio.get_session(sid)
    id_org = session['id_org']
    id_user = session['id_user']

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            INSERT INTO rooms (id_org, type, name, created_by)
            VALUES (%s::uuid, 'group', %s, %s)
            RETURNING id_room, name, type
        """, (id_org, group_name, id_user))
        new_room = cur.fetchone()
        cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s)", (new_room['id_room'], id_user))
        conn.commit()
        await sio.emit('refresh_rooms_trigger')
    except Exception as e:
        print(f"Ошибка кабинета: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()

@sio.event
async def create_private_chat(sid, data):
    target_user_id = data.get('target_user_id')
    target_username = data.get('target_username')
    session = await sio.get_session(sid)
    id_user = session['id_user']
    username = session['username']
    id_org = session['id_org']

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        query_check = """
            SELECT r.id_room FROM rooms r
            INNER JOIN room_participants p1 ON r.id_room = p1.id_room
            INNER JOIN room_participants p2 ON r.id_room = p2.id_room
            WHERE r.type = 'private' AND r.id_org = %s::uuid 
              AND p1.id_user = %s AND p2.id_user = %s
        """
        cur.execute(query_check, (id_org, id_user, target_user_id))
        existing = cur.fetchone()
        if existing:
            await sio.emit('private_chat_created', {'id_room': existing['id_room']}, to=sid)
            return

        chat_name = f"{username} ⇄ {target_username}"
        cur.execute("""
            INSERT INTO rooms (id_org, type, name, created_by)
            VALUES (%s::uuid, 'private', %s, %s)
            RETURNING id_room
        """, (id_org, chat_name, id_user))
        new_room = cur.fetchone()
        room_id = new_room['id_room']

        cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s)", (room_id, id_user))
        cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s)", (room_id, target_user_id))
        conn.commit()
        await sio.emit('private_chat_created', {'id_room': room_id}, to=sid)
        await sio.emit('refresh_rooms_trigger')
    except Exception as e:
        print(f"Ошибка приватного чата: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()

@sio.event
async def disconnect(sid):
    print(f"Отключился: {sid}")

app.mount("/socket.io", socket_app)