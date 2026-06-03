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

fastify.register(multipart, {
    limits: { fileSize: 100 * 1024 * 1024 }
});

fastify.register(fastifyStatic, {
    root: UPLOADS_DIR,
    prefix: '/uploads/'
});

// Админ-панель
fastify.get('/admin', async (request, reply) => {
    try {
        const adminPath = path.join(__dirname, 'admin.html');
        const htmlContent = fs.readFileSync(adminPath, 'utf8');
        reply.type('text/html').send(htmlContent);
    } catch (err) {
        reply.status(500).send('Ошибка загрузки админ-панели');
    }
});

// API: Получить все настройки
fastify.get('/api/admin/settings', async (request, reply) => {
    try {
        const result = await pool.query('SELECT setting_key, setting_value, setting_type FROM admin_settings');
        const settings = {};
        result.rows.forEach(row => {
            settings[row.setting_key] = {
                value: row.setting_value,
                type: row.setting_type
            };
        });
        return settings;
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка получения настроек' });
    }
});

// API: Обновить настройку
fastify.post('/api/admin/settings', async (request, reply) => {
    try {
        const { key, value, admin_user } = request.body;
        
        // Проверка прав
        const adminCheck = await pool.query(
            'SELECT can_manage_themes FROM admin_users WHERE id_user = $1',
            [admin_user]
        );
        
        if (adminCheck.rows.length === 0 || !adminCheck.rows[0].can_manage_themes) {
            return reply.status(403).send({ error: 'Недостаточно прав' });
        }
        
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

// API: Получить список пользователей для админки
fastify.get('/api/admin/users', async (request, reply) => {
    try {
        const result = await pool.query(`
            SELECT u.id_user, u.username, u.role, 
                   COALESCE(au.can_manage_themes, false) as can_manage_themes,
                   COALESCE(au.can_manage_users, false) as can_manage_users,
                   COALESCE(au.can_manage_roles, false) as can_manage_roles
            FROM users u
            LEFT JOIN admin_users au ON u.id_user = au.id_user
            WHERE u.role = 'admin'
            ORDER BY u.username
        `);
        return result.rows;
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка получения пользователей' });
    }
});

// API: Обновить права пользователя
fastify.post('/api/admin/user/permissions', async (request, reply) => {
    try {
        const { target_user, permissions, admin_user } = request.body;
        
        // Проверка прав текущего админа
        const adminCheck = await pool.query(
            'SELECT can_manage_users, can_manage_roles FROM admin_users WHERE id_user = $1',
            [admin_user]
        );
        
        if (adminCheck.rows.length === 0) {
            return reply.status(403).send({ error: 'Недостаточно прав' });
        }
        
        await pool.query(
            `INSERT INTO admin_users (id_user, can_manage_themes, can_manage_users, can_manage_roles, can_view_logs)
             VALUES ($1, $2, $3, $4, $5)
             ON CONFLICT (id_user)
             DO UPDATE SET 
                can_manage_themes = $2,
                can_manage_users = $3,
                can_manage_roles = $4,
                can_view_logs = $5`,
            [target_user, permissions.can_manage_themes, permissions.can_manage_users, 
             permissions.can_manage_roles, permissions.can_view_logs]
        );
        
        return { success: true };
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка обновления прав' });
    }
});

// API: Получить логи (только для админов)
fastify.get('/api/admin/logs', async (request, reply) => {
    try {
        const { admin_user } = request.query;
        
        const adminCheck = await pool.query(
            'SELECT can_view_logs FROM admin_users WHERE id_user = $1',
            [admin_user]
        );
        
        if (adminCheck.rows.length === 0 || !adminCheck.rows[0].can_view_logs) {
            return reply.status(403).send({ error: 'Недостаточно прав' });
        }
        
        // Читаем последние 100 строк лога
        const logPath = path.join(__dirname, 'logs', 'app.log');
        if (fs.existsSync(logPath)) {
            const logs = fs.readFileSync(logPath, 'utf8').split('\n').slice(-100);
            return { logs };
        }
        return { logs: [] };
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка получения логов' });
    }
});

// API: Применить стили к index.html
fastify.get('/api/admin/theme', async (request, reply) => {
    try {
        const settings = await pool.query(
            'SELECT setting_key, setting_value FROM admin_settings WHERE setting_type IN ($1, $2, $3)',
            ['color', 'css', 'url']
        );
        
        const theme = {};
        settings.rows.forEach(row => {
            theme[row.setting_key] = row.setting_value;
        });
        
        return theme;
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка получения темы' });
    }
});

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

// Функция инициализации всех таблиц
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
        )`
    ];
    
    for (const query of queries) {
        try {
            await pool.query(query);
        } catch (err) {
            console.error("Ошибка создания таблицы:", err);
            throw err;
        }
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

fastify.get('/favicon.ico', async (request, reply) => {
    return reply.status(204).send();
});

const start = async () => {
    try {
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
            
            let cleanOrgId = String(id_org).toLowerCase().trim();
            const uuidRegex = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
            if (!uuidRegex.test(cleanOrgId)) {
                const hash = crypto.createHash('md5').update(cleanOrgId).digest('hex');
                cleanOrgId = `${hash.slice(0, 8)}-${hash.slice(8, 12)}-${hash.slice(12, 16)}-${hash.slice(16, 20)}-${hash.slice(20, 32)}`;
            }
            
            const cleanUserId = String(id_user).trim();
            const cleanRole = user_role === 'admin' ? 'admin' : 'user';
            
            socket.data = { 
                id_user: cleanUserId, 
                id_org: cleanOrgId, 
                username: String(username).trim(),
                role: cleanRole
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
                
                await pool.query(`INSERT INTO rooms (id_room, id_org, type, name, created_by) 
                    VALUES (1, $1, 'general', 'Общий чат', $2) ON CONFLICT (id_room) DO NOTHING`, 
                    [id_org, id_user]);
                
                // Добавляем пользователя в общий чат
                await pool.query(`INSERT INTO room_participants (id_room, id_user) 
                    VALUES (1, $1) ON CONFLICT DO NOTHING`, [id_user]);
                    
            } catch (err) {
                console.error('Ошибка регистрации:', err);
            }

            socket.join(`org_${id_org}`);
            socket.join(`room_1`);

            const sendRoomsList = async () => {
                try {
                    const roomsResult = await pool.query(`
                        SELECT DISTINCT r.id_room, r.name, r.type, r.created_by,
                            (SELECT COUNT(*) FROM room_participants WHERE id_room = r.id_room) as member_count
                        FROM rooms r
                        LEFT JOIN room_participants rp ON r.id_room = rp.id_room
                        WHERE r.id_org = $1 AND (rp.id_user = $2 OR r.type = 'general')
                        ORDER BY r.id_room
                    `, [id_org, id_user]);
                    socket.emit('rooms_list', roomsResult.rows);
                } catch (err) {
                    console.error('Ошибка получения списка комнат:', err);
                }
            };

            await sendRoomsList();

            // Получение списка пользователей
            socket.on('get_users_list', async () => {
                try {
                    const usersResult = await pool.query(
                        'SELECT id_user, username, role FROM users WHERE id_org = $1 AND id_user != $2',
                        [id_org, id_user]
                    );
                    socket.emit('users_list', usersResult.rows);
                } catch (err) { console.error(err); }
            });

            // СОЗДАНИЕ ГРУППЫ - только для администраторов
            socket.on('create_group_chat', async (data: { group_name: string, user_ids: string[] }) => {
                if (role !== 'admin') {
                    socket.emit('error', { message: 'Только администраторы могут создавать группы' });
                    return;
                }
                
                try {
                    const newRoom = await pool.query(
                        'INSERT INTO rooms (id_org, type, name, created_by) VALUES ($1, $2, $3, $4) RETURNING id_room',
                        [id_org, 'group', data.group_name, id_user]
                    );
                    const newRoomId = newRoom.rows[0].id_room;

                    // Добавляем создателя
                    await pool.query('INSERT INTO room_participants (id_room, id_user) VALUES ($1, $2)', 
                        [newRoomId, id_user]);
                    
                    // Добавляем выбранных пользователей
                    for (const targetId of data.user_ids) {
                        await pool.query('INSERT INTO room_participants (id_room, id_user) VALUES ($1, $2) ON CONFLICT DO NOTHING', 
                            [newRoomId, targetId]);
                    }

                    io.to(`org_${id_org}`).emit('refresh_rooms_trigger');
                    socket.emit('group_created', { id_room: newRoomId, name: data.group_name });
                } catch (err) { 
                    console.error(err);
                    socket.emit('error', { message: 'Ошибка создания группы' });
                }
            });

            // ПОЛУЧЕНИЕ СПИСКА УЧАСТНИКОВ КОМНАТЫ
            socket.on('get_room_members', async (data: { room_id: number }) => {
                try {
                    const members = await pool.query(`
                        SELECT u.id_user, u.username, u.role
                        FROM room_participants rp
                        JOIN users u ON rp.id_user = u.id_user
                        WHERE rp.id_room = $1
                    `, [data.room_id]);
                    socket.emit('room_members', members.rows);
                } catch (err) {
                    console.error(err);
                }
            });

            // ДОБАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯ В ГРУППУ (только для администраторов)
            socket.on('add_user_to_group', async (data: { room_id: number, user_id: string }) => {
                if (role !== 'admin') {
                    socket.emit('error', { message: 'Только администраторы могут добавлять пользователей' });
                    return;
                }
                
                try {
                    // Проверяем, что комната - группа
                    const room = await pool.query('SELECT type FROM rooms WHERE id_room = $1 AND id_org = $2', 
                        [data.room_id, id_org]);
                    
                    if (room.rows[0]?.type !== 'group') {
                        socket.emit('error', { message: 'Это не групповая комната' });
                        return;
                    }
                    
                    await pool.query('INSERT INTO room_participants (id_room, id_user) VALUES ($1, $2) ON CONFLICT DO NOTHING',
                        [data.room_id, data.user_id]);
                    
                    // Уведомляем всех участников организации
                    io.to(`org_${id_org}`).emit('refresh_rooms_trigger');
                    socket.emit('user_added', { room_id: data.room_id, user_id: data.user_id });
                } catch (err) {
                    console.error(err);
                    socket.emit('error', { message: 'Ошибка добавления пользователя' });
                }
            });

            // УДАЛЕНИЕ ПОЛЬЗОВАТЕЛЯ ИЗ ГРУППЫ (только для администраторов)
            socket.on('remove_user_from_group', async (data: { room_id: number, user_id: string }) => {
                if (role !== 'admin') {
                    socket.emit('error', { message: 'Только администраторы могут удалять пользователей' });
                    return;
                }
                
                try {
                    // Нельзя удалить создателя группы
                    const room = await pool.query('SELECT created_by FROM rooms WHERE id_room = $1', [data.room_id]);
                    if (room.rows[0]?.created_by === data.user_id) {
                        socket.emit('error', { message: 'Нельзя удалить создателя группы' });
                        return;
                    }
                    
                    await pool.query('DELETE FROM room_participants WHERE id_room = $1 AND id_user = $2',
                        [data.room_id, data.user_id]);
                    
                    io.to(`org_${id_org}`).emit('refresh_rooms_trigger');
                    socket.emit('user_removed', { room_id: data.room_id, user_id: data.user_id });
                } catch (err) {
                    console.error(err);
                    socket.emit('error', { message: 'Ошибка удаления пользователя' });
                }
            });

            socket.on('create_private_chat', async (data: { target_user_id: string, target_username: string }) => {
                try {
                    const checkChat = await pool.query(`
                        SELECT rp1.id_room FROM room_participants rp1
                        JOIN room_participants rp2 ON rp1.id_room = rp2.id_room
                        JOIN rooms r ON r.id_room = rp1.id_room
                        WHERE r.type = 'private' AND r.id_org = $1 AND rp1.id_user = $2 AND rp2.id_user = $3
                    `, [id_org, id_user, data.target_user_id]);

                    if (checkChat.rows.length > 0) {
                        socket.emit('private_chat_created', { id_room: checkChat.rows[0].id_room });
                        return;
                    }

                    const roomName = `${username} ⇄ ${data.target_username}`;
                    const newRoom = await pool.query(
                        'INSERT INTO rooms (id_org, type, name, created_by) VALUES ($1, $2, $3, $4) RETURNING id_room',
                        [id_org, 'private', roomName, id_user]
                    );
                    const newRoomId = newRoom.rows[0].id_room;

                    await pool.query('INSERT INTO room_participants (id_room, id_user) VALUES ($1, $2), ($1, $3)', 
                        [newRoomId, id_user, data.target_user_id]);
                    
                    io.to(`org_${id_org}`).emit('refresh_rooms_trigger');
                } catch (err) { console.error(err); }
            });

            socket.on('join_room_pool', (data: { room_id: number }) => {
                socket.join(`room_${data.room_id}`);
                socket.emit('joined_room', { room_id: data.room_id });
            });

            socket.on('get_rooms_again', async () => {
                await sendRoomsList();
            });

            socket.on('send_message', async (data: { room_id: number, text: string, is_secret: boolean }) => {
                try {
                    const encryptedTextForDB = encryptForDB(data.text);
                    await pool.query(
                        `INSERT INTO messages (id_room, id_user_from, encrypted_text, is_user_encrypted) 
                         VALUES ($1, $2, $3, $4)`,
                        [data.room_id, id_user, encryptedTextForDB, data.is_secret]
                    );

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

        const listenAddress = await fastify.listen({ 
            port: Number(PORT), 
            host: '0.0.0.0' 
        });
        
        console.log(`=== МЕССЕНДЖЕР УСПЕШНО ЗАПУЩЕН НА: ${listenAddress} ===`);

    } catch (err) {
        console.error('Критическая ошибка при старте Fastify:', err);
        process.exit(1);
    }
};

start();