"""Bot de Telegram que muestra las tareas pendientes del Moodle del CUGDL.

Cada usuario registra con /start el enlace de exportación de su calendario
de Moodle. El bot lo guarda cifrado y lo consulta para listar las entregas
próximas. Este es el punto de entrada: define los comandos, inicializa la
base de datos, arranca el planificador de alertas y hace polling a Telegram.

Las funciones de database.py y moodle.py son bloqueantes; aquí siempre se
llaman con asyncio.to_thread() para no congelar el bot.
"""
import asyncio
import logging
import os
from html import escape
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from aiogram import Bot, Dispatcher, types, F
from aiogram.types import BotCommand, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import CommandStart, Command

from database import (
    init_db, registrar_usuario, obtener_usuario, eliminar_usuario, 
    obtener_estadisticas, marcar_tarea_entregada, obtener_cache
)
from moodle import obtener_tareas_pendientes
from scheduler import iniciar_scheduler

# Configuración de logs
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# ID de Telegram del administrador (para /stats). 0 = sin configurar.
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

bot = Bot(token=TOKEN)
dp = Dispatcher()

zona_mexico = ZoneInfo("America/Mexico_City")

def obtener_tareas_con_cache(enlace, telegram_id):
    """Tareas frescas de Moodle; si Moodle falla, la última copia guardada.

    Devuelve (tareas, fecha_cache): fecha_cache es None si los datos son frescos,
    y tareas es None si no hay ni Moodle ni caché.
    """
    tareas = obtener_tareas_pendientes(enlace, telegram_id)
    if tareas is not None:
        return tareas, None

    datos_cache, fecha_cache = obtener_cache(telegram_id)
    if datos_cache is None:
        return None, None
    # La columna es TIMESTAMP sin zona; Postgres la guarda en UTC
    if fecha_cache.tzinfo is None:
        fecha_cache = fecha_cache.replace(tzinfo=timezone.utc)
    return datos_cache, fecha_cache.astimezone(zona_mexico)

# --- COMANDO START ---
@dp.message(CommandStart())
async def comando_start(message: types.Message):
    """/start: da la bienvenida o, con un enlace como argumento, lo registra.

    Uso: /start https://mi.cugdl.udg.mx/calendar/export_secure.php?...
    """
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        usuario_existente = await asyncio.to_thread(obtener_usuario, message.from_user.id)
        if usuario_existente:
            await message.answer(
                "¡Hola de nuevo! Ya tienes un enlace de Moodle registrado.\n\n"
                "Usa /pendientes para ver tus tareas o /estado para verificar tu conexión."
            )
        else:
            await message.answer(
                "👋 ¡Bienvenido a tu Bot Académico de Moodle!\n\n"
                "Para empezar, envíame tu enlace de calendario de Moodle con este formato:\n"
                "<code>/start https://mi.cugdl.udg.mx/calendar/export_secure.php?...</code>",
                parse_mode="HTML"
            )
        return

    moodle_url = args[1]
    telegram_id = message.from_user.id

    if await asyncio.to_thread(registrar_usuario, telegram_id, moodle_url):
        await message.answer("✅ ¡Enlace de Moodle registrado y cifrado con éxito!\n\nUsa /pendientes para consultar tus entregas.")
    else:
        await message.answer("❌ Hubo un error al guardar tu enlace en la base de datos.")

