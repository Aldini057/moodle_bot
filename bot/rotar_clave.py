"""Re-cifra todos los enlaces guardados con la llave actual (FERNET_KEY).

Uso, durante una rotación de llave:
  1. Pon la llave nueva en FERNET_KEY y la vieja en FERNET_KEY_ANTERIOR.
  2. Ejecuta: python bot/rotar_clave.py
  3. Borra FERNET_KEY_ANTERIOR del .env y reinicia el bot.
"""
from cryptography.fernet import InvalidToken
from database import get_db_connection, cipher

def rotar_enlaces():
    """Re-cifra cada enlace de la tabla usuarios con la llave FERNET_KEY actual."""
    conn = get_db_connection()
    if not conn:
        print("❌ Error de conexión a la base de datos.")
        return

    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, moodle_url FROM usuarios")
    usuarios = cursor.fetchall()

    rotados = 0
    fallidos = 0
    for telegram_id, url_guardada in usuarios:
        try:
            nuevo = cipher.rotate(url_guardada.encode('utf-8'))
        except InvalidToken:
            if url_guardada.startswith(("http://", "https://")):
                # Enlace en texto plano: simplemente lo ciframos
                nuevo = cipher.encrypt(url_guardada.encode('utf-8'))
            else:
                print(f"⚠️ No se pudo descifrar el enlace del usuario {telegram_id}; se deja igual.")
                fallidos += 1
                continue
        cursor.execute(
            "UPDATE usuarios SET moodle_url = %s WHERE telegram_id = %s",
            (nuevo.decode('utf-8'), telegram_id)
        )
        rotados += 1

    conn.commit()
    cursor.close()
    conn.close()
    print(f"✅ Enlaces re-cifrados: {rotados}. Fallidos: {fallidos}.")

if __name__ == "__main__":
    rotar_enlaces()
