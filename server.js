const Fastify = require('fastify');
const { Server } = require('socket.io');
const { Pool } = require('pg');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const multipart = require('@fastify/multipart');
const fastifyStatic = require('@fastify/static');
const fastifyCors = require('@fastify/cors');
require('dotenv').config();

console.log("=== Инициализация Fastify сервера (Production JS) ===");

const fastify = Fastify({ logger: true, bodyLimit: 100 * 1024 * 1024 });

// Хранилище логов в памяти для вкладки "Логи"
const systemLogs = [];
const originalLog = console.log;
console.log = function(...args) {
    const msg = `[${new Date().toISOString()}] ${args.join(' ')}`;
    systemLogs.push(msg);
    if (systemLogs.length > 500) systemLogs.shift(); // Храним последние 500 строк
    originalLog.apply(console, args);
};

fastify.register(fastifyCors, { origin: true, methods: ["GET", "POST", "PUT", "DELETE"] });

const PORT = process.env.PORT || 3000;
const SERVER_SECRET = process.env.BACKEND_SECRET_KEY || 'default_secret_key_32_chars_long!!';
const UPLOADS_DIR = path.join(__dirname, 'uploads');
if (!fs.existsSync(UPLOADS_DIR)) { fs.mkdirSync(UPLOADS_DIR); }

fastify.register(multipart, { limits: { fileSize: 100 * 1024 * 1024 } });
fastify.register(fastifyStatic, { root: UPLOADS_DIR, prefix: '/uploads/' });

const pool = new Pool({
    user: process.env.DB_USER,
    password: process.env.DB_PASSWORD,
    host: process.env.DB_HOST,
    port: Number(process.env.DB_PORT || 5432),
    database: process.env.DB_NAME,
    ssl: process.env.DB_SSL === 'false' ? false : { rejectUnauthorized: false }
});

// Инициализация структуры таблиц при старте (если они отсутствуют)
async function initDB() {
    await pool.query(`
        CREATE TABLE IF NOT EXISTS organizations (id_org VARCHAR(50) PRIMARY KEY, name VARCHAR(100));
        CREATE TABLE IF NOT EXISTS users (
            id_user VARCHAR(50) PRIMARY KEY, id_org VARCHAR(50), username VARCHAR(100), 
            role VARCHAR(20) DEFAULT 'user', can_manage_themes BOOLEAN DEFAULT false, 
            can_manage_users BOOLEAN DEFAULT false, can_view_logs BOOLEAN DEFAULT false
        );
        CREATE TABLE IF NOT EXISTS rooms (id_room SERIAL PRIMARY KEY, id_org VARCHAR(50), type VARCHAR(20), name VARCHAR(100));
        CREATE TABLE IF NOT EXISTS room_participants (id_room INT, id_user VARCHAR(50), PRIMARY KEY(id_room, id_user));
        CREATE TABLE IF NOT EXISTS messages (id SERIAL PRIMARY KEY, id_room INT, id_user_from VARCHAR(50), encrypted_text TEXT, is_user_encrypted BOOLEAN, created_at TIMESTAMP DEFAULT NOW());
        CREATE TABLE IF NOT EXISTS settings (key VARCHAR(50) PRIMARY KEY, value TEXT);
    `);
    // Делаем первого пользователя или дефолтного админа 1С полноценным администратором в БД
    await pool.query(`INSERT INTO users (id_user, id_org, username, role, can_manage_themes, can_manage_users, can_view_logs) 
                      VALUES ('Admin', '00001', 'Администратор', 'admin', true, true, true) ON CONFLICT DO NOTHING`);
}

function encryptForDB(text) {
    const iv = crypto.randomBytes(16);
    const key = crypto.scryptSync(SERVER_SECRET, 'salt', 32);
    const cipher = crypto.createCipheriv('aes-256-cbc', key, iv);
    let encrypted = cipher.update(text, 'utf8', 'hex');
    encrypted += cipher.final('hex');
    return `${iv.toString('hex')}:${encrypted}`;
}