# --- COMANDO /PENDIENTES ---
@dp.message(F.text == "/pendientes")
async def comando_pendientes(message: types.Message):
    """/pendientes: un mensaje por tarea de los próximos 14 días.

    Cada mensaje lleva un botón para marcar la tarea como entregada. Si Moodle
    no responde, muestra la última copia guardada e indica de cuándo es.
    """
    telegram_id = message.from_user.id
    enlace = await asyncio.to_thread(obtener_usuario, telegram_id)

    if not enlace:
        await message.answer("⚠️ Aún no has registrado tu enlace. Usa <code>/start [tu_enlace]</code> para configurarlo.", parse_mode="HTML")
        return

    await message.answer("🔄 Sincronizando con Moodle y base de datos...")

    tareas, fecha_cache = await asyncio.to_thread(obtener_tareas_con_cache, enlace, telegram_id)

    if tareas is None:
        await message.answer("❌ Hubo un error al intentar leer tu calendario y no hay caché disponible.")
        return
    if fecha_cache:
        fecha_str = fecha_cache.strftime("%d/%m/%Y a las %H:%M")
        await message.answer(f"⚠️ <b>Moodle no responde.</b> Mostrando tu última copia de seguridad guardada el {fecha_str}:", parse_mode="HTML")

    if len(tareas) == 0:
        await message.answer("🎉 ¡No tienes tareas pendientes en los próximos 14 días! Todo al día.")
        return

    for tarea in tareas:
        fecha_local = datetime.fromisoformat(tarea['fecha']).astimezone(zona_mexico)
        fecha_formato = fecha_local.strftime("%d/%m/%Y %H:%M")
        
        # Los títulos y materias vienen de Moodle: se escapan para que
        # caracteres como < o & no rompan el formato del mensaje.
        texto = f"🔹 <b>{escape(tarea['materia'])}</b>\n"
        texto += f"📝 {escape(tarea['titulo'])}\n"
        
        if tarea['tipo_evento'] == "abre":
            texto += f"🟢 Disponible desde: {fecha_formato}\n"
        elif tarea['tipo_evento'] == "cierra":
            texto += f"🔴 Cierra: {fecha_formato}\n"
        else:
            texto += f"⏳ Vence: {fecha_formato}\n"

        teclado = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Marcar como Entregado", callback_data=f"done_{tarea['uid']}")]
        ])

        await message.answer(texto, parse_mode="HTML", reply_markup=teclado)

# --- COMANDO /HOY ---
@dp.message(F.text == "/hoy")
async def comando_hoy(message: types.Message):
    """/hoy: resumen de lo que cierra o vence en las próximas 24 horas."""
    telegram_id = message.from_user.id
    enlace = await asyncio.to_thread(obtener_usuario, telegram_id)

    if not enlace:
        await message.answer("⚠️ Registra tu enlace primero con /start.")
        return

    tareas, fecha_cache = await asyncio.to_thread(obtener_tareas_con_cache, enlace, telegram_id)
    if tareas is None:
        await message.answer("❌ Hubo un error al intentar leer tu calendario y no hay caché disponible.")
        return
    if fecha_cache:
        fecha_str = fecha_cache.strftime("%d/%m/%Y a las %H:%M")
        await message.answer(f"⚠️ <b>Moodle no responde.</b> Usando tu última copia de seguridad guardada el {fecha_str}.", parse_mode="HTML")
    if not tareas:
        await message.answer("🎉 No hay tareas pendientes.")
        return

    ahora_utc = datetime.now(timezone.utc)
    urgentes = []

    for tarea in tareas:
        if tarea['tipo_evento'] == "abre":
            continue
        fecha_obj = datetime.fromisoformat(tarea['fecha'])
        diferencia_horas = (fecha_obj - ahora_utc).total_seconds() / 3600
        
        if 0 < diferencia_horas <= 24:
            urgentes.append((tarea, fecha_obj.astimezone(zona_mexico)))

    if not urgentes:
        await message.answer("✨ ¡Excelente! No tienes nada que venza en las próximas 24 horas.")
        return

    mensaje = "🚨 <b>URGENTE: Vence en las próximas 24 horas:</b>\n\n"
    for tarea, fecha_local in urgentes:
        mensaje += f"🔹 <b>{escape(tarea['materia'])}</b>\n"
        mensaje += f"📝 {escape(tarea['titulo'])}\n"
        mensaje += f"⏳ Límite: {fecha_local.strftime('%H:%M')} hrs\n\n"

    await message.answer(mensaje, parse_mode="HTML")

