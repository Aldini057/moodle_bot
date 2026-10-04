import asyncio
import logging
import os
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
from scheduler import iniciar_scheduler  # <-- Importamos nuestro nuevo motor automático

# Configuración de logs
logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
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
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        usuario_existente = obtener_usuario(message.from_user.id)
        if usuario_existente:
            await message.answer(
                "¡Hola de nuevo! Ya tienes un enlace de Moodle registrado.\n\n"
                "Usa /pendientes para ver tus tareas o /estado para verificar tu conexión."
            )
        else:
            await message.answer(
                "👋 ¡Bienvenido a tu Bot Académico de Moodle!\n\n"
                "Para empezar, envíame tu enlace de calendario de Moodle con este formato:\n"
                "`/start https://mi.cugdl.udg.mx/calendar/export_secure.php?...`",
                parse_mode="Markdown"
            )
        return

    moodle_url = args[1]
    telegram_id = message.from_user.id

    if registrar_usuario(telegram_id, moodle_url):
        await message.answer("✅ ¡Enlace de Moodle registrado y cifrado con éxito!\n\nUsa /pendientes para consultar tus entregas.")
    else:
        await message.answer("❌ Hubo un error al guardar tu enlace en la base de datos.")

# --- COMANDO /PENDIENTES ---
@dp.message(F.text == "/pendientes")
async def comando_pendientes(message: types.Message):
    telegram_id = message.from_user.id
    enlace = obtener_usuario(telegram_id)

    if not enlace:
        await message.answer("⚠️ Aún no has registrado tu enlace. Usa `/start [tu_enlace]` para configurarlo.", parse_mode="Markdown")
        return

    await message.answer("🔄 Sincronizando con Moodle y base de datos...")

    tareas, fecha_cache = obtener_tareas_con_cache(enlace, telegram_id)

    if tareas is None:
        await message.answer("❌ Hubo un error al intentar leer tu calendario y no hay caché disponible.")
        return
    if fecha_cache:
        fecha_str = fecha_cache.strftime("%d/%m/%Y a las %H:%M")
        await message.answer(f"⚠️ **Moodle no responde.** Mostrando tu última copia de seguridad guardada el {fecha_str}:", parse_mode="Markdown")

    if len(tareas) == 0:
        await message.answer("🎉 ¡No tienes tareas pendientes en los próximos 14 días! Todo al día.")
        return

    for tarea in tareas:
        fecha_local = datetime.fromisoformat(tarea['fecha']).astimezone(zona_mexico)
        fecha_formato = fecha_local.strftime("%d/%m/%Y %H:%M")
        
        texto = f"🔹 *{tarea['materia']}*\n"
        texto += f"📝 {tarea['titulo']}\n"
        
        if tarea['tipo_evento'] == "abre":
            texto += f"🟢 Disponible desde: {fecha_formato}\n"
        elif tarea['tipo_evento'] == "cierra":
            texto += f"🔴 Cierra: {fecha_formato}\n"
        else:
            texto += f"⏳ Vence: {fecha_formato}\n"

        teclado = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Marcar como Entregado", callback_data=f"done_{tarea['uid']}")]
        ])

        await message.answer(texto, parse_mode="Markdown", reply_markup=teclado)

# --- COMANDO /HOY ---
@dp.message(F.text == "/hoy")
async def comando_hoy(message: types.Message):
    telegram_id = message.from_user.id
    enlace = obtener_usuario(telegram_id)

    if not enlace:
        await message.answer("⚠️ Registra tu enlace primero con /start.")
        return

    tareas, fecha_cache = obtener_tareas_con_cache(enlace, telegram_id)
    if tareas is None:
        await message.answer("❌ Hubo un error al intentar leer tu calendario y no hay caché disponible.")
        return
    if fecha_cache:
        fecha_str = fecha_cache.strftime("%d/%m/%Y a las %H:%M")
        await message.answer(f"⚠️ **Moodle no responde.** Usando tu última copia de seguridad guardada el {fecha_str}.", parse_mode="Markdown")
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

    mensaje = "🚨 **URGENTE: Vence en las próximas 24 horas:**\n\n"
    for tarea, fecha_local in urgentes:
        mensaje += f"🔹 *{tarea['materia']}*\n"
        mensaje += f"📝 {tarea['titulo']}\n"
        mensaje += f"⏳ Límite: {fecha_local.strftime('%H:%M')} hrs\n\n"

    await message.answer(mensaje, parse_mode="Markdown")

# --- BOTÓN DE CHECKLIST ---
@dp.callback_query(F.data.startswith("done_"))
async def callback_marcar_entregado(callback: types.CallbackQuery):
    uid_tarea = callback.data.split("_")[1]
    telegram_id = callback.from_user.id

    marcar_tarea_entregada(telegram_id, uid_tarea)
    
    await callback.message.edit_text(
        callback.message.text + "\n\n✅ *¡Completado y archivado!*", 
        parse_mode="Markdown", 
        reply_markup=None
    )
    await callback.answer("¡Tarea guardada como entregada!")

# --- COMANDO /ESTADO ---
@dp.message(F.text == "/estado")
async def comando_estado(message: types.Message):
    telegram_id = message.from_user.id
    enlace = obtener_usuario(telegram_id)

    if not enlace:
        await message.answer("⚠️ No tienes ningún enlace registrado.")
        return

    import requests
    try:
        response = requests.get(enlace, timeout=5)
        if response.status_code == 200:
            await message.answer("🟢 **Estado:** Tu enlace de Moodle responde correctamente. Sincronización activa.")
        else:
            await message.answer(f"🔴 **Estado:** Moodle respondió con código de error `{response.status_code}`.", parse_mode="Markdown")
    except Exception:
        await message.answer("❌ **Estado:** No se pudo contactar con el servidor de Moodle.")

# --- COMANDO /OLVIDAR ---
@dp.message(F.text == "/olvidar")
async def comando_olvidar(message: types.Message):
    telegram_id = message.from_user.id
    if eliminar_usuario(telegram_id):
        await message.answer("🗑️ **Datos borrados.** Tu ID, enlace cifrado y registros fueron eliminados.")
    else:
        await message.answer("⚠️ No tenías datos registrados previamente.")

# --- COMANDO /STATS ---
@dp.message(F.text == "/stats")
async def comando_stats(message: types.Message):
    if message.from_user.id != ADMIN_ID and ADMIN_ID != 0:
        await message.answer("⛔ Comando no autorizado.")
        return

    stats = obtener_estadisticas()
    await message.answer(
        f"📊 **Estadísticas del Servidor:**\n\n"
        f"👤 Usuarios registrados: `{stats['usuarios']}`\n"
        f"📚 Materias en catálogo: `{stats['materias']}`\n"
        f"✅ Tareas completadas globalmente: `{stats['entregas']}`",
        parse_mode="Markdown"
    )

# --- ARRANQUE Y SCHEDULER ---
async def main():
    print("Inicializando base de datos...")
    init_db()
    
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
