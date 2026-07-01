import os
import json
import uuid
import urllib.parse
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException, UploadFile, File, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from pydantic import BaseModel
from typing import Optional
import socketio
import psycopg2
from psycopg2.extras import RealDictCursor
import secrets
from fastapi.staticfiles import StaticFiles
from fastapi import Security, Depends
from fastapi.security.api_key import APIKeyHeader

# Секретный ключ, который будете знать только вы и ваша 1С
API_KEY_1C = "MasterKey@For1C_5835234"
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

app = FastAPI()

# Убедись, что папка static существует
if not os.path.exists("static"):
    os.makedirs("static")
app.mount("/static", StaticFiles(directory="static"), name="static")

UPLOAD_DIR = "uploads"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

@app.exception_handler(422)
async def validation_exception_handler(request: Request, exc):
    print(f"Ошибка валидации JSON: {exc.errors()}") 
    return JSONResponse(status_code=422, content={"detail": exc.errors()})

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
socket_app = socketio.ASGIApp(sio, other_asgi_app=app)

UPLOAD_DIR = "uploads"
if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

# СЕРВЕРНЫЙ РЕЕСТР ДЛЯ СТАТУСОВ И ОТМЕТОК ПРОЧТЕНИЯ
online_users = {}       
message_reads = {}      

# Pydantic модели
class ShopInfo(BaseModel):
    address: Optional[str] = ""
    phones: Optional[str] = ""
    schedule: Optional[str] = ""
    note: Optional[str] = ""

class OneCAuthRequest(BaseModel):
    id_user: str
    id_org: str
    username: Optional[str] = ""
    role: Optional[str] = "user"
    shop_name: Optional[str] = None
    shop_info: Optional[ShopInfo] = None

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
    login: str
    password: str
    id_org: str

class UpdateUserRoleRequest(BaseModel):
    id_user: str
    role: str
    admin_login: Optional[str] = "Admin"

class UpdateRoomRequest(BaseModel):
    id_room: int
    name: str
    type: str
    created_by: str 
    admin_login: Optional[str] = "Admin"

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

