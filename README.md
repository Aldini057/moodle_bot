# Moodle Bot (CUGDL)

Bot de Telegram que lee el calendario de Moodle del CUGDL (UdeG) y te
muestra las tareas por entregar. Además te avisa automáticamente cuando
algo vence en menos de 24 horas.

## Comandos

| Comando | Qué hace |
|---|---|
| `/start <enlace>` | Registra tu enlace de calendario de Moodle (se guarda cifrado) |
| `/pendientes` | Lista las tareas de los próximos 14 días, con un botón para marcarlas como entregadas |
| `/hoy` | Lo que cierra o vence en las próximas 24 horas |
| `/estado` | Comprueba si tu enlace de Moodle responde |
| `/olvidar` | Borra todos tus datos |
| `/stats` | Estadísticas globales (solo para `ADMIN_ID`) |

### Cómo obtener tu enlace de calendario

En Moodle: **Calendario → Exportar calendario**. Elige los eventos y el
periodo, pulsa **Obtener URL del calendario** y copia el enlace
(`https://mi.cugdl.udg.mx/calendar/export_secure.php?...`). Ese enlace lleva
un token personal: no lo compartas.

## Cómo funciona

```
Telegram ⇄ main.py (comandos) ─┬─ moodle.py    descarga y filtra el .ics
                               ├─ database.py  PostgreSQL + cifrado Fernet
                               └─ scheduler.py alertas cada hora
```

- **`bot/main.py`**: punto de entrada. Define los comandos y arranca el bot y las alertas.
- **`bot/moodle.py`**: descarga el iCal y devuelve las tareas próximas. Descarta
  las de asistencia y las que ya marcaste como entregadas.
- **`bot/database.py`**: tablas, consultas y cifrado de los enlaces.
- **`bot/scheduler.py`**: cada hora avisa de lo que vence en menos de 24 h. Cada
  tarea se avisa una sola vez, o de nuevo si cambia su fecha límite.
- **`bot/seed.py`**: carga el catálogo `materias_raw.txt`, que traduce las
  claves numéricas de Moodle (p. ej. `225126`) a nombres de materia.
- **`bot/rotar_clave.py`**: re-cifra los enlaces cuando cambias `FERNET_KEY`.

Si Moodle no responde, `/pendientes` y `/hoy` usan la última copia guardada
del calendario.

## Puesta en marcha

Requisitos: Docker y Docker Compose.

1. Crea tu `.env` a partir de la plantilla y rellénalo:
   ```bash
   cp .env.example .env
   ```
   Genera `FERNET_KEY` con:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```
   Guarda esa llave en un lugar seguro: si la pierdes, los enlaces guardados
   no se pueden recuperar y cada usuario tendrá que registrarse de nuevo.

2. Levanta los servicios:
   ```bash
   docker compose up -d --build
   ```

3. Carga el catálogo de materias (solo la primera vez, o al actualizar `materias_raw.txt`):
   ```bash
   docker compose exec bot python bot/seed.py
   ```

Logs: `docker compose logs -f bot`.

## Variables de entorno

| Variable | Obligatoria | Descripción |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Sí | Token de @BotFather |
| `DB_USER`, `DB_PASSWORD`, `DB_NAME` | Sí | Credenciales de PostgreSQL (las usan el bot y el contenedor `db`) |
| `FERNET_KEY` | Sí | Llave para cifrar los enlaces de Moodle. Sin ella el bot no arranca |
| `FERNET_KEY_ANTERIOR` | No | Solo durante una rotación de llave (ver abajo) |
| `ADMIN_ID` | No | Tu ID de Telegram, para `/stats`. Si se deja vacío, `/stats` queda abierto a todos |

## Rotar la llave de cifrado

1. En `.env`, mueve la llave actual a `FERNET_KEY_ANTERIOR` y pon una nueva en `FERNET_KEY`.
2. Reinicia y re-cifra:
   ```bash
   docker compose up -d
   docker compose exec bot python bot/rotar_clave.py
   ```
3. Borra `FERNET_KEY_ANTERIOR` de `.env` y reinicia el bot.

## Datos que se guardan

Por usuario: su ID de Telegram, el enlace del calendario (cifrado), las
tareas que marcó como entregadas, las alertas ya enviadas y una copia del
último calendario leído. `/olvidar` lo borra todo.
