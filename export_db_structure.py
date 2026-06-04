import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor

# Подключение к БД
DATABASE_URL = os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

conn = psycopg2.connect(DATABASE_URL)
cur = conn.cursor(cursor_factory=RealDictCursor)

# Получаем все таблицы
cur.execute("""
    SELECT table_name 
    FROM information_schema.tables 
    WHERE table_schema = 'public' 
    AND table_type = 'BASE TABLE'
    ORDER BY table_name
""")
tables = cur.fetchall()

result = {}

for table in tables:
    table_name = table['table_name']
    
    # Получаем структуру таблицы
    cur.execute("""
        SELECT 
            c.column_name,
            c.data_type,
            c.is_nullable,
            c.column_default,
            c.character_maximum_length,
            CASE WHEN pk.constraint_type = 'PRIMARY KEY' THEN true ELSE false END as is_primary_key,
            CASE WHEN fk.constraint_type = 'FOREIGN KEY' THEN true ELSE false END as is_foreign_key,
            fk.referenced_table_name,
            fk.referenced_column_name
        FROM information_schema.columns c
        LEFT JOIN (
            SELECT kcu.column_name, tc.constraint_type
            FROM information_schema.key_column_usage kcu
            JOIN information_schema.table_constraints tc ON kcu.constraint_name = tc.constraint_name
            WHERE tc.constraint_type = 'PRIMARY KEY' AND kcu.table_name = %s
        ) pk ON c.column_name = pk.column_name
        LEFT JOIN (
            SELECT 
                kcu.column_name,
                tc.constraint_type,
                ccu.table_name as referenced_table_name,
                ccu.column_name as referenced_column_name
            FROM information_schema.key_column_usage kcu
            JOIN information_schema.table_constraints tc ON kcu.constraint_name = tc.constraint_name
            JOIN information_schema.constraint_column_usage ccu ON tc.constraint_name = ccu.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY' AND kcu.table_name = %s
        ) fk ON c.column_name = fk.column_name
        WHERE c.table_schema = 'public' AND c.table_name = %s
        ORDER BY c.ordinal_position
    """, (table_name, table_name, table_name))
    
    columns = cur.fetchall()
    result[table_name] = columns

# Сохраняем в JSON файл
with open('database_structure.json', 'w', encoding='utf-8') as f:
    json.dump(result, f, ensure_ascii=False, indent=2, default=str)

print(f"✅ Структура сохранена в database_structure.json")
print(f"📊 Всего таблиц: {len(result)}")

for table_name, columns in result.items():
    print(f"  - {table_name}: {len(columns)} колонок")

cur.close()
conn.close()