def check_and_create_global_rooms(cur, id_org, user_id, user_role, shop_name=None):
    # 1. ОБЩИЙ
    cur.execute(
        "SELECT id_room FROM rooms WHERE id_org = %s::uuid AND UPPER(name) = 'ОБЩИЙ' LIMIT 1", 
        (id_org,)
    )
    room_general = cur.fetchone()
    if not room_general:
        cur.execute(
            "INSERT INTO rooms (id_org, type, name, created_by) VALUES (%s::uuid, 'admin_group', 'ОБЩИЙ', %s) RETURNING id_room", 
            (id_org, user_id)
        )
        room_general = cur.fetchone()
    
    general_room_id = room_general['id_room'] if isinstance(room_general, dict) else room_general[0]
    cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s) ON CONFLICT DO NOTHING", (general_room_id, user_id))

    # 2. АДМИН
    if user_role == 'admin':
        cur.execute(
            "SELECT id_room FROM rooms WHERE id_org = %s::uuid AND UPPER(name) = 'АДМИН' LIMIT 1", 
            (id_org,)
        )
        room_admin = cur.fetchone()
        if not room_admin:
            cur.execute(
                "INSERT INTO rooms (id_org, type, name, created_by) VALUES (%s::uuid, 'admin_group', 'АДМИН', %s) RETURNING id_room", 
                (id_org, user_id)
            )
            room_admin = cur.fetchone()
        
        admin_room_id = room_admin['id_room'] if isinstance(room_admin, dict) else room_admin[0]
        cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s) ON CONFLICT DO NOTHING", (admin_room_id, user_id))

    # 3. МАГАЗИН
    if shop_name and shop_name.strip() != "":
        cur.execute(
            "SELECT id_room FROM rooms WHERE id_org = %s::uuid AND name = %s LIMIT 1", 
            (id_org, shop_name)
        )
        room_shop = cur.fetchone()
        if not room_shop:
            cur.execute(
                "INSERT INTO rooms (id_org, type, name, created_by) VALUES (%s::uuid, 'group', %s, %s) RETURNING id_room", 
                (id_org, shop_name, user_id)
            )
            room_shop = cur.fetchone()
        
        shop_room_id = room_shop['id_room'] if isinstance(room_shop, dict) else room_shop[0]
        cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s) ON CONFLICT DO NOTHING", (shop_room_id, user_id))

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS organizations (
                id_org UUID PRIMARY KEY,
                name VARCHAR(255)
            );
            CREATE TABLE IF NOT EXISTS users (
                id_user VARCHAR(100) PRIMARY KEY,
                id_org UUID NOT NULL,
                username VARCHAR(255) NOT NULL,
                role VARCHAR(50) DEFAULT 'user',
                is_active BOOLEAN DEFAULT true
            );
            CREATE TABLE IF NOT EXISTS rooms (
                id_room SERIAL PRIMARY KEY,
                id_org UUID NOT NULL,
                type VARCHAR(50) NOT NULL,
                name VARCHAR(255) NOT NULL,
                created_by VARCHAR(100),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS room_participants (
                id_room INT REFERENCES rooms(id_room) ON DELETE CASCADE,
                id_user VARCHAR(100),
                PRIMARY KEY (id_room, id_user)
            );
            CREATE TABLE IF NOT EXISTS messages (
                id_message SERIAL PRIMARY KEY,
                id_room INT REFERENCES rooms(id_room) ON DELETE CASCADE,
                reply_to INT REFERENCES messages(id_message) ON DELETE SET NULL,
                id_user_from VARCHAR(100),
                encrypted_text TEXT NOT NULL,
                is_user_encrypted BOOLEAN DEFAULT false,
                ui_styles TEXT DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS api_sessions (
                token VARCHAR(64) PRIMARY KEY,
                id_user VARCHAR(100) NOT NULL,
                id_org UUID NOT NULL,
                username VARCHAR(255) NOT NULL,
                role VARCHAR(50) DEFAULT 'user',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS auth_tickets (
                ticket VARCHAR(64) PRIMARY KEY,
                id_user VARCHAR(100) NOT NULL,
                id_org UUID NOT NULL,
                username VARCHAR(255) NOT NULL,
                role VARCHAR(50) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS shops (
                shop_name VARCHAR(255) PRIMARY KEY,
                address TEXT,
                phones TEXT,
                schedule TEXT,
                note TEXT
            );
            CREATE TABLE IF NOT EXISTS user_shop_info (
                id_user VARCHAR(255) PRIMARY KEY,
                shop_name TEXT,
                address TEXT,
                phones TEXT,
                schedule TEXT,
                note TEXT
            ); 
            CREATE TABLE IF NOT EXISTS admin_users (
                login VARCHAR(100) PRIMARY KEY,
                password VARCHAR(255) NOT NULL,
                id_org UUID NOT NULL
            );
            CREATE TABLE IF NOT EXISTS admin_settings (
                key VARCHAR(255) PRIMARY KEY,
                value TEXT
            ); 
            CREATE TABLE IF NOT EXISTS admin_logs (
                id SERIAL PRIMARY KEY,
                admin_login VARCHAR(100),
                action TEXT,
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

def log_admin_action(admin_login: str, action: str):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO admin_logs (admin_login, action) VALUES (%s, %s)", (admin_login, action))
        conn.commit()
    except:
        conn.rollback()
    finally:
        cur.close(); conn.close()

def get_session_by_token(token: str):
    if not token: return None
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM api_sessions WHERE token = %s", (token,))
        return cur.fetchone()
    except Exception: return None
    finally: cur.close(); conn.close()

@app.get("/api/admin/logs")
async def get_admin_logs():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM admin_logs ORDER BY created_at DESC LIMIT 50")
        return cur.fetchall()
    finally:
        cur.close(); conn.close()
        
@app.get("/admin", response_class=HTMLResponse)
async def get_admin_page():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(current_dir, "admin.html"), "r", encoding="utf-8") as f: 
        return f.read()
    
@app.post("/api/admin/auth")
async def admin_auth(data: AdminAuthRequest):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT COUNT(*) FROM admin_users")
        if cur.fetchone()['count'] == 0:
            raise HTTPException(status_code=403, detail="Админ-панель не инициализирована.")
        
        cur.execute("SELECT * FROM admin_users WHERE login = %s AND password = %s AND id_org = %s", 
                    (data.login, data.password, data.id_org))
        admin = cur.fetchone()
        if not admin:
            raise HTTPException(status_code=403, detail="Неверные данные.")
        
        log_admin_action(admin['login'], "Успешный вход в админ-панель")
        return {"success": True, "admin": {"username": admin['login']}}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close(); conn.close()

@app.get("/api/admin/users")
async def admin_get_users():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT id_user, username, role FROM users ORDER BY username ASC")
        return cur.fetchall()
    finally:
        cur.close(); conn.close()

@app.post("/api/admin/update-role")
async def admin_update_role(data: UpdateUserRoleRequest):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE users SET role = %s WHERE id_user = %s", (data.role, data.id_user))
        conn.commit()
        log_admin_action(data.admin_login, f"Изменена роль пользователя {data.id_user} на '{data.role}'")
        return {"success": True}
    except Exception as e:
        conn.rollback()
        return {"success": False, "message": str(e)}
    finally:
        cur.close(); conn.close()

@app.get("/api/admin/rooms")
async def admin_get_rooms():
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT id_room, name, type, created_by FROM rooms")
        return cur.fetchall()
    finally:
        cur.close(); conn.close()

@app.post("/api/admin/update-room")
async def admin_update_room(data: UpdateRoomRequest):
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("UPDATE rooms SET name = %s, type = %s, created_by = %s WHERE id_room = %s", 
                    (data.name, data.type, data.created_by, data.id_room))
        conn.commit()
        log_admin_action(data.admin_login, f"Обновлен кабинет/комната №{data.id_room} (новое имя: '{data.name}')")
        return {"success": True}
    except Exception as e:
        conn.rollback()
        return {"success": False, "message": str(e)}
    finally:
        cur.close(); conn.close()

@app.get("/api/admin/files/{folder}")
async def admin_get_files(folder: str):
    target = "static" if folder == "static" else UPLOAD_DIR
    if os.path.exists(target):
        return os.listdir(target)
    return []

@app.post("/api/admin/upload-file/{folder}")
async def admin_upload_file(folder: str, file: UploadFile = File(...)):
    target_dir = "static" if folder == "static" else UPLOAD_DIR
    file_location = os.path.join(target_dir, file.filename)
    with open(file_location, "wb+") as file_object:
        file_object.write(file.file.read())
    
    # Логирование замены/добавления статики/лого
    log_admin_action("Admin", f"Загружен или обновлен файл '{file.filename}' в директорию '{folder}'")
    return {"success": True, "filename": file.filename}

@app.delete("/api/admin/files/uploads/{filename}")
async def admin_delete_upload(filename: str):
    file_path = os.path.join(UPLOAD_DIR, filename)
    if os.path.exists(file_path):
        os.remove(file_path)
        log_admin_action("Admin", f"Вручную удален файл вложений: {filename}")
        return {"success": True}
    raise HTTPException(status_code=404, detail="Файл не найден")

@app.post("/api/admin/settings")
async def admin_save_settings(settings: dict):
    conn = get_db_connection(); cur = conn.cursor()
    try:
        for k, v in settings.items():
            cur.execute("INSERT INTO admin_settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (k, v))
        conn.commit()
        log_admin_action("Admin", f"Обновлены настройки админ-панели (цветовая схема / ссылки)")
        return {"success": True}
    except Exception as e: 
        conn.rollback() 
        return {"success": False, "message": str(e)}
    finally: 
        cur.close(); conn.close()

@app.get("/api/admin/settings")
async def admin_get_settings():
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT key, value FROM admin_settings")
        return {r['key']: r['value'] for r in cur.fetchall()}
    finally: cur.close(); conn.close()

@app.post("/api/admin/sql")
async def admin_execute_sql(data: dict):
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        sql_query = data.get("sql", "")
        cur.execute(sql_query)
        
        # Логируем выполнение произвольных SQL запросов (кроме запросов структуры)
        if not sql_query.strip().lower().startswith("select") and not "information_schema" in sql_query:
            log_admin_action("Admin", "Выполнен прямой SQL-запрос изменения данных")

        if sql_query.strip().lower().startswith("select"):
            return {"success": True, "data": cur.fetchall()}
        conn.commit()
        return {"success": True, "message": "Запрос успешно выполнен"}
    except Exception as e: conn.rollback(); return {"success": False, "message": str(e)}
    finally: cur.close(); conn.close()

@app.get("/", response_class=HTMLResponse)
async def get_chat_page():
    current_dir = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(current_dir, "index.html"), "r", encoding="utf-8") as f: 
        return f.read()

@app.get("/style.css")
async def get_style():
    return FileResponse("style.css", media_type="text/css")

def sync_user_shop_room(cur, id_org, user_id, user_role, shop_name, shop_info=None):
    # Проверка: shop_name должен быть строкой и не быть пустым
    if not isinstance(shop_name, str) or shop_name.strip() == "":
        return

    # Сохраняем информацию о магазине
    if shop_info:
        cur.execute("""
            INSERT INTO user_shop_info (id_user, shop_name, address, phones, schedule, note)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (id_user) DO UPDATE SET 
            shop_name = EXCLUDED.shop_name, address = EXCLUDED.address, 
            phones = EXCLUDED.phones, schedule = EXCLUDED.schedule, note = EXCLUDED.note
        """, (user_id, shop_name, shop_info.address, shop_info.phones, shop_info.schedule, shop_info.note))

    # 1. Пытаемся найти существующий кабинет по имени
    cur.execute(
        "SELECT id_room, type FROM rooms WHERE id_org = %s::uuid AND name = %s LIMIT 1", 
        (id_org, shop_name)
    )
    room = cur.fetchone()

    # 2. Логика создания или использования кабинета
    if room:
        # Извлекаем ID корректно для любого типа курсора
        room_id = room['id_room'] if isinstance(room, dict) else room[0]
        room_type = room['type'] if isinstance(room, dict) else room[1]
        
        # Если админ зашел в комнату, которая была создана как 'group', обновляем до 'admin_group'
        if user_role == 'admin' and room_type != 'admin_group':
            cur.execute("UPDATE rooms SET type = 'admin_group' WHERE id_room = %s", (room_id,))
    else:
        # Кабинета нет — создаем с нужным типом
        new_type = 'admin_group' if user_role == 'admin' else 'group'
        cur.execute(
            "INSERT INTO rooms (id_org, type, name, created_by) VALUES (%s::uuid, %s, %s, %s) RETURNING id_room", 
            (id_org, new_type, shop_name, user_id)
        )
        room_id = cur.fetchone()[0]

    # 3. Добавляем пользователя, если его нет в участниках
    cur.execute(
        "INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s) ON CONFLICT DO NOTHING", 
        (room_id, user_id)
    )

@app.post("/api/1c/auth")
async def onec_auth(data: OneCAuthRequest):
    import traceback
    validated_org = clean_uuid(data.id_org)
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        print(f"--- ПОПЫТКА АВТОРИЗАЦИИ 1С ---")
        print(f"User ID: {data.id_user}, Username: {data.username}, Role: {data.role}, Org ID: {validated_org}")

        cur.execute(
            "INSERT INTO organizations (id_org, name) VALUES (%s::uuid, %s) ON CONFLICT DO NOTHING",
            (validated_org, f"Организация {validated_org[:8]}")
        )

        cur.execute("""
            INSERT INTO users (id_user, id_org, username, role, is_active)
            VALUES (%s, %s::uuid, %s, %s, true)
            ON CONFLICT (id_user) DO UPDATE SET username = EXCLUDED.username, role = EXCLUDED.role, id_org = EXCLUDED.id_org
        """, (data.id_user, validated_org, data.username, data.role))

        check_and_create_global_rooms(cur, validated_org, data.id_user, data.role)
        sync_user_shop_room(cur, validated_org, data.id_user, data.role, data.shop_name, data.shop_info)

        one_time_ticket = secrets.token_hex(32)
        cur.execute("DELETE FROM auth_tickets WHERE id_user = %s", (data.id_user,))
        cur.execute(
            "INSERT INTO auth_tickets (ticket, id_user, id_org, username, role) VALUES (%s, %s, %s::uuid, %s, %s)", 
            (one_time_ticket, data.id_user, validated_org, data.username, data.role)
        )
        
        conn.commit()
        return {"success": True, "token": one_time_ticket}
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        cur.close()
        conn.close()

async def verify_1c_key(header_key: str = Security(api_key_header)):
    if header_key == API_KEY_1C:
        return header_key
    raise HTTPException(status_code=403, detail="Доступ запрещен: неверный API-ключ")

# 1С делает запрос сюда с заголовком X-API-Key. Эндпоинт защищен.
@app.get("/api/1c/files")
async def get_uploads_list_for_1c(auth: str = Depends(verify_1c_key)):
    mapping_file = os.path.join(UPLOAD_DIR, "file_map.json")
    file_map = {}
    
    if os.path.exists(mapping_file):
        with open(mapping_file, "r", encoding="utf-8") as f:
            file_map = json.load(f)
            
    files_list = []
    if os.path.exists(UPLOAD_DIR):
        for filename in os.listdir(UPLOAD_DIR):
            if filename == "file_map.json" or filename.startswith("archive_"):
                continue
            file_uuid = os.path.splitext(filename)[0]
            original_name = file_map.get(file_uuid, filename)
            
            files_list.append({
                "storage_name": filename,
                "original_name": original_name,
                "download_url": f"/download/{filename}" # Ссылка снова простая
            })
    return {"success": True, "files": files_list}

# Скачивание файла по UUID. Доступно без токенов.
@app.get("/download/{file_uuid_with_ext}")
async def download_file(file_uuid_with_ext: str):
    file_uuid = file_uuid_with_ext.split(".")[0]
    target_filename = ""
    if os.path.exists(UPLOAD_DIR):
        for filename in os.listdir(UPLOAD_DIR):
            if filename.startswith(file_uuid): 
                target_filename = filename
                break
                
    if target_filename:
        file_path = os.path.join(UPLOAD_DIR, target_filename)
        mapping_file = os.path.join(UPLOAD_DIR, "file_map.json")
        original_name = None
        if os.path.exists(mapping_file):
            with open(mapping_file, "r", encoding="utf-8") as f: 
                original_name = json.load(f).get(file_uuid)
        encoded_name = urllib.parse.quote(original_name or target_filename)
        return FileResponse(file_path, media_type='application/force-download', headers={'Content-Disposition': f'attachment; filename="{encoded_name}"; filename*=UTF-8\'\' {encoded_name}'})
        
    return HTMLResponse("Файл не найден", status_code=404)

@app.get("/api/web/user-info/{user_id}")
async def get_user_info(user_id: str, x_token: Optional[str] = Header(None)):
    session = get_session_by_token(x_token)
    if not session: raise HTTPException(status_code=401)
    
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT * FROM user_shop_info WHERE id_user = %s", (user_id,))
        return cur.fetchone() or {"error": "Информация не найдена"}
    finally: cur.close(); conn.close()

@app.post("/api/web/exchange-ticket")
async def exchange_ticket_for_session(data: WebTicketExchangeRequest):
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # Увеличим время жизни билета до 60 секунд (на случай задержек сети)
        cur.execute("SELECT * FROM auth_tickets WHERE ticket = %s AND created_at >= NOW() - INTERVAL '60 seconds'", (data.ticket,))
        ticket_data = cur.fetchone()
        
        if not ticket_data: 
            raise HTTPException(status_code=403, detail="Билет авторизации истек или не существует.")
            
        cur.execute("DELETE FROM auth_tickets WHERE ticket = %s", (data.ticket,))
        session_token = secrets.token_hex(32)
        
        # Явное приведение UUID к строке для безопасности
        user_id = str(ticket_data['id_user'])
        org_id = str(ticket_data['id_org'])
        
        cur.execute("DELETE FROM api_sessions WHERE id_user = %s", (user_id,))
        cur.execute(
            "INSERT INTO api_sessions (token, id_user, id_org, username, role) VALUES (%s, %s, %s::uuid, %s, %s)", 
            (session_token, user_id, org_id, ticket_data['username'], ticket_data['role'])
        )
        conn.commit()
        
        return {
            "success": True, 
            "token": session_token, 
            "user": {
                "id_user": user_id, 
                "username": ticket_data['username'], 
                "role": ticket_data['role'], 
                "id_org": org_id
            }
        }
    except HTTPException:
        # ПРОПУСКАЕМ НАШИ 403 ОШИБКИ БЕЗ ИЗМЕНЕНИЙ (Чтобы Railway не сыпал 500)
        conn.rollback()
        raise
    except Exception as e: 
        conn.rollback()
        # ВАЖНО: это выведет реальную ошибку в логи Railway
        print(f"DEBUG ERROR: {type(e).__name__}: {str(e)}") 
        raise HTTPException(status_code=500, detail=str(e))
    finally: 
        cur.close(); conn.close()

@app.get("/api/web/rooms")
async def web_get_rooms(x_token: Optional[str] = Header(None)):
    session = get_session_by_token(x_token)
    if not session: raise HTTPException(status_code=401)
    
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        if session['role'] == 'admin':
            query = """
                SELECT id_room, name, type, created_by, 
                       (SELECT COUNT(*) FROM room_participants WHERE id_room = rooms.id_room) as participants_count
                FROM rooms
                WHERE id_org = %s::uuid AND TRIM(type) = 'admin_group'
                
                UNION
                
                SELECT r.id_room, r.name, r.type, r.created_by,
                       (SELECT COUNT(*) FROM room_participants WHERE id_room = r.id_room) as participants_count
                FROM rooms r
                INNER JOIN room_participants rp ON r.id_room = rp.id_room
                WHERE r.id_org = %s::uuid AND rp.id_user = %s
                
                ORDER BY name ASC
            """
            # Передаем: id_org для admin_group, id_org для обычных групп, id_user для обычных групп
            cur.execute(query, (str(session['id_org']), str(session['id_org']), session['id_user']))

        else:
            # Обычный юзер видит только то, где он участник
            query = """
                SELECT r.id_room, r.name, r.type, r.created_by,
                       (SELECT COUNT(*) FROM room_participants WHERE id_room = r.id_room) as participants_count
                FROM rooms r
                INNER JOIN room_participants rp ON r.id_room = rp.id_room
                WHERE r.id_org = %s::uuid AND rp.id_user = %s
                ORDER BY CASE WHEN UPPER(r.name)='АДМИН' THEN 1 WHEN UPPER(r.name)='ОБЩИЙ' THEN 2 ELSE 3 END, r.name ASC
            """
            print(session['id_org'])
            cur.execute(query, (str(session['id_org']), session['id_user']))
            
        all_rooms = cur.fetchall()
        
        print(f"DEBUG: Отправляю список комнат на фронтенд: {[r['name'] for r in all_rooms]}")
    
        return {
            "active": [r for r in all_rooms if r['participants_count'] >= 2], 
            "inactive_text_group": [r for r in all_rooms if r['participants_count'] < 2]
        }
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))
    finally: cur.close(); conn.close()

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
        
        return {"url": f"/download/{unique_id}{file_extension}", "filename": orig_filename}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/admin/upload-logo")
async def upload_logo(file: UploadFile = File(...)):
    file_location = f"static/logo.png"
    with open(file_location, "wb+") as file_object:
        file_object.write(file.file.read())
    log_admin_action("Admin", "Обновлен логотип проекта (logo.png)")
    return {"info": f"Логотип обновлен: {file_location}"}

@app.get("/download-archive/{file_uuid}")
async def download_archive_endpoint(file_uuid: str):
    mapping_file = os.path.join(UPLOAD_DIR, "file_map.json")
    with open(mapping_file, "r", encoding="utf-8") as f: file_map = json.load(f)
    target_filename = ""
    for filename in os.listdir(UPLOAD_DIR):
        if file_uuid in filename and filename.endswith(".json"): target_filename = filename; break
    return FileResponse(path=os.path.join(UPLOAD_DIR, target_filename), media_type="application/json", headers={'Content-Disposition': f'attachment; filename="{urllib.parse.quote(file_map[file_uuid])}"'})

# СВЯЗЫВАНИЕ СОБЫТИЙ SOCKET.IO
# Глобальный словарь для поиска: {id_user: sid}
user_sid_map = {}

@sio.event
async def connect(sid, environ, auth=None):
    query_params = environ.get('QUERY_STRING', '')
    params = dict(x.split('=') for x in query_params.split('&') if '=' in x)
    id_user = urllib.parse.unquote(params.get('id_user', ''))
    id_org = clean_uuid(urllib.parse.unquote(params.get('id_org', '')))
    username = urllib.parse.unquote(params.get('username', ''))
    user_role = urllib.parse.unquote(params.get('role', 'user'))
    user_sid_map[id_user] = sid # Запоминаем связь

    if not id_user: return False

    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("INSERT INTO users (id_user, id_org, username, role, is_active) VALUES (%s, %s::uuid, %s, %s, true) ON CONFLICT (id_user) DO UPDATE SET username = EXCLUDED.username, role = EXCLUDED.role", (id_user, id_org, username, user_role))
        check_and_create_global_rooms(cur, id_org, id_user, user_role)
        conn.commit()
    except Exception: 
        conn.rollback()
    finally: 
        cur.close(); conn.close()
    
        
    await sio.save_session(sid, {'id_user': id_user, 'id_org': id_org, 'username': username, 'role': user_role})
    online_users[id_user] = username
    await sio.emit('user_statuses', online_users)

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
        if session.get('role') == 'admin':
            # Админ видит все 'admin_group' своей организации ПЛЮС все комнаты, где он участник
            query = """
                SELECT id_room, name, type, id_org, created_by,
                       (SELECT COUNT(*) FROM room_participants WHERE id_room = rooms.id_room) as participants_count
                FROM rooms
                WHERE id_org = %s::uuid AND TRIM(type) = 'admin_group'
                
                UNION
                
                SELECT r.id_room, r.name, r.type, r.id_org, r.created_by,
                       (SELECT COUNT(*) FROM room_participants WHERE id_room = r.id_room) as participants_count
                FROM rooms r
                INNER JOIN room_participants rp ON r.id_room = rp.id_room
                WHERE r.id_org = %s::uuid AND rp.id_user = %s
                
                ORDER BY name ASC
            """
            cur.execute(query, (str(session['id_org']), str(session['id_org']), session['id_user']))
        else:
            # Обычный юзер видит только то, где он участник
            query = """
                SELECT r.id_room, r.name, r.type, r.id_org, r.created_by,
                       (SELECT COUNT(*) FROM room_participants WHERE id_room = r.id_room) as participants_count
                FROM rooms r
                INNER JOIN room_participants rp ON r.id_room = rp.id_room
                WHERE r.id_org = %s::uuid AND rp.id_user = %s
                ORDER BY CASE WHEN UPPER(r.name)='АДМИН' THEN 1 WHEN UPPER(r.name)='ОБЩИЙ' THEN 2 ELSE 3 END, r.name ASC
            """
            cur.execute(query, (str(session['id_org']), session['id_user']))
            
        await sio.emit('rooms_list', cur.fetchall(), to=sid)
    except Exception as e: 
        print(f"Ошибка в get_rooms_again: {e}")
    finally: 
        cur.close(); conn.close()

@sio.event
async def get_users_list(sid):
    session = await sio.get_session(sid)
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute('SELECT id_user, username, role FROM users WHERE id_org = %s::uuid AND is_active = true ORDER BY username ASC', (session['id_org'],))
        await sio.emit('users_list', cur.fetchall(), to=sid)
    except Exception as e: print(e)
    finally: cur.close(); conn.close()

@sio.event
async def get_room_history(sid, data):
    room_id = data.get('room_id')
    if not room_id: return
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("""
            SELECT m.id_message, m.id_room, m.id_user_from, COALESCE(u.username, m.id_user_from) as username, 
                   m.encrypted_text, m.is_user_encrypted, m.ui_styles, m.created_at, m.reply_to,
                   rm.encrypted_text as reply_text, COALESCE(u2.username, rm.id_user_from) as reply_author
            FROM messages m 
            LEFT JOIN users u ON m.id_user_from = u.id_user 
            LEFT JOIN messages rm ON m.reply_to = rm.id_message
            LEFT JOIN users u2 ON rm.id_user_from = u2.id_user
            WHERE m.id_room = %s 
            ORDER BY m.created_at ASC LIMIT 100
        """, (room_id,))
        messages = cur.fetchall()
        for m in messages:
            msg_id = m['id_message']
            if m['created_at']: m['created_at'] = m['created_at'].isoformat()
            m['reads'] = message_reads.get(msg_id, [])
        await sio.emit('room_history', {'messages': messages}, to=sid)
    except Exception as e: print(e)
    finally: cur.close(); conn.close()

@sio.event
async def get_files_history(sid, data):
    room_id = data.get('room_id')
    extension = data.get('extension')  # 'all', 'png', 'pdf', 'xlsx' и т.д.
    session = await sio.get_session(sid)
    if not session: 
        return

    user_id = session['id_user']
    conn = get_db_connection()
    cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        # ВАЖНО: В psycopg2 знак процента в строке запроса нужно экранировать как %%
        query = """
            SELECT m.id_message, m.id_room, m.id_user_from, COALESCE(u.username, m.id_user_from) as username, 
                   m.encrypted_text, m.is_user_encrypted, m.ui_styles, m.created_at, r.name as room_name, m.reply_to
            FROM messages m 
            LEFT JOIN users u ON m.id_user_from = u.id_user 
            INNER JOIN room_participants rp ON m.id_room = rp.id_room
            INNER JOIN rooms r ON m.id_room = r.id_room
            WHERE rp.id_user = %s AND m.encrypted_text LIKE '/download/%%'
        """
        params = [user_id]

        # Если конкретная комната выбрана — фильтруем по ней
        if room_id:
            query += " AND m.id_room = %s"
            params.append(room_id)
            
        # Если выбрано конкретное расширение — фильтруем строку вложения
        if extension and extension != 'all':
            query += " AND m.encrypted_text ILIKE %s"
            params.append(f"%.{extension}%")

        query += " ORDER BY m.created_at DESC LIMIT 300"
        
        cur.execute(query, tuple(params))
        messages = cur.fetchall()
        
        # Разворачиваем в хронологический порядок для отображения в чате
        messages.reverse()
        
        for m in messages:
            if m['created_at']: 
                m['created_at'] = m['created_at'].isoformat()
            m['reads'] = message_reads.get(m['id_message'], [])
            
        await sio.emit('files_history_response', {'messages': messages, 'room_id': room_id}, to=sid)
    except Exception as e: 
        print(f"Ошибка получения файлов: {e}")
    finally: 
        cur.close()
        conn.close()

@sio.event
async def send_message(sid, data):
    room_id, text, is_secret, ui_styles = data.get('room_id'), data.get('text'), data.get('is_secret', False), data.get('ui_styles', '{}')
    reply_to = data.get('reply_to')
    if not room_id or not text: return
    session = await sio.get_session(sid)
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("INSERT INTO messages (id_room, id_user_from, encrypted_text, is_user_encrypted, ui_styles, reply_to) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id_message, id_room, id_user_from, encrypted_text, is_user_encrypted, ui_styles, created_at, reply_to", (room_id, session['id_user'], text, is_secret, ui_styles, reply_to))
        new_msg = cur.fetchone(); conn.commit()
        new_msg['username'] = session['username']
        new_msg['reads'] = []
        
        if new_msg.get('reply_to'):
            cur.execute("SELECT m.encrypted_text, COALESCE(u.username, m.id_user_from) as username FROM messages m LEFT JOIN users u ON m.id_user_from = u.id_user WHERE m.id_message = %s", (new_msg['reply_to'],))
            r_info = cur.fetchone()
            if r_info: new_msg['reply_text'] = r_info['encrypted_text']; new_msg['reply_author'] = r_info['username']

        if new_msg['created_at']: new_msg['created_at'] = new_msg['created_at'].isoformat()
        await sio.emit('new_message', new_msg, room=f"room_{room_id}")
    except Exception as e: print(e)
    finally: cur.close(); conn.close()

@sio.event
async def edit_message(sid, data):
    msg_id = data.get('id_message')
    new_text = data.get('new_text')
    if not msg_id or not new_text: return
    
    conn = get_db_connection(); cur = conn.cursor()
    try:
        cur.execute("""
            UPDATE messages 
            SET encrypted_text = %s 
            WHERE id_message = %s 
            RETURNING id_room
        """, (new_text, msg_id))
        room = cur.fetchone()
        conn.commit()
        
        if room:
            await sio.emit('message_edited', {
                'id_message': msg_id, 
                'new_text': new_text
            }, room=f"room_{room[0]}")
    except Exception as e: 
        print(e)
    finally: 
        cur.close(); conn.close()

@sio.event
async def message_read_click(sid, data):
    msg_id = data.get('message_id')
    username = data.get('username')
    if not msg_id or not username: return
    if msg_id not in message_reads:
        message_reads[msg_id] = []
    if username not in message_reads[msg_id]:
        message_reads[msg_id].append(username)
    await sio.emit('message_read_update', {'message_id': msg_id, 'users_list': message_reads[msg_id]})

@sio.event
async def get_room_participants(sid, data):
    room_id = data.get('room_id')
    if not room_id: return
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT rp.id_user, u.username, u.role FROM room_participants rp INNER JOIN users u ON rp.id_user = u.id_user WHERE rp.id_room = %s", (room_id,))
        await sio.emit('room_participants_list', {'participants': cur.fetchall()}, to=sid)
    except Exception as e: print(e)
    finally: cur.close(); conn.close()

@sio.event
async def create_advanced_room(sid, data):
    name, participants = data.get('name'), data.get('participants', [])
    if not name or not participants: return
    session = await sio.get_session(sid)
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("INSERT INTO rooms (id_org, type, name, created_by) VALUES (%s::uuid, 'group', %s, %s) RETURNING id_room", (session['id_org'], name, session['id_user']))
        room_id = cur.fetchone()['id_room']
        cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s)", (room_id, session['id_user']))
        for u_id in participants:
            cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s) ON CONFLICT DO NOTHING", (room_id, u_id))
        conn.commit()
        log_admin_action(session.get('username', 'User'), f"Создан расширенный кабинет: {name}")
        await sio.emit('private_chat_created', {'id_room': room_id}, to=sid)
        await sio.emit('refresh_rooms_trigger')
    except Exception: conn.rollback()
    finally: cur.close(); conn.close()

