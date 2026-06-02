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

const fastify = Fastify({ 
    logger: true,
    bodyLimit: 100 * 1024 * 1024
});

// Разрешаем CORS для всех HTTP запросов
fastify.register(fastifyCors, {
    origin: true,
    methods: ["GET", "POST", "PUT", "DELETE"]
});

// Railway автоматически передает правильный порт через переменную окружения PORT
const PORT = process.env.PORT || 3000;
const SERVER_SECRET = process.env.BACKEND_SECRET_KEY || 'default_secret_key_32_chars_long!!';

// Создаем локальную директорию для хранения файлов, если её нет
const UPLOADS_DIR = path.join(__dirname, 'uploads');
if (!fs.existsSync(UPLOADS_DIR)) {
    fs.mkdirSync(UPLOADS_DIR);
}

// Регистрируем плагин для работы с файлами (лимит 100 МБ)
fastify.register(multipart, {
    limits: { fileSize: 100 * 1024 * 1024 }
});

// Открываем статический доступ к папке uploads для файлов
fastify.register(fastifyStatic, {
    root: UPLOADS_DIR,
    prefix: '/uploads/'
});

// Настройка подключения к PostgreSQL в Railway с поддержкой SSL
const pool = new Pool({
    user: process.env.DB_USER,
    password: process.env.DB_PASSWORD,
    host: process.env.DB_HOST,
    port: Number(process.env.DB_PORT),
    database: process.env.DB_NAME,
    // Включаем SSL, чтобы Railway не сбрасывал соединение с БД из облака
    ssl: {
        rejectUnauthorized: false
    }
});

// Функция шифрования текста для базы данных (AES-256-CBC)
function encryptForDB(text: string): string {
    const iv = crypto.randomBytes(16);
    const key = crypto.scryptSync(SERVER_SECRET, 'salt', 32);
    const cipher = crypto.createCipheriv('aes-256-cbc', key, iv);
    let encrypted = cipher.update(text, 'utf8', 'hex');
    encrypted += cipher.final('hex');
    return `${iv.toString('hex')}:${encrypted}`;
}

// HTTP эндпоинт для безопасной загрузки тяжелых файлов и скриншотов
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

        // ИЗМЕНЕНО: Динамически определяем домен (например, https://your-subdomain.up.railway.app)
        // вместо жесткой привязки к localhost
        const domain = `${request.protocol}://${request.hostname}`;
        const fileUrl = `${domain}/uploads/${uniqueFileName}`;
        
        return { url: fileUrl, originalName: data.filename };
    } catch (err) {
        return reply.status(500).send({ error: 'Ошибка сервера при загрузке файла' });
    }
});

// Прямой роут для главной страницы (Считывает index.html через файловую систему)
fastify.get('/', async (request, reply) => {
    try {
        const indexPath = path.join(__dirname, 'index.html');
        const htmlContent = fs.readFileSync(indexPath, 'utf8');
        reply.type('text/html').send(htmlContent);
    } catch (err) {
        reply.status(500).send('Ошибка сервера: не найден файл index.html в папке сервера');
    }
});

// Заглушка для иконки favicon, чтобы логи были чистыми
fastify.get('/favicon.ico', async (request, reply) => {
    return reply.status(204).send();
});

const start = async () => {
    try {
        await pool.query('SELECT NOW()');
        console.log('Успешное подключение к базе данных Railway!');

        // Запуск прослушивания порта. На Railway обязательно хост '0.0.0.0'
        await fastify.listen({ port: Number(PORT), host: '0.0.0.0' });
        
        const io = new Server(fastify.server, { 
            cors: { origin: "*" },
            maxHttpBufferSize: 1e8 // 100 МБ
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

            try {
                const roomsResult = await pool.query('SELECT id_room, name, type FROM rooms WHERE id_room = 1 OR id_org = $1', [id_org]);
                console.log(`[БД] Отправляем список комнат для ${username}:`, roomsResult.rows);
                socket.emit('rooms_list', roomsResult.rows);
            } catch (err) {
                console.error('Ошибка получения списка комнат:', err);
            }

            socket.on('get_rooms_again', async () => {
                try {
                    const roomsResult = await pool.query('SELECT id_room, name, type FROM rooms WHERE id_room = 1 OR id_org = $1', [id_org]);
                    socket.emit('rooms_list', roomsResult.rows);
                } catch (err) {
                    console.error('Ошибка обновления списка комнат:', err);
                }
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

    } catch (err) {
        fastify.log.error(err);
        process.exit(1);
    }
};

start();