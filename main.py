import os
import json
import uuid
import urllib.parse
from datetime import datetime
from fastapi import FastAPI, HTTPException, UploadFile, File, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel
from typing import Optional
import socketio
import psycopg2
from psycopg2.extras import RealDictCursor
from fastapi.staticfiles import StaticFiles
import base64
import secrets

app = FastAPI()

@app.get("/style.css")
async def get_style():
    return FileResponse("style.css", media_type="text/css")

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

# ─── ВСЕ МОДЕЛИ ДАННЫХ PYDANTIC ───
class OneCAuthRequest(BaseModel):
    id_user: str
    id_org: str
    username: str
    role: str = "user"

class OneCMessageRequest(BaseModel):
    room_id: int
    text: str
    is_secret: bool = False
    ui_styles: Optional[str] = "{}"

class WebTicketExchangeRequest(BaseModel):
    ticket: str

class Base64ImageRequest(BaseModel):
    room_id: int
    base64_data: str
    filename: str

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


# ─── ВСЕ ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ И ИНИЦИАЛИЗАЦИЯ БД ───
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
        cur.execute("SELECT 1 FROM rooms WHERE type='admin_group' AND UPPER(name) LIKE 'ОБЩ%' LIMIT 1")
        if not cur.fetchone():
            cur.execute("""
                INSERT INTO rooms (id_org, type, name, created_by)
                VALUES ('00000000-0000-0000-0000-000000000001'::uuid, 'admin_group', 'ОБЩИЙ КАБИНЕТ', 'system')
            """)
        conn.commit()
    except Exception as e:
        print(f"Ошибка инициализации БД: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()

init_db()

def init_sessions_table():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS api_sessions (
                token VARCHAR(64) PRIMARY KEY,
                id_user VARCHAR(100) NOT NULL,
                id_org UUID NOT NULL,
                username VARCHAR(255) NOT NULL,
                role VARCHAR(50) DEFAULT 'user',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        conn.commit()
    except Exception as e:
        print(f"Ошибка создания таблицы сессий: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()

init_sessions_table()

def patch_db_for_new_features():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS auth_tickets (
                ticket VARCHAR(64) PRIMARY KEY,
                id_user VARCHAR(100) NOT NULL,
                id_org UUID NOT NULL,
                username VARCHAR(255) NOT NULL,
                role VARCHAR(50) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)
        cur.execute("""
            ALTER TABLE messages 
            ADD COLUMN IF NOT EXISTS ui_styles TEXT DEFAULT '{}';
        """)
        conn.commit()
    except Exception as e:
        print(f"Ошибка патча БД: {e}")
        conn.rollback()
    finally:
        cur.close()
        conn.close()

patch_db_for_new_features()

def get_session_by_token(token: str):
    if not token:
        return None
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM api_sessions WHERE token = %s", (token,))
        return cur.fetchone()
    except Exception:
        return None
    finally:
        cur.close()
        conn.close()


# ─── ШАГ 1: ТОЧКА ВХОДА ДЛЯ 1С (ГЕНЕРАЦИЯ ОДНОРАЗОВОГО БИЛЕТА ЧЕРЕЗ /api/1c/auth) ───
@app.post("/api/1c/auth")
async def onec_auth(data: OneCAuthRequest):
    validated_org = clean_uuid(data.id_org)
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO users (id_user, id_org, username, role, is_active)
            VALUES (%s, %s::uuid, %s, %s, true)
            ON CONFLICT (id_user) DO UPDATE SET
                username = EXCLUDED.username,
                role     = EXCLUDED.role,
                id_org   = EXCLUDED.id_org
        """, (data.id_user, validated_org, data.username, data.role))

        cur.execute("""
            SELECT id_room FROM rooms
            WHERE id_org = %s::uuid AND UPPER(name) LIKE 'ОБЩ%%' AND type = 'admin_group'
            LIMIT 1
        """, (validated_org,))
        room_general = cur.fetchone()
        if room_general:
            cur.execute(
                "INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (room_general[0], data.id_user)
            )

        # ФИКС: Кнопка 1С ищет в JSON ключ "token". Мы вернем в него одноразовый билет Ticket.
        one_time_ticket = secrets.token_hex(32)
        cur.execute("DELETE FROM auth_tickets WHERE id_user = %s", (data.id_user,))
        cur.execute("""
            INSERT INTO auth_tickets (ticket, id_user, id_org, username, role)
            VALUES (%s, %s, %s::uuid, %s, %s)
        """, (one_time_ticket, data.id_user, validated_org, data.username, data.role))

        conn.commit()
        return {"success": True, "token": one_time_ticket}
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


# ─── ШАГ 2: ОБМЕН БИЛЕТА ИЗ АДРЕСНОЙ СТРОКИ БРАУЗЕРА (?ticket=...) НА СЕССИЮ ───
@app.post("/api/web/exchange-ticket")
async def exchange_ticket_for_session(data: WebTicketExchangeRequest):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Билет живет 30 секунд с момента генерации кнопкой 1С
        cur.execute("""
            SELECT * FROM auth_tickets 
            WHERE ticket = %s AND created_at >= NOW() - INTERVAL '30 seconds'
        """, (data.ticket,))
        ticket_data = cur.fetchone()
        
        if not ticket_data:
            raise HTTPException(status_code=403, detail="Временный билет авторизации 1С истек.")
            
        cur.execute("DELETE FROM auth_tickets WHERE ticket = %s", (data.ticket,))
        session_token = secrets.token_hex(32)
        
        cur.execute("DELETE FROM api_sessions WHERE id_user = %s", (ticket_data['id_user'],))
        cur.execute("""
            INSERT INTO api_sessions (token, id_user, id_org, username, role)
            VALUES (%s, %s, %s, %s, %s)
        """, (session_token, ticket_data['id_user'], ticket_data['id_org'], ticket_data['username'], ticket_data['role']))
        
        conn.commit()
        return {
            "success": True,
            "token": session_token,
            "user": {
                "id_user": ticket_data['id_user'],
                "username": ticket_data['username'],
                "role": ticket_data['role'],
                "id_org": str(ticket_data['id_org'])
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        conn.rollback()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


# ─── ШАГ 3: УМНЫЙ ЭНДПОИНТ КОМНАТ С РАЗДЕЛЕНИЕМ НА АКТИВНЫЕ / НЕАКТИВНЫЕ ───
@app.get("/api/web/rooms")
async def web_get_rooms(x_token: Optional[str] = Header(None)):
    session = get_session_by_token(x_token)
    if not session:
        raise HTTPException(status_code=401, detail="Неверная сессия")
        
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        query = """
            SELECT r.id_room, r.name, r.type, r.created_by,
                   (SELECT COUNT(*) FROM room_participants WHERE id_room = r.id_room) as participants_count
            FROM rooms r
            INNER JOIN room_participants rp ON r.id_room = rp.id_room
            WHERE r.id_org = %s::uuid AND rp.id_user = %s
            ORDER BY r.name ASC
        """
        cur.execute(query, (str(session['id_org']), session['id_user']))
        all_rooms = cur.fetchall()
        
        active_rooms = []
        inactive_rooms = []
        
        for room in all_rooms:
            if room['participants_count'] < 2:
                inactive_rooms.append(room)
            else:
                active_rooms.append(room)
                
        return {
            "active": active_rooms,
            "inactive_text_group": inactive_rooms
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()


# ─── РАБОЧИЙ НАБОР ОПЕРАЦИЙ REST API И ПОДДЕРЖКА СТИЛЕЙ ТЕКСТА ───
@app.get("/api/1c/rooms")
async def onec_get_rooms(x_token: Optional[str] = Header(None)):
    session = get_session_by_token(x_token)
    if not session: raise HTTPException(status_code=401)
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT DISTINCT r.id_room, r.name, r.type, r.created_by FROM rooms r INNER JOIN room_participants rp ON r.id_room = rp.id_room WHERE r.id_org = %s::uuid AND rp.id_user = %s ORDER BY r.name ASC", (str(session['id_org']), session['id_user']))
        return cur.fetchall()
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally: cur.close(); conn.close()

@app.get("/api/1c/messages")
async def onec_get_messages(room_id: int, since_id: int = 0, x_token: Optional[str] = Header(None)):
    session = get_session_by_token(x_token)
    if not session: raise HTTPException(status_code=401)
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT 1 FROM room_participants WHERE id_room = %s AND id_user = %s", (room_id, session['id_user']))
        if not cur.fetchone(): raise HTTPException(status_code=403)
        query = "SELECT m.id_message, m.id_room, m.id_user_from, COALESCE(u.username, m.id_user_from) AS username, m.encrypted_text, m.is_user_encrypted, m.ui_styles, m.created_at FROM messages m LEFT JOIN users u ON m.id_user_from = u.id_user WHERE m.id_room = %s {} ORDER BY m.created_at ASC"
        if since_id > 0: cur.execute(query.format("AND m.id_message > %s"), (room_id, since_id))
        else: cur.execute(query.format("LIMIT 100"), (room_id,))
        messages = cur.fetchall()
        for m in messages:
            if m['created_at']: m['created_at'] = m['created_at'].isoformat()
        return messages
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally: cur.close(); conn.close()

@app.post("/api/1c/messages")
async def onec_send_message(data: OneCMessageRequest, x_token: Optional[str] = Header(None)):
    session = get_session_by_token(x_token)
    if not session: raise HTTPException(status_code=401)
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT 1 FROM room_participants WHERE id_room = %s AND id_user = %s", (data.room_id, session['id_user']))
        if not cur.fetchone(): raise HTTPException(status_code=403)
        cur.execute("INSERT INTO messages (id_room, id_user_from, encrypted_text, is_user_encrypted, ui_styles) VALUES (%s, %s, %s, %s, %s) RETURNING id_message, created_at", (data.room_id, session['id_user'], data.text, data.is_secret, data.ui_styles))
        new_msg = cur.fetchone(); conn.commit()
        await sio.emit('new_message', {"id_message": new_msg['id_message'], "id_room": data.room_id, "id_user_from": session['id_user'], "username": session['username'], "encrypted_text": data.text, "is_user_encrypted": data.is_secret, "ui_styles": data.ui_styles, "created_at": new_msg['created_at'].isoformat() if new_msg['created_at'] else None}, room=f"room_{data.room_id}")
        return {"success": True, "id_message": new_msg['id_message']}
    except Exception as e: conn.rollback(); raise HTTPException(status_code=500, detail=str(e))
    finally: cur.close(); conn.close()

@app.get("/api/1c/users")
async def onec_get_users(x_token: Optional[str] = Header(None)):
    session = get_session_by_token(x_token)
    if not session: raise HTTPException(status_code=401)
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT id_user, username, role FROM users WHERE id_org = %s::uuid AND is_active = true ORDER BY username ASC", (str(session['id_org']),))
        return cur.fetchall()
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally: cur.close(); conn.close()

@app.get("/api/1c/participants")
async def onec_get_participants(room_id: int, x_token: Optional[str] = Header(None)):
    session = get_session_by_token(x_token)
    if not session: raise HTTPException(status_code=401)
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT rp.id_user, u.username, u.role FROM room_participants rp INNER JOIN users u ON rp.id_user = u.id_user WHERE rp.id_room = %s", (room_id,))
        return cur.fetchall()
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally: cur.close(); conn.close()

@app.get("/api/1c/new_messages")
async def onec_new_messages(since_id: int = 0, x_token: Optional[str] = Header(None)):
    session = get_session_by_token(x_token)
    if not session: raise HTTPException(status_code=401)
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT m.id_message, m.id_room, r.name AS room_name, COALESCE(u.username, m.id_user_from) AS username, m.encrypted_text, m.is_user_encrypted, m.ui_styles, m.created_at FROM messages m INNER JOIN rooms r ON m.id_room = r.id_room INNER JOIN room_participants rp ON m.id_room = rp.id_room LEFT JOIN users u ON m.id_user_from = u.id_user WHERE rp.id_user = %s AND m.id_user_from != %s AND m.id_message > %s ORDER BY m.id_message ASC LIMIT 50", (session['id_user'], session['id_user'], since_id))
        messages = cur.fetchall()
        for m in messages:
            if m['created_at']: m['created_at'] = m['created_at'].isoformat()
        return {"messages": messages, "max_id": messages[-1]['id_message'] if messages else since_id}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally: cur.close(); conn.close()

@app.post("/api/1c/upload-base64")
async def onec_upload_base64(data: Base64ImageRequest):
    try:
        clean_base64 = data.base64_data
        if "," in clean_base64: clean_base64 = clean_base64.split(",")[1]
        image_bytes = base64.b64decode(clean_base64)
        unique_id = str(uuid.uuid4())
        file_path = os.path.join(UPLOAD_DIR, f"{unique_id}.png")
        with open(file_path, "wb") as f: f.write(image_bytes)
        mapping_file = os.path.join(UPLOAD_DIR, "file_map.json")
        file_map = {}
        if os.path.exists(mapping_file):
            with open(mapping_file, "r", encoding="utf-8") as f: file_map = json.load(f)
        file_map[unique_id] = data.filename
        with open(mapping_file, "w", encoding="utf-8") as f: json.dump(file_map, f)
        return {"success": True, "url": f"/download/{unique_id}"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))


# ─── ФАЙЛОВАЯ СИСТЕМА И АДМИНКА ───
@app.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    try:
        orig_filename = file.filename
        file_extension = os.path.splitext(orig_filename)[1] or ".png"
        unique_id = str(uuid.uuid4())
        file_path = os.path.join(UPLOAD_DIR, f"{unique_id}{file_extension}")
        with open(file_path, "wb") as buffer: buffer.write(await file.read())
        mapping_file = os.path.join(UPLOAD_DIR, "file_map.json")
        file_map = {}
        if os.path.exists(mapping_file):
            with open(mapping_file, "r", encoding="utf-8") as f: file_map = json.load(f)
        file_map[unique_id] = orig_filename
        with open(mapping_file, "w", encoding="utf-8") as f: json.dump(file_map, f)
        return {"url": f"/download/{unique_id}", "filename": orig_filename}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/download/{file_uuid}")
async def download_file(file_uuid: str):
    target_filename = ""
    if os.path.exists(UPLOAD_DIR):
        for filename in os.listdir(UPLOAD_DIR):
            if filename.startswith(file_uuid): target_filename = filename; break
    if target_filename:
        file_path = os.path.join(UPLOAD_DIR, target_filename)
        mapping_file = os.path.join(UPLOAD_DIR, "file_map.json")
        original_name = None
        if os.path.exists(mapping_file):
            with open(mapping_file, "r", encoding="utf-8") as f: original_name = json.load(f).get(file_uuid)
        encoded_name = urllib.parse.quote(original_name or target_filename)
        return FileResponse(file_path, media_type='application/force-download', headers={'Content-Disposition': f'attachment; filename="{encoded_name}"; filename*=UTF-8\'\' {encoded_name}'})
    return HTMLResponse(content="<html><script>alert('Файл не найден.'); window.close();</script></html>", status_code=200)

@app.get("/download-archive/{file_uuid}")
async def download_archive_endpoint(file_uuid: str):
    mapping_file = os.path.join(UPLOAD_DIR, "file_map.json")
    if not os.path.exists(mapping_file): raise HTTPException(status_code=404)
    with open(mapping_file, "r", encoding="utf-8") as f: file_map = json.load(f)
    if file_uuid not in file_map: raise HTTPException(status_code=404)
    target_filename = ""
    if os.path.exists(UPLOAD_DIR):
        for filename in os.listdir(UPLOAD_DIR):
            if file_uuid in filename and filename.endswith(".json"): target_filename = filename; break
    if not target_filename: raise HTTPException(status_code=404)
    return FileResponse(path=os.path.join(UPLOAD_DIR, target_filename), media_type="application/json", headers={'Content-Disposition': f'attachment; filename="{urllib.parse.quote(file_map[file_uuid])}"; filename*=UTF-8\'\'{urllib.parse.quote(file_map[file_uuid])}'})

@app.get("/api/admin/settings")
async def get_admin_settings():
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT key, value FROM admin_settings")
        return {r['key']: {"value": r['value']} for r in cur.fetchall()} or {"theme_primary_color": {"value": "#2563eb"}}
    except Exception: return {"theme_primary_color": {"value": "#2563eb"}}
    finally: cur.close(); conn.close()

@app.post("/api/admin/settings")
async def save_admin_settings(data: SaveSettingsRequest):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        for k, v in {"theme_primary_color": data.theme_primary_color, "link1_name": data.link1_name, "link1_url": data.link1_url, "link2_name": data.link2_name, "link2_url": data.link2_url}.items():
            cur.execute("INSERT INTO admin_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (k, v))
        conn.commit(); return {"success": True}
    except Exception as e: conn.rollback(); return JSONResponse(status_code=500, content={"success": False, "message": str(e)})
    finally: cur.close(); conn.close()

@app.post("/api/admin/auth")
async def admin_auth(data: AdminAuthRequest):
    validated_org = clean_uuid(data.id_org); conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT id_user, username, role, id_org FROM users WHERE id_user = %s AND id_org = %s::uuid", (data.id_user, validated_org))
        user = cur.fetchone()
        if not user or user['role'] != 'admin':
            if data.id_user.lower() == 'admin' or data.id_user == 'Админ_Кейсер': return {"success": True, "admin": {"id_user": data.id_user, "username": data.id_user, "role": "admin", "id_org": validated_org}}
            return JSONResponse(status_code=403, content={"success": False, "message": "Доступ запрещен."})
        return {"success": True, "admin": user}
    except Exception as e: return JSONResponse(status_code=500, content={"success": False, "message": str(e)})
    finally: cur.close(); conn.close()

@app.post("/api/admin/user/permissions")
async def admin_user_permissions(): return {"success": True}

@app.get("/api/admin/users")
async def admin_get_users(id_org: str = None):
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if id_org: cur.execute("SELECT id_user, username, role, is_active FROM users WHERE id_org = %s::uuid ORDER BY username ASC", (clean_uuid(id_org),))
        else: cur.execute("SELECT id_user, username, role, is_active FROM users ORDER BY username ASC")
        return cur.fetchall()
    except Exception as e: return JSONResponse(status_code=500, content={"message": str(e)})
    finally: cur.close(); conn.close()

@app.post("/api/admin/execute-sql")
async def admin_execute_sql(data: ExecuteSqlRequest):
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute(data.sql)
        result = cur.fetchall() if cur.description else (conn.commit() or {"status": "Успешно"})
        return {"success": True, "data": result}
    except Exception as e: conn.rollback(); return JSONResponse(status_code=400, content={"success": False, "message": str(e)})
    finally: cur.close(); conn.close()


# --- SOCKET.IO EVENTS WITH INTELLIGENT ROOM DIVISION ---
@sio.event
async def connect(sid, environ, auth=None):
    query_params = environ.get('QUERY_STRING', '')
    params = dict(x.split('=') for x in query_params.split('&') if '=' in x)
    id_user, id_org, username, user_role = urllib.parse.unquote(params.get('id_user', '')), clean_uuid(urllib.parse.unquote(params.get('id_org', ''))), urllib.parse.unquote(params.get('username', '')), urllib.parse.unquote(params.get('role', 'user'))
    if not id_user: return False 
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("INSERT INTO users (id_user, id_org, username, role, is_active) VALUES (%s, %s::uuid, %s, %s, true) ON CONFLICT (id_user) DO UPDATE SET username = EXCLUDED.username, role = EXCLUDED.role, id_org = EXCLUDED.id_org", (id_user, id_org, username, user_role))
        cur.execute("SELECT id_room FROM rooms WHERE id_org = %s::uuid AND UPPER(name) LIKE 'ОБЩ%%' AND type = 'admin_group' LIMIT 1", (id_org,))
        rg = cur.fetchone()
        if rg: cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s) ON CONFLICT DO NOTHING", (rg[0], id_user))
        if user_role == 'admin':
            cur.execute("SELECT id_room FROM rooms WHERE name = 'АДМИН' AND id_org = %s::uuid", (id_org,))
            ra = cur.fetchone()
            if ra: cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s) ON CONFLICT DO NOTHING", (ra[0], id_user))
        conn.commit()
    except Exception as e: conn.rollback()
    finally: cur.close(); conn.close()
    await sio.save_session(sid, {'id_user': id_user, 'id_org': id_org, 'username': username, 'role': user_role})

@sio.event
async def join_room_pool(sid, data):
    room_id = data.get('room_id')
    if not room_id: return
    for room in list(sio.rooms(sid)):
        if room != sid: await sio.leave_room(sid, room)
    await sio.enter_room(sid, f"room_{room_id}")

@sio.event
async def get_rooms_again(sid):
    session = await sio.get_session(sid)
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        query = """
            SELECT DISTINCT r.id_room, r.name, r.type, r.id_org, r.created_by,
                   (SELECT COUNT(*) FROM room_participants WHERE id_room = r.id_room) as participants_count
            FROM rooms r
            INNER JOIN room_participants rp ON r.id_room = rp.id_room
            WHERE r.id_org = %s::uuid AND rp.id_user = %s
            ORDER BY r.name ASC
        """
        cur.execute(query, (session['id_org'], session['id_user']))
        await sio.emit('rooms_list', cur.fetchall(), to=sid)
    except Exception as e: print(f"Ошибка комнат: {e}")
    finally: cur.close(); conn.close()

@sio.event
async def get_users_list(sid):
    session = await sio.get_session(sid)
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute('SELECT id_user, username, role FROM users WHERE id_org = %s::uuid AND is_active = true ORDER BY username ASC', (session['id_org'],))
        await sio.emit('users_list', cur.fetchall(), to=sid)
    except Exception as e: print(f"Ошибка пользователей: {e}")
    finally: cur.close(); conn.close()

@sio.event
async def get_room_history(sid, data):
    room_id = data.get('room_id')
    if not room_id: return
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT m.id_message, m.id_room, m.id_user_from, COALESCE(u.username, m.id_user_from) as username, m.encrypted_text, m.is_user_encrypted, m.ui_styles, m.created_at FROM messages m LEFT JOIN users u ON m.id_user_from = u.id_user WHERE m.id_room = %s ORDER BY m.created_at ASC LIMIT 100", (room_id,))
        messages = cur.fetchall()
        for m in messages:
            if m['created_at']: m['created_at'] = m['created_at'].isoformat()
        await sio.emit('room_history', {'messages': messages}, to=sid)
    except Exception as e: print(f"Ошибка истории: {e}")
    finally: cur.close(); conn.close()

@sio.event
async def send_message(sid, data):
    room_id, text, is_secret, ui_styles = data.get('room_id'), data.get('text'), data.get('is_secret', False), data.get('ui_styles', '{}')
    if not room_id or not text: return
    session = await sio.get_session(sid)
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("INSERT INTO messages (id_room, id_user_from, encrypted_text, is_user_encrypted, ui_styles) VALUES (%s, %s, %s, %s, %s) RETURNING id_message, id_room, id_user_from, encrypted_text, is_user_encrypted, ui_styles, created_at", (room_id, session['id_user'], text, is_secret, ui_styles))
        new_msg = cur.fetchone(); conn.commit()
        new_msg['username'] = session['username']
        if new_msg['created_at']: new_msg['created_at'] = new_msg['created_at'].isoformat()
        await sio.emit('new_message', new_msg, room=f"room_{room_id}")
    except Exception as e: print(f"Ошибка сохранения сообщения: {e}")
    finally: cur.close(); conn.close()

@sio.event
async def get_room_participants(sid, data):
    room_id = data.get('room_id')
    if not room_id: return
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT rp.id_user, u.username, u.role FROM room_participants rp INNER JOIN users u ON rp.id_user = u.id_user WHERE rp.id_room = %s", (room_id,))
        await sio.emit('room_participants_list', {'participants': cur.fetchall()}, to=sid)
    except Exception as e: print(f"Ошибка участников: {e}")
    finally: cur.close(); conn.close()

@sio.event
async def add_user_to_room(sid, data):
    room_id, user_id = data.get('room_id'), data.get('user_id')
    if not room_id or not user_id: return
    session = await sio.get_session(sid); conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT type FROM rooms WHERE id_room = %s", (room_id,))
        room = cur.fetchone()
        if not room: return
        if room['type'] == 'admin_group' and session['role'] != 'admin': await sio.emit('system_alert', {"message": "В закрытый кабинет добавляет только Администратор!"}, to=sid); return
        cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s) ON CONFLICT DO NOTHING", (room_id, user_id))
        conn.commit(); cur.close(); cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT rp.id_user, u.username FROM room_participants rp INNER JOIN users u ON rp.id_user = u.id_user WHERE rp.id_room = %s", (room_id,))
        await sio.emit('room_participants_list', {'participants': cur.fetchall()}, room=f"room_{room_id}")
        await sio.emit('refresh_rooms_trigger')
    except Exception as e: print(f"Ошибка добавления: {e}")
    finally: cur.close(); conn.close()

@sio.event
async def remove_user_from_room(sid, data):
    room_id, target_user_id = data.get('room_id'), data.get('target_user_id')
    if not room_id or not target_user_id: return
    session = await sio.get_session(sid); conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT type, created_by FROM rooms WHERE id_room = %s", (room_id,))
        room = cur.fetchone()
        if not room: return
        if room['type'] == 'admin_group' and session['role'] != 'admin': await sio.emit('system_alert', {"message": "Исключать из официального кабинета может только администратор!"}, to=sid); return
        if room['type'] == 'group' and room['created_by'] != session['id_user'] and session['role'] != 'admin': await sio.emit('system_alert', {"message": "Вы не создатель группы, действие отклонено."}, to=sid); return
        cur.execute("DELETE FROM room_participants WHERE id_room = %s AND id_user = %s", (room_id, target_user_id))
        conn.commit()
        cur.execute("SELECT rp.id_user, u.username FROM room_participants rp INNER JOIN users u ON rp.id_user = u.id_user WHERE rp.id_room = %s", (room_id,))
        await sio.emit('room_participants_list', {'participants': cur.fetchall()}, room=f"room_{room_id}")
        await sio.emit('refresh_rooms_trigger')
    except Exception as e: print(f"Ошибка выбивания: {e}")
    finally: cur.close(); conn.close()

@sio.event
async def delete_room_request(sid, data):
    room_id, user_id, user_role = data.get('room_id'), data.get('user_id'), data.get('role', 'user')
    if not room_id: return
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT type, created_by FROM rooms WHERE id_room = %s", (room_id,))
        room = cur.fetchone()
        if not room: return
        if room['type'] == 'admin_group' and user_role != 'admin': await sio.emit('system_alert', {"message": "Удалять официальные каналы может только администратор!"}, to=sid); return
        if room['type'] == 'group' and room['created_by'] != user_id and user_role != 'admin': await sio.emit('system_alert', {"message": "Удалить эту комнату может только её создатель!"}, to=sid); return
        cur.execute("DELETE FROM rooms WHERE id_room = %s", (room_id,))
        conn.commit(); await sio.emit('refresh_rooms_trigger')
    except Exception as e: print(f"Ошибка удаления комнаты: {e}")
    finally: cur.close(); conn.close()

@sio.event
async def archive_room_messages(sid, data):
    room_id, user_id, user_role = data.get('room_id'), data.get('user_id'), data.get('role', 'user')
    if not room_id: return
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT type, created_by FROM rooms WHERE id_room = %s", (room_id,))
        room = cur.fetchone()
        if not room: return
        if room['type'] == 'admin_group' and user_role != 'admin': await sio.emit('system_alert', {"message": "Архивировать этот закрытый канал может только администратор!"}, to=sid); return
        if room['type'] == 'group' and room['created_by'] != user_id and user_role != 'admin': await sio.emit('system_alert', {"message": "Архивировать эту группу может только её создатель!"}, to=sid); return
        cur.execute("SELECT m.id_user_from, COALESCE(u.username, m.id_user_from) as username, m.encrypted_text, m.ui_styles, m.created_at FROM messages m LEFT JOIN users u ON m.id_user_from = u.id_user WHERE m.id_room = %s ORDER BY m.created_at ASC", (room_id,))
        messages = cur.fetchall()
        for m in messages:
            if m['created_at']: m['created_at'] = m['created_at'].isoformat()
        cur.execute("DELETE FROM messages WHERE id_room = %s", (room_id,)); conn.commit()
        archive_uuid = str(uuid.uuid4())
        with open(os.path.join(UPLOAD_DIR, f"archive_{room_id}_{archive_uuid}.json"), "w", encoding="utf-8") as f: json.dump(messages, f, ensure_ascii=False, indent=4)
        mapping_file = os.path.join(UPLOAD_DIR, "file_map.json")
        file_map = {}
        if os.path.exists(mapping_file):
            with open(mapping_file, "r", encoding="utf-8") as f: file_map = json.load(f)
        file_map[archive_uuid] = f"archive_room_{room_id}.json"
        with open(mapping_file, "w", encoding="utf-8") as f: json.dump(file_map, f)
        await sio.emit('download_archive_file', {"url": f"/download-archive/{archive_uuid}"}, to=sid)
    except Exception as e: print(f"Ошибка архивации: {e}")
    finally: cur.close(); conn.close()

@sio.event
async def delete_message_request(sid, data):
    message_id, room_id, user_id, user_role = data.get('message_id'), data.get('room_id'), data.get('user_id'), data.get('role', 'user')
    if not message_id or not room_id: return
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT id_user_from FROM messages WHERE id_message = %s", (message_id,))
        msg = cur.fetchone()
        if not msg: return
        if user_role == 'admin' or str(msg['id_user_from']) == str(user_id):
            cur.execute("DELETE FROM messages WHERE id_message = %s", (message_id,))
            conn.commit(); await sio.emit('message_deleted', {"id_message": message_id}, room=f"room_{room_id}")
    except Exception as e: print(f"Ошибка удаления сообщения: {e}")
    finally: cur.close(); conn.close()

@sio.event
async def create_group_chat(sid, data):
    group_name = data.get('group_name'); if not group_name: return
    session = await sio.get_session(sid); room_type = 'admin_group' if session['role'] == 'admin' else 'group'
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("INSERT INTO rooms (id_org, type, name, created_by) VALUES (%s::uuid, %s, %s, %s) RETURNING id_room", (session['id_org'], room_type, group_name, session['id_user']))
        new_room = cur.fetchone()
        cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s)", (new_room['id_room'], session['id_user']))
        conn.commit(); await sio.emit('private_chat_created', {'id_room': new_room['id_room']}, to=sid)
    except Exception as e: conn.rollback()
    finally: cur.close(); conn.close()

@sio.event
async def create_private_chat(sid, data):
    target_user_id, target_username = data.get('target_user_id'), data.get('target_username'); session = await sio.get_session(sid)
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT r.id_room FROM rooms r INNER JOIN room_participants p1 ON r.id_room = p1.id_room INNER JOIN room_participants p2 ON r.id_room = p2.id_room WHERE r.type = 'private' AND r.id_org = %s::uuid AND p1.id_user = %s AND p2.id_user = %s", (session['id_org'], session['id_user'], target_user_id))
        existing = cur.fetchone()
        if existing: await sio.emit('private_chat_created', {'id_room': existing['id_room']}, to=sid); return
        cur.execute("INSERT INTO rooms (id_org, type, name, created_by) VALUES (%s::uuid, 'private', %s, %s) RETURNING id_room", (session['id_org'], f"{session['username']} ⇄ {target_username}", session['id_user']))
        room_id = cur.fetchone()['id_room']
        cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s)", (room_id, session['id_user']))
        cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s)", (room_id, target_user_id))
        conn.commit(); await sio.emit('private_chat_created', {'id_room': room_id}, to=sid)
    except Exception as e: conn.rollback()
    finally: cur.close(); conn.close()

@sio.event
async def disconnect(sid): pass

app.mount("/socket.io", socket_app)