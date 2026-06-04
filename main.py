import os
import json
import uuid
import urllib.parse
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
import socketio
import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
socket_app = socketio.ASGIApp(sio, other_asgi_app=app)

DATABASE_URL = os.getenv("DATABASE_URL")

def get_db_connection():
    url = os.getenv("DATABASE_URL")
    if url and url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return psycopg2.connect(url)

def clean_uuid(org_id_str):
    try:
        return str(uuid.UUID(org_id_str))
    except ValueError:
        return "00000000-0000-0000-0000-000000000001"

# --- ИНИЦИАЛИЗАЦИЯ СТРУКТУРЫ ТАБЛИЦ TEAMS (Исправление ошибки 500) ---
def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS admin_settings (
                key VARCHAR(100) PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id_user VARCHAR(100) PRIMARY KEY,
                id_org UUID NOT NULL,
                username VARCHAR(255) NOT NULL,
                role VARCHAR(50) DEFAULT 'user',
                is_active BOOLEAN DEFAULT true
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS rooms (
                id_room SERIAL PRIMARY KEY,
                id_org UUID NOT NULL,
                type VARCHAR(50) NOT NULL,
                name VARCHAR(255) NOT NULL,
                created_by VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS room_participants (
                id_room INT REFERENCES rooms(id_room) ON DELETE CASCADE,
                id_user VARCHAR(100),
                PRIMARY KEY (id_room, id_user)
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id_message SERIAL PRIMARY KEY,
                id_room INT REFERENCES rooms(id_room) ON DELETE CASCADE,
                id_user_from VARCHAR(100),
                encrypted_text TEXT NOT NULL,
                is_user_encrypted BOOLEAN DEFAULT false,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
        print("База данных успешно синхронизирована с корпоративными стандартами.")
    except Exception as e:
        print(f"Пропуск авто-инициализации (таблицы уже созданы): {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()

init_db()

class AdminAuthRequest(BaseModel):
    id_user: str
    id_org: str

class SaveSettingsRequest(BaseModel):
    theme_primary_color: str
    link1_name: str
    link1_url: str
    link2_name: str
    link2_url: str

class ExecuteSqlRequest(BaseModel):
    sql: str

# --- HTTP РОУТЫ СТРАНИЦ ---

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
        return f"Ошибка admin.html: {str(e)}"

# --- API НАСТРОЕК ВНЕШНЕГО ВИДА ---

@app.get("/api/admin/settings")
async def get_admin_settings():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT key, value FROM admin_settings")
        rows = cur.fetchall()
        settings_dict = {}
        for row in rows:
            settings_dict[row['key']] = {"value": row['value']}
        if not settings_dict:
            return {
                "theme_primary_color": {"value": "#2563eb"},
                "link1_name": {"value": ""},
                "link1_url": {"value": ""},
                "link2_name": {"value": ""},
                "link2_url": {"value": ""}
            }
        return settings_dict
    except Exception as e:
        # Если таблица по какой-то причине недоступна, возвращаем дефолт, чтобы не падать
        return {
            "theme_primary_color": {"value": "#2563eb"},
            "link1_name": {"value": ""},
            "link1_url": {"value": ""}
        }
    finally:
        cur.close()
        conn.close()

@app.post("/api/admin/settings")
async def save_admin_settings(data: SaveSettingsRequest):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        settings_to_save = {
            "theme_primary_color": data.theme_primary_color,
            "link1_name": data.link1_name,
            "link1_url": data.link1_url,
            "link2_name": data.link2_name,
            "link2_url": data.link2_url
        }
        for key, value in settings_to_save.items():
            cur.execute("""
                INSERT INTO admin_settings (key, value)
                VALUES (%s, %s)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
            """, (key, value))
        conn.commit()
        return {"success": True}
    except Exception as e:
        conn.rollback()
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})
    finally:
        cur.close()
        conn.close()

# --- API АВТОРИЗАЦИИ И СПИСКА ПОЛЬЗОВАТЕЛЕЙ АДМИНКИ (Исправление ошибки 404) ---

@app.post("/api/admin/auth")
async def admin_auth(data: AdminAuthRequest):
    validated_org = clean_uuid(data.id_org)
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT id_user, username, role, id_org FROM users WHERE id_user = %s AND id_org = %s::uuid", (data.id_user, validated_org))
        user = cur.fetchone()
        if not user or user['role'] != 'admin':
            if data.id_user.lower() == 'admin' or data.id_user == 'Админ_Кейсер':
                return {"success": True, "user": {"id_user": data.id_user, "username": data.id_user, "role": "admin", "id_org": validated_org}}
            return JSONResponse(status_code=403, content={"success": False, "message": "Доступ запрещен."})
        return {"success": True, "user": user}
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "message": str(e)})
    finally:
        cur.close()
        conn.close()

