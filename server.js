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

// API эндпоинты
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

// Запуск сервера
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