// Загрузка файлов/картинок из чата
fastify.post('/upload', async (request, reply) => {
    try {
        const data = await request.file();
        if (!data) return reply.status(400).send({ error: 'Файл не прикреплен' });
        const fileExt = path.extname(data.filename);
        const uniqueFileName = `${crypto.randomUUID()}${fileExt}`;
        const saveTo = path.join(UPLOADS_DIR, uniqueFileName);
        
        await new Promise((resolve, reject) => {
            const out = fs.createWriteStream(saveTo);
            data.file.pipe(out);
            out.on('finish', resolve);
            out.on('error', reject);
        });
        return { url: `${request.protocol}://${request.hostname}/uploads/${uniqueFileName}`, originalName: data.filename };
    } catch (err) { return reply.status(500).send({ error: 'Ошибка загрузки файла' }); }
});

// Загрузка кастомного логотипа из админки
fastify.post('/api/admin/upload-logo', async (request, reply) => {
    try {
        const data = await request.file();
        if (!data) return reply.status(400).send({ error: 'Файл логотипа не найден' });
        const saveTo = path.join(UPLOADS_DIR, 'custom_logo.png');
        await new Promise((resolve, reject) => {
            const out = fs.createWriteStream(saveTo);
            data.file.pipe(out);
            out.on('finish', resolve);
            out.on('error', reject);
        });
        await pool.query(`INSERT INTO settings (key, value) VALUES ('logo_url', '/uploads/custom_logo.png') ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value`);
        return { success: true };
    } catch (err) { return reply.status(500).send({ error: 'Ошибка сохранения логотипа' }); }
});

