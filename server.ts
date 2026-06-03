import Fastify from 'fastify';
import { Server } from 'socket.io';
import { Pool } from 'pg';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as path from 'path';
import multipart from '@fastify/multipart';
import fastifyStatic from '@fastify/static';
import fastifyCors from '@fastify/cors';
import 'dotenv/config';

console.log("=== Инициализация Fastify сервера ===");

const fastify = Fastify({ 
    logger: true,
    bodyLimit: 100 * 1024 * 1024
});

fastify.register(fastifyCors, {
    origin: true,
    methods: ["GET", "POST", "PUT", "DELETE"]
});

const PORT = process.env.PORT || 3000;
const SERVER_SECRET = process.env.BACKEND_SECRET_KEY || 'default_secret_key_32_chars_long!!';

const UPLOADS_DIR = path.join(__dirname, 'uploads');
if (!fs.existsSync(UPLOADS_DIR)) {
    fs.mkdirSync(UPLOADS_DIR);
}

const MAX_FILE_SIZE_MB = 10;
fastify.register(multipart, {
    limits: { fileSize: MAX_FILE_SIZE_MB * 1024 * 1024 }
});

fastify.register(fastifyStatic, {
    root: UPLOADS_DIR,
    prefix: '/uploads/'
});

// ===== ПОДКЛЮЧЕНИЕ К БД (ДО ИСПОЛЬЗОВАНИЯ!) =====
const pool = new Pool({
    user: process.env.DB_USER,
    password: process.env.DB_PASSWORD,
    host: process.env.DB_HOST,
    port: Number(process.env.DB_PORT),
    database: process.env.DB_NAME,
    ssl: {
        rejectUnauthorized: false
    }
});

// ===== ФУНКЦИЯ ДЛЯ ПОДСЧЕТА РАЗМЕРА ДИРЕКТОРИИ =====
function getDirectorySize(dirPath: string): number {
    let size = 0;
    try {
        const files = fs.readdirSync(dirPath);
        for (const file of files) {
            const stats = fs.statSync(path.join(dirPath, file));
            if (stats.isFile()) size += stats.size;
        }
    } catch (err) {
        console.error('Ошибка подсчета размера:', err);
    }
    return size;
}

// ===== ФУНКЦИИ БАЗЫ ДАННЫХ =====
async function initializeDatabase() {
    console.log("Проверка и создание таблиц БД...");
    
    const queries = [
        `CREATE TABLE IF NOT EXISTS organizations (
            id_org VARCHAR(36) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )`,
        `CREATE TABLE IF NOT EXISTS users (
            id_user VARCHAR(36) PRIMARY KEY,
            id_org VARCHAR(36) REFERENCES organizations(id_org) ON DELETE CASCADE,
            username VARCHAR(255) NOT NULL,
            role VARCHAR(50) DEFAULT 'user',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )`,
        `CREATE TABLE IF NOT EXISTS rooms (
            id_room SERIAL PRIMARY KEY,
            id_org VARCHAR(36) REFERENCES organizations(id_org) ON DELETE CASCADE,
            type VARCHAR(50) NOT NULL,
            name VARCHAR(255) NOT NULL,
            created_by VARCHAR(36) REFERENCES users(id_user),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )`,
        `CREATE TABLE IF NOT EXISTS room_participants (
            id_room INTEGER REFERENCES rooms(id_room) ON DELETE CASCADE,
            id_user VARCHAR(36) REFERENCES users(id_user) ON DELETE CASCADE,
            joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (id_room, id_user)
        )`,
        `CREATE TABLE IF NOT EXISTS messages (
            id_message SERIAL PRIMARY KEY,
            id_room INTEGER REFERENCES rooms(id_room) ON DELETE CASCADE,
            id_user_from VARCHAR(36) REFERENCES users(id_user) ON DELETE CASCADE,
            encrypted_text TEXT NOT NULL,
            is_user_encrypted BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )`,
        `CREATE TABLE IF NOT EXISTS admin_settings (
            id SERIAL PRIMARY KEY,
            setting_key VARCHAR(100) UNIQUE NOT NULL,
            setting_value TEXT,
            setting_type VARCHAR(50) DEFAULT 'text',
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_by VARCHAR(36)
        )`,
        `CREATE TABLE IF NOT EXISTS admin_users (
            id_user VARCHAR(36) PRIMARY KEY,
            can_manage_themes BOOLEAN DEFAULT TRUE,
            can_manage_users BOOLEAN DEFAULT TRUE,
            can_manage_roles BOOLEAN DEFAULT TRUE,
            can_view_logs BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )`
    ];
    
    for (const query of queries) {
        try {
            await pool.query(query);
        } catch (err) {
            console.error("Ошибка создания таблицы:", err);
        }
    }
    
    // Добавляем настройки по умолчанию
    const defaultSettings = `
        INSERT INTO admin_settings (setting_key, setting_value, setting_type) VALUES
            ('theme_primary_color', '#2563eb', 'color'),
            ('theme_background_color', '#f3f4f6', 'color'),
            ('theme_text_color', '#1f2937', 'color'),
            ('company_name', 'Corporate 1C Chat', 'text'),
            ('link1_name', '', 'text'),
            ('link1_url', '', 'url'),
            ('link2_name', '', 'text'),
            ('link2_url', '', 'url')
        ON CONFLICT (setting_key) DO NOTHING
    `;
    
    try {
        await pool.query(defaultSettings);
    } catch (err) {
        console.error("Ошибка добавления настроек:", err);
    }
    
    console.log("✅ Все таблицы БД готовы");
}

