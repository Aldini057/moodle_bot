"""Lectura y filtrado del calendario iCal exportado por Moodle."""
import requests
from icalendar import Calendar
from datetime import datetime, timezone, timedelta
import logging
from database import buscar_nombre_materia, obtener_tareas_entregadas, guardar_cache

def obtener_tareas_pendientes(moodle_url, telegram_id):
    """Descarga el calendario del usuario y devuelve sus tareas próximas.

    Incluye los eventos que terminan en los próximos 14 días, excepto los de
    asistencia y los que el usuario ya marcó como entregados. Cada tarea es un
    dict con: uid, titulo, materia, descripcion, fecha (ISO 8601 con zona
    horaria) y tipo_evento ("abre", "cierra" o "vence"). La lista va ordenada
    por fecha y se guarda también como caché.

    Devuelve None si Moodle no respondió o el calendario no se pudo procesar
    (para distinguirlo de una lista vacía: "no hay tareas").
    """
    try:
        response = requests.get(moodle_url, timeout=10)
        if response.status_code != 200:
            return None
        
        cal = Calendar.from_ical(response.content)
        tareas = []
        ahora = datetime.now(timezone.utc)
        
        # 1. Obtenemos tu lista de tareas ya entregadas desde PostgreSQL
        tareas_completadas = obtener_tareas_entregadas(telegram_id)
        
        for component in cal.walk('vevent'):
            # 2. Extraemos el ID único de la tarea
            uid = str(component.get('uid'))
            
            # 3. Si la tarea ya la marcaste como entregada, la ignoramos por completo
            if uid in tareas_completadas:
                continue
                
            fecha_fin = component.get('dtend').dt
            limite_tiempo = ahora + timedelta(days=14)
            
            if ahora < fecha_fin <= limite_tiempo:
                titulo = str(component.get('summary')).replace(" está en fecha de entrega", "")
                
                if "asistencia" in titulo.lower():
                    continue
                    
                # Lógica de etiquetas (abre/cierra/vence)
                if " abre" in titulo:
                    tipo_evento = "abre"
                    titulo = titulo.replace(" abre", "")
                elif " cierra" in titulo:
                    tipo_evento = "cierra"
                    titulo = titulo.replace(" cierra", "")
                else:
                    tipo_evento = "vence"
                
                categoria = component.get('categories')
                if categoria:
                    clave_original = categoria.to_ical().decode('utf-8')
                    materia = buscar_nombre_materia(clave_original)
                else:
                    materia = 'Sin Materia'
                    
                # 4. Extraemos las instrucciones para el futuro comando /detalles
                # Quitamos saltos de línea excesivos y etiquetas HTML residuales
                descripcion_bruta = str(component.get('description', 'Sin instrucciones adicionales.'))
                descripcion_limpia = descripcion_bruta.strip().replace('\\n', '\n')
                    
                tareas.append({
                    'uid': uid,
                    'titulo': titulo,
                    'materia': materia,
                    'descripcion': descripcion_limpia,
                    # Convertimos la fecha a texto ISO para poder guardarla en JSON
                    'fecha': fecha_fin.isoformat(), 
                    'tipo_evento': tipo_evento
                })
        
        # Ordenar cronológicamente
        tareas.sort(key=lambda x: x['fecha'])
        
        # 5. Guardamos esta lectura exitosa en la caché de la base de datos
        guardar_cache(telegram_id, tareas)
        
        return tareas

    except Exception as e:
        logging.error(f"Error procesando el enlace: {e}")
        return None