// Роуты статических страниц
fastify.get('/', async (request, reply) => {
    return reply.type('text/html').send(fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8'));
});

fastify.get('/admin', async (request, reply) => {
    return reply.type('text/html').send(fs.readFileSync(path.join(__dirname, 'admin.html'), 'utf8'));
});

fastify.get('/favicon.ico', async (req, res) => { res.status(204).send(); });

// === API ДЛЯ АДМИН-ПАНЕЛИ ===

// 1. Авторизация в админке
// Safe Permissions Update
fastify.post('/api/admin/user/permissions', async (request, reply) => {
    const { target_user, permissions } = request.body;
    try {
        // Явно проверяем и обновляем только разрешенные колонки
        if ('can_manage_themes' in permissions) {
            await pool.query('UPDATE users SET can_manage_themes = $1 WHERE id_user = $2', [permissions.can_manage_themes, target_user]);
        }
        if ('can_manage_users' in permissions) {
            await pool.query('UPDATE users SET can_manage_users = $1 WHERE id_user = $2', [permissions.can_manage_users, target_user]);
        }
        if ('can_view_logs' in permissions) {
            await pool.query('UPDATE users SET can_view_logs = $1 WHERE id_user = $2', [permissions.can_view_logs, target_user]);
        }
        return { success: true };
    } catch (err) { return reply.status(500).send({ error: err.message }); }
});

// 2. Получение текущих настроек внешнего вида и ссылок
fastify.get('/api/admin/settings', async (request, reply) => {
    try {
        const res = await pool.query('SELECT * FROM settings');
        const settingsMap = {};
        res.rows.forEach(row => { settingsMap[row.key] = { value: row.value }; });
        return settingsMap;
    } catch (err) { return reply.status(500).send(err); }
});

// 3. Сохранение отдельной настройки
fastify.post('/api/admin/settings', async (request, reply) => {
    const { key, value } = request.body;
    try {
        await pool.query('INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value', [key, value]);
        return { success: true };
    } catch (err) { return reply.status(500).send(err); }
});

// 4. Получение списка пользователей чата
fastify.get('/api/admin/users', async (request, reply) => {
    try {
        const res = await pool.query('SELECT id_user, username, role, can_manage_themes, can_manage_users, can_view_logs FROM users ORDER BY username');
        return res.rows;
    } catch (err) { return reply.status(500).send(err); }
});

// 5. Изменение прав доступа администратора

// 6. Выполнение SQL-запросов напрямую из админки
fastify.post('/api/admin/sql', async (request, reply) => {
    const { sql } = request.body;
    try {
        const res = await pool.query(sql);
        return { rows: res.rows || [] };
    } catch (err) { return { error: err.message }; }
});

// 7. Сохранение ссылок главного экрана
fastify.post('/api/admin/links', async (request, reply) => {
    const { links } = request.body;
    try {
        for (const [key, value] of Object.entries(links)) {
            await pool.query('INSERT INTO settings (key, value) VALUES ($1, $2) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value', [key, value]);
        }
        return { success: true };
    } catch (err) { return reply.status(500).send(err); }
});

// 8. Получение логов сервера
fastify.get('/api/admin/logs', async (request, reply) => {
    return { logs: systemLogs };
});


// === WEB-SOCKET И ЛОГИКА ЧАТА ===

const start = async () => {
    try {
        // Обертываем подключение к БД в try/catch, чтобы сервер не падал при старте
        try {
            console.log("Пробуем подключиться к PostgreSQL...");
            await initDB();
            console.log("Успешное подключение и проверка структуры таблиц PostgreSQL.");
        } catch (dbErr) {
            console.log("!!! ОШИБКА ПОДКЛЮЧЕНИЯ К БАЗЕ ДАННЫХ !!!");
            console.log(dbErr.message);
            console.log("Сервер продолжит работу, но запросы к БД будут вызывать ошибки. Проверьте переменные окружения.");
        }

        const io = new Server(fastify.server, { cors: { origin: "*" }, maxHttpBufferSize: 1e8 });

        io.use(async (socket, next) => {
            let { id_user, id_org, username, user_role } = socket.handshake.query;
            if (!id_user || !id_org || !username) return next(new Error("Ошибка авторизации"));
            
            let cleanOrgId = String(id_org).toLowerCase().trim();
            if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(cleanOrgId)) {
                const hash = crypto.createHash('md5').update(cleanOrgId).digest('hex');
                cleanOrgId = `${hash.slice(0, 8)}-${hash.slice(8, 12)}-${hash.slice(12, 16)}-${hash.slice(16, 20)}-${hash.slice(20, 32)}`;
            }
            socket.data = { 
                id_user: String(id_user).trim(), 
                id_org: cleanOrgId, 
                username: username,
                role: user_role || 'user'
            };
            next();
        });

        io.on('connection', async (socket) => {
            const { id_org, id_user, username, role } = socket.data;
            
            try {
                await pool.query('INSERT INTO organizations (id_org, name) VALUES ($1, $2) ON CONFLICT (id_org) DO NOTHING', [id_org, 'Организация из 1С']);
                
                // Безопасный UPSERT
                await pool.query(`
                    INSERT INTO users (id_user, id_org, username, role) 
                    VALUES ($1, $2, $3, $4) 
                    ON CONFLICT (id_user) 
                    DO UPDATE SET 
                        username = EXCLUDED.username, 
                        role = CASE WHEN EXCLUDED.role = 'admin' THEN 'admin' ELSE users.role END
                `, [id_user, id_org, username, role]);
                
                const checkGeneral = await pool.query(`SELECT id_room FROM rooms WHERE id_org = $1 AND type = 'general'`, [id_org]);
                if (checkGeneral.rows.length === 0) {
                    await pool.query(`INSERT INTO rooms (id_org, type, name) VALUES ($1, 'general', 'Общий чат')`, [id_org]);
                }
            } catch (err) { console.error("Ошибка при connection сокета:", err.message); }

            socket.join(`org_${id_org}`);

            const sendRoomsList = async () => {
                try {
                    const roomsResult = await pool.query(`
                        SELECT r.id_room, r.name, r.type FROM rooms r WHERE r.type = 'general' AND r.id_org = $1
                        UNION
                        SELECT r.id_room, r.name, r.type FROM rooms r
                        JOIN room_participants rp ON r.id_room = rp.id_room WHERE r.id_org = $1 AND rp.id_user = $2
                    `, [id_org, id_user]);
                    
                    roomsResult.rows.forEach(room => {
                        socket.join(`room_${room.id_room}`);
                    });

                    socket.emit('rooms_list', roomsResult.rows);
                } catch (err) { console.error("Ошибка при получении списка комнат:", err.message); }
            };
            await sendRoomsList();

            socket.on('get_users_list', async () => {
                try {
                    const usersResult = await pool.query('SELECT id_user, username FROM users WHERE id_org = $1 AND id_user != $2', [id_org, id_user]);
                    socket.emit('users_list', usersResult.rows);
                } catch (err) { console.error(err.message); }
            });

            socket.on('create_private_chat', async (data) => {
                try {
                    const checkChat = await pool.query(`
                        SELECT rp1.id_room FROM room_participants rp1 JOIN room_participants rp2 ON rp1.id_room = rp2.id_room
                        JOIN rooms r ON r.id_room = rp1.id_room WHERE r.type = 'private' AND r.id_org = $1 AND rp1.id_user = $2 AND rp2.id_user = $3
                    `, [id_org, id_user, data.target_user_id]);
                    
                    if (checkChat.rows.length > 0) { 
                        socket.emit('private_chat_created', { id_room: checkChat.rows[0].id_room }); 
                        return; 
                    }
                    const newRoom = await pool.query('INSERT INTO rooms (id_org, type, name) VALUES ($1, $2, $3) RETURNING id_room', [id_org, 'private', `${username} ⇄ ${data.target_username}`]);
                    const newRoomId = newRoom.rows[0].id_room;
                    await pool.query('INSERT INTO room_participants (id_room, id_user) VALUES ($1, $2), ($1, $3)', [newRoomId, id_user, data.target_user_id]);
                    
                    io.to(`org_${id_org}`).emit('refresh_rooms_trigger');
                } catch (err) { console.error(err.message); }
            });

            socket.on('create_group_chat', async (data) => {
                try {
                    const newRoom = await pool.query('INSERT INTO rooms (id_org, type, name) VALUES ($1, $2, $3) RETURNING id_room', [id_org, 'group', data.group_name]);
                    const nrId = newRoom.rows[0].id_room;
                    await pool.query('INSERT INTO room_participants (id_room, id_user) VALUES ($1, $2)', [nrId, id_user]);
                    
                    if (data.user_ids && Array.isArray(data.user_ids)) {
                        for (const tId of data.user_ids) { 
                            await pool.query('INSERT INTO room_participants (id_room, id_user) VALUES ($1, $2) ON CONFLICT DO NOTHING', [nrId, tId]); 
                        }
                    }
                    io.to(`org_${id_org}`).emit('refresh_rooms_trigger');
                } catch (err) { console.error(err.message); }
            });

            socket.on('join_room_pool', (data) => { socket.join(`room_${data.room_id}`); });
            socket.on('get_rooms_again', async () => { await sendRoomsList(); });

            socket.on('send_message', async (data) => {
                try {
                    await pool.query(`INSERT INTO messages (id_room, id_user_from, encrypted_text, is_user_encrypted) VALUES ($1, $2, $3, $4)`, [data.room_id, id_user, encryptForDB(data.text), data.is_secret]);
                    io.to(`room_${data.room_id}`).emit('new_message', { id_room: data.room_id, id_user_from: id_user, username: username, text: data.text, is_secret: data.is_secret, created_at: new Date() });
                } catch (err) { console.error(err.message); }
            });
        });

        const listenAddress = await fastify.listen({ port: Number(PORT), host: '0.0.0.0' });
        console.log(`=== МЕССЕНДЖЕР И АДМИНКА УСПЕШНО ЗАПУЩЕНЫ НА: ${listenAddress} ===`);
    } catch (err) { 
        console.error("Критическая ошибка запуска Fastify:", err); 
        process.exit(1); 
    }
};

start();