function encryptForDB(text: string): string {
    const iv = crypto.randomBytes(16);
    const key = crypto.scryptSync(SERVER_SECRET, 'salt', 32);
    const cipher = crypto.createCipheriv('aes-256-cbc', key, iv);
    let encrypted = cipher.update(text, 'utf8', 'hex');
    encrypted += cipher.final('hex');
    return `${iv.toString('hex')}:${encrypted}`;
}

async function cleanupOldFiles() {
    const MAX_FILE_AGE_DAYS = 30;
    try {
        const files = fs.readdirSync(UPLOADS_DIR);
        const now = Date.now();
        for (const file of files) {
            if (file === 'logo.png' || file === 'logo.jpg') continue;
            const filePath = path.join(UPLOADS_DIR, file);
            const stats = fs.statSync(filePath);
            const fileAge = (now - stats.mtimeMs) / (1000 * 60 * 60 * 24);
            if (fileAge > MAX_FILE_AGE_DAYS) {
                fs.unlinkSync(filePath);
                console.log(`Удален старый файл: ${file}`);
            }
        }
        console.log('Очистка файлов завершена');
    } catch (err) {
        console.error('Ошибка очистки файлов:', err);
    }
}

setInterval(cleanupOldFiles, 24 * 60 * 60 * 1000);

// ===== API ЭНДПОИНТЫ (ТОЛЬКО ОДИН РАЗ!) =====

