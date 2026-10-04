import psycopg2
import os
import logging
import re
import json
from cryptography.fernet import Fernet, MultiFernet, InvalidToken

# Llave de cifrado simétrico (AES). Es obligatoria: sin ella no arrancamos,
# para no volver a cifrar con una llave pública escrita en el código.
key_env = os.getenv("FERNET_KEY")
if not key_env:
    raise RuntimeError(
        "Falta FERNET_KEY en el entorno. Genera una con: "
        "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
    )

# La primera llave cifra; las demás solo descifran (útil al rotar la llave).
llaves = [Fernet(key_env.encode())]
key_anterior = os.getenv("FERNET_KEY_ANTERIOR")
if key_anterior:
    llaves.append(Fernet(key_anterior.encode()))
cipher = MultiFernet(llaves)

def get_db_connection():
    try:
        return psycopg2.connect(
            host="db",
            database=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD")
        )
    except Exception as e:
        logging.error(f"Error BD: {e}")
        return None

def init_db():
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        
        # 1. Tabla de usuarios principal
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS usuarios (
                telegram_id BIGINT PRIMARY KEY,
                moodle_url TEXT NOT NULL,
                fecha_registro TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 2. Tabla del diccionario de materias
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS materias_cugdl (
                id_moodle VARCHAR(10) PRIMARY KEY,
                nombre_oficial TEXT NOT NULL
            )
        ''')

        # 3. NUEVA: Tabla de Tareas Entregadas (Check-list)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tareas_entregadas (
                telegram_id BIGINT,
                id_tarea TEXT,
                fecha_completada TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (telegram_id, id_tarea)
            )
        ''')

        # 4. NUEVA: Tabla de Caché (Resiliencia si Moodle se cae)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS cache_calendario (
                telegram_id BIGINT PRIMARY KEY,
                datos_json TEXT NOT NULL,
                ultima_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        conn.commit()
        cursor.close()
        conn.close()

# --- FUNCIONES DE USUARIO Y CIFRADO ---
def registrar_usuario(telegram_id, moodle_url):
    # Encriptamos el enlace antes de guardarlo (Seguridad Data at Rest)
    url_cifrada = cipher.encrypt(moodle_url.encode('utf-8')).decode('utf-8')
    
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO usuarios (telegram_id, moodle_url)
            VALUES (%s, %s)
            ON CONFLICT (telegram_id) DO UPDATE SET moodle_url = EXCLUDED.moodle_url
        ''', (telegram_id, url_cifrada))
        conn.commit()
        cursor.close()
        conn.close()
        return True
    return False

def obtener_usuario(telegram_id):
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        cursor.execute("SELECT moodle_url FROM usuarios WHERE telegram_id = %s", (telegram_id,))
        resultado = cursor.fetchone()
        cursor.close()
        conn.close()
        
        if resultado:
            url_guardada = resultado[0]
            try:
                # Intentamos desencriptar
                url_descifrada = cipher.decrypt(url_guardada.encode('utf-8')).decode('utf-8')
                return url_descifrada
            except InvalidToken:
                if url_guardada.startswith(("http://", "https://")):
                    # Enlace viejo guardado en texto plano: lo re-encriptamos para sanearlo.
                    registrar_usuario(telegram_id, url_guardada)
                    return url_guardada
                # Está cifrado con una llave que no tenemos. No lo tocamos: re-cifrarlo
                # como si fuera texto plano destruiría el enlace original.
                logging.error(
                    f"No se pudo descifrar el enlace del usuario {telegram_id}. "
                    "¿Cambiaste FERNET_KEY sin definir FERNET_KEY_ANTERIOR?"
                )
                return None
    return None

def eliminar_usuario(telegram_id):
    """Borrado en cascada para garantizar la privacidad."""
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tareas_entregadas WHERE telegram_id = %s", (telegram_id,))
        cursor.execute("DELETE FROM cache_calendario WHERE telegram_id = %s", (telegram_id,))
        cursor.execute("DELETE FROM usuarios WHERE telegram_id = %s", (telegram_id,))
        conn.commit()
        cursor.close()
        conn.close()
        return True
    return False

def obtener_todos_los_usuarios():
    """Para el motor de notificaciones en segundo plano."""
    conn = get_db_connection()
    usuarios = []
    if conn:
        cursor = conn.cursor()
        cursor.execute("SELECT telegram_id FROM usuarios")
        resultados = cursor.fetchall()
        cursor.close()
        conn.close()
        usuarios = [row[0] for row in resultados]
    return usuarios

# --- FUNCIONES DE MATERIAS ---
def buscar_nombre_materia(clave_moodle):
    match = re.search(r'(\d{6})', clave_moodle)
    if not match: return clave_moodle 
    id_numerico = match.group(1)
    
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        cursor.execute("SELECT nombre_oficial FROM materias_cugdl WHERE id_moodle = %s", (id_numerico,))
        resultado = cursor.fetchone()
        cursor.close()
        conn.close()
        if resultado: return resultado[0]
    return clave_moodle

# --- FUNCIONES DE CHECKLIST (Entregados) ---
def marcar_tarea_entregada(telegram_id, id_tarea):
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO tareas_entregadas (telegram_id, id_tarea)
            VALUES (%s, %s)
            ON CONFLICT DO NOTHING
        ''', (telegram_id, id_tarea))
        conn.commit()
        cursor.close()
        conn.close()
        return True
    return False

def obtener_tareas_entregadas(telegram_id):
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id_tarea FROM tareas_entregadas WHERE telegram_id = %s", (telegram_id,))
        resultados = cursor.fetchall()
        cursor.close()
        conn.close()
        return [row[0] for row in resultados]
    return []

# --- FUNCIONES DE CACHÉ ---
def guardar_cache(telegram_id, datos_json):
    json_str = json.dumps(datos_json)
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO cache_calendario (telegram_id, datos_json, ultima_actualizacion)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (telegram_id) DO UPDATE SET 
            datos_json = EXCLUDED.datos_json,
            ultima_actualizacion = CURRENT_TIMESTAMP
        ''', (telegram_id, json_str))
        conn.commit()
        cursor.close()
        conn.close()

def obtener_cache(telegram_id):
    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        cursor.execute("SELECT datos_json, ultima_actualizacion FROM cache_calendario WHERE telegram_id = %s", (telegram_id,))
        resultado = cursor.fetchone()
        cursor.close()
        conn.close()
        if resultado:
            datos = json.loads(resultado[0])
            fecha = resultado[1]
            return datos, fecha
    return None, None

# --- MODO ADMIN ---
def obtener_estadisticas():
    conn = get_db_connection()
    stats = {"usuarios": 0, "materias": 0, "entregas": 0}
    if conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM usuarios")
        stats["usuarios"] = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM materias_cugdl")
        stats["materias"] = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM tareas_entregadas")
        stats["entregas"] = cursor.fetchone()[0]
        cursor.close()
        conn.close()
    return stats
