const Fastify = require('fastify');
const { Server } = require('socket.io');
const { Pool } = require('pg');
const crypto = require('crypto');
const fs = require('fs');
const path = require('path');
const multipart = require('@fastify/multipart');
const fastifyStatic = require('@fastify/static');
const fastifyCors = require('@fastify/cors');
require('dotenv/config');

console.log("=== Инициализация Fastify сервера ===");

const fastify = Fastify({ logger: true, bodyLimit: 100 * 1024 * 1024 });
fastify.register(fastifyCors, { origin: true, methods: ["GET", "POST", "PUT", "DELETE"] });

const PORT = process.env.PORT || 3000;
const SERVER_SECRET = process.env.BACKEND_SECRET_KEY || 'default_secret_key_32_chars_long!!';
const UPLOADS_DIR = path.join(__dirname, 'uploads');
if (!fs.existsSync(UPLOADS_DIR)) fs.mkdirSync(UPLOADS_DIR);

fastify.register(multipart, { limits: { fileSize: 10 * 1024 * 1024 } });
fastify.register(fastifyStatic, { root: UPLOADS_DIR, prefix: '/uploads/' });

const pool = new Pool({
    user: process.env.DB_USER,
    password: process.env.DB_PASSWORD,
    host: process.env.DB_HOST,
    port: Number(process.env.DB_PORT),
    database: process.env.DB_NAME,
    ssl: { rejectUnauthorized: false }
});

function encryptForDB(text) {
    const iv = crypto.randomBytes(16);
    const key = crypto.scryptSync(SERVER_SECRET, 'salt', 32);
    const cipher = crypto.createCipheriv('aes-256-cbc', key, iv);
    let encrypted = cipher.update(text, 'utf8', 'hex');
    encrypted += cipher.final('hex');
    return iv.toString('hex') + ':' + encrypted;
}

// ===== АДМИН-ПАНЕЛЬ API =====

// Авторизация админа
fastify.post('/api/admin/auth', async (req, reply) => {
    try {
        const { id_user, id_org } = req.body;
        const result = await pool.query(
            `SELECT u.id_user, u.username, u.role 
             FROM users u 
             WHERE u.id_user = $1 AND u.id_org = $2 AND u.role = 'admin'`,
            [id_user, id_org]
        );
        if (result.rows.length > 0) {
            return { success: true, admin: result.rows[0] };
        } else {
            return reply.status(403).send({ success: false, error: 'Недостаточно прав' });
        }
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка авторизации' });
    }
});

// Получить настройки
fastify.get('/api/admin/settings', async (req, reply) => {
    try {
        const result = await pool.query('SELECT setting_key, setting_value, setting_type FROM admin_settings');
        const settings = {};
        for (const row of result.rows) {
            settings[row.setting_key] = { value: row.setting_value, type: row.setting_type };
        }
        return settings;
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка получения настроек' });
    }
});

// Обновить настройку
fastify.post('/api/admin/settings', async (req, reply) => {
    try {
        const { key, value, admin_user } = req.body;
        await pool.query(
            `INSERT INTO admin_settings (setting_key, setting_value, updated_by, updated_at) 
             VALUES ($1, $2, $3, CURRENT_TIMESTAMP)
             ON CONFLICT (setting_key) 
             DO UPDATE SET setting_value = $2, updated_by = $3, updated_at = CURRENT_TIMESTAMP`,
            [key, value, admin_user]
        );
        return { success: true };
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка обновления' });
    }
});

// Получить пользователей
fastify.get('/api/admin/users', async (req, reply) => {
    try {
        const result = await pool.query('SELECT id_user, username, role FROM users ORDER BY username');
        return result.rows;
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка получения пользователей' });
    }
});

// Обновить права пользователя
fastify.post('/api/admin/user/permissions', async (req, reply) => {
    try {
        const { target_user, permissions } = req.body;
        await pool.query(
            `INSERT INTO admin_users (id_user, can_manage_themes, can_manage_users, can_manage_roles, can_view_logs)
             VALUES ($1, $2, $3, $4, $5)
             ON CONFLICT (id_user) 
             DO UPDATE SET 
                can_manage_themes = $2,
                can_manage_users = $3,
                can_manage_roles = $4,
                can_view_logs = $5`,
            [target_user, 
             permissions.can_manage_themes || false,
             permissions.can_manage_users || false,
             permissions.can_manage_roles || false,
             permissions.can_view_logs || false]
        );
        return { success: true };
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка обновления прав' });
    }
});