fastify.post('/api/admin/auth', async (request, reply) => {
    try {
        const { id_user, id_org } = request.body as { id_user: string; id_org: string };
        const result = await pool.query(
            `SELECT u.id_user, u.username, u.role, 
                    COALESCE(au.can_manage_themes, false) as can_manage_themes,
                    COALESCE(au.can_manage_users, false) as can_manage_users,
                    COALESCE(au.can_manage_roles, false) as can_manage_roles,
                    COALESCE(au.can_view_logs, false) as can_view_logs
             FROM users u
             LEFT JOIN admin_users au ON u.id_user = au.id_user
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

fastify.get('/api/admin/settings', async (request, reply) => {
    try {
        const result = await pool.query('SELECT setting_key, setting_value, setting_type FROM admin_settings');
        const settings: Record<string, { value: string; type: string }> = {};
        result.rows.forEach(row => {
            settings[row.setting_key] = { value: row.setting_value, type: row.setting_type };
        });
        return settings;
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка получения настроек' });
    }
});

fastify.post('/api/admin/settings', async (request, reply) => {
    try {
        const { key, value, admin_user } = request.body as { key: string; value: string; admin_user: string };
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

fastify.get('/api/admin/stats', async (request, reply) => {
    try {
        const stats = await pool.query(`
            SELECT 
                (SELECT COUNT(*) FROM messages) as total_messages,
                (SELECT COUNT(*) FROM messages WHERE created_at > NOW() - INTERVAL '1 day') as messages_today,
                pg_database_size(current_database()) as db_size_bytes,
                (SELECT COUNT(*) FROM messages WHERE is_user_encrypted = true) as encrypted_messages
        `);
        const uploadsSize = getDirectorySize(UPLOADS_DIR);
        return {
            messages: stats.rows[0],
            dbSizeMB: (stats.rows[0].db_size_bytes / 1024 / 1024).toFixed(2),
            uploadsSizeMB: (uploadsSize / 1024 / 1024).toFixed(2),
            storageUsedPercent: ((stats.rows[0].db_size_bytes + uploadsSize) / (1024 * 1024 * 1024) * 100).toFixed(1)
        };
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка получения статистики' });
    }
});

fastify.get('/api/admin/users', async (request, reply) => {
    try {
        const result = await pool.query(`
            SELECT u.id_user, u.username, u.role,
                   COALESCE(au.can_manage_themes, false) as can_manage_themes,
                   COALESCE(au.can_manage_users, false) as can_manage_users,
                   COALESCE(au.can_manage_roles, false) as can_manage_roles,
                   COALESCE(au.can_view_logs, false) as can_view_logs
            FROM users u
            LEFT JOIN admin_users au ON u.id_user = au.id_user
            ORDER BY u.username
        `);
        return result.rows;
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка получения пользователей' });
    }
});

fastify.post('/api/admin/user/permissions', async (request, reply) => {
    try {
        const { target_user, permissions } = request.body as { target_user: string; permissions: any };
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

fastify.post('/api/admin/sql', async (request, reply) => {
    try {
        const { sql } = request.body as { sql: string };
        const sqlLower = sql.trim().toLowerCase();
        if (!sqlLower.startsWith('select')) {
            return reply.status(400).send({ error: 'Разрешены только SELECT запросы' });
        }
        const result = await pool.query(sql);
        return { rows: result.rows };
    } catch (err: any) {
        return reply.status(500).send({ error: err.message });
    }
});

fastify.get('/api/admin/logs', async (request, reply) => {
    try {
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

fastify.post('/api/admin/upload-logo', async (request, reply) => {
    try {
        const data = await request.file();
        if (!data) {
            return reply.status(400).send({ error: 'Файл не загружен' });
        }
        const fileExt = path.extname(data.filename);
        const uniqueFileName = `logo${fileExt}`;
        const saveTo = path.join(UPLOADS_DIR, uniqueFileName);
        await new Promise((resolve, reject) => {
            const out = fs.createWriteStream(saveTo);
            data.file.pipe(out);
            out.on('finish', resolve);
            out.on('error', reject);
        });
        const logoUrl = `/uploads/${uniqueFileName}`;
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

fastify.get('/api/admin/links', async (request, reply) => {
    try {
        const result = await pool.query(
            "SELECT setting_key, setting_value FROM admin_settings WHERE setting_key LIKE 'link_%'"
        );
        const links: Record<string, string> = {};
        result.rows.forEach(row => {
            links[row.setting_key] = row.setting_value;
        });
        return links;
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка получения ссылок' });
    }
});

fastify.post('/api/admin/links', async (request, reply) => {
    try {
        const { links, admin_user } = request.body as { links: Record<string, string>; admin_user: string };
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

fastify.get('/api/admin/theme', async (request, reply) => {
    try {
        const result = await pool.query(
            "SELECT setting_key, setting_value FROM admin_settings WHERE setting_type IN ('color', 'css')"
        );
        const theme: Record<string, string> = {};
        result.rows.forEach(row => {
            theme[row.setting_key] = row.setting_value;
        });
        return theme;
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка получения темы' });
    }
});
// ===== HEALTH CHECK ENDPOINT =====
fastify.get('/health', async (request, reply) => {
    try {
        // Проверяем подключение к БД
        await pool.query('SELECT 1');
        return { 
            status: 'ok', 
            timestamp: new Date().toISOString(),
            database: 'connected',
            uptime: process.uptime()
        };
    } catch (err) {
        return reply.status(503).send({ 
            status: 'error', 
            database: 'disconnected',
            error: err.message 
        });
    }
});

fastify.get('/ping', async (request, reply) => {
    return { pong: Date.now() };
});
// ===== ОСНОВНЫЕ МАРШРУТЫ =====

fastify.post('/upload', async (request, reply) => {
    try {
        const data = await request.file();
        if (!data) {
            return reply.status(400).send({ error: 'Файл не прикреплен' });
        }
        const fileExt = path.extname(data.filename);
        const uniqueFileName = `${crypto.randomUUID()}${fileExt}`;
        const saveTo = path.join(UPLOADS_DIR, uniqueFileName);
        await new Promise((resolve, reject) => {
            const out = fs.createWriteStream(saveTo);
            data.file.pipe(out);
            out.on('finish', resolve);
            out.on('error', reject);
        });
        const domain = `${request.protocol}://${request.hostname}`;
        const fileUrl = `${domain}/uploads/${uniqueFileName}`;
        return { url: fileUrl, originalName: data.filename };
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка сервера при загрузке файла' });
    }
});

fastify.get('/', async (request, reply) => {
    try {
        const indexPath = path.join(__dirname, 'index.html');
        const htmlContent = fs.readFileSync(indexPath, 'utf8');
        reply.type('text/html').send(htmlContent);
    } catch (err) {
        reply.status(500).send('Ошибка сервера: не найден файл index.html');
    }
});

fastify.get('/admin', async (request, reply) => {
    try {
        const adminPath = path.join(__dirname, 'admin.html');
        const htmlContent = fs.readFileSync(adminPath, 'utf8');
        reply.type('text/html').send(htmlContent);
    } catch (err) {
        reply.status(500).send('Ошибка загрузки админ-панели');
    }
});

fastify.get('/favicon.ico', async (request, reply) => {
    return reply.status(204).send();
});

// ===== ЗАПУСК СЕРВЕРА =====

const start = async () => {
    try {
        // Небольшая задержка для Railway
        await new Promise(resolve => setTimeout(resolve, 2000));
        
        await pool.query('SELECT NOW()');
        console.log('=== Успешное подключение к базе данных Railway! ===');
        await pool.query('SELECT NOW()');
        console.log('=== Успешное подключение к базе данных Railway! ===');
        
        await initializeDatabase();

        const io = new Server(fastify.server, { 
            cors: { origin: "*" },
            maxHttpBufferSize: 1e8
        });

        io.use(async (socket, next) => {
            let { id_user, id_org, username, user_role } = socket.handshake.query;
            if (!id_user || !id_org || !username) {
                return next(new Error("Ошибка: 1С не передала параметры авторизации"));
            }
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
            console.log(`✅ ${username} (${role}) подключен`);
            
            try {
                await pool.query('INSERT INTO organizations (id_org, name) VALUES ($1, $2) ON CONFLICT (id_org) DO NOTHING', 
                    [id_org, 'Организация из 1С']);
                await pool.query(`INSERT INTO users (id_user, id_org, username, role) 
                    VALUES ($1, $2, $3, $4) ON CONFLICT (id_user) DO UPDATE SET role = $4`, 
                    [id_user, id_org, username, role]);
                await pool.query(`INSERT INTO rooms (id_room, id_org, type, name) 
                    VALUES (1, $1, 'general', 'Общий чат') ON CONFLICT (id_room) DO NOTHING`, [id_org]);
            } catch (err) {
                console.error('Ошибка регистрации:', err);
            }

            socket.join(`org_${id_org}`);
            socket.join(`room_1`);

            socket.on('send_message', async (data: { room_id: number; text: string; is_secret: boolean }) => {
                try {
                    const encryptedTextForDB = encryptForDB(data.text);
                    await pool.query(`INSERT INTO messages (id_room, id_user_from, encrypted_text, is_user_encrypted) 
                        VALUES ($1, $2, $3, $4)`, [data.room_id, id_user, encryptedTextForDB, data.is_secret]);
                    io.to(`room_${data.room_id}`).emit('new_message', {
                        id_room: data.room_id,
                        id_user_from: id_user,
                        username: username,
                        text: data.text, 
                        is_secret: data.is_secret,
                        created_at: new Date()
                    });
                } catch (err) {
                    console.error('Ошибка при сохранении сообщения:', err);
                }
            });
        });

        const listenAddress = await fastify.listen({ port: Number(PORT), host: '0.0.0.0' });
        console.log(`=== МЕССЕНДЖЕР УСПЕШНО ЗАПУЩЕН НА: ${listenAddress} ===`);
    } catch (err) {
        console.error('Критическая ошибка при старте Fastify:', err);
        process.exit(1);
    }
};

start();