@sio.event
async def add_user_to_room(sid, data):
    room_id, user_id = data.get('room_id'), data.get('user_id')
    if not room_id or not user_id: return
    session = await sio.get_session(sid); conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT type, created_by FROM rooms WHERE id_room = %s", (room_id,))
        room = cur.fetchone()
        
        if room:
            # Админ-группы могут модерироваться только админами
            if room['type'] == 'admin_group' and session['role'] != 'admin':
                await sio.emit('system_alert', {"message": "Добавлять в этот официальный кабинет может только Администратор!"}, to=sid); return
            
            # Обычные группы/кабинеты - только их создателем
            if room['type'] == 'group' and str(room['created_by']) != str(session['id_user']) and session['role'] != 'admin':
                await sio.emit('system_alert', {"message": "Добавлять участников может только создатель кабинета!"}, to=sid); return

        cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s) ON CONFLICT DO NOTHING", (room_id, user_id))
        conn.commit()
        cur.execute("SELECT rp.id_user, u.username, u.role FROM room_participants rp INNER JOIN users u ON rp.id_user = u.id_user WHERE rp.id_room = %s", (room_id,))
        await sio.emit('room_participants_list', {'participants': cur.fetchall()}, room=f"room_{room_id}")
        await sio.emit('refresh_rooms_trigger')
    except Exception as e: print(e)
    finally: cur.close(); conn.close()