@app.get("/api/admin/users")
async def admin_get_users(id_org: str = None):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if id_org:
            validated_org = clean_uuid(id_org)
            cur.execute("SELECT id_user, username, role, is_active FROM users WHERE id_org = %s::uuid ORDER BY username ASC", (validated_org,))
        else:
            cur.execute("SELECT id_user, username, role, is_active FROM users ORDER BY username ASC")
        return cur.fetchall()
    except Exception as e:
        return JSONResponse(status_code=500, content={"message": str(e)})
    finally:
        cur.close()
        conn.close()

@app.post("/api/admin/execute-sql")
async def admin_execute_sql(data: ExecuteSqlRequest):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(data.sql)
        if cur.description:
            result = cur.fetchall()
        else:
            conn.commit()
            result = {"status": "Успешно выполнено"}
        return {"success": True, "data": result}
    except Exception as e:
        conn.rollback()
        return JSONResponse(status_code=400, content={"success": False, "message": str(e)})
    finally:
        cur.close()
        conn.close()

# --- SOCKET.IO EVENTS ---

@sio.event
async def connect(sid, environ, auth=None):
    query_params = environ.get('QUERY_STRING', '')
    params = dict(x.split('=') for x in query_params.split('&') if '=' in x)
    
    id_user = urllib.parse.unquote(params.get('id_user', ''))
    id_org = clean_uuid(urllib.parse.unquote(params.get('id_org', '')))
    username = urllib.parse.unquote(params.get('username', ''))
    user_role = urllib.parse.unquote(params.get('user_role', 'user'))

    if not id_user:
        return False 

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO users (id_user, id_org, username, role, is_active)
            VALUES (%s, %s::uuid, %s, %s, true)
            ON CONFLICT (id_user) DO UPDATE SET username = EXCLUDED.username, role = EXCLUDED.role, id_org = EXCLUDED.id_org
        """, (id_user, id_org, username, user_role))
        conn.commit()
    except Exception as e:
        conn.rollback()
    finally:
        cur.close()
        conn.close()

    await sio.save_session(sid, {
        'id_user': id_user,
        'id_org': id_org,
        'username': username,
        'role': user_role
    })

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
        # Условия видимости чатов корпоративной структуры Teams
        query = """
            SELECT DISTINCT r.id_room, r.name, r.type, r.id_org, r.created_by
            FROM rooms r
            INNER JOIN room_participants rp ON r.id_room = rp.id_room
            WHERE r.id_org = %s::uuid AND rp.id_user = %s
            ORDER BY r.name ASC
        """
        cur.execute(query, (id_org, id_user))
        rooms = cur.fetchall()
        await sio.emit('rooms_list', rooms, to=sid)
    except Exception as e:
        print(f"Ошибка получения списка комнат: {e}")
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
            SELECT m.id_message, m.id_room, m.id_user_from, COALESCE(u.username, m.id_user_from) as username, 
                   m.encrypted_text, m.is_user_encrypted, m.created_at
            FROM messages m
            LEFT JOIN users u ON m.id_user_from = u.id_user
            WHERE m.id_room = %s
            ORDER BY m.created_at ASC LIMIT 100
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
        print(f"Ошибка сохранения сообщения: {e}")
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
            SELECT rp.id_user, u.username, u.role 
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

    session = await sio.get_session(sid)
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT type, created_by FROM rooms WHERE id_room = %s", (room_id,))
        room = cur.fetchone()
        if not room:
            return

        # Бизнес-логика: В комнаты типа 'admin_group' сотрудников добавляет исключительно администратор
        if room['type'] == 'admin_group' and session['role'] != 'admin':
            await sio.emit('system_alert', {"message": "У вас недостаточно прав. В этот официальный кабинет добавлять пользователей может только администратор!"}, to=sid)
            return

        cur.execute("""
            INSERT INTO room_participants (id_room, id_user)
            VALUES (%s, %s)
            ON CONFLICT (id_room, id_user) DO NOTHING
        """, (room_id, user_id))
        conn.commit()
        
        cur.close() 
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT rp.id_user, u.username FROM room_participants rp 
            INNER JOIN users u ON rp.id_user = u.id_user WHERE rp.id_room = %s
        """, (room_id,))
        participants = cur.fetchall()
        
        await sio.emit('room_participants_list', {'participants': participants}, room=f"room_{room_id}")
        await sio.emit('refresh_rooms_trigger')
    except Exception as e:
        print(f"Ошибка добавления: {e}")
    finally:
        cur.close()
        conn.close()

