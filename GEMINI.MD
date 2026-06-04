Проект: Corporate 1C Chat (Teams Mode)
Этот проект представляет собой корпоративный чат, оптимизированный для работы внутри платформы 1С:Предприятие 8.3.5 (движок Internet Explorer/Trident).

Структура проекта
main.py — основной бэкенд на FastAPI + Socket.IO для управления пользователями, сообщениями и базой данных.

index.html — клиентская часть чата, адаптированная под старые движки браузеров.

admin.html — панель администратора для управления настройками и БД.

requirements.txt — список зависимостей Python.

Procfile — конфигурация для деплоя на Railway.

.env — переменные окружения с данными для подключения к PostgreSQL.

.gitignore — список файлов, игнорируемых Git.

Исходный код файлов
1. main.py
import os
import json
import uuid
import urllib.parse
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
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

UPLOAD_DIR = "uploads"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

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
    except Exception as e:
        print(f"Ошибка инициализации БД: {e}")
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

# Измененная логика upload: формируем "чистый" путь без знаков вопроса
@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    try:
        orig_filename = file.filename
        file_extension = os.path.splitext(orig_filename)[1]
        if not file_extension:
            file_extension = ".png" # Фолбек для скриншотов
            
        unique_id = str(uuid.uuid4())
        saved_filename = f"{unique_id}{file_extension}"
        file_path = os.path.join(UPLOAD_DIR, saved_filename)
        
        with open(file_path, "wb") as buffer:
            buffer.write(await file.read())
            
        # Формируем URL вида /download/уникальный_ид/ОригинальноеИмя.ext
        encoded_orig_name = urllib.parse.quote(orig_filename)
        return {"url": f"/download/{saved_filename}/{encoded_orig_name}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ПРАВКА СКАЧИВАНИЯ: Роут полностью избавлен от Query-параметров, мешавших 1С
@app.get("/download/{file_uuid}")
async def download_file_direct(file_uuid: str):
    # Ищем файл в папке по UUID (мы сохраняли их как UUID.ext)
    for filename in os.listdir(UPLOAD_DIR):
        if filename.startswith(file_uuid):
            file_path = os.path.join(UPLOAD_DIR, filename)
            # Извлекаем оригинальное имя из базы (или передаем через заголовок)
            return FileResponse(file_path, media_type='application/octet-stream')
    raise HTTPException(status_code=404, detail="Файл не найден")

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
        return {"theme_primary_color": {"value": "#2563eb"}}
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

@app.post("/api/admin/user/permissions")
async def admin_user_permissions():
    return {"success": True}

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
            result = {"status": "Успешно"}
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
        # Упрощенный, безопасный запрос создания пользователя
        cur.execute("""
            INSERT INTO users (id_user, id_org, username, role, is_active)
            VALUES (%s, %s::uuid, %s, %s, true)
            ON CONFLICT (id_user) DO UPDATE SET 
            username = EXCLUDED.username, 
            role = EXCLUDED.role, 
            id_org = EXCLUDED.id_org
        """, (id_user, id_org, username, user_role))
        
        # Авто-вход в кабинет "ОБЩИЙ" (admin_group)
        cur.execute("""
            SELECT id_room FROM rooms 
            WHERE id_org = %s::uuid AND UPPER(name) LIKE 'ОБЩ%%' AND type = 'admin_group'
            LIMIT 1
        """, (id_org,))
        room_general = cur.fetchone()
        if room_general:
            # Исправлено: передача id_room как списка с одним элементом (кортеж)
            cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s) ON CONFLICT DO NOTHING", 
                        (room_general[0], id_user))
            
        # Авто-вход админов в АДМИН
        if user_role == 'admin':
            cur.execute("SELECT id_room FROM rooms WHERE name = 'АДМИН' AND id_org = %s::uuid", (id_org,))
            room_admin = cur.fetchone()
            if room_admin:
                cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s) ON CONFLICT DO NOTHING", 
                            (room_admin[0], id_user))
                
        conn.commit()
    except Exception as e:
        print(f"Ошибка при коннекте: {e}")
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
        cur.execute("SELECT type FROM rooms WHERE id_room = %s", (room_id,))
        room = cur.fetchone()
        if not room:
            return
        if room['type'] == 'admin_group' and session['role'] != 'admin':
            await sio.emit('system_alert', {"message": "В закрытый официальный кабинет сотрудников добавляет только Администратор!"}, to=sid)
            return

        cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s) ON CONFLICT DO NOTHING", (room_id, user_id))
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
            await sio.emit('system_alert', {"message": "Исключать из официального кабинета может только администратор!"}, to=sid)
            return
        if room['type'] == 'group' and room['created_by'] != session['id_user'] and session['role'] != 'admin':
            await sio.emit('system_alert', {"message": "Вы не создатель группы, действие отклонено."}, to=sid)
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
        cur.execute("SELECT type FROM rooms WHERE id_room = %s", (room_id,))
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
        
        await sio.emit('private_chat_created', {'id_room': new_room['id_room']}, to=sid)
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
2. index.html
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta http-equiv="X-UA-Compatible" content="IE=edge" />
    <meta charset="UTF-8">
    <title>1C Chat Client</title>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/crypto-js/4.2.0/crypto-js.min.js"></script>
    
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; font-family: Arial, sans-serif; }
        body { height: 100vh; display: flex; flex-direction: column; background-color: #f3f4f6; overflow: hidden; }
        .header { background-color: #2563eb; color: white; padding: 12px 16px; display: flex; justify-content: space-between; align-items: center; }
        .header h1 { font-size: 16px; }
        .header-controls { display: flex; align-items: center; gap: 15px; }
        .header-links { display: flex; align-items: center; gap: 10px; font-size: 12px; }
        .header-links span.link-click { color: white; text-decoration: underline; font-weight: bold; cursor: pointer; }
        .header-links span.link-click:hover { text-decoration: none; }
        .user-info { font-size: 13px; }
        
        .main-container { flex: 1; display: flex; overflow: hidden; }
        .sidebar { width: 240px; background-color: white; border-right: 1px solid #e5e7eb; display: flex; flex-direction: column; }
        .sidebar-section { flex: 1; padding: 12px; overflow-y: auto; }
        .sidebar h3 { font-size: 12px; font-weight: bold; color: #374151; margin-bottom: 10px; }
        
        .room-item { padding: 6px 8px; margin-bottom: 4px; cursor: pointer; font-size: 12px; border-radius: 4px; }
        .room-item:hover { background-color: #f3f4f6; }
        .room-item.active { background-color: #dbeafe; color: #1e40af; font-weight: bold; }
        
        .btn-small { padding: 2px 6px; font-size: 10px; background-color: #10b981; color: white; border: none; border-radius: 4px; cursor: pointer; float: right; }
        .btn-danger-xs { padding: 1px 4px; font-size: 9px; background-color: #ef4444; color: white; border: none; border-radius: 3px; cursor: pointer; float: right; }
        
        .chat-window { flex: 1; display: flex; flex-direction: column; background-color: #f9fafb; position: relative; border-right: 1px solid #e5e7eb; }
        .chat-header { padding: 10px 16px; background: white; border-bottom: 1px solid #e5e7eb; display: flex; justify-content: space-between; align-items: center; font-size: 12px; font-weight: bold; color: #374151; }
        .chat-actions { display: flex; gap: 6px; }
        
        .messages-container { flex: 1; padding: 16px; overflow-y: auto; display: flex; flex-direction: column; }
        .msg-block { background-color: white; padding: 8px 12px; border-radius: 8px; max-width: 70%; margin-bottom: 8px; align-self: flex-start; }
        .msg-author { font-size: 10px; color: #6b7280; font-weight: bold; display: block; margin-bottom: 2px; }
        .msg-text { font-size: 12px; color: #1f2937; word-break: break-word; }
        
        .participants-panel { width: 240px; background-color: white; display: flex; flex-direction: column; padding: 12px; overflow-y: auto; }
        .participants-panel h3 { font-size: 12px; font-weight: bold; color: #374151; margin-bottom: 10px; border-bottom: 1px solid #e5e7eb; padding-bottom: 6px; }
        .participants-panel h3.employees-title { margin-top: 15px; }
        .participant-item { padding: 4px 6px; font-size: 12px; color: #4b5563; line-height: 18px; clear: both; }

        .input-panel { padding: 12px; background-color: white; border-top: 1px solid #e5e7eb; }
        .secret-row { margin-bottom: 6px; }
        .btn-secret { padding: 3px 10px; font-size: 11px; background-color: #e5e7eb; border: none; border-radius: 9999px; cursor: pointer; }
        .btn-secret.active { background-color: #eab308; color: white; }
        .controls-row { display: flex; gap: 8px; align-items: center; }
        .btn-file { background-color: #e5e7eb; border: none; padding: 6px 10px; border-radius: 6px; cursor: pointer; }
        
        .input-msg-div { flex: 1; border: 1px solid #d1d5db; border-radius: 6px; padding: 8px; font-size: 12px; min-height: 34px; max-height: 80px; overflow-y: auto; background: white; outline: none; color: #1f2937; }
        .input-msg-div:empty:before { content: attr(data-placeholder); color: #9ca3af; }
        
        .btn-send { background-color: #2563eb; color: white; border: none; padding: 6px 16px; border-radius: 6px; cursor: pointer; }
        img { max-width: 100%; max-height: 150px; border-radius: 4px; cursor: pointer; display: block; margin-top: 4px; }
        .drop-zone { position: absolute; top: 0; left: 0; right: 0; bottom: 0; background: rgba(37,99,235,0.05); border: 3px dashed #2563eb; z-index: 50; display: none; align-items: center; justify-content: center; }
        .drop-box { background: white; padding: 12px; border-radius: 6px; font-weight: bold; color: #2563eb; font-size: 12px; }
        
        .modal-overlay { position: fixed; top: 0; left: 0; right: 0; bottom: 0; background: rgba(0,0,0,0.4); display: none; align-items: center; justify-content: center; z-index: 100; }
        .modal-body { background: white; padding: 16px; border-radius: 8px; width: 300px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
        .modal-body h4 { font-size: 13px; margin-bottom: 10px; }
        .modal-body select, .modal-body input { width: 100%; padding: 6px; font-size: 12px; margin-bottom: 12px; outline: none; border: 1px solid #d1d5db; }
        .modal-buttons { display: flex; justify-content: flex-end; gap: 8px; }
    </style>
</head>
<body>

<div class="header">
    <h1>Corporate 1C Chat (Teams Mode)</h1>
    <div class="header-controls">
        <div id="custom-links-container" class="header-links"></div>
        <button onclick="apply1CAuth('Admin', '00001', 'Администратор', 'admin')" style="padding:2px 8px; font-size:11px; background:#eab308; border:none; border-radius:4px; cursor:pointer;">Войти</button>
        <span id="user-info" class="user-info">Авторизация...</span>
    </div>
</div>

<div class="main-container">
    <div class="sidebar">
        <div class="sidebar-section">
            <h3>Кабинеты <button id="btn-create-group" class="btn-small">+ Кабинет</button></h3>
            <div id="rooms-list">Загрузка...</div>
        </div>
    </div>

    <div class="chat-window" id="chat-window">
        <div id="drop-zone" class="drop-zone"><div class="drop-box">📎 Перетащите файлы</div></div>
        
        <div class="chat-header">
            <span id="current-chat-title">Выберите кабинет или сотрудника</span>
            <div class="chat-actions" id="chat-management-actions" style="display: none;">
                <button onclick="triggerArchive()" class="btn-small" style="background:#eab308; float:none;">📦 В архив</button>
                <button onclick="triggerDeleteRoom()" class="btn-small" style="background:#ef4444; float:none;">❌ Удалить</button>
            </div>
        </div>

        <div id="messages-container" class="messages-container">
            <div class="msg-block"><span class="msg-author">Система</span><div class="msg-text">Выберите кабинет или сотрудника для начала общения.</div></div>
        </div>
        <div class="input-panel">
            <div class="secret-row">
                <button id="btn-secret" class="btn-secret">🔒 Шифровать</button>
                <button id="btn-paste-screen" class="btn-secret" style="background:#e5e7eb; color:#1f2937; margin-left:6px;">📋 Вставить скриншот</button>
            </div>
            <div class="controls-row">
                <input type="file" id="file-input" style="display:none">
                <button id="btn-file" class="btn-file">📎</button>
                <div id="input-msg" class="input-msg-div" contenteditable="true" data-placeholder="Напишите сообщение... Для вставки скриншота нажмите Ctrl+V или кнопку выше"></div>
                <button id="btn-send" class="btn-send">Отправить</button>
            </div>
        </div>
    </div>

    <div class="participants-panel">
        <h3>В кабинете: <button id="btn-add-participant" class="btn-small" style="display:none;">+ Человек</button></h3>
        <div id="participants-list">Не выбран чат</div>
        
        <h3 class="employees-title">Сотрудники компании <button id="btn-start-private" class="btn-small" style="background:#2563eb;">💬 Начать диалог</button></h3>
        <div id="users-list-right" style="font-size:12px; color:#4b5563;">Загрузка сотрудников...</div>
    </div>
</div>

<div class="modal-overlay" id="user-select-modal">
    <div class="modal-body">
        <h4 id="modal-action-title">Выберите сотрудника</h4>
        <select id="modal-user-dropdown"></select>
        <div class="modal-buttons">
            <button onclick="closeUserModal()" class="btn-small" style="background:#9ca3af;">Отмена</button>
            <button id="modal-submit-btn" class="btn-small">Выбрать</button>
        </div>
    </div>
</div>

<div class="modal-overlay" id="secret-input-modal">
    <div class="modal-body">
        <h4 id="secret-modal-title">Введите ключ шифрования</h4>
        <input type="password" id="modal-secret-field" placeholder="Ключ..." />
        <div class="modal-buttons">
            <button onclick="closeSecretModal()" class="btn-small" style="background:#9ca3af;">Отмена</button>
            <button id="secret-modal-submit-btn" class="btn-small" style="background:#eab308;">Применить</button>
        </div>
    </div>
</div>

<div class="modal-overlay" id="text-input-modal">
    <div class="modal-body">
        <h4 id="text-modal-title">Введите имя нового кабинета</h4>
        <input type="text" id="modal-text-field" placeholder="Название..." />
        <div class="modal-buttons">
            <button onclick="closeTextModal()" class="btn-small" style="background:#9ca3af;">Отмена</button>
            <button id="text-modal-submit-btn" class="btn-small" style="background:#10b981;">Создать</button>
        </div>
    </div>
</div>

<div class="modal-overlay" id="base64-image-modal">
    <div class="modal-body">
        <h4>Вставка скриншота</h4>
        <p style="font-size:11px; color:#6b7280; margin-bottom:8px;">Вставьте скриншот (Ctrl+V) прямо в поле ниже:</p>
        <input type="text" id="modal-base64-field" placeholder="Ожидание вставки..." />
        <div class="modal-buttons">
            <button onclick="closeBase64Modal()" class="btn-small" style="background:#9ca3af;">Отмена</button>
            <button id="base64-modal-submit-btn" class="btn-small" style="background:#2563eb;">Отправить</button>
        </div>
    </div>
</div>

<script>
window.onerror = function(message, url, line) { return true; };

var socket = null;
var currentRoomId = null;
var currentRoomType = "";
var currentRoomCreator = "";
var isSecretMode = false;
var userSecretKey = "";
var currentUserId = "";
var currentUserRole = "";
var globalUsersCache = []; 
var originalTitle = document.title;
var titleBlinkInterval = null;

var inputMsg = document.getElementById('input-msg');

function playNotificationSound() {
    try {
        var snd = new Audio("data:audio/wav;base64,UklGRigAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQQAAAAAAA==");
        snd.play();
    } catch(e) {}
}

function startTitleBlink(authorName) {
    if (titleBlinkInterval) clearInterval(titleBlinkInterval);
    playNotificationSound();
    var isOldTitle = true;
    titleBlinkInterval = setInterval(function() {
        document.title = isOldTitle ? "*** НОВОЕ СООБЩЕНИЕ ОТ " + authorName.toUpperCase() + " ***" : originalTitle;
        isOldTitle = !isOldTitle;
    }, 1000);
}

function stopTitleBlink() {
    if (titleBlinkInterval) { clearInterval(titleBlinkInterval); titleBlinkInterval = null; }
    document.title = originalTitle;
}

window.onclick = function() { stopTitleBlink(); };

function selectRoomById(roomId) {
    currentRoomId = roomId;
    if (!socket) return;
    socket.emit('join_room_pool', { room_id: roomId });
    socket.emit('get_room_history', { room_id: roomId });
    socket.emit('get_room_participants', { room_id: roomId });
    socket.emit('get_rooms_again');
}

function apply1CAuth(id_user, id_org, username, user_role) {
    currentUserId = id_user;
    currentUserRole = user_role || 'user';
    
    var userInfoEl = document.getElementById('user-info');
    if (userInfoEl) {
        userInfoEl.innerHTML = String(username) + (currentUserRole === 'admin' ? ' 👑' : '');
    }
    
    if (socket) {
        try { socket.removeAllListeners(); socket.disconnect(); } catch(e) {}
        socket = null;
    }
    
    var socketUrl = window.location.protocol + '//' + window.location.host;
    socket = io(socketUrl, {
        path: '/socket.io',
        transports: ['polling', 'websocket'],
        query: { id_user: id_user, id_org: id_org, username: username, user_role: currentUserRole }
    });

    socket.on('connect', function() {
        if (socket) {
            socket.emit('get_users_list');
            socket.emit('get_rooms_again');
        }
    });

    socket.on('rooms_list', function(rooms) { renderRoomsList(rooms); });
    socket.on('users_list', function(users) { globalUsersCache = users; renderUsersRightPanel(users); });
    
    socket.on('private_chat_created', function(data) {
        if (data && data.id_room) {
            selectRoomById(data.id_room);
        }
    });
    
    socket.on('room_history', function(data) {
        var container = document.getElementById('messages-container');
        if (!container) return;
        container.innerHTML = "";
        if (data && data.messages) {
            for (var i = 0; i < data.messages.length; i++) { displayMessage(data.messages[i], true); }
        }
        container.scrollTop = container.scrollHeight;
    });

    socket.on('room_participants_list', function(data) { renderParticipantsList(data.participants); });

    socket.on('new_message', function(msg) { 
        if (msg && msg.id_room == currentRoomId) {
            displayMessage(msg, false); 
        } else {
            startTitleBlink(msg.username);
        }
    });
    
    socket.on('system_alert', function(data) { alert(data.message); });
    socket.on('refresh_rooms_trigger', function() { if (socket) socket.emit('get_rooms_again'); });
    
    socket.on('download_archive_file', function(data) {
        try {
            var jsonString = JSON.stringify(data.messages, null, 2);
            var blob = new Blob([jsonString], {type: "application/json;charset=utf-8"});
            
            if (window.navigator && window.navigator.msSaveOrOpenBlob) {
                window.navigator.msSaveOrOpenBlob(blob, "archive_room_" + currentRoomId + ".json");
            } else {
                var downloadAnchor = document.createElement('a');
                downloadAnchor.href = URL.createObjectURL(blob);
                downloadAnchor.download = "archive_room_" + currentRoomId + ".json";
                document.body.appendChild(downloadAnchor);
                downloadAnchor.click();
                downloadAnchor.remove();
            }
            alert("История сохранена. Из БД ветка полностью очищена.");
        } catch(e) { alert("Ошибка архивации: " + e.message); }
    });
}

function renderRoomsList(rooms) {
    var container = document.getElementById('rooms-list');
    if (!container) return;
    container.innerHTML = "";
    if (!rooms || rooms.length === 0) { container.innerHTML = "<div>Нет чатов</div>"; return; }
    for (var i = 0; i < rooms.length; i++) {
        var room = rooms[i];
        var div = document.createElement('div');
        div.className = 'room-item' + (room.id_room == currentRoomId ? ' active' : '');
        div.innerText = (room.type === 'private' ? '💬 ' : (room.type === 'admin_group' ? '👑 ' : '👥 ')) + room.name;
        
        if (room.id_room == currentRoomId) {
            currentRoomType = room.type;
            currentRoomCreator = room.created_by;
            document.getElementById('current-chat-title').innerText = room.name;
            
            var mgtActions = document.getElementById('chat-management-actions');
            if (mgtActions) {
                if (room.type === 'private') mgtActions.style.display = 'none';
                else mgtActions.style.display = (currentUserRole === 'admin' || room.created_by == currentUserId) ? 'flex' : 'none';
            }

            var btnAdd = document.getElementById('btn-add-participant');
            if (btnAdd) {
                if (room.type === 'private') btnAdd.style.display = 'none';
                else if (room.type === 'admin_group') btnAdd.style.display = (currentUserRole === 'admin') ? 'inline-block' : 'none';
                else btnAdd.style.display = 'inline-block';
            }
        }
        
        (function(r) {
            div.onclick = function() {
                stopTitleBlink();
                selectRoomById(r.id_room);
            };
        })(room);
        container.appendChild(div);
    }
}

function renderUsersRightPanel(users) {
    var container = document.getElementById('users-list-right');
    if (!container) return;
    container.innerHTML = "";
    for (var i = 0; i < users.length; i++) {
        var u = users[i];
        if (u.id_user == currentUserId) continue;
        var div = document.createElement('div');
        div.style.cssText = "padding: 3px 0; border-bottom: 1px dashed #f3f4f6;";
        div.innerText = "• " + u.username + (u.role === 'admin' ? ' (Админ)' : '');
        container.appendChild(div);
    }
}

function renderParticipantsList(list) {
    var container = document.getElementById('participants-list');
    if (!container) return;
    container.innerHTML = "";
    if (!list || list.length === 0) { container.innerHTML = "<div>Только вы</div>"; return; }
    for (var i = 0; i < list.length; i++) {
        var p = list[i];
        var div = document.createElement('div');
        div.className = 'participant-item';
        div.innerText = "• " + p.username;
        
        if (currentRoomType !== 'private' && p.id_user != currentUserId) {
            if (currentUserRole === 'admin' || currentRoomCreator == currentUserId) {
                var btnKick = document.createElement('button');
                btnKick.className = 'btn-danger-xs';
                btnKick.innerText = 'Выгнать';
                (function(targetId) {
                    btnKick.onclick = function() {
                        if (confirm("Исключить сотрудника из кабинета?")) {
                            socket.emit('remove_user_from_room', { room_id: currentRoomId, target_user_id: targetId });
                        }
                    };
                })(p.id_user);
                div.appendChild(btnKick);
            }
        }
        container.appendChild(div);
    }
}

function openUserModal(title, submitCallback) {
    document.getElementById('modal-action-title').innerText = title;
    var dropdown = document.getElementById('modal-user-dropdown');
    dropdown.innerHTML = "";
    for (var i = 0; i < globalUsersCache.length; i++) {
        var option = document.createElement('option');
        option.value = globalUsersCache[i].id_user;
        option.innerText = globalUsersCache[i].username;
        dropdown.appendChild(option);
    }
    document.getElementById('user-select-modal').style.display = 'flex';
    document.getElementById('modal-submit-btn').onclick = function() {
        submitCallback(dropdown.value);
        closeUserModal();
    };
}

function closeUserModal() { document.getElementById('user-select-modal').style.display = 'none'; }

function openSecretModal(title, submitCallback) {
    document.getElementById('secret-modal-title').innerText = title;
    var field = document.getElementById('modal-secret-field');
    field.value = "";
    document.getElementById('secret-input-modal').style.display = 'flex';
    document.getElementById('secret-modal-submit-btn').onclick = function() {
        if(field.value.trim() !== "") {
            submitCallback(field.value.trim());
        }
        closeSecretModal();
    };
}

function closeSecretModal() { document.getElementById('secret-input-modal').style.display = 'none'; }

function openTextModal(title, submitCallback) {
    document.getElementById('text-modal-title').innerText = title;
    var field = document.getElementById('modal-text-field');
    field.value = "";
    document.getElementById('text-input-modal').style.display = 'flex';
    document.getElementById('text-modal-submit-btn').onclick = function() {
        if(field.value.trim() !== "") {
            submitCallback(field.value.trim());
        }
        closeTextModal();
    };
}

function closeTextModal() { document.getElementById('text-input-modal').style.display = 'none'; }

function openBase64Modal(submitCallback) {
    var field = document.getElementById('modal-base64-field');
    field.value = "";
    document.getElementById('base64-image-modal').style.display = 'flex';
    document.getElementById('base64-modal-submit-btn').onclick = function() {
        if (field.value.trim() !== "") {
            submitCallback(field.value.trim());
        }
        closeBase64Modal();
    };
}

function closeBase64Modal() { document.getElementById('base64-image-modal').style.display = 'none'; }

document.getElementById('btn-add-participant').onclick = function() {
    if (!currentRoomId) return;
    openUserModal("Добавить сотрудника в кабинет", function(selectedUserId) {
        socket.emit('add_user_to_room', { room_id: currentRoomId, user_id: selectedUserId });
    });
};

document.getElementById('btn-start-private').onclick = function() {
    openUserModal("Начать личную переписку", function(selectedUserId) {
        var targetUser = null;
        // КЛАССИЧЕСКИЙ ЦИКЛ ДЛЯ 1С 8.3.5
        for (var i = 0; i < globalUsersCache.length; i++) {
            if (globalUsersCache[i].id_user == selectedUserId) {
                targetUser = globalUsersCache[i];
                break;
            }
        }
        if (targetUser) {
            socket.emit('create_private_chat', { 
                target_user_id: targetUser.id_user, 
                target_username: targetUser.username 
            });
        }
    });
};

function triggerDeleteRoom() {
    if (currentRoomId && confirm("Удалить этот кабинет и всю историю переписки?")) {
        socket.emit('delete_room', { room_id: currentRoomId });
        document.getElementById('messages-container').innerHTML = "";
        document.getElementById('current-chat-title').innerText = "Выберите кабинет или сотрудника";
        document.getElementById('chat-management-actions').style.display = 'none';
        currentRoomId = null;
    }
}

function triggerArchive() {
    if (currentRoomId && confirm("Выгрузить лог переписки на устройство и очистить базу данных?")) {
        socket.emit('archive_room_history', { room_id: currentRoomId });
    }
}

function displayMessage(msg, isBulkLoad) {
    try {
        var container = document.getElementById('messages-container');
        if (!container) return;
        var div = document.createElement('div');
        div.className = 'msg-block';
        
        var text = String(msg.encrypted_text || msg.text || "");
        var author = String(msg.username || "Сотрудник");
        var isUserEncrypted = msg.is_user_encrypted || false;

        if (isUserEncrypted || text.indexOf("SECRET:") === 0) {
            var cipher = text.replace("SECRET:", "").replace(/<\/?[^>]+(>|$)/g, "").trim();
            var uniqueId = 'c_' + Math.random().toString(36).substr(2, 6);
            
            text = '<div style="color:#eab308;">🔒 Зашифровано</div><div id="'+uniqueId+'" data-cipher="'+cipher+'">[Скрыто]</div>' +
                   '<button class="btn-small" style="float:none; margin-top:4px; background:#eab308;" ' +
                   'onclick="decryptMsg(\''+uniqueId+'\')">Расшифровать</button>';
        }
        
        div.innerHTML = '<span class="msg-author">' + author + '</span><div class="msg-text">' + text + '</div>';
        container.appendChild(div);
        if (!isBulkLoad) container.scrollTop = container.scrollHeight;
    } catch(e) { }
}

window.decryptMsg = function(elementId) {
    var targetElement = document.getElementById(elementId);
    if (!targetElement) return;
    var cipher = targetElement.getAttribute("data-cipher");
    
    openSecretModal("Введите секретный ключ", function(enteredKey) {
        try {
            var decryptedBytes = CryptoJS.AES.decrypt(cipher, enteredKey);
            var clearText = decryptedBytes.toString(CryptoJS.enc.Utf8);
            if (clearText) {
                targetElement.innerHTML = clearText;
                targetElement.style.cssText = "color:#1f2937; font-weight:bold;";
            } else { alert("Неверный ключ!"); }
        } catch(e) { alert("Ошибка расшифровки!"); }
    });
};

function sendMessage() {
    var text = "";
    if (inputMsg) { text = (inputMsg.innerText || inputMsg.textContent || "").trim(); }
    if (!text || !currentRoomId) return;
    
    var sendAsSecret = isSecretMode;
    try {
        if (isSecretMode) {
            text = "SECRET:" + CryptoJS.AES.encrypt(text, userSecretKey).toString();
        }
    } catch(e) { }
    
    if (socket) {
        socket.emit('send_message', { room_id: currentRoomId, text: text, is_secret: sendAsSecret });
    }
    if (inputMsg) inputMsg.innerHTML = "";
}

function uploadFile(file) {
    try {
        var fd = new FormData();
        fd.append('file', file);
        var xhr = new XMLHttpRequest();
        xhr.open('POST', '/upload', true);
        xhr.onload = function() {
            if (xhr.status === 200) {
                try {
                    var data = JSON.parse(xhr.responseText);
                    if (data && data.url && socket) {
                        var isImg = /\.(jpg|jpeg|png|gif|webp)$/i.test(file.name);
                        var absoluteUrl = window.location.protocol + '//' + window.location.host + data.url;
                        var content = isImg ? '<img src="'+absoluteUrl+'" />' : '<a href="'+absoluteUrl+'" target="_blank" style="color:#2563eb; text-decoration:underline; font-weight:bold; cursor:pointer;">📄 Скачать: '+file.name+'</a>';
                        socket.emit('send_message', { room_id: currentRoomId, text: content, is_secret: false });
                    }
                } catch(e) { alert("Ошибка парсинга ответа сервера"); }
            } else { alert('Ошибка отправки файла.'); }
        };
        xhr.onerror = function() { alert('Ошибка сети при отправке файла'); };
        xhr.send(fd);
    } catch(e) { alert('Ошибка при вызове uploadFile: ' + e.message); }
}

function uploadBase64Image(rawInputString) {
    if (!currentRoomId || !socket) return;
    
    var cleanBase64 = rawInputString;
    // Парсинг Base64 из HTML-вставки
    if (rawInputString.indexOf('src="') !== -1) {
        var match = rawInputString.match(/src="([^"]+)"/);
        if (match && match[1]) { cleanBase64 = match[1]; }
    }
    if (cleanBase64.indexOf(",") !== -1) {
        cleanBase64 = cleanBase64.split(",")[1];
    }
    cleanBase64 = cleanBase64.replace(/<\/?[^>]+(>|$)/g, "").replace(/\s/g, "");

    try {
        // Проверка: atob работает в большинстве версий 1С 8.3.5, но если нет — используем fallback
        if (typeof atob === 'function') {
            var byteCharacters = atob(cleanBase64);
            var byteNumbers = new Array(byteCharacters.length);
            for (var i = 0; i < byteCharacters.length; i++) {
                byteNumbers[i] = byteCharacters.charCodeAt(i);
            }
            var byteArray = new Uint8Array(byteNumbers);
            var blob = new Blob([byteArray], {type: 'image/png'});
            var file = new File([blob], "screenshot_1c_" + Date.now() + ".png", {type: "image/png"});
            uploadFile(file);
        } else {
            alert("Ваш браузер 1С не поддерживает декодирование Base64. Используйте кнопку 'Прикрепить файл'.");
        }
    } catch(e) {
        alert("Ошибка декодирования скриншота: " + e.message);
    }
}

// ПРАВКА: Вставка скриншота через Ctrl+V
if (inputMsg) {
    inputMsg.onpaste = function(event) {
        var clipboardData = event.clipboardData || window.clipboardData;
        if (!clipboardData) return;
        
        // Для IE/1С используем getData
        var htmlData = "";
        try { htmlData = clipboardData.getData("HTML") || clipboardData.getData("Text"); } catch(e) {}
        
        if (htmlData && htmlData.indexOf("data:image") !== -1) {
            uploadBase64Image(htmlData);
            if (event.preventDefault) event.preventDefault();
            return false;
        }
    };
}

// ФИКС ОПЕЧАТКИ: Переменная base64Code теперь маппится корректно
document.getElementById('btn-paste-screen').onclick = function() {
    openBase64Modal(function(base64Code) {
        uploadBase64Image(base64Code);
    });
};

document.getElementById('btn-create-group').onclick = function() {
    openTextModal("Создание нового кабинета", function(enteredRoomName) {
        if(socket) socket.emit('create_group_chat', { group_name: enteredRoomName });
    });
};

document.getElementById('btn-secret').onclick = function() {
    if (!isSecretMode) {
        openSecretModal("Задайте ключ шифрования для исходящих", function(enteredKey) {
            userSecretKey = enteredKey;
            isSecretMode = true;
            document.getElementById('btn-secret').className = 'btn-secret active';
        });
    } else {
        isSecretMode = false;
        userSecretKey = "";
        this.className = 'btn-secret';
    }
};

document.getElementById('btn-send').onclick = sendMessage;
document.getElementById('btn-file').onclick = function() { document.getElementById('file-input').click(); };
document.getElementById('file-input').onchange = function(e) { if (e.target.files[0]) uploadFile(e.target.files[0]); this.value = ""; };

if (inputMsg) {
    inputMsg.onkeydown = function(e) { 
        var ev = e || window.event;
        if (ev && ev.key === 'Enter' && !ev.shiftKey) { 
            if (ev.preventDefault) ev.preventDefault(); else ev.returnValue = false;
            sendMessage(); 
        } 
    };
}

function loadCustomThemeAndLinks() {
    try {
        var xhr = new XMLHttpRequest();
        xhr.open('GET', '/api/admin/settings', true);
        xhr.onload = function() {
            if (xhr.status === 200) {
                try {
                    var settings = JSON.parse(xhr.responseText);
                    if (!settings) return;
                    if (settings.theme_primary_color && settings.theme_primary_color.value) {
                        var headerEl = document.querySelector('.header');
                        if (headerEl) headerEl.style.backgroundColor = settings.theme_primary_color.value;
                        var btnSendEl = document.getElementById('btn-send');
                        if (btnSendEl) btnSendEl.style.backgroundColor = settings.theme_primary_color.value;
                    }
                } catch(e) {}
            }
        };
        xhr.send();
    } catch(e) {}
}

loadCustomThemeAndLinks();

window.onload = function() {
    try {
        var hash = window.location.hash;
        if (hash && hash.length > 1) {
            var cleanHash = decodeURIComponent(hash.substring(1));
            var p = cleanHash.split("---");
            if (p && p.length >= 3) {
                apply1CAuth(p[0], p[1], p[2], p.length >= 4 ? p[3] : 'user');
            }
        }
    } catch(e) {}
};
</script>
</body>
</html>
3. admin.html
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Admin Panel - 1C Chat</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Segoe UI', Arial, sans-serif; background: #f3f4f6; }
        
        .login-overlay {
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.8);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 1000;
        }
        
        .login-box {
            background: white;
            padding: 30px;
            border-radius: 12px;
            width: 400px;
            box-shadow: 0 4px 20px rgba(0,0,0,0.2);
        }
        
        .login-box h2 { margin-bottom: 20px; color: #1f2937; }
        .login-box input { width: 100%; padding: 10px; margin: 10px 0; border: 1px solid #d1d5db; border-radius: 6px; }
        .login-box button { width: 100%; padding: 10px; background: #2563eb; color: white; border: none; border-radius: 6px; cursor: pointer; }
        .error { color: #ef4444; margin-top: 10px; display: none; }
        
        .admin-container { display: none; min-height: 100vh; }
        .sidebar { width: 260px; background: #1f2937; color: white; padding: 20px; }
        .sidebar h2 { font-size: 20px; margin-bottom: 30px; color: #60a5fa; }
        .sidebar nav a { display: block; padding: 12px; margin-bottom: 8px; color: #9ca3af; text-decoration: none; border-radius: 8px; cursor: pointer; transition: all 0.3s; }
        .sidebar nav a:hover, .sidebar nav a.active { background: #374151; color: white; }
        
        .main-content { flex: 1; padding: 20px; overflow-y: auto; }
        .tab { display: none; }
        .tab.active { display: block; }
        
        .card { background: white; border-radius: 12px; padding: 24px; margin-bottom: 24px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }
        .card h3 { margin-bottom: 20px; color: #374151; font-size: 18px; }
        
        .form-group { margin-bottom: 20px; }
        .form-group label { display: block; margin-bottom: 8px; font-weight: 600; color: #4b5563; }
        .form-group input, .form-group select, .form-group textarea { width: 100%; padding: 10px; border: 1px solid #d1d5db; border-radius: 6px; font-size: 14px; }
        .form-group textarea { min-height: 100px; font-family: monospace; }
        
        button { background: #2563eb; color: white; border: none; padding: 10px 20px; border-radius: 6px; cursor: pointer; font-size: 14px; transition: all 0.3s; }
        button:hover { background: #1d4ed8; transform: translateY(-1px); }
        
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #e5e7eb; }
        th { background: #f9fafb; font-weight: 600; color: #374151; }
        
        .sql-result { margin-top: 20px; overflow-x: auto; background: #1f2937; color: #10b981; padding: 15px; border-radius: 6px; font-family: monospace; font-size: 12px; }
        
        .color-row { display: flex; gap: 10px; align-items: center; }
        .color-row input[type="color"] { width: 60px; height: 40px; cursor: pointer; }
        .color-row span { flex: 1; }
        
        .toast { position: fixed; bottom: 20px; right: 20px; background: #10b981; color: white; padding: 12px 20px; border-radius: 8px; display: none; z-index: 1000; }
    </style>
</head>
<body>
    <div id="login-overlay" class="login-overlay">
        <div class="login-box">
            <h2>🔐 Вход в админ-панель</h2>
            <input type="text" id="login-user-id" placeholder="ID пользователя">
            <input type="text" id="login-org-id" placeholder="ID организации">
            <div id="login-error" class="error">❌ Недостаточно прав для входа</div>
            <button onclick="login()">Войти</button>
        </div>
    </div>
    
    <div id="admin-container" class="admin-container">
        <div class="sidebar">
            <h2>⚙️ Админ-панель</h2>
            <nav>
                <a id="nav-theme" onclick="showTab('theme')" class="active">🎨 Внешний вид</a>
                <a id="nav-users" onclick="showTab('users')">👥 Пользователи</a>
                <a id="nav-permissions" onclick="showTab('permissions')">🔐 Права доступа</a>
                <a id="nav-database" onclick="showTab('database')">🗄️ База данных</a>
                <a id="nav-links" onclick="showTab('links')">🔗 Ссылки</a>
                <a id="nav-logs" onclick="showTab('logs')">📋 Логи</a>
            </nav>
        </div>
        
        <div class="main-content">
            <div id="theme-tab" class="tab active">
                <div class="card">
                    <h3>🎨 Настройка цветовой схемы</h3>
                    <div class="form-group">
                        <label>Основной цвет (шапка, кнопки):</label>
                        <div class="color-row">
                            <input type="color" id="primary-color">
                            <span id="primary-color-value"></span>
                        </div>
                    </div>
                    <div class="form-group">
                        <label>Цвет фона страницы:</label>
                        <div class="color-row">
                            <input type="color" id="bg-color">
                            <span id="bg-color-value"></span>
                        </div>
                    </div>
                    <div class="form-group">
                        <label>Цвет текста:</label>
                        <div class="color-row">
                            <input type="color" id="text-color">
                            <span id="text-color-value"></span>
                        </div>
                    </div>
                    <div class="form-group">
                        <label>Название компании:</label>
                        <input type="text" id="company-name" placeholder="Название">
                    </div>
                    <div class="form-group">
                        <label>Логотип:</label>
                        <input type="file" id="logo-file" accept="image/*">
                        <div id="logo-preview" style="margin-top: 10px;"></div>
                    </div>
                    <div class="form-group">
                        <label>Пользовательский CSS:</label>
                        <textarea id="custom-css" placeholder="/* Ваши дополнительные стили */"></textarea>
                    </div>
                    <button onclick="saveTheme()">💾 Сохранить настройки</button>
                </div>
            </div>
            
            <div id="users-tab" class="tab">
                <div class="card">
                    <h3>👥 Пользователи системы</h3>
                    <table id="users-table">
                        <thead>
                            <tr><th>ID</th><th>Имя</th><th>Роль</th><th>Админ-права</th></tr>
                        </thead>
                        <tbody></tbody>
                    </table>
                </div>
            </div>
            
            <div id="permissions-tab" class="tab">
                <div class="card">
                    <h3>🔐 Настройка прав администраторов</h3>
                    <table id="permissions-table">
                        <thead>
                            <tr><th>Администратор</th><th>Управление темами</th><th>Управление БД</th><th>Просмотр логов</th></tr>
                        </thead>
                        <tbody></tbody>
                    </table>
                </div>
            </div>
            
            <div id="database-tab" class="tab">
                <div class="card">
                    <h3>🗄️ Выполнить SQL запрос</h3>
                    <textarea id="sql-query" rows="5" placeholder="SELECT * FROM users LIMIT 10" style="width:100%; font-family:monospace;"></textarea>
                    <button onclick="executeSQL()" style="margin-top: 10px;">▶️ Выполнить</button>
                    <div id="sql-result" class="sql-result"></div>
                </div>
                <div class="card" style="border: 1px solid #fca5a5; background: #fff5f5;">
                    <h3 style="color: #dc2626;">⚠️ Инструменты быстрой очистки (Редактирование БД)</h3>
                    <p style="font-size: 13px; color: #4b5563; margin-bottom: 15px;">Удаление истории сообщений или очистка комнат в один клик:</p>
                    <div style="display: flex; gap: 10px;">
                        <button onclick="quickDbAction('clear_messages')" style="background: #dc2626;">🗑️ Очистить всю историю сообщений</button>
                        <button onclick="quickDbAction('clear_chats')" style="background: #7f1d1d;">❌ Удалить все созданные группы и приватные чаты</button>
                    </div>
                </div>
                <div class="card">
                    <h3>📊 Таблицы базы данных</h3>
                    <div id="tables-list"></div>
                </div>
            </div>
            
            <div id="links-tab" class="tab">
                <div class="card">
                    <h3>🔗 Ссылки на главной странице</h3>
                    <div class="form-group">
                        <label>Ссылка 1 (название):</label>
                        <input type="text" id="link1-name" placeholder="Название ссылки">
                        <label style="margin-top: 10px;">Ссылка 1 (URL):</label>
                        <input type="url" id="link1-url" placeholder="https://...">
                    </div>
                    <div class="form-group">
                        <label>Ссылка 2 (название):</label>
                        <input type="text" id="link2-name" placeholder="Название ссылки">
                        <label style="margin-top: 10px;">Ссылка 2 (URL):</label>
                        <input type="url" id="link2-url" placeholder="https://...">
                    </div>
                    <button onclick="saveLinks()">💾 Сохранить ссылки</button>
                </div>
            </div>
            
            <div id="logs-tab" class="tab">
                <div class="card">
                    <h3>📋 Системные логи</h3>
                    <button onclick="loadLogs()">🔄 Обновить</button>
                    <pre id="logs-content" style="margin-top: 15px; background: #1f2937; color: #10b981; padding: 15px; border-radius: 6px; overflow-x: auto; max-height: 500px; font-size: 12px;"></pre>
                </div>
            </div>
        </div>
    </div>
    
    <div id="toast" class="toast"></div>
    
    <script>
        let currentAdmin = null;
        
        function showToast(message, isError = false) {
            const toast = document.getElementById('toast');
            toast.textContent = message;
            toast.style.background = isError ? '#ef4444' : '#10b981';
            toast.style.display = 'block';
            setTimeout(() => { toast.style.display = 'none'; }, 3000);
        }
        
        async function login() {
            const id_user = document.getElementById('login-user-id').value;
            const id_org = document.getElementById('login-org-id').value;
            
            if (!id_user || !id_org) {
                document.getElementById('login-error').style.display = 'block';
                return;
            }
            
            const response = await fetch('/api/admin/auth', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ id_user, id_org })
            });
            
            const data = await response.json();
            
            if (data.success) {
                currentAdmin = data.admin;
                document.getElementById('login-overlay').style.display = 'none';
                document.getElementById('admin-container').style.display = 'flex';
                loadAllData();
                showToast(`Добро пожаловать, ${currentAdmin.username}!`);
            } else {
                document.getElementById('login-error').style.display = 'block';
            }
        }
        
        async function loadAllData() {
            loadSettings();
            loadUsers();
            loadPermissions();
            loadTables();
            loadLogs();
        }
        
        async function loadSettings() {
            const response = await fetch('/api/admin/settings');
            const settings = await response.json();
            
            if (settings.theme_primary_color) {
                document.getElementById('primary-color').value = settings.theme_primary_color.value;
                document.getElementById('primary-color-value').textContent = settings.theme_primary_color.value;
            }
            if (settings.theme_background_color) {
                document.getElementById('bg-color').value = settings.theme_background_color.value;
                document.getElementById('bg-color-value').textContent = settings.theme_background_color.value;
            }
            if (settings.theme_text_color) {
                document.getElementById('text-color').value = settings.theme_text_color.value;
                document.getElementById('text-color-value').textContent = settings.theme_text_color.value;
            }
            if (settings.company_name) document.getElementById('company-name').value = settings.company_name.value;
            if (settings.custom_css) document.getElementById('custom-css').value = settings.custom_css.value;
            if (settings.link1_name) document.getElementById('link1-name').value = settings.link1_name.value;
            if (settings.link1_url) document.getElementById('link1-url').value = settings.link1_url.value;
            if (settings.link2_name) document.getElementById('link2-name').value = settings.link2_name.value;
            if (settings.link2_url) document.getElementById('link2-url').value = settings.link2_url.value;
            
            document.getElementById('primary-color').oninput = function(e) {
                document.getElementById('primary-color-value').textContent = e.target.value;
            };
            document.getElementById('bg-color').oninput = function(e) {
                document.getElementById('bg-color-value').textContent = e.target.value;
            };
            document.getElementById('text-color').oninput = function(e) {
                document.getElementById('text-color-value').textContent = e.target.value;
            };
        }
        
        async function saveTheme() {
            const theme = {
                theme_primary_color: document.getElementById('primary-color').value,
                theme_background_color: document.getElementById('bg-color').value,
                theme_text_color: document.getElementById('text-color').value,
                company_name: document.getElementById('company-name').value,
                custom_css: document.getElementById('custom-css').value
            };
            
            for (const [key, value] of Object.entries(theme)) {
                await fetch('/api/admin/settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ key, value, admin_user: currentAdmin.id_user })
                });
            }
            
            const logoFile = document.getElementById('logo-file').files[0];
            if (logoFile) {
                const formData = new FormData();
                formData.append('file', logoFile);
                await fetch('/api/admin/upload-logo', { method: 'POST', body: formData });
            }
            
            showToast('Настройки сохранены! Страница обновится через 3 секунды');
            setTimeout(() => location.reload(), 3000);
        }
        
        async function loadUsers() {
            const response = await fetch('/api/admin/users');
            const users = await response.json();
            const tbody = document.querySelector('#users-table tbody');
            tbody.innerHTML = '';
            users.forEach(user => {
                const row = tbody.insertRow();
                row.insertCell(0).textContent = user.id_user;
                row.insertCell(1).textContent = user.username;
                
                const roleCell = row.insertCell(2);
                roleCell.innerHTML = `
                    <span>${user.role === 'admin' ? '👑 Админ' : '👤 Юзер'}</span>
                    <button style="padding:2px 5px; font-size:10px; margin-left:10px; background:#6b7280;" 
                            onclick="toggleUserRole('${user.id_user}', '${user.role}')">🔄 Изменить</button>
                `;
                
                row.insertCell(3).textContent = user.can_manage_themes ? '✅' : '❌';
            });
        }

        async function toggleUserRole(userId, currentRole) {
            const newRole = currentRole === 'admin' ? 'user' : 'admin';
            const sql = `UPDATE users SET role = '${newRole}' WHERE id_user = '${userId}'`;
            
            await fetch('/api/admin/sql', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sql, admin_user: currentAdmin.id_user })
            });
            
            showToast('Роль пользователя изменена успешно');
            loadUsers();
            loadPermissions();
        }
                        
        async function loadPermissions() {
            const response = await fetch('/api/admin/users');
            const users = await response.json();
            const admins = users.filter(u => u.role === 'admin');
            const tbody = document.querySelector('#permissions-table tbody');
            tbody.innerHTML = '';
            admins.forEach(admin => {
                const row = tbody.insertRow();
                row.insertCell(0).textContent = admin.username;
                
                const cell1 = row.insertCell(1);
                const cb1 = document.createElement('input');
                cb1.type = 'checkbox';
                cb1.checked = admin.can_manage_themes;
                cb1.onclick = function() { updatePermissions(admin.id_user, 'can_manage_themes', cb1.checked); };
                cell1.appendChild(cb1);
                
                const cell2 = row.insertCell(2);
                const cb2 = document.createElement('input');
                cb2.type = 'checkbox';
                cb2.checked = admin.can_manage_users;
                cb2.onclick = function() { updatePermissions(admin.id_user, 'can_manage_users', cb2.checked); };
                cell2.appendChild(cb2);
                
                const cell3 = row.insertCell(3);
                const cb3 = document.createElement('input');
                cb3.type = 'checkbox';
                cb3.checked = admin.can_view_logs;
                cb3.onclick = function() { updatePermissions(admin.id_user, 'can_view_logs', cb3.checked); };
                cell3.appendChild(cb3);
            });
        }
        
        async function updatePermissions(userId, permission, value) {
            const permissions = {};
            permissions[permission] = value;
            
            await fetch('/api/admin/user/permissions', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ target_user: userId, permissions })
            });
            
            showToast('Права обновлены');
        }
        
        async function executeSQL() {
            const sql = document.getElementById('sql-query').value;
            if (!sql.trim()) return;
            
            const response = await fetch('/api/admin/sql', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sql, admin_user: currentAdmin.id_user })
            });
            
            const result = await response.json();
            
            if (result.error) {
                document.getElementById('sql-result').innerHTML = `<div style="color: #ef4444;">❌ ${result.error}</div>`;
            } else {
                document.getElementById('sql-result').innerHTML = `<pre>${JSON.stringify(result.rows, null, 2)}</pre>`;
            }
        }
        
        async function loadTables() {
            const response = await fetch('/api/admin/sql', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ 
                    sql: "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY table_name",
                    admin_user: currentAdmin.id_user 
                })
            });
            
            const result = await response.json();
            if (!result.error && result.rows) {
                const tablesDiv = document.getElementById('tables-list');
                tablesDiv.innerHTML = '<div style="display: flex; flex-wrap: wrap; gap: 10px;">';
                result.rows.forEach(row => {
                    tablesDiv.innerHTML += `<span style="background: #e5e7eb; padding: 5px 10px; border-radius: 6px;">📊 ${row.table_name}</span>`;
                });
                tablesDiv.innerHTML += '</div>';
            }
        }
        
        async function saveLinks() {
            const links = {
                link1_name: document.getElementById('link1-name').value,
                link1_url: document.getElementById('link1-url').value,
                link2_name: document.getElementById('link2-name').value,
                link2_url: document.getElementById('link2-url').value
            };
            
            await fetch('/api/admin/links', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ links, admin_user: currentAdmin.id_user })
            });
            
            showToast('Ссылки сохранены');
        }
        
        async function loadLogs() {
            const response = await fetch(`/api/admin/logs?admin_user=${currentAdmin.id_user}`);
            const data = await response.json();
            document.getElementById('logs-content').textContent = data.logs.join('\n');
        }
        
        // Исправленное безопасное переключение вкладок
        function showTab(tabName) {
            document.querySelectorAll('.tab').forEach(function(tab) { tab.classList.remove('active'); });
            document.querySelectorAll('.sidebar nav a').forEach(function(link) { link.classList.remove('active'); });
            
            document.getElementById(tabName + '-tab').classList.add('active');
            
            var activeLink = document.getElementById('nav-' + tabName);
            if (activeLink) {
                activeLink.classList.add('active');
            }
        }
        
        async function quickDbAction(action) {
            let sql = "";
            if (action === 'clear_messages') {
                if (!confirm("Вы уверены, что хотите БЕЗВОЗВРАТНО удалить все сообщения во всех чатах?")) return;
                sql = "TRUNCATE TABLE messages RESTART IDENTITY;";
            } else if (action === 'clear_chats') {
                if (!confirm("Вы уверены, что хотите удалить все приватные и групповые чаты? Общий чат будет сохранен.")) return;
                sql = "DELETE FROM room_participants WHERE id_room != 1; DELETE FROM rooms WHERE id_room != 1; DELETE FROM messages WHERE id_room != 1;";
            }
            
            if (!sql) return;
            
            const response = await fetch('/api/admin/sql', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sql, admin_user: currentAdmin.id_user })
            });
            const result = await response.json();
            if (result.error) {
                showToast('Ошибка БД: ' + result.error, true);
            } else {
                showToast('Таблицы успешно модифицированы!');
                if(typeof loadTables === 'function') loadTables();
            }
        }        
    </script>
</body>
</html>
4. requirements.txt
Plaintext
fastapi
uvicorn
python-socketio
psycopg2-binary
python-multipart
5. Procfile
Plaintext
web: uvicorn main:app --host 0.0.0.0 --port $PORT
Инструкция по развертыванию
Убедись, что переменная DATABASE_URL настроена в Railway.

Остались баги.

- скачивание прикрепленного файла не происходит, один скачивает, другой не скачивает, видимо из-за названия файла.

- скрины не отправляет в чат.

* Учитывай, что фронт запускается на 1С 8.3.5.1231.

* Всегда подглядывай в GEMINI.MD, что бы код не терялся.

* Всегда, когда вносишь правки в файлы, выдавай мне готовый файл.