@sio.event
async def remove_user_from_room(sid, data):
    room_id, target_user_id = data.get('room_id'), data.get('target_user_id')
    if not room_id or not target_user_id: return
    session = await sio.get_session(sid); conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT type, created_by FROM rooms WHERE id_room = %s", (room_id,))
        room = cur.fetchone()
        if room and room['type'] == 'admin_group' and session['role'] != 'admin': return
        if room and room['type'] == 'group' and (room['created_by'] != session['id_user'] and session['role'] != 'admin'): return
        cur.execute("DELETE FROM room_participants WHERE id_room = %s AND id_user = %s", (room_id, target_user_id))
        conn.commit()
        log_admin_action(session.get('username', 'Admin'), f"Удален участник {target_user_id} из комнаты {room_id}")
        cur.execute("SELECT rp.id_user, u.username, u.role FROM room_participants rp INNER JOIN users u ON rp.id_user = u.id_user WHERE rp.id_room = %s", (room_id,))
        await sio.emit('room_participants_list', {'participants': cur.fetchall()}, room=f"room_{room_id}")
        await sio.emit('refresh_rooms_trigger')
    except Exception as e: print(e)
    finally: cur.close(); conn.close()

@sio.event
async def delete_room_request(sid, data):
    room_id, user_id, user_role = data.get('room_id'), data.get('user_id'), data.get('role', 'user')
    if not room_id: return
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT type, created_by, name FROM rooms WHERE id_room = %s", (room_id,))
        room = cur.fetchone()    
        if room:
            is_creator = str(room['created_by']) == str(user_id)
            is_admin_of_group = (room['type'] == 'admin_group' and user_role == 'admin')
            
            if is_creator or is_admin_of_group:
                cur.execute("DELETE FROM rooms WHERE id_room = %s", (room_id,))
                conn.commit()
                log_admin_action(data.get('username', 'Admin'), f"Удален кабинет: {room['name']}")
                await sio.emit('refresh_rooms_trigger')
            else:
                await sio.emit('system_alert', {"message": "Удалять кабинеты может только их создатель!"}, to=sid)
    except Exception as e: print(e)
    finally: cur.close(); conn.close()