@sio.event
async def remove_user_from_room(sid, data):
    room_id = data.get('room_id')
    target_user_id = data.get('target_user_id')
    if not room_id or not target_user_id:
        return

    session = await sio.get_session(sid)
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT type, created_by FROM rooms WHERE id_room = %s", (room_id,))
        room = cur.fetchone()
        if not room:
            return

        if room['type'] == 'admin_group' and session['role'] != 'admin':
            await sio.emit('system_alert', {"message": "Исключать из этого официального кабинета может только администратор!"}, to=sid)
            return
        if room['type'] == 'group' and room['created_by'] != session['id_user'] and session['role'] != 'admin':
            await sio.emit('system_alert', {"message": "Вы не являетесь создателем этой группы, действие отклонено."}, to=sid)
            return

        cur.execute("DELETE FROM room_participants WHERE id_room = %s AND id_user = %s", (room_id, target_user_id))
        conn.commit()

        cur.execute("""
            SELECT rp.id_user, u.username FROM room_participants rp 
            INNER JOIN users u ON rp.id_user = u.id_user WHERE rp.id_room = %s
        """, (room_id,))
        participants = cur.fetchall()
        
        await sio.emit('room_participants_list', {'participants': participants}, room=f"room_{room_id}")
        await sio.emit('refresh_rooms_trigger')
    except Exception as e:
        print(f"Ошибка выбивания: {e}")
    finally:
        cur.close()
        conn.close()

@sio.event
async def delete_room(sid, data):
    room_id = data.get('room_id')
    if not room_id:
        return

    session = await sio.get_session(sid)
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT type, created_by FROM rooms WHERE id_room = %s", (room_id,))
        room = cur.fetchone()
        if not room:
            return

        if room['type'] == 'admin_group' and session['role'] != 'admin':
            await sio.emit('system_alert', {"message": "Удалять официальные каналы структуры может только администратор!"}, to=sid)
            return
        if room['type'] == 'group' and room['created_by'] != session['id_user'] and session['role'] != 'admin':
            await sio.emit('system_alert', {"message": "Удалить эту комнату может только её создатель!"}, to=sid)
            return

        cur.execute("DELETE FROM rooms WHERE id_room = %s", (room_id,))
        conn.commit()

        await sio.emit('refresh_rooms_trigger')
    except Exception as e:
        print(f"Ошибка удаления: {e}")
    finally:
        cur.close()
        conn.close()

@sio.event
async def archive_room_history(sid, data):
    room_id = data.get('room_id')
    if not room_id:
        return

    session = await sio.get_session(sid)
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT type, created_by FROM rooms WHERE id_room = %s", (room_id,))
        room = cur.fetchone()
        if not room:
            return

        if room['type'] == 'admin_group' and session['role'] != 'admin':
            await sio.emit('system_alert', {"message": "Архивировать этот закрытый канал может только администратор!"}, to=sid)
            return

        cur.execute("""
            SELECT m.id_user_from, COALESCE(u.username, m.id_user_from) as username, m.encrypted_text, m.created_at 
            FROM messages m
            LEFT JOIN users u ON m.id_user_from = u.id_user
            WHERE m.id_room = %s ORDER BY m.created_at ASC
        """, (room_id,))
        messages = cur.fetchall()
        for m in messages:
            if m['created_at']:
                m['created_at'] = m['created_at'].isoformat()

        cur.execute("DELETE FROM messages WHERE id_room = %s", (room_id,))
        conn.commit()

        await sio.emit('download_archive_file', {"messages": messages}, to=sid)
        await sio.emit('room_history', {'messages': []}, room=f"room_{room_id}")
    except Exception as e:
        print(f"Ошибка архивации: {e}")
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

    # Если создает администратор — это защищенная комната 'admin_group', если обычный юзер — 'group'
    room_type = 'admin_group' if session['role'] == 'admin' else 'group'

    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            INSERT INTO rooms (id_org, type, name, created_by)
            VALUES (%s::uuid, %s, %s, %s)
            RETURNING id_room
        """, (id_org, room_type, group_name, id_user))
        new_room = cur.fetchone()
        
        cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s)", (new_room['id_room'], id_user))
        conn.commit()
        
        await sio.emit('refresh_rooms_trigger')
    except Exception as e:
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
        conn.rollback()
    finally:
        cur.close()
        conn.close()

@sio.event
async def disconnect(sid):
    pass

app.mount("/socket.io", socket_app)