// Выполнить SQL запрос
fastify.post('/api/admin/sql', async (req, reply) => {
    try {
        const { sql, admin_user } = req.body;
        const adminCheck = await pool.query(
            'SELECT can_manage_users FROM admin_users WHERE id_user = $1',
            [admin_user]
        );
        if (adminCheck.rows.length === 0 || !adminCheck.rows[0].can_manage_users) {
            return reply.status(403).send({ error: 'Недостаточно прав' });
        }
        const sqlLower = sql.trim().toLowerCase();
        if (!sqlLower.startsWith('select')) {
            return reply.status(400).send({ error: 'Разрешены только SELECT запросы' });
        }
        const result = await pool.query(sql);
        return { rows: result.rows };
    } catch (err) {
        return reply.status(500).send({ error: err.message });
    }
});

// Получить логи
fastify.get('/api/admin/logs', async (req, reply) => {
    try {
        const { admin_user } = req.query;
        const adminCheck = await pool.query(
            'SELECT can_view_logs FROM admin_users WHERE id_user = $1',
            [admin_user]
        );
        if (adminCheck.rows.length === 0 || !adminCheck.rows[0].can_view_logs) {
            return reply.status(403).send({ error: 'Недостаточно прав' });
        }
        const logDir = path.join(__dirname, 'logs');
        if (!fs.existsSync(logDir)) fs.mkdirSync(logDir);
        const logPath = path.join(logDir, 'app.log');
        if (fs.existsSync(logPath)) {
            const logs = fs.readFileSync(logPath, 'utf8').split('\n').filter(l => l.trim()).slice(-200);
            return { logs };
        }
        return { logs: ['Логи не найдены'] };
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка получения логов' });
    }
});

// Получить тему
fastify.get('/api/admin/theme', async (req, reply) => {
    try {
        const result = await pool.query(
            "SELECT setting_key, setting_value FROM admin_settings WHERE setting_type IN ('color', 'css')"
        );
        const theme = {};
        for (const row of result.rows) {
            theme[row.setting_key] = row.setting_value;
        }
        return theme;
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка получения темы' });
    }
});

// Получить ссылки
fastify.get('/api/admin/links', async (req, reply) => {
    try {
        const result = await pool.query(
            "SELECT setting_key, setting_value FROM admin_settings WHERE setting_key LIKE 'link_%'"
        );
        const links = {};
        for (const row of result.rows) {
            links[row.setting_key] = row.setting_value;
        }
        return links;
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка получения ссылок' });
    }
});

// Обновить ссылки
fastify.post('/api/admin/links', async (req, reply) => {
    try {
        const { links, admin_user } = req.body;
        for (const [key, value] of Object.entries(links)) {
            await pool.query(
                `INSERT INTO admin_settings (setting_key, setting_value, setting_type, updated_by) 
                 VALUES ($1, $2, 'url', $3)
                 ON CONFLICT (setting_key) 
                 DO UPDATE SET setting_value = $2, updated_by = $3`,
                [key, value, admin_user]
            );
        }
        return { success: true };
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка сохранения ссылок' });
    }
});

// Загрузить логотип
fastify.post('/api/admin/upload-logo', async (req, reply) => {
    try {
        const data = await req.file();
        if (!data) return reply.status(400).send({ error: 'Файл не загружен' });
        const ext = path.extname(data.filename);
        const name = 'logo' + ext;
        const saveTo = path.join(UPLOADS_DIR, name);
        await new Promise((resolve, reject) => {
            const out = fs.createWriteStream(saveTo);
            data.file.pipe(out);
            out.on('finish', resolve);
            out.on('error', reject);
        });
        const logoUrl = '/uploads/' + name;
        await pool.query(
            `INSERT INTO admin_settings (setting_key, setting_value, setting_type) 
             VALUES ('logo_url', $1, 'url')
             ON CONFLICT (setting_key) 
             DO UPDATE SET setting_value = $1`,
            [logoUrl]
        );
        return { url: logoUrl };
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка загрузки логотипа' });
    }
});

// ===== ОСНОВНЫЕ МАРШРУТЫ =====

fastify.post('/upload', async (req, reply) => {
    try {
        const data = await req.file();
        if (!data) return reply.status(400).send({ error: 'Файл не прикреплен' });
        const ext = path.extname(data.filename);
        const name = crypto.randomUUID() + ext;
        const saveTo = path.join(UPLOADS_DIR, name);
        await new Promise((resolve, reject) => {
            const out = fs.createWriteStream(saveTo);
            data.file.pipe(out);
            out.on('finish', resolve);
            out.on('error', reject);
        });
        const url = req.protocol + '://' + req.hostname + '/uploads/' + name;
        return { url: url, originalName: data.filename };
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка загрузки' });
    }
});

fastify.get('/', async (req, reply) => {
    try {
        const html = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8');
        reply.type('text/html').send(html);
    } catch (err) {
        reply.status(500).send('Ошибка загрузки index.html');
    }
});