@sio.event
async def archive_room_messages(sid, data):
    room_id = data.get('room_id')
    if not room_id: return
    session = await sio.get_session(sid)
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT type, created_by, name FROM rooms WHERE id_room = %s", (room_id,))
        room = cur.fetchone()
        if room and room['type'] == 'group' and (room['created_by'] != session['id_user'] and session['role'] != 'admin'): return
        if room and room['type'] == 'admin_group' and session['role'] != 'admin': return
        cur.execute("SELECT m.id_user_from, COALESCE(u.username, m.id_user_from) as username, m.encrypted_text, m.ui_styles, m.created_at FROM messages m LEFT JOIN users u ON m.id_user_from = u.id_user WHERE m.id_room = %s ORDER BY m.created_at ASC", (room_id,))
        messages = cur.fetchall()
        for m in messages:
            if m['created_at']: m['created_at'] = m['created_at'].isoformat()
        cur.execute("DELETE FROM messages WHERE id_room = %s", (room_id,)); conn.commit()
        
        log_admin_action(session.get('username', 'Admin'), f"Архивированы сообщения из кабинета: {room['name']}")
        
        archive_uuid = str(uuid.uuid4())
        with open(os.path.join(UPLOAD_DIR, f"archive_{room_id}_{archive_uuid}.json"), "w", encoding="utf-8") as f: json.dump(messages, f, ensure_ascii=False, indent=4)
        mapping_file = os.path.join(UPLOAD_DIR, "file_map.json")
        file_map = {}
        if os.path.exists(mapping_file):
            with open(mapping_file, "r", encoding="utf-8") as f: file_map = json.load(f)
        file_map[archive_uuid] = f"archive_room_{room_id}.json"
        with open(mapping_file, "w", encoding="utf-8") as f: json.dump(file_map, f)
        await sio.emit('download_archive_file', {"url": f"/download-archive/{archive_uuid}"}, to=sid)
    except Exception as e: print(e)
    finally: cur.close(); conn.close()