# --- BOTÓN DE CHECKLIST ---
@dp.callback_query(F.data.startswith("done_"))
async def callback_marcar_entregado(callback: types.CallbackQuery):
    """Botón "Marcar como Entregado": guarda la tarea y la oculta en adelante.

    El callback_data tiene la forma "done_<uid de la tarea>".
    """
    uid_tarea = callback.data.split("_")[1]
    telegram_id = callback.from_user.id

    await asyncio.to_thread(marcar_tarea_entregada, telegram_id, uid_tarea)
    
    await callback.message.edit_text(
        # html_text conserva el formato original (y lo escapa); .text lo perdería
        callback.message.html_text + "\n\n✅ <b>¡Completado y archivado!</b>",
        parse_mode="HTML",
        reply_markup=None
    )
    await callback.answer("¡Tarea guardada como entregada!")

# --- COMANDO /ESTADO ---
@dp.message(F.text == "/estado")
async def comando_estado(message: types.Message):
    """/estado: comprueba si el enlace de Moodle del usuario responde."""
    telegram_id = message.from_user.id
    enlace = await asyncio.to_thread(obtener_usuario, telegram_id)

    if not enlace:
        await message.answer("⚠️ No tienes ningún enlace registrado.")
        return

    import requests
    try:
        response = await asyncio.to_thread(requests.get, enlace, timeout=5)
        if response.status_code == 200:
            await message.answer("🟢 <b>Estado:</b> Tu enlace de Moodle responde correctamente. Sincronización activa.", parse_mode="HTML")
        else:
            await message.answer(f"🔴 <b>Estado:</b> Moodle respondió con código de error <code>{response.status_code}</code>.", parse_mode="HTML")
    except Exception:
        await message.answer("❌ <b>Estado:</b> No se pudo contactar con el servidor de Moodle.", parse_mode="HTML")

# --- COMANDO /OLVIDAR ---
@dp.message(F.text == "/olvidar")
async def comando_olvidar(message: types.Message):
    """/olvidar: borra todos los datos del usuario (enlace, entregas, caché y alertas)."""
    telegram_id = message.from_user.id
    if await asyncio.to_thread(eliminar_usuario, telegram_id):
        await message.answer("🗑️ <b>Datos borrados.</b> Tu ID, enlace cifrado y registros fueron eliminados.", parse_mode="HTML")
    else:
        await message.answer("⚠️ No tenías datos registrados previamente.")

# --- COMANDO /STATS ---
@dp.message(F.text == "/stats")
async def comando_stats(message: types.Message):
    """/stats: estadísticas globales, solo para ADMIN_ID.

    Si ADMIN_ID no está configurado (vale 0), cualquiera puede usarlo.
    """
    if message.from_user.id != ADMIN_ID and ADMIN_ID != 0:
        await message.answer("⛔ Comando no autorizado.")
        return

    stats = await asyncio.to_thread(obtener_estadisticas)
    await message.answer(
        f"📊 <b>Estadísticas del Servidor:</b>\n\n"
        f"👤 Usuarios registrados: <code>{stats['usuarios']}</code>\n"
        f"📚 Materias en catálogo: <code>{stats['materias']}</code>\n"
        f"✅ Tareas completadas globalmente: <code>{stats['entregas']}</code>",
        parse_mode="HTML"
    )

# --- ARRANQUE Y SCHEDULER ---
async def main():
    """Inicializa la base de datos, arranca las alertas y empieza a recibir mensajes."""
    print("Inicializando base de datos...")
    if not init_db():
        # Salimos con error para que Docker reinicie el contenedor, en vez de
        # seguir corriendo sin tablas.
        raise SystemExit("No se pudo conectar a la base de datos para inicializarla.")
    
    # ⏱️ Arrancamos el planificador automático en segundo plano
    iniciar_scheduler(bot)
    
    comandos = [
        BotCommand(command="start", description="Registrar tu enlace de Moodle"),
        BotCommand(command="pendientes", description="Dashboard de tus próximas tareas"),
        BotCommand(command="hoy", description="Urgencias que vencen en 24 horas"),
        BotCommand(command="estado", description="Verificar salud de tu conexión a Moodle"),
        BotCommand(command="olvidar", description="Borrar todos tus datos del sistema")
    ]
    await bot.set_my_commands(comandos)
    print("Menú de comandos actualizado en Telegram.")
    
    print("Iniciando el bot y planificador...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
