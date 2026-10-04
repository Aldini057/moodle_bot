"""Carga el catálogo de materias (materias_raw.txt) en la tabla materias_cugdl.

Se ejecuta a mano, desde la raíz del proyecto:
    docker compose exec bot python bot/seed.py

Es idempotente: las claves que ya existen se ignoran.
"""
import re
from database import get_db_connection, init_db

def poblar_base_de_datos():
    """Lee las líneas "Nombre (123456)" del archivo y las inserta en la tabla."""
    print("Inicializando tablas...")
    init_db()
    
    print("Leyendo el archivo materias_raw.txt...")
    try:
        with open('bot/materias_raw.txt', 'r', encoding='utf-8') as file:
            contenido = file.read()
    except FileNotFoundError:
        print("❌ Error: No se encontró el archivo bot/materias_raw.txt")
        return

    # Usamos RegEx para buscar: "Cualquier Texto (6 números)"
    # Ignorará automáticamente la basura como "Next page" o "Page 1"
    patron = re.compile(r'(.+?)\s*\((\d{6})\)')
    materias = patron.findall(contenido)

    # Limpiar los nombres de espacios extra al final
    materias = [(nombre.strip(), id_num) for nombre, id_num in materias]

    conn = get_db_connection()
    if conn:
        cursor = conn.cursor()
        insertadas = 0
        
        for nombre, id_num in materias:
            # Insertamos. Si ya existe ese ID, simplemente lo ignoramos.
            cursor.execute('''
                INSERT INTO materias_cugdl (id_moodle, nombre_oficial)
                VALUES (%s, %s)
                ON CONFLICT (id_moodle) DO NOTHING
            ''', (id_num, nombre))
            insertadas += cursor.rowcount
            
        conn.commit()
        cursor.close()
        conn.close()
        
        print(f"✅ Éxito: Se inyectaron {insertadas} materias nuevas a la base de datos.")
    else:
        print("❌ Error de conexión a la base de datos.")

if __name__ == "__main__":
    poblar_base_de_datos()