@sio.event
async def delete_message_request(sid, data):
    message_id, room_id, user_id, user_role = data.get('message_id'), data.get('room_id'), data.get('user_id'), data.get('role', 'user')
    if not message_id or not room_id: return
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("SELECT id_user_from FROM messages WHERE id_message = %s", (message_id,))
        msg = cur.fetchone()
        if msg and str(msg['id_user_from']) == str(user_id):
            cur.execute("DELETE FROM messages WHERE id_message = %s", (message_id,))
            conn.commit()
            await sio.emit('message_deleted', {"id_message": message_id}, room=f"room_{room_id}")
    except Exception as e: print(e)
    finally: cur.close(); conn.close()

@sio.event
async def create_group_chat(sid, data):
    group_name = data.get('group_name')
    if not group_name: return
    session = await sio.get_session(sid); room_type = 'admin_group' if session['role'] == 'admin' else 'group'
    conn = get_db_connection(); cur = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cur.execute("INSERT INTO rooms (id_org, type, name, created_by) VALUES (%s::uuid, %s, %s, %s) RETURNING id_room", (session['id_org'], room_type, group_name, session['id_user']))
        new_room = cur.fetchone()
        cur.execute("INSERT INTO room_participants (id_room, id_user) VALUES (%s, %s)", (new_room['id_room'], session['id_user']))
        conn.commit()
        log_admin_action(session.get('username', 'User'), f"Создан групповой чат: {group_name}")
        await sio.emit('private_chat_created', {'id_room': new_room['id_room']}, to=sid)
        await sio.emit('refresh_rooms_trigger')
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
        conn.commit()
        await sio.emit('private_chat_created', {'id_room': room_id}, to=sid)
        await sio.emit('refresh_rooms_trigger')
    except Exception as e: conn.rollback()
    finally: cur.close(); conn.close()

