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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )`,
        `CREATE TABLE IF NOT EXISTS rooms (
            id_room SERIAL PRIMARY KEY,
            id_org VARCHAR(36) REFERENCES organizations(id_org) ON DELETE CASCADE,
            type VARCHAR(50) NOT NULL,
            name VARCHAR(255) NOT NULL,
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
        reply.status(500).send('Ошибка сервера: не найден файл index.html в папке сервера');
    }
});

fastify.get('/favicon.ico', async (request, reply) => {
    return reply.status(204).send();
});

const start = async () => {
    try {
        await pool.query('SELECT NOW()');
        console.log('=== Успешное подключение к базе данных Railway! ===');
        
        // Инициализируем таблицы
        await initializeDatabase();

        const io = new Server(fastify.server, { 
            cors: { origin: "*" },
            maxHttpBufferSize: 1e8
        });

        io.use(async (socket, next) => {
            let { id_user, id_org, username } = socket.handshake.query;
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
            socket.data = { id_user: cleanUserId, id_org: cleanOrgId, username };
            next();
        });

        io.on('connection', async (socket) => {
            const { id_org, id_user, username } = socket.data;
            
            try {
                await pool.query('INSERT INTO organizations (id_org, name) VALUES ($1, $2) ON CONFLICT (id_org) DO NOTHING', [id_org, 'Организация из 1С']);
                await pool.query('INSERT INTO users (id_user, id_org, username) VALUES ($1, $2, $3) ON CONFLICT (id_user) DO NOTHING', [id_user, id_org, username]);
                await pool.query(`INSERT INTO rooms (id_room, id_org, type, name) VALUES (1, $1, 'general', 'Общий чат') ON CONFLICT (id_room) DO NOTHING`, [id_org]);
            } catch (err) {
                console.error('Ошибка синхронизации данных с БД:', err);
            }

            socket.join(`org_${id_org}`);
            socket.join(`room_1`);

            const sendRoomsList = async () => {
                try {
                    const roomsResult = await pool.query(`
                        SELECT r.id_room, r.name, r.type 
                        FROM rooms r
                        WHERE r.id_room = 1 AND r.id_org = $1
                        UNION
                        SELECT r.id_room, r.name, r.type 
                        FROM rooms r
                        JOIN room_participants rp ON r.id_room = rp.id_room
                        WHERE r.id_org = $1 AND rp.id_user = $2
                    `, [id_org, id_user]);
                    socket.emit('rooms_list', roomsResult.rows);
                } catch (err) {
                    console.error('Ошибка получения списка комнат:', err);
                }
            };

            await sendRoomsList();

            socket.on('get_users_list', async () => {
                try {
                    const usersResult = await pool.query('SELECT id_user, username FROM users WHERE id_org = $1 AND id_user != $2', [id_org, id_user]);
                    socket.emit('users_list', usersResult.rows);
                } catch (err) { console.error(err); }
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
                    const newRoom = await pool.query('INSERT INTO rooms (id_org, type, name) VALUES ($1, $2, $3) RETURNING id_room', [id_org, 'private', roomName]);
                    const newRoomId = newRoom.rows[0].id_room;

                    await pool.query('INSERT INTO room_participants (id_room, id_user) VALUES ($1, $2), ($1, $3)', [newRoomId, id_user, data.target_user_id]);
                    
                    io.to(`org_${id_org}`).emit('refresh_rooms_trigger');
                } catch (err) { console.error(err); }
            });

            socket.on('create_group_chat', async (data: { group_name: string, user_ids: string[] }) => {
                try {
                    const newRoom = await pool.query('INSERT INTO rooms (id_org, type, name) VALUES ($1, $2, $3) RETURNING id_room', [id_org, 'group', data.group_name]);
                    const newRoomId = newRoom.rows[0].id_room;

                    await pool.query('INSERT INTO room_participants (id_room, id_user) VALUES ($1, $2)', [newRoomId, id_user]);
                    for (const targetId of data.user_ids) {
                        await pool.query('INSERT INTO room_participants (id_room, id_user) VALUES ($1, $2) ON CONFLICT DO NOTHING', [newRoomId, targetId]);
                    }

                    io.to(`org_${id_org}`).emit('refresh_rooms_trigger');
                } catch (err) { console.error(err); }
            });

            socket.on('join_room_pool', (data: { room_id: number }) => {
                socket.join(`room_${data.room_id}`);
            });

            socket.on('get_rooms_again', async () => {
                await sendRoomsList();
            });

            socket.on('send_message', async (data: { room_id: number, text: string, is_secret: boolean }) => {
                try {
                    const encryptedTextForDB = encryptForDB(data.text);
                    await pool.query(`INSERT INTO messages (id_room, id_user_from, encrypted_text, is_user_encrypted) VALUES ($1, $2, $3, $4)`, [data.room_id, id_user, encryptedTextForDB, data.is_secret]);

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