fastify.get('/admin', async (req, reply) => {
    try {
        const html = fs.readFileSync(path.join(__dirname, 'admin.html'), 'utf8');
        reply.type('text/html').send(html);
    } catch (err) {
        reply.status(500).send('Ошибка загрузки admin.html');
    }
});

fastify.get('/favicon.ico', async (req, reply) => reply.status(204).send());

// ===== ЗАПУСК СЕРВЕРА =====

const start = async () => {
    try {
        await pool.query('SELECT NOW()');
        console.log('=== Успешное подключение к базе данных ===');

        const io = new Server(fastify.server, { cors: { origin: "*" }, maxHttpBufferSize: 1e8 });

        io.use(async (socket, next) => {
            const { id_user, id_org, username, user_role } = socket.handshake.query;
            if (!id_user || !id_org || !username) return next(new Error("Ошибка авторизации"));
            socket.data = {
                id_user: String(id_user).trim(),
                id_org: String(id_org).trim(),
                username: String(username).trim(),
                role: user_role === 'admin' ? 'admin' : 'user'
            };
            next();
        });

        io.on('connection', async (socket) => {
            const { id_org, id_user, username, role } = socket.data;
            console.log(username + ' (' + role + ') подключен');

            try {
                await pool.query('INSERT INTO organizations (id_org, name) VALUES ($1, $2) ON CONFLICT (id_org) DO NOTHING', [id_org, 'Организация']);
                await pool.query('INSERT INTO users (id_user, id_org, username, role) VALUES ($1, $2, $3, $4) ON CONFLICT (id_user) DO UPDATE SET role = $4', [id_user, id_org, username, role]);
                await pool.query('INSERT INTO rooms (id_room, id_org, type, name) VALUES (1, $1, \'general\', \'Общий чат\') ON CONFLICT (id_room) DO NOTHING', [id_org]);
            } catch (err) { console.error('Ошибка регистрации:', err); }

            socket.join('org_' + id_org);
            socket.join('room_1');

            socket.on('get_rooms_again', async () => {
                try {
                    const res = await pool.query('SELECT id_room, name, type FROM rooms WHERE id_org = $1', [id_org]);
                    socket.emit('rooms_list', res.rows);
                } catch (err) { console.error(err); }
            });

            socket.on('get_users_list', async () => {
                try {
                    const res = await pool.query('SELECT id_user, username FROM users WHERE id_org = $1 AND id_user != $2', [id_org, id_user]);
                    socket.emit('users_list', res.rows);
                } catch (err) { console.error(err); }
            });

            socket.on('create_private_chat', async (data) => {
                try {
                    const roomName = username + ' ⇄ ' + data.target_username;
                    const newRoom = await pool.query('INSERT INTO rooms (id_org, type, name) VALUES ($1, $2, $3) RETURNING id_room', [id_org, 'private', roomName]);
                    const newRoomId = newRoom.rows[0].id_room;
                    await pool.query('INSERT INTO room_participants (id_room, id_user) VALUES ($1, $2), ($1, $3)', [newRoomId, id_user, data.target_user_id]);
                    io.to('org_' + id_org).emit('refresh_rooms_trigger');
                } catch (err) { console.error(err); }
            });

            socket.on('create_group_chat', async (data) => {
                if (role !== 'admin') {
                    socket.emit('error', { message: 'Только администраторы могут создавать группы' });
                    return;
                }
                try {
                    const newRoom = await pool.query('INSERT INTO rooms (id_org, type, name, created_by) VALUES ($1, $2, $3, $4) RETURNING id_room', [id_org, 'group', data.group_name, id_user]);
                    const newRoomId = newRoom.rows[0].id_room;
                    await pool.query('INSERT INTO room_participants (id_room, id_user) VALUES ($1, $2)', [newRoomId, id_user]);
                    io.to('org_' + id_org).emit('refresh_rooms_trigger');
                } catch (err) { console.error(err); }
            });

            socket.on('join_room_pool', (data) => socket.join('room_' + data.room_id));

            socket.on('send_message', async (data) => {
                try {
                    const encrypted = encryptForDB(data.text);
                    await pool.query('INSERT INTO messages (id_room, id_user_from, encrypted_text, is_user_encrypted) VALUES ($1, $2, $3, $4)', [data.room_id, id_user, encrypted, data.is_secret]);
                    io.to('room_' + data.room_id).emit('new_message', {
                        id_room: data.room_id,
                        id_user_from: id_user,
                        username: username,
                        text: data.text,
                        is_secret: data.is_secret,
                        created_at: new Date()
                    });
                } catch (err) { console.error(err); }
            });
        });

        const address = await fastify.listen({ port: Number(PORT), host: '0.0.0.0' });
        console.log('=== МЕССЕНДЖЕР ЗАПУЩЕН НА: ' + address + ' ===');
    } catch (err) {
        console.error('Критическая ошибка:', err);
        process.exit(1);
    }
};

start();