@sio.event
async def get_sid_by_user(sid, data):
    target_user_id = data.get('user_id')
    return user_sid_map.get(target_user_id)

@sio.event
async def disconnect(sid):
    session = await sio.get_session(sid)
    if session and 'id_user' in session:
        id_user = session['id_user']
        # Проверяем, что отключается именно текущая активная сессия (предотвращает ложные оффлайны при переподключении телефона)
        if user_sid_map.get(id_user) == sid:
            online_users.pop(id_user, None)
            user_sid_map.pop(id_user, None)
            await sio.emit('user_statuses', online_users)

@sio.event
async def offer(sid, data):
    """Браузер А отправляет предложение (SDP) браузеру Б"""
    target_sid = data.get('target_sid')
    if target_sid:
        await sio.emit('offer', {
            'offer': data['offer'], 
            'from_sid': sid,
            'caller_name': data.get('caller_name', 'Коллега')
        }, to=target_sid)

@sio.event
async def answer(sid, data):
    """Браузер Б отвечает браузеру А"""
    target_sid = data.get('target_sid')
    if target_sid:
        await sio.emit('answer', {'answer': data['answer'], 'from_sid': sid}, to=target_sid)

@sio.event
async def ice_candidate(sid, data):
    """Обмен сетевыми данными (ICE Candidates)"""
    target_sid = data.get('target_sid')
    if target_sid:
        await sio.emit('ice_candidate', {'candidate': data['candidate'], 'from_sid': sid}, to=target_sid)

@sio.event
async def end_call(sid, data):
    """Событие для завершения звонка"""
    target_sid = data.get('target_sid')
    if target_sid:
        await sio.emit('call_ended', {'from_sid': sid}, to=target_sid)

app.mount("/socket.io", socket_app)