from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from supabase import create_client, Client
import os
from contextlib import asynccontextmanager, suppress
from starlette.middleware.sessions import SessionMiddleware
import asyncio
import random
from io import BytesIO
import qrcode
import fitz
from aiogram import Bot, Dispatcher, types
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from aiogram.types import FSInputFile, ContentType, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
import aiohttp
from urllib.parse import quote

# ===================== CONFIG =====================

SUPABASE_URL  = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY  = os.getenv("SUPABASE_KEY", "")
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
BASE_URL      = os.getenv("BASE_URL", "https://https-gob-smyt-tlaxcalaanddigital.onrender.com").rstrip("/")
ENTIDAD       = "tlaxcala"
TZ            = "America/Mexico_City"
ADMIN_USER    = os.getenv("ADMIN_USER", "Serg890105tm3")
ADMIN_PASS    = os.getenv("ADMIN_PASS", "Serg890105tm3")
STATIC_DIR    = "static"
OUTPUT_DIR    = "documentos"
BUCKET_NAME   = "permisos-tlaxcala"
PLANTILLA_PDF = "TLAXCALA2026(1).pdf"
FOLIO_PREFIJO = "ZX"
FOLIO_INICIO  = 53314
_folio_counter = {"siguiente": FOLIO_INICIO}
_folio_lock    = asyncio.Lock()
PAGE_SIZE = 100

# Paleta oficial tlaxcaladigital.gob.mx
C1 = "#422b7c"   # navbar-color (morado oscuro)
C2 = "#341f63"   # variante oscura
C3 = "#e6d194"   # borde dorado
ACCENT = "#a11a5c"   # magenta
GREEN  = "#64ad0b"   # verde
BLUE   = "#2856ad"   # azul

os.makedirs(STATIC_DIR, exist_ok=True)
os.makedirs(OUTPUT_DIR, exist_ok=True)

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
_bot_session = AiohttpSession(timeout=aiohttp.ClientTimeout(total=300))
bot     = Bot(token=BOT_TOKEN, session=_bot_session)
storage = MemoryStorage()
dp      = Dispatcher(storage=storage)

TABLAS_DISPONIBLES = {
    "folios_registrados": {
        "nombre": "Folios Registrados", "pk_col": "folio",
        "columnas": ["folio","marca","linea","anio","numero_serie","numero_motor","color","nombre",
                     "cve_vehicular","fecha_expedicion","fecha_vencimiento","entidad","estado",
                     "estado_pago","creado_por","pdf_url"],
    },
    "verificacion_tlaxcala": {
        "nombre": "Usuarios del Sistema", "pk_col": "id",
        "columnas": ["id","username","password","folios_asignac","folios_usados"],
    },
    "folio_watermark": {
        "nombre": "Watermark Folios", "pk_col": "prefijo",
        "columnas": ["prefijo","ultimo_asignado"],
    },
}

# ===================== TIMERS (SOLO RAM, SIN SUPABASE) =====================
#
# El timer vive en RAM en la variable `timers_activos`.
# Si Render reinicia, se pierden los timers activos (es aceptable).
# Los timers se cancelan solo desde el bot (botones de callback).
#
# timers_activos = {
#   "ZX12345": {
#     "task": asyncio.Task,
#     "user_id": 12345,
#     "start_time": datetime,
#     "nombre": "Juan Pérez"
#   }
# }
# ============================================================================

timers_activos       = {}   # folio -> {task, user_id, start_time, nombre}
user_folios          = {}   # user_id -> [folio1, folio2, ...]
pending_comprobantes = {}
TOTAL_MINUTOS_TIMER  = 36 * 60


async def detener_timer(folio: str, motivo: str, 
                       estado_pago: str = None, estado: str = None) -> dict:
    """
    Detiene un timer.
    - Cancela la tarea asyncio local
    - Actualiza el folio en Supabase (sin tocar timer, que ya no existe en BD)
    - Retorna resultado para sincronizar con usuario
    """
    folio = folio.strip().upper()
    
    # Cancelar tarea local si existe
    cancelado_mem = False
    user_id = None
    nombre = ""
    
    if folio in timers_activos:
        user_id = timers_activos[folio].get("user_id")
        nombre = timers_activos[folio].get("nombre", "")
        with suppress(Exception):
            timers_activos[folio]["task"].cancel()
        del timers_activos[folio]
        cancelado_mem = True
        
        # Limpiar del diccionario user_folios
        if user_id and user_id in user_folios and folio in user_folios[user_id]:
            user_folios[user_id].remove(folio)
            if not user_folios[user_id]:
                del user_folios[user_id]
    
    # Actualizar la BD si hay cambios que registrar
    parches = {}
    if estado_pago:
        parches["estado_pago"] = estado_pago
    if estado:
        parches["estado"] = estado
    
    try:
        if parches:
            await asyncio.to_thread(
                lambda: supabase.table("folios_registrados").update(parches).eq("folio", folio).execute()
            )
        print(f"[TIMER] STOP {folio} motivo={motivo} mem={cancelado_mem}")
    except Exception as e:
        print(f"[TIMER] Error actualizando BD {folio}: {e}")
    
    return {
        "ok": cancelado_mem,
        "folio": folio,
        "user_id": user_id,
        "nombre": nombre,
        "cancelado_mem": cancelado_mem,
    }


async def eliminar_folio_automatico(folio: str):
    """Borra el folio del sistema al vencer el timer."""
    try:
        # Leer datos del folio antes de borrar
        row = await asyncio.to_thread(lambda: 
            supabase.table("folios_registrados").select("*").eq("folio", folio).execute())
        fila = row.data[0] if row.data else None
        
        # Borrar de BD
        await asyncio.to_thread(
            lambda: supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        )
        
        # Borrar PDF de Storage
        try:
            await asyncio.to_thread(lambda: 
                supabase.storage.from_(BUCKET_NAME).remove([f"{folio}.pdf"]))
        except Exception as e:
            print(f"[STORAGE] Error borrando {folio}.pdf: {e}")
        
        # Borrar archivo local
        ruta_local = os.path.join(OUTPUT_DIR, f"{folio}.pdf")
        if os.path.exists(ruta_local):
            os.remove(ruta_local)
        
        # Notificar al usuario
        if fila:
            uid = fila.get("user_id")
            if uid:
                with suppress(Exception):
                    await bot.send_message(uid,
                        f"⏰ TIEMPO AGOTADO - TLAXCALA\n\nEl folio {folio} fue eliminado por no completar el pago en 36 horas.\n\n📋 Use /tlaxcala para generar otro permiso.")
        
        # Limpiar de memoria
        if folio in timers_activos:
            del timers_activos[folio]
        
        print(f"[TIMER] {folio} eliminado por vencimiento")
    except Exception as e:
        print(f"[ERROR] eliminando folio {folio}: {e}")


async def enviar_recordatorio(folio: str, minutos_restantes: int):
    """Envía un recordatorio por Telegram."""
    try:
        if folio not in timers_activos:
            return
        
        uid = timers_activos[folio].get("user_id")
        if not uid:
            return
        
        await bot.send_message(uid,
            f"⚡ RECORDATORIO - TLAXCALA\n\nFolio: {folio}\nTiempo restante: {minutos_restantes} minutos\n\n"
            f"📸 Envíe su comprobante de pago.\n\n📋 Use /tlaxcala para otro permiso.")
    except Exception as e:
        print(f"[ERROR] recordatorio {folio}: {e}")


async def iniciar_timer_36h(user_id: int, folio: str, nombre: str = ""):
    """Inicia el timer de 36 horas en RAM."""
    total = TOTAL_MINUTOS_TIMER * 60

    async def timer_task():
        restante = total
        # Recordatorios a 90, 60, 30, 10 minutos
        for falta, minutos in [(90 * 60, 90), (60 * 60, 60), (30 * 60, 30), (10 * 60, 10)]:
            espera = restante - falta
            if espera > 0:
                await asyncio.sleep(espera)
                restante = falta
                # Verificar que el timer sigue vivo
                if folio not in timers_activos:
                    print(f"[TIMER] {folio} fue detenido — task termina")
                    return
                await enviar_recordatorio(folio, minutos)
        
        # Esperar lo que queda y eliminar
        if restante > 0:
            await asyncio.sleep(restante)
        await eliminar_folio_automatico(folio)

    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task": task,
        "user_id": user_id,
        "start_time": datetime.now(),
        "nombre": nombre,
    }
    user_folios.setdefault(user_id, []).append(folio)
    print(f"[TIMER] Iniciado {folio} ({nombre}) — 36 horas")


def obtener_folios_usuario(user_id: int) -> list:
    """Devuelve lista de folios activos del usuario en RAM."""
    return user_folios.get(user_id, [])


# ===================== FOLIOS (ZX + 5 dígitos) =====================

def _sb_leer_watermark():
    try:
        r = supabase.table("folio_watermark").select("ultimo_asignado").eq("prefijo", FOLIO_PREFIJO).execute()
        return r.data[0]["ultimo_asignado"] if r.data else None
    except:
        return None

def _sb_guardar_watermark(numero):
    try:
        supabase.table("folio_watermark").upsert({"prefijo": FOLIO_PREFIJO, "ultimo_asignado": numero}).execute()
    except Exception as e:
        print(f"[ERROR] guardar_watermark: {e}")

def _sb_inicializar_folio():
    wm = _sb_leer_watermark()
    if wm is not None:
        _folio_counter["siguiente"] = wm + 1
        return
    try:
        resp = supabase.table("folios_registrados").select("folio").eq("entidad", ENTIDAD).like("folio", f"{FOLIO_PREFIJO}%").execute()
        nums = []
        for row in resp.data or []:
            f = row.get("folio", "")
            if isinstance(f, str) and f.startswith(FOLIO_PREFIJO):
                suf = f[len(FOLIO_PREFIJO):]
                if suf.isdigit():
                    nums.append(int(suf))
        if nums:
            maximo = max(nums)
            _folio_counter["siguiente"] = maximo + 1
            _sb_guardar_watermark(maximo)
        else:
            _folio_counter["siguiente"] = FOLIO_INICIO
    except Exception as e:
        print(f"[ERROR] inicializar_folio: {e}")

def _folio_existe(folio):
    try:
        r = supabase.table("folios_registrados").select("folio").eq("folio", folio).execute()
        return len(r.data) > 0
    except:
        return False

def _generar_folio_sync():
    candidato = _folio_counter["siguiente"]
    for _ in range(100_000):
        folio = f"{FOLIO_PREFIJO}{candidato}"
        if not _folio_existe(folio):
            _folio_counter["siguiente"] = candidato + 1
            _sb_guardar_watermark(candidato)
            print(f"[FOLIO] Asignado: {folio}")
            return folio
        candidato += 1
    return f"{FOLIO_PREFIJO}{random.randint(90000,99999)}"

async def _generar_folio_async():
    async with _folio_lock:
        return await asyncio.to_thread(_generar_folio_sync)

def generar_folio():
    return _generar_folio_sync()

# ===================== ANTI DOBLE-CLICK =====================

_locks_usuario   = {}
_registros_recientes = {}
DEDUPE_SEGUNDOS  = 180

def _lock_de_usuario(username: str) -> asyncio.Lock:
    if username not in _locks_usuario:
        _locks_usuario[username] = asyncio.Lock()
    return _locks_usuario[username]

def _huella(username: str, serie: str, motor: str, nombre: str) -> str:
    return "|".join([
        (username or "").strip().upper(),
        (serie or "").strip().upper(),
        (motor or "").strip().upper(),
        (nombre or "").strip().upper(),
    ])

def _buscar_reciente(huella: str):
    reg = _registros_recientes.get(huella)
    if not reg:
        return None
    if (datetime.now() - reg["ts"]).total_seconds() > DEDUPE_SEGUNDOS:
        _registros_recientes.pop(huella, None)
        return None
    return reg

def _guardar_reciente(huella: str, folio: str, pdf_url: str):
    _registros_recientes[huella] = {"folio": folio, "pdf_url": pdf_url, "ts": datetime.now()}
    if len(_registros_recientes) > 500:
        ahora = datetime.now()
        for k in [k for k, v in _registros_recientes.items()
                  if (ahora - v["ts"]).total_seconds() > DEDUPE_SEGUNDOS]:
            _registros_recientes.pop(k, None)

# ===================== STORAGE =====================

def subir_pdf_a_storage(ruta_local: str, folio: str) -> str:
    try:
        if not os.path.exists(ruta_local):
            return ""
        with open(ruta_local, "rb") as f:
            contenido = f.read()
        nombre = f"{folio}.pdf"
        supabase.storage.from_(BUCKET_NAME).upload(path=nombre, file=contenido,
            file_options={"content-type": "application/pdf", "upsert": "true"})
        url = supabase.storage.from_(BUCKET_NAME).get_public_url(nombre)
        print(f"[STORAGE] ✅ Subido: {url}")
        return url
    except Exception as e:
        print(f"[STORAGE] ❌ Error {folio}: {e}")
        return ""

# ===================== QR (2 por permiso) =====================

def _generar_qr_url(folio: str):
    try:
        url = f"{BASE_URL}/consulta/{folio}"
        qr  = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=4, border=1)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        return img
    except Exception as e:
        print(f"[QR] Error url: {e}")
        return None

def _generar_qr_datos(datos: dict):
    try:
        texto = (
            f"FOLIO: {datos.get('folio','')}\n"
            f"FECHA EXPEDICION: {datos.get('fecha_exp','')}\n"
            f"FECHA VENCIMIENTO: {datos.get('fecha_ven','')}\n"
            f"NOMBRE: {datos.get('nombre','')}\n"
            f"MARCA: {datos.get('marca','')}\n"
            f"LINEA: {datos.get('linea','')}\n"
            f"AÑO: {datos.get('anio','')}\n"
            f"SERIE: {datos.get('serie','')}\n"
            f"MOTOR: {datos.get('motor','')}\n"
            f"COLOR: {datos.get('color','')}"
        )
        qr  = qrcode.QRCode(version=4, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=3, border=1)
        qr.add_data(texto)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        return img
    except Exception as e:
        print(f"[QR] Error datos: {e}")
        return None

# ===================== PDF =====================

def generar_pdf(datos: dict) -> str:
    folio = datos["folio"]
    out   = os.path.join(OUTPUT_DIR, f"{folio}.pdf")

    nombre = str(datos.get("nombre", "")).upper()
    marca  = str(datos.get("marca", "")).upper()
    linea  = str(datos.get("linea", "")).upper()
    modelo = str(datos.get("anio", ""))
    serie  = str(datos.get("serie", "")).upper()
    motor  = str(datos.get("motor", "")).upper()
    color  = str(datos.get("color", "")).upper()
    cve    = str(datos.get("cve_vehicular", "")).upper()

    F  = "helv"
    FB = "hebo"
    S  = 9

    try:
        if os.path.exists(PLANTILLA_PDF):
            doc = fitz.open(PLANTILLA_PDF)
            pg  = doc[0]

            pg.insert_text((405, 280), folio, fontsize=80, fontname=FB, color=(0, 0, 0))
            pg.insert_text((52, 205), datos["fecha_exp"], fontsize=S, fontname=F, color=(0, 0, 0))
            pg.insert_text((52, 239), datos["fecha_ven"], fontsize=S, fontname=F, color=(0, 0, 0))
            pg.insert_text((92, 298), nombre, fontsize=S, fontname=F, color=(0, 0, 0))
            pg.insert_text((53, 369), serie, fontsize=8, fontname=F, color=(0, 0, 0))
            pg.insert_text((53, 403), serie, fontsize=S, fontname=F, color=(0, 0, 0))
            pg.insert_text((185, 403), modelo, fontsize=S, fontname=F, color=(0, 0, 0))
            pg.insert_text((238, 403), color, fontsize=S, fontname=F, color=(0, 0, 0))
            pg.insert_text((53, 437), motor, fontsize=S, fontname=F, color=(0, 0, 0))
            pg.insert_text((168, 437), marca, fontsize=8, fontname=F, color=(0, 0, 0))
            pg.insert_text((168, 449), linea, fontsize=8, fontname=F, color=(0, 0, 0))
            pg.insert_text((264, 437), cve, fontsize=7, fontname=F, color=(0, 0, 0))

            img_url = _generar_qr_url(folio)
            if img_url:
                buf = BytesIO()
                img_url.save(buf, format="PNG")
                buf.seek(0)
                pg.insert_image(fitz.Rect(76, 478, 150, 552), pixmap=fitz.Pixmap(buf.read()), overlay=True)

            img_datos = _generar_qr_datos(datos)
            if img_datos:
                buf2 = BytesIO()
                img_datos.save(buf2, format="PNG")
                buf2.seek(0)
                pg.insert_image(fitz.Rect(650, 451, 717, 518), pixmap=fitz.Pixmap(buf2.read()), overlay=True)

            doc.save(out)
            doc.close()
            print(f"[PDF] ✅ {out}")
        else:
            print(f"[PDF] ⚠️ Plantilla no encontrada: {PLANTILLA_PDF}")
            doc = fitz.open()
            pg = doc.new_page(width=792, height=612)
            pg.insert_text((50, 50), f"PLANTILLA NO ENCONTRADA — Folio: {folio}", fontsize=10)
            doc.save(out)
            doc.close()
    except Exception as e:
        print(f"[PDF] ❌ Error: {e}")
        doc_fb = fitz.open()
        doc_fb.new_page().insert_text((50, 50), f"ERROR - Folio: {folio}", fontsize=12)
        doc_fb.save(out)
        doc_fb.close()

    return out

def generar_subir_y_guardar_pdf(datos_pdf: dict) -> str:
    folio    = datos_pdf["folio"]
    ruta_pdf = generar_pdf(datos_pdf)
    url_pdf  = subir_pdf_a_storage(ruta_pdf, folio)
    if url_pdf:
        try:
            supabase.table("folios_registrados").update({"pdf_url": url_pdf}).eq("folio", folio).execute()
        except Exception as e:
            print(f"[DB] ❌ Error pdf_url: {e}")
    return url_pdf

# ===================== BACKGROUND BOT =====================

async def generar_y_enviar_background(chat_id: int, datos: dict, user_id: int):
    folio = datos["folio"]
    nombre = datos["nombre"]
    try:
        pdf_path = await asyncio.to_thread(generar_pdf, datos)
        pdf_url  = await asyncio.to_thread(subir_pdf_a_storage, pdf_path, folio)
        
        await asyncio.to_thread(lambda: supabase.table("folios_registrados").insert({
            "folio": folio, "marca": datos["marca"], "linea": datos["linea"],
            "anio": datos["anio"], "numero_serie": datos["serie"],
            "numero_motor": datos.get("motor", ""),
            "color": datos.get("color", ""), "nombre": nombre,
            "cve_vehicular": datos.get("cve_vehicular", ""),
            "fecha_expedicion":  datos["fecha_exp_dt"].date().isoformat(),
            "fecha_vencimiento": (datos["fecha_exp_dt"] + timedelta(days=30)).date().isoformat(),
            "entidad": ENTIDAD, "estado": "ACTIVO", "estado_pago": "PENDIENTE_PAGO",
            "user_id": user_id,
            "creado_por": f"BOT_TG_{datos.get('username','unknown')}",
            "pdf_url": pdf_url,
        }).execute())
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Validar Admin",  callback_data=f"validar_{folio}"),
            InlineKeyboardButton(text="⏹️ Detener Timer", callback_data=f"detener_{folio}")
        ]])
        
        await bot.send_document(chat_id, FSInputFile(pdf_path),
            caption=(
                f"📄 PERMISO PROVISIONAL — TLAXCALA\n"
                f"Folio: {folio}\nTitular: {nombre}\n"
                f"Expedición: {datos['fecha_exp']}\nVencimiento: {datos['fecha_ven']}\n\n"
                f"🔗 {BASE_URL}/consulta/{folio}\n\n"
                f"⏰ TIMER ACTIVO (36 horas)"
            ), reply_markup=keyboard)
        
        await iniciar_timer_36h(user_id, folio, nombre)
        
        await bot.send_message(user_id,
            f"💰 INSTRUCCIONES DE PAGO — TLAXCALA\n\n"
            f"📄 Folio: {folio}\n⏰ Tiempo límite: 36 horas\n\n"
            f"📸 Envía la foto de tu comprobante aquí mismo.\n"
            f"⚠️ Sin pago en 36h el folio se elimina.\n\n"
            f"📋 Use /tlaxcala para generar otro permiso.")
    except Exception as e:
        print(f"[ERROR] background folio {folio}: {e}")
        try:
            await bot.send_message(user_id, f"❌ Error al generar el documento: {e}\n\nUse /tlaxcala para reintentar.")
        except Exception:
            pass

# ===================== BOT FSM =====================

class PermisoForm(StatesGroup):
    marca         = State()
    linea         = State()
    anio          = State()
    serie         = State()
    motor         = State()
    color         = State()
    nombre        = State()
    cve_vehicular = State()

@dp.message(Command("start"))
async def start_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer("🏛️ GOBIERNO DEL ESTADO DE TLAXCALA\nSecretaría de Movilidad y Transporte (SMyT)\n\n📋 Use /tlaxcala para generar un permiso provisional de circulación.")

@dp.message(Command("tlaxcala"))
async def tlaxcala_cmd(message: types.Message, state: FSMContext):
    await state.clear()
    activos = obtener_folios_usuario(message.from_user.id)
    if activos:
        texto = "📋 FOLIOS ACTIVOS\n" + "─" * 28 + "\n\n"
        botones = []
        for f in activos:
            try:
                row = supabase.table("folios_registrados").select("nombre").eq("folio", f).limit(1).execute()
                nombre = row.data[0].get("nombre", "") if row.data else ""
            except:
                nombre = ""
            texto += f"Folio: {f}\n{nombre or ''}\n\n"
            botones.append([InlineKeyboardButton(text=f"⏹️ Detener {f}", callback_data=f"detener_{f}")])
        await message.answer(texto.strip(), reply_markup=InlineKeyboardMarkup(inline_keyboard=botones))
        await message.answer("Para NUEVO permiso escribe la MARCA del vehículo:")
    else:
        await message.answer("🚗 NUEVO PERMISO PROVISIONAL — TLAXCALA\n\n⏰ Plazo de pago: 36 horas\n\nPaso 1/8: MARCA del vehículo:")
    await state.set_state(PermisoForm.marca)

@dp.message(PermisoForm.marca)
async def get_marca(message: types.Message, state: FSMContext):
    await state.update_data(marca=message.text.strip().upper())
    await message.answer("Paso 2/8: LÍNEA del vehículo (ej. POLO, SENTRA, JETTA):")
    await state.set_state(PermisoForm.linea)

@dp.message(PermisoForm.linea)
async def get_linea(message: types.Message, state: FSMContext):
    await state.update_data(linea=message.text.strip().upper())
    await message.answer("Paso 3/8: AÑO / MODELO del vehículo (4 dígitos):")
    await state.set_state(PermisoForm.anio)

@dp.message(PermisoForm.anio)
async def get_anio(message: types.Message, state: FSMContext):
    anio = message.text.strip()
    if not anio.isdigit() or len(anio) != 4:
        await message.answer("⚠️ Año inválido. Usa 4 dígitos (ej. 2021):")
        return
    await state.update_data(anio=anio)
    await message.answer("Paso 4/8: NÚMERO DE SERIE / NIV (se usa para ambos campos):")
    await state.set_state(PermisoForm.serie)

@dp.message(PermisoForm.serie)
async def get_serie(message: types.Message, state: FSMContext):
    await state.update_data(serie=message.text.strip().upper())
    await message.answer("Paso 5/8: NÚMERO DE MOTOR:")
    await state.set_state(PermisoForm.motor)

@dp.message(PermisoForm.motor)
async def get_motor(message: types.Message, state: FSMContext):
    await state.update_data(motor=message.text.strip().upper())
    await message.answer("Paso 6/8: COLOR del vehículo:")
    await state.set_state(PermisoForm.color)

@dp.message(PermisoForm.color)
async def get_color(message: types.Message, state: FSMContext):
    await state.update_data(color=message.text.strip().upper())
    await message.answer("Paso 7/8: NOMBRE COMPLETO del propietario:")
    await state.set_state(PermisoForm.nombre)

@dp.message(PermisoForm.nombre)
async def get_nombre_step(message: types.Message, state: FSMContext):
    await state.update_data(nombre=message.text.strip().upper())
    await message.answer("Paso 8/8: CLAVE VEHICULAR:")
    await state.set_state(PermisoForm.cve_vehicular)

@dp.message(PermisoForm.cve_vehicular)
async def get_cve_vehicular(message: types.Message, state: FSMContext):
    datos = await state.get_data()
    datos["cve_vehicular"] = message.text.strip().upper()
    datos["username"]      = message.from_user.username or "Sin username"
    datos["folio"]     = await _generar_folio_async()
    tz = ZoneInfo(TZ)
    hoy = datetime.now(tz)
    ven = hoy + timedelta(days=30)
    datos["fecha_exp"]    = hoy.strftime("%d/%m/%Y")
    datos["fecha_ven"]    = ven.strftime("%d/%m/%Y")
    datos["fecha_exp_dt"] = hoy
    await state.clear()
    await message.answer(f"🔄 Generando permiso...\n📄 Folio: {datos['folio']}\n👤 Titular: {datos['nombre']}")
    asyncio.create_task(generar_y_enviar_background(message.chat.id, datos, message.from_user.id))

@dp.message(lambda m: m.text and m.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper()
    folio = texto.replace("SERO", "", 1).strip()
    if not folio or not folio.startswith(FOLIO_PREFIJO):
        await message.answer(f"⚠️ Formato: SERO{FOLIO_PREFIJO}XXXXX\n\n📋 Use /tlaxcala para otro permiso.")
        return
    r = await detener_timer(folio, "admin_telegram_SERO", estado_pago="VALIDADO")
    if not r["ok"]:
        await message.answer(f"⚠️ Folio {folio} no encontrado en memoria.\n\n📋 Use /tlaxcala para otro permiso.")
        return
    await message.answer(
        f"✅ Validación admin\nFolio: {folio}\n⏹️ Timer detenido"
        f"\n\n📋 Use /tlaxcala para otro permiso.")

@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    uid = message.from_user.id
    folios = obtener_folios_usuario(uid)
    if not folios:
        await message.answer("ℹ️ No tienes folios pendientes.\n\n📋 Use /tlaxcala para generar un permiso.")
        return
    if len(folios) > 1:
        lista = "\n".join(f"• {f}" for f in folios)
        pending_comprobantes[uid] = "waiting_folio"
        await message.answer(f"📄 Varios folios activos:\n\n{lista}\n\nResponde con el NÚMERO DE FOLIO.\n\n📋 Use /tlaxcala para otro permiso.")
        return
    folio = folios[0]
    await detener_timer(folio, "comprobante_telegram", estado="COMPROBANTE_ENVIADO")
    with suppress(Exception):
        await asyncio.to_thread(lambda: supabase.table("folios_registrados").update({
            "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", folio).execute())
    await message.answer(f"✅ Comprobante recibido\nFolio: {folio}\n⏹️ Timer detenido.\n\n📋 Use /tlaxcala para otro permiso.")

@dp.message(lambda m: m.from_user.id in pending_comprobantes and pending_comprobantes[m.from_user.id] == "waiting_folio")
async def especificar_folio_comprobante(message: types.Message):
    uid = message.from_user.id
    fe = message.text.strip().upper()
    fl = obtener_folios_usuario(uid)
    if fe not in fl:
        await message.answer("❌ Folio no en tu lista.\n\n📋 Use /tlaxcala para otro permiso.")
        return
    await detener_timer(fe, "comprobante_telegram", estado="COMPROBANTE_ENVIADO")
    with suppress(Exception):
        await asyncio.to_thread(lambda: supabase.table("folios_registrados").update({
            "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", fe).execute())
    pending_comprobantes.pop(uid, None)
    await message.answer(f"✅ Comprobante asociado.\nFolio: {fe}\n⏹️ Timer detenido.\n\n📋 Use /tlaxcala para otro permiso.")

@dp.callback_query(lambda c: c.data and c.data.startswith("validar_"))
async def callback_validar(callback: CallbackQuery):
    folio = callback.data.replace("validar_", "")
    r = await detener_timer(folio, "callback_validar_telegram", estado_pago="VALIDADO")
    if not r["ok"]:
        await callback.answer("❌ Folio no encontrado", show_alert=True)
        return
    with suppress(Exception):
        await asyncio.to_thread(lambda: supabase.table("folios_registrados").update({
            "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", r["folio"]).execute())
    await callback.answer("✅ Folio validado", show_alert=True)
    with suppress(Exception):
        await callback.message.edit_reply_markup(reply_markup=None)
    if r.get("user_id"):
        with suppress(Exception):
            await bot.send_message(r["user_id"],
                f"✅ PAGO VALIDADO — TLAXCALA\nFolio: {r['folio']}\nTitular: {r.get('nombre','')}\nTu permiso está activo.\n\n📋 Use /tlaxcala para otro permiso.")

@dp.callback_query(lambda c: c.data and c.data.startswith("detener_"))
async def callback_detener(callback: CallbackQuery):
    folio = callback.data.replace("detener_", "")
    r = await detener_timer(folio, "callback_detener_telegram", estado="TIMER_DETENIDO")
    if not r["ok"]:
        await callback.answer("❌ Folio no encontrado", show_alert=True)
        return
    await callback.answer("⏹️ Timer detenido", show_alert=True)
    with suppress(Exception):
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"⏹️ TIMER DETENIDO\nFolio: {r['folio']}\nTitular: {r.get('nombre','')}\n\n📋 Use /tlaxcala para otro permiso.")

@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    uid = message.from_user.id
    activos = obtener_folios_usuario(uid)
    if not activos:
        await message.answer("ℹ️ No hay folios activos.\n\n📋 Use /tlaxcala para generar uno.")
        return
    lista, botones = [], []
    for f in activos:
        try:
            row = supabase.table("folios_registrados").select("nombre").eq("folio", f).limit(1).execute()
            nombre = row.data[0].get("nombre", "") if row.data else ""
        except:
            nombre = ""
        lista.append(f"• {f} — {nombre or ''}")
        botones.append([InlineKeyboardButton(text=f"⏹️ Detener {f}", callback_data=f"detener_{f}")])
    await message.answer(f"📋 FOLIOS ACTIVOS ({len(activos)})\n\n" + "\n\n".join(lista) + "\n\n📋 Use /tlaxcala para otro permiso.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=botones))

@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Gobierno del Estado de Tlaxcala — SMyT.\n\n📋 Use /tlaxcala para generar un permiso.")

# ===================== HTML / CSS =====================

CSS = f"""
*{{font-family:'Roboto',sans-serif;box-sizing:border-box;}}
body{{margin:0;background:#f4f4f4;}}
.layout-container{{min-height:100vh;}}
.sidebar{{display:none;}}
@media (min-width:992px){{
  .sidebar{{display:block;width:300px;height:100vh;position:fixed;top:0;left:0;z-index:1020;background:white;overflow-y:auto;box-shadow:0 4px 12px rgba(0,0,0,.12);}}
  .main-content-wrapper{{margin-left:300px;}}
}}
.sidebar-header{{background:{ACCENT};padding:24px 16px;color:white;text-align:center;}}
.sidebar-header img{{height:70px;object-fit:contain;filter:brightness(10);margin-bottom:10px;border-radius:50%;}}
.sidebar-header p{{margin:6px 0 0;font-size:14px;opacity:.95;font-weight:600;}}
.sidebar ul{{list-style:none;margin:0;padding:10px 0;}}
.sidebar ul li a{{display:flex;align-items:center;gap:12px;padding:14px 20px;color:#000;text-decoration:none;font-size:14px;font-weight:600;transition:.15s;border-bottom:1px solid #f0f0f0;}}
.sidebar ul li a:hover{{background:rgba(161,26,92,.08);}}
.sidebar ul li a i{{color:{ACCENT};width:18px;text-align:center;}}
.sidebar ul li a.danger{{color:#c00;}}.sidebar ul li a.danger i{{color:#c00;}}
.topbar{{background:{C1};color:white;padding:12px 16px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:100;box-shadow:0 2px 8px rgba(0,0,0,.15);}}
.topbar img{{height:38px;object-fit:contain;}}
.hamburger{{display:flex;flex-direction:column;gap:5px;cursor:pointer;padding:4px;background:none;border:none;}}
.hamburger span{{display:block;width:24px;height:3px;background:white;border-radius:2px;}}
@media (min-width:992px){{ .hamburger{{display:none;}} }}
.offcanvas-sb{{position:fixed;top:0;left:-300px;width:300px;height:100%;background:white;z-index:1030;transition:.3s;overflow-y:auto;box-shadow:4px 0 20px rgba(0,0,0,.2);}}
.offcanvas-sb.open{{left:0;}}
.overlay{{position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:1025;display:none;}}
.overlay.show{{display:block;}}
@media (min-width:992px){{ .offcanvas-sb,.overlay{{display:none!important;}} }}
.admin-bar{{max-width:680px;margin:16px auto 0;padding:0 24px;color:{ACCENT};font-weight:700;font-size:12px;text-transform:uppercase;letter-spacing:.5px;display:flex;align-items:center;gap:8px;}}
.content{{padding:24px;max-width:680px;margin:20px auto;background:white;border:1px solid {C3};border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,.08);}}
.stat-card{{background:#f8f9fa;border-radius:14px;padding:20px;text-align:center;border:1px solid {C3};box-shadow:0 4px 16px rgba(0,0,0,.06);margin-bottom:8px;}}
.stat-num{{font-size:36px;font-weight:700;color:{C1};line-height:1;}}
.stat-lbl{{font-size:11px;color:#888;font-weight:700;text-transform:uppercase;margin-top:6px;}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px;}}
.grid-full{{grid-column:1/-1;}}
.menu-btn{{background:#f8f9fa;border:1px solid {C3};border-radius:14px;padding:22px 12px;text-align:center;text-decoration:none;color:#1d1d1b;display:block;transition:.2s;}}
.menu-btn:hover{{border-color:{ACCENT};color:{ACCENT};transform:translateY(-2px);box-shadow:0 4px 16px rgba(0,0,0,.1);}}
.menu-btn i{{font-size:28px;display:block;margin-bottom:8px;color:{ACCENT};}}
.menu-btn span{{font-size:13px;font-weight:600;}}
.menu-btn.danger i{{color:#dc3545;}}.menu-btn.danger:hover{{border-color:#dc3545;color:#dc3545;}}
table{{font-size:12px;width:100%;border-collapse:collapse;}}
thead th{{background:{C1};color:white;padding:10px 8px;text-align:left;white-space:nowrap;}}
tbody td{{padding:9px 8px;vertical-align:middle;border-bottom:1px solid #eee;}}
tbody tr:last-child td{{border-bottom:none;}}tbody tr:hover td{{background:#f6f3fb;}}
.tabla-wrap{{overflow-x:auto;background:white;border-radius:12px;box-shadow:0 2px 8px rgba(0,0,0,.08);}}
.bp{{display:inline-block;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:700;color:white;}}
.bp-p{{background:#dc3545;}}.bp-v{{background:#1a6e2e;}}.bp-vig{{background:#1a6e2e;}}.bp-ven{{background:{C1};}}
.form-card{{background:#f8f9fa;border-radius:14px;padding:20px;border:1px solid {C3};box-shadow:0 4px 16px rgba(0,0,0,.06);}}
.form-label{{font-weight:600;font-size:14px;display:block;margin-bottom:4px;}}
.form-control{{display:block;width:100%;padding:10px 12px;border:1.5px solid #ddd;border-radius:8px;font-size:14px;transition:.2s;font-family:inherit;}}
.form-control:focus{{border-color:{C1};outline:none;box-shadow:0 0 0 3px rgba(75,46,131,.1);}}
select.form-control{{appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23666' d='M6 8L1 3h10z'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center;padding-right:32px;}}
.mb-3{{margin-bottom:14px;}}.mb-4{{margin-bottom:20px;}}.mt-3{{margin-top:14px;}}
.btn{{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:11px 20px;border-radius:8px;font-weight:700;font-size:14px;border:none;cursor:pointer;text-decoration:none;transition:.2s;font-family:inherit;}}
.btn-primary{{background:{GREEN};color:white;width:100%;}}
.btn-primary:hover{{background:#5a9e07;}}
.btn-sm{{padding:5px 12px;font-size:11px;border-radius:6px;}}
.btn-outline{{background:white;border:1.5px solid #ddd;color:#444;}}
.btn-outline:hover{{border-color:{C1};color:{C1};}}
.btn-danger{{background:#dc3545;color:white;}}.btn-success{{background:#1a6e2e;color:white;}}
.btn-warn{{background:#e67e22;color:white;}}
.alert{{padding:12px 14px;border-radius:8px;margin-bottom:14px;font-size:13px;font-weight:600;}}
.alert-ok{{background:#d4edda;color:#155724;border:1px solid #c3e6cb;}}
.alert-err{{background:#f8d7da;color:#721c24;border:1px solid #f5c6cb;}}
.info-box{{background:#f8f8f8;border-radius:8px;padding:14px;font-size:13px;margin-bottom:14px;line-height:1.7;}}
.footer-clon{{background:#f7f9f9;color:#000;text-align:center;padding:14px;position:relative;border-top:1px solid #dee2e6;}}
.filter-bar{{background:white;border-radius:12px;padding:14px;box-shadow:0 2px 8px rgba(0,0,0,.08);margin-bottom:14px;display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;}}
.page-title{{font-size:20px;font-weight:700;color:#1d1d1b;margin-bottom:20px;padding-bottom:14px;border-bottom:1px solid #eee;}}
"""

FA     = '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.7.2/css/all.min.css">'
BI     = '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">'
ROBOTO = '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap" rel="stylesheet">'

JS_NAV = """<script>
function openNav(){document.getElementById('offsb').classList.add('open');document.getElementById('overlay').classList.add('show');}
function closeNav(){document.getElementById('offsb').classList.remove('open');document.getElementById('overlay').classList.remove('show');}
document.addEventListener('DOMContentLoaded',function(){
  document.getElementById('overlay').addEventListener('click',closeNav);
});
</script>"""

LOGO_URL   = "/static/logo_brand.png"
ESCUDO_URL = "/static/logo_brand.png"

def header_tramite(titulo_html: str, subtitulo: str = "OFICINA VIRTUAL DE TRÁMITES", icono: str = "fa-solid fa-car") -> str:
    return f"""<div style="display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:20px">
      <div style="flex:1">
        <h4 style="font-size:19px;font-weight:700;color:#1d1d1b;margin:0;display:flex;align-items:center;gap:8px">
          <i class="{icono}" style="color:{ACCENT}"></i> {titulo_html}</h4>
        <hr style="border:none;border-top:1px solid #ddd;width:50%;margin:6px 0 0">
        <h6 style="font-size:13px;color:#555;margin:6px 0 0;font-weight:400">{subtitulo}</h6>
      </div>
      <img src="{LOGO_URL}" alt="Tlaxcala" style="max-height:60px;object-fit:contain">
    </div>"""

def head(titulo):
    return f"""<!DOCTYPE html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{titulo} — Tlaxcala</title>
<link rel="icon" href="{ESCUDO_URL}" sizes="32x32"/>
{ROBOTO}{FA}{BI}<style>{CSS}</style></head><body>"""

def _sidebar_links():
    return """
    <li><a href="/panel/admin"><i class="fa-solid fa-house"></i>Inicio</a></li>
    <li><a href="/panel/folios"><i class="fa-solid fa-list-check"></i>Ver Folios</a></li>
    <li><a href="/panel/timers"><i class="fa-solid fa-stopwatch"></i>Timers Activos</a></li>
    <li><a href="/panel/registro_admin"><i class="fa-solid fa-file-circle-plus"></i>Registrar Permiso</a></li>
    <li><a href="/panel/crear_usuario"><i class="fa-solid fa-user-plus"></i>Crear Usuario</a></li>
    <li><a href="/panel/tablas"><i class="fa-solid fa-database"></i>Tablas BD</a></li>
    <li><a href="/consulta_folio"><i class="fa-solid fa-magnifying-glass"></i>Consultar Folio</a></li>
    <li><a href="/panel/logout" class="danger"><i class="fa-solid fa-right-from-bracket"></i>Cerrar Sesión</a></li>"""

def navbar():
    links = _sidebar_links()
    return f"""<nav class="sidebar">
      <div class="sidebar-header">
        <img src="{LOGO_URL}" alt="Tlaxcala">
        <p>Secretaría de Movilidad<br>y Transporte</p>
      </div>
      <ul>{links}</ul>
    </nav>
    <div class="overlay" id="overlay"></div>
    <nav class="offcanvas-sb" id="offsb">
      <div class="sidebar-header">
        <img src="{LOGO_URL}" alt="Tlaxcala">
        <p>Secretaría de Movilidad<br>y Transporte</p>
      </div>
      <ul>{links}</ul>
    </nav>
    <div class="topbar">
      <button class="hamburger" onclick="openNav()"><span></span><span></span><span></span></button>
      <img src="{LOGO_URL}" alt="Tlaxcala">
    </div>"""

def admin_bar(seccion):
    return f'<div class="admin-bar"><i class="fa-solid fa-shield-halved"></i> {seccion}</div>'

def footer_clon():
    return """<footer class="footer-clon">
      <p style="margin:0;font-size:12px;">Secretaría de Movilidad y Transporte — Gobierno del Estado de Tlaxcala © 2026</p>
    </footer>"""

def footer(scripts=""):
    return f"""{scripts}{JS_NAV}{footer_clon()}</div></body></html>"""

def page(titulo, seccion, contenido, scripts=""):
    return (head(titulo) + '<div class="layout-container">' + navbar()
            + '<div class="main-content-wrapper">' + admin_bar(seccion)
            + f'<div class="content">{contenido}</div>' + footer(scripts))

def login_html(error=False):
    err = '<div class="alert alert-err"><i class="fa-solid fa-triangle-exclamation"></i> Usuario o contraseña incorrectos</div>' if error else ""
    return f"""<!DOCTYPE html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Acceso — Tlaxcala SMyT</title>
<link rel="icon" href="{ESCUDO_URL}" sizes="32x32"/>
{ROBOTO}{FA}{BI}
<style>
*{{font-family:'Roboto',sans-serif;box-sizing:border-box;}}
body{{background:{C1};min-height:100vh;margin:0;display:flex;flex-direction:column;}}
.lw{{flex:1;display:flex;align-items:center;justify-content:center;padding:30px 15px;}}
.lc{{background:white;border-radius:16px;padding:32px;max-width:420px;width:100%;box-shadow:0 12px 40px rgba(0,0,0,.3);border:1px solid {C3};}}
.le{{text-align:center;margin-bottom:20px;}}.le img{{max-height:70px;max-width:100%;object-fit:contain;}}
.lt{{text-align:center;font-size:18px;font-weight:700;color:#1d1d1b;margin-bottom:8px;}}
.lhr{{border:none;border-top:1px solid #ddd;width:50%;margin:0 auto 10px;}}
.ls{{text-align:center;font-size:12px;color:#777;margin-bottom:22px;}}
.form-label{{font-weight:600;font-size:14px;display:block;margin-bottom:4px;}}
.form-control{{display:block;width:100%;padding:11px 13px;border:1.5px solid #ddd;border-radius:8px;font-size:14px;font-family:inherit;}}
.form-control:focus{{border-color:{C1};outline:none;box-shadow:0 0 0 3px rgba(75,46,131,.1);}}
.mb-3{{margin-bottom:14px;}}.mb-4{{margin-bottom:20px;}}
.alert{{padding:11px 13px;border-radius:8px;font-size:13px;font-weight:600;margin-bottom:14px;}}
.alert-err{{background:#f8d7da;color:#721c24;border:1px solid #f5c6cb;}}
.btn-in{{background:{C1};border:none;color:white;width:100%;padding:13px;font-weight:700;font-size:15px;border-radius:8px;cursor:pointer;font-family:inherit;}}
.btn-in:hover{{background:{C2};}}
.lf{{background:rgba(0,0,0,.2);color:rgba(255,255,255,.7);text-align:center;padding:14px;font-size:12px;}}
</style></head><body>
<div class="lw"><div class="lc">
  <div class="le"><img src="{LOGO_URL}" alt="Tlaxcala — Secretaría de Finanzas"></div>
  <div class="lt">Acceso al Sistema — SMyT Tlaxcala</div>
  <hr class="lhr">
  <div class="ls">Gobierno del Estado de Tlaxcala · Sistema Administrativo</div>
  {err}
  <form method="POST" action="/panel/login">
    <div class="mb-3"><label class="form-label">Usuario</label><input type="text" name="username" class="form-control" required autofocus autocomplete="off"></div>
    <div class="mb-4"><label class="form-label">Contraseña</label><input type="password" name="password" class="form-control" required></div>
    <button type="submit" class="btn-in"><i class="fa-solid fa-right-to-bracket"></i> &nbsp;Ingresar al Sistema</button>
  </form>
</div></div>
<div class="lf">Secretaría de Movilidad y Transporte — Gobierno del Estado de Tlaxcala © 2026</div>
</body></html>"""

# ===================== LIFESPAN =====================

_keep_task = None

async def keep_alive():
    while True:
        await asyncio.sleep(600)
        print(f"[HEARTBEAT] Tlaxcala activo — timers en RAM: {len(timers_activos)}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    await asyncio.to_thread(_sb_inicializar_folio)
    await bot.delete_webhook(drop_pending_updates=True)
    webhook_url = f"{BASE_URL}/webhook"
    await bot.set_webhook(webhook_url, allowed_updates=["message", "callback_query"])
    _keep_task = asyncio.create_task(keep_alive())
    print(f"[SISTEMA] Tlaxcala v4.0 (RAM timers) listo — siguiente folio: {FOLIO_PREFIJO}{_folio_counter['siguiente']}")
    yield
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError):
            await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Tránsito Tlaxcala", version="4.0")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "tlaxcala_clave_super_segura_cambiar"))
try:
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
except Exception:
    pass

# ===================== WEBHOOK =====================

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        await dp.feed_webhook_update(bot, types.Update(**data))
        return {"ok": True}
    except Exception as e:
        print(f"[WEBHOOK] Error: {e}")
        return {"ok": False, "error": str(e)}

# ===================== AUTH =====================

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if request.session.get("admin"):
        return RedirectResponse(url="/panel/admin", status_code=303)
    if request.session.get("username"):
        return RedirectResponse(url="/registro_usuario", status_code=303)
    return RedirectResponse(url="/panel/login", status_code=303)

@app.get("/panel/login", response_class=HTMLResponse)
async def login_get(request: Request):
    if request.session.get("admin"):
        return RedirectResponse(url="/panel/admin", status_code=303)
    return HTMLResponse(login_html(bool(request.query_params.get("error", ""))))

@app.post("/panel/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USER and password == ADMIN_PASS:
        request.session["admin"] = True
        request.session["username"] = username
        return RedirectResponse(url="/panel/admin", status_code=303)
    try:
        res = supabase.table("verificacion_tlaxcala").select("*").eq("username", username).eq("password", password).execute()
        if res.data:
            u = res.data[0]
            request.session["admin"] = False
            request.session["username"] = u["username"]
            request.session["user_id"] = u.get("id")
            return RedirectResponse(url="/registro_usuario", status_code=303)
    except Exception as e:
        print(f"[LOGIN] Error: {e}")
    return RedirectResponse(url="/panel/login?error=1", status_code=303)

@app.get("/panel/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/panel/login", status_code=303)

# ===================== PANEL ADMIN =====================

@app.get("/panel/admin", response_class=HTMLResponse)
async def panel_admin(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    pendientes = 0
    timers_ram = len(timers_activos)
    try:
        r = supabase.table("folios_registrados").select("folio").eq("estado_pago", "PENDIENTE_PAGO").eq("entidad", ENTIDAD).execute()
        pendientes = len(r.data or [])
    except Exception:
        pass
    color_pend = "#dc3545" if pendientes else "#1a6e2e"
    contenido = f"""
    {header_tramite("PANEL DE ADMINISTRACIÓN", icono="fa-solid fa-house")}
    <div class="row-2 mb-3">
      <div class="stat-card"><div class="stat-num">{timers_ram}</div><div class="stat-lbl">Timers en RAM</div></div>
      <div class="stat-card"><div class="stat-num" style="color:{color_pend}">{pendientes}</div><div class="stat-lbl">Pendientes Pago</div></div>
    </div>
    <div class="stat-card mb-3"><div class="stat-num">{FOLIO_PREFIJO}{_folio_counter['siguiente']}</div><div class="stat-lbl">Siguiente Folio</div></div>
    <div class="grid anim-scroll ArribaAbajo">
      <a href="/panel/folios" class="menu-btn"><i class="fa-solid fa-list-check"></i><span>Ver Folios</span></a>
      <a href="/panel/timers" class="menu-btn"><i class="fa-solid fa-stopwatch"></i><span>Timers Activos</span></a>
      <a href="/panel/registro_admin" class="menu-btn"><i class="fa-solid fa-file-circle-plus"></i><span>Registrar Permiso</span></a>
      <a href="/panel/crear_usuario" class="menu-btn"><i class="fa-solid fa-user-plus"></i><span>Crear Usuario</span></a>
      <a href="/panel/tablas" class="menu-btn"><i class="fa-solid fa-database"></i><span>Tablas BD</span></a>
      <a href="/consulta_folio" class="menu-btn"><i class="fa-solid fa-magnifying-glass"></i><span>Consultar Folio</span></a>
      <a href="/panel/logout" class="menu-btn danger grid-full"><i class="fa-solid fa-right-from-bracket"></i><span>Cerrar Sesión</span></a>
    </div>"""
    return HTMLResponse(page("Panel Admin", "Panel de Administración — Tlaxcala SMyT", contenido))

# ===================== TIMERS (PANEL WEB) =====================

@app.get("/panel/timers", response_class=HTMLResponse)
async def panel_timers(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    msg = request.query_params.get("msg", "")
    
    # Mostrar SOLO timers en RAM
    filas = ""
    for f, info in timers_activos.items():
        start = info.get("start_time")
        elapsed = (datetime.now() - start).total_seconds() if start else 0
        restante = max(0, TOTAL_MINUTOS_TIMER * 60 - elapsed)
        h, m = divmod(int(restante // 60), 60)
        color = "#dc3545" if restante < 3600 else C1
        nombre = info.get("nombre", "")
        filas += f"""<tr>
          <td><strong style="color:{C1}">{f}</strong><br><small style="color:#999">🟢 {nombre}</small></td>
          <td>—</td>
          <td><strong style="color:{color}">{h}h {m}min</strong></td>
          <td>
            <form method="POST" action="/panel/timer/detener/{f}" style="display:inline">
              <button class="btn btn-warn btn-sm" onclick="return confirm('¿Detener timer?')">⏹️ Detener</button>
            </form>
          </td>
        </tr>"""
    
    contenido = f"""
    {header_tramite("TIMERS ACTIVOS (RAM)", icono="fa-solid fa-stopwatch")}
    {"<div class='alert alert-ok'>"+msg+"</div>" if msg else ""}
    <div class="info-box">
      <strong>💾 Timers en memoria RAM:</strong> Solo existen mientras el servidor está activo.
      Si Render reinicia, todos los timers se pierden. Los timers se detienen SOLO desde el bot de Telegram 
      o desde los botones de abajo.
    </div>
    <div class="tabla-wrap"><table>
      <thead><tr><th>Folio / Titular</th><th>Vehículo</th><th>Restante</th><th>Acciones</th></tr></thead>
      <tbody>{filas or '<tr><td colspan="4" style="text-align:center;color:#999;padding:20px">Sin timers activos</td></tr>'}</tbody>
    </table></div>
    <div class="mt-3"><a href="/panel/admin" class="btn btn-outline btn-sm">← Panel</a></div>"""
    return HTMLResponse(page("Timers", "Timers Activos — Tlaxcala", contenido))

@app.post("/panel/timer/detener/{folio}")
async def panel_detener_timer(request: Request, folio: str):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    r = await detener_timer(folio, "panel_web_detener", estado="TIMER_DETENIDO")
    if r.get("user_id"):
        with suppress(Exception):
            await bot.send_message(r["user_id"],
                f"⏹️ TIMER DETENIDO — TLAXCALA\nFolio: {r['folio']}\nTitular: {r.get('nombre','')}\n"
                f"Un administrador detuvo el timer. Tu folio NO será eliminado.\n\n📋 Use /tlaxcala para otro permiso.")
    aviso = "Timer de " + r["folio"] + " detenido ⏹️"
    return RedirectResponse(url=f"/panel/timers?msg={quote(aviso)}", status_code=303)

# ===================== FOLIOS =====================

@app.get("/panel/folios", response_class=HTMLResponse)
async def admin_folios(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    filtro  = request.query_params.get("filtro", "").strip()
    crit    = request.query_params.get("criterio", "folio")
    ep_fil  = request.query_params.get("estado_pago", "todos")
    msg     = request.query_params.get("msg", "")
    try:
        q = supabase.table("folios_registrados").select("*").eq("entidad", ENTIDAD)
        if filtro:
            q = q.ilike(crit, f"%{filtro}%")
        if ep_fil != "todos":
            q = q.eq("estado_pago", ep_fil)
        folios = q.order("fecha_expedicion", desc=True).execute().data or []
    except Exception as e:
        folios = []
        print(f"[FOLIOS] Error: {e}")
    msg_html = f'<div class="alert alert-ok">{msg}</div>' if msg else ""
    filas = ""
    for f in folios:
        folio_id = f.get("folio", "")
        pago = f.get("estado_pago", "VALIDADO") or "VALIDADO"
        bp   = '<span class="bp bp-p">PEND</span>' if pago == "PENDIENTE_PAGO" else '<span class="bp bp-v">OK</span>'
        en_ram = "🟢" if folio_id in timers_activos else "⚪"
        bval = f'<form method="POST" action="/panel/validar/{folio_id}" style="display:inline"><button class="btn btn-success btn-sm" onclick="return confirm(\'¿Validar?\')">✅</button></form> ' if pago == "PENDIENTE_PAGO" else ""
        bpdf = f'<a href="/descargar/{folio_id}" target="_blank" class="btn btn-sm" style="background:{C1};color:white">📄</a> '
        filas += f"""<tr>
          <td><strong style="color:{C1}">{folio_id}</strong><br><small style="color:#999">{f.get("creado_por","")}</small></td>
          <td>{(f.get("nombre","") or "")[:18]}</td>
          <td>{f.get("marca","")} {f.get("linea","")}</td>
          <td>{bp} {en_ram}</td>
          <td>{bval}{bpdf}<a href="/consulta/{folio_id}" target="_blank" class="btn btn-sm btn-outline">🔗</a></td>
        </tr>"""
    filtros = f"""<div class="filter-bar">
      <form method="GET" style="display:contents">
        <input type="text" name="filtro" class="form-control" value="{filtro}" placeholder="Buscar...">
        <select name="criterio" class="form-control" style="max-width:100px">
          <option value="folio" {"selected" if crit == "folio" else ""}>Folio</option>
          <option value="nombre" {"selected" if crit == "nombre" else ""}>Nombre</option>
        </select>
        <select name="estado_pago" class="form-control" style="max-width:100px">
          <option value="todos" {"selected" if ep_fil == "todos" else ""}>Todos</option>
          <option value="PENDIENTE_PAGO" {"selected" if ep_fil == "PENDIENTE_PAGO" else ""}>Pendiente</option>
          <option value="VALIDADO" {"selected" if ep_fil == "VALIDADO" else ""}>Validado</option>
        </select>
        <button type="submit" class="btn btn-primary btn-sm" style="width:auto">Filtrar</button>
        <a href="/panel/folios" class="btn btn-outline btn-sm">✕</a>
      </form>
      <span style="font-size:12px;color:#888">{len(folios)} resultados</span>
    </div>"""
    contenido = f"""
    {header_tramite("FOLIOS REGISTRADOS", icono="fa-solid fa-list-check")}
    {msg_html}{filtros}
    <div class="tabla-wrap"><table>
      <thead><tr><th>Folio</th><th>Titular</th><th>Vehículo</th><th>Estado</th><th>Acc.</th></tr></thead>
      <tbody>{filas or '<tr><td colspan="5" style="text-align:center;color:#999;padding:20px">Sin folios</td></tr>'}</tbody>
    </table></div>"""
    return HTMLResponse(page("Folios", "Folios Registrados — Tlaxcala", contenido))

@app.post("/panel/validar/{folio}")
async def validar_pago(request: Request, folio: str):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    r = await detener_timer(folio, "panel_web_validar", estado_pago="VALIDADO")
    if r.get("user_id"):
        with suppress(Exception):
            await bot.send_message(r["user_id"],
                f"✅ PAGO VALIDADO — TLAXCALA\nFolio: {r['folio']}\nTitular: {r.get('nombre','')}\n"
                f"Tu permiso está activo.\n\n📋 Use /tlaxcala para otro permiso.")
    return RedirectResponse(url=f"/panel/folios?msg={quote('Folio ' + r['folio'] + ' validado ✅')}", status_code=303)

@app.get("/descargar/{folio}")
async def descargar_pdf_publico(folio: str):
    folio = folio.strip().upper()
    from fastapi.responses import FileResponse
    try:
        res = supabase.table("folios_registrados").select("*").eq("folio", folio).limit(1).execute()
        fila = res.data[0] if res.data else None
    except:
        fila = None
    if fila and fila.get("pdf_url"):
        return RedirectResponse(url=fila["pdf_url"])
    ruta = os.path.join(OUTPUT_DIR, f"{folio}.pdf")
    if os.path.exists(ruta):
        return FileResponse(ruta, media_type="application/pdf", filename=f"{folio}_tlaxcala.pdf")
    if fila:
        try:
            fe_txt = str(fila.get("fecha_expedicion", ""))[:10]
            fv_txt = str(fila.get("fecha_vencimiento", ""))[:10]
            tz = ZoneInfo(TZ)
            fe_dt = datetime.fromisoformat(fe_txt).replace(tzinfo=tz) if fe_txt else datetime.now(tz)
            fv_dt = datetime.fromisoformat(fv_txt).replace(tzinfo=tz) if fv_txt else fe_dt + timedelta(days=30)
            datos = {
                "folio": folio,
                "marca": fila.get("marca", ""), "linea": fila.get("linea", ""),
                "anio": fila.get("anio", ""), "serie": fila.get("numero_serie", ""),
                "motor": fila.get("numero_motor", ""), "color": fila.get("color", ""),
                "nombre": fila.get("nombre", ""), "cve_vehicular": fila.get("cve_vehicular", ""),
                "fecha_exp": fe_dt.strftime("%d/%m/%Y"),
                "fecha_ven": fv_dt.strftime("%d/%m/%Y"),
                "fecha_exp_dt": fe_dt,
            }
            ruta_nueva = await asyncio.to_thread(generar_pdf, datos)
            return FileResponse(ruta_nueva, media_type="application/pdf", filename=f"{folio}_tlaxcala.pdf")
        except Exception as e:
            print(f"[DESCARGA] Error regenerando {folio}: {e}")
    return HTMLResponse("<p>PDF no encontrado.</p>", status_code=404)

# ===================== REGISTRO ADMIN =====================

@app.get("/panel/registro_admin", response_class=HTMLResponse)
async def registro_admin_get(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    tz = ZoneInfo(TZ)
    hoy = datetime.now(tz).strftime("%Y-%m-%d")
    err = request.query_params.get("error", "")
    err_html = f'<div class="alert alert-err">{err}</div>' if err else ""
    contenido = f"""
    {header_tramite("REGISTRAR PERMISO (ADMIN)")}
    {err_html}
    <div class="form-card">
    <form method="POST" action="/panel/registro_admin">
      <div class="mb-3"><label class="form-label">Folio manual <small style="color:#999;font-weight:400">(vacío = auto)</small></label>
        <input type="text" name="folio" class="form-control" placeholder="{FOLIO_PREFIJO}53314" style="text-transform:uppercase"></div>
      <div class="mb-3"><label class="form-label">Marca *</label><input type="text" name="marca" class="form-control" required style="text-transform:uppercase"></div>
      <div class="mb-3"><label class="form-label">Línea *</label><input type="text" name="linea" class="form-control" required style="text-transform:uppercase"></div>
      <div class="mb-3"><label class="form-label">Año *</label><input type="text" name="anio" class="form-control" maxlength="4" required></div>
      <div class="mb-3"><label class="form-label">Color *</label><input type="text" name="color" class="form-control" required style="text-transform:uppercase"></div>
      <div class="mb-3"><label class="form-label">Núm. Serie / NIV *</label><input type="text" name="numero_serie" class="form-control" required style="text-transform:uppercase"></div>
      <div class="mb-3"><label class="form-label">Núm. Motor *</label><input type="text" name="numero_motor" class="form-control" required style="text-transform:uppercase"></div>
      <div class="mb-3"><label class="form-label">Clave Vehicular *</label><input type="text" name="cve_vehicular" class="form-control" required style="text-transform:uppercase"></div>
      <div class="mb-3"><label class="form-label">Nombre del propietario *</label><input type="text" name="nombre" class="form-control" required style="text-transform:uppercase"></div>
      <button type="submit" class="btn btn-primary mt-3"><i class="fa-solid fa-file-circle-plus"></i> Generar Permiso</button>
    </form>
    </div>"""
    return HTMLResponse(page("Registrar Permiso", "Registrar Permiso — Tlaxcala", contenido))

@app.post("/panel/registro_admin")
async def registro_admin_post(request: Request,
    folio: str = Form(None), marca: str = Form(...), linea: str = Form(...),
    anio: str = Form(...), color: str = Form(""), numero_serie: str = Form(...),
    numero_motor: str = Form(""), cve_vehicular: str = Form(""),
    nombre: str = Form(...)):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    try:
        tz = ZoneInfo(TZ)
        fg = folio.strip().upper() if folio and folio.strip() else generar_folio()
        fe = datetime.now(tz)
        fv = fe + timedelta(days=30)
        
        supabase.table("folios_registrados").insert({
            "folio": fg, "marca": marca.upper(), "linea": linea.upper(),
            "anio": anio, "numero_serie": numero_serie.upper(), "numero_motor": numero_motor.upper(),
            "color": color.upper(), "nombre": nombre.upper(), "cve_vehicular": cve_vehicular.upper(),
            "fecha_expedicion": fe.date().isoformat(), "fecha_vencimiento": fv.date().isoformat(),
            "entidad": ENTIDAD, "estado": "ACTIVO", "estado_pago": "VALIDADO",
            "creado_por": request.session.get("username", "admin")
        }).execute()
        
        datos_pdf = {
            "folio": fg, "marca": marca.upper(), "linea": linea.upper(), "anio": anio,
            "serie": numero_serie.upper(), "motor": numero_motor.upper(),
            "cve_vehicular": cve_vehicular.upper(), "color": color.upper(), "nombre": nombre.upper(),
            "fecha_exp": fe.strftime("%d/%m/%Y"), "fecha_ven": fv.strftime("%d/%m/%Y"), "fecha_exp_dt": fe
        }
        await asyncio.to_thread(generar_subir_y_guardar_pdf, datos_pdf)
        return RedirectResponse(url=f"/panel/folios?msg={quote(f'Permiso {fg} generado ✅')}", status_code=303)
    except Exception as e:
        print(f"[REGISTRO ADMIN] Error: {e}")
        return RedirectResponse(url=f"/panel/registro_admin?error={quote(str(e))}", status_code=303)

# ===================== CREAR USUARIO =====================

@app.get("/panel/crear_usuario", response_class=HTMLResponse)
async def crear_usuario_get(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    msg = request.query_params.get("msg", "")
    err = request.query_params.get("error", "")
    contenido = f"""
    {header_tramite("CREAR USUARIO", icono="fa-solid fa-user-plus")}
    {"<div class='alert alert-ok'>"+msg+"</div>" if msg else ""}
    {"<div class='alert alert-err'>"+err+"</div>" if err else ""}
    <div class="form-card">
    <form method="POST" action="/panel/crear_usuario">
      <div class="mb-3"><label class="form-label">Usuario *</label><input type="text" name="username" class="form-control" required autocomplete="off"></div>
      <div class="mb-3"><label class="form-label">Contraseña *</label><input type="password" name="password" class="form-control" required></div>
      <div class="mb-4"><label class="form-label">Folios asignados *</label><input type="number" name="folios" class="form-control" min="1" required></div>
      <button type="submit" class="btn btn-primary"><i class="fa-solid fa-user-plus"></i> Crear Usuario</button>
    </form>
    </div>"""
    return HTMLResponse(page("Crear Usuario", "Crear Usuario — Tlaxcala", contenido))

@app.post("/panel/crear_usuario")
async def crear_usuario_post(request: Request,
    username: str = Form(...), password: str = Form(...), folios: int = Form(...)):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    try:
        existe = supabase.table("verificacion_tlaxcala").select("id").eq("username", username).execute()
        if existe.data:
            return RedirectResponse(url=f"/panel/crear_usuario?error={quote('El usuario ya existe')}", status_code=303)
        supabase.table("verificacion_tlaxcala").insert({
            "username": username, "password": password, "folios_asignac": folios, "folios_usados": 0
        }).execute()
        return RedirectResponse(url=f"/panel/crear_usuario?msg={quote(f'Usuario {username} creado ✅')}", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/panel/crear_usuario?error={quote(str(e))}", status_code=303)

# ===================== CONSULTA PÚBLICA =====================

@app.get("/consulta_folio", response_class=HTMLResponse)
async def consulta_folio_form(request: Request):
    contenido = f"""
    {header_tramite("CONSULTAR FOLIO", icono="fa-solid fa-magnifying-glass")}
    <div class="form-card">
    <form method="POST" action="/consulta_folio">
      <div class="mb-3"><label class="form-label">Número de Folio</label>
        <input type="text" name="folio" class="form-control" placeholder="{FOLIO_PREFIJO}53314" required autofocus style="text-transform:uppercase"></div>
      <button type="submit" class="btn btn-primary"><i class="fa-solid fa-magnifying-glass"></i> Buscar</button>
    </form>
    </div>"""
    return HTMLResponse(page("Consultar Folio", "Consultar Folio", contenido))

@app.post("/consulta_folio")
async def consulta_folio_post(request: Request, folio: str = Form(...)):
    return RedirectResponse(url=f"/consulta/{folio.strip().upper()}", status_code=303)

@app.get("/consulta/{folio}", response_class=HTMLResponse)
async def consulta_publica(folio: str):
    folio = folio.strip().upper()
    def _row(label, valor):
        return f'<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #eee"><span style="color:#888;font-weight:600">{label}</span><span style="font-weight:600;text-align:right;max-width:60%">{valor or "—"}</span></div>'
    try:
        res = supabase.table("folios_registrados").select("*").eq("folio", folio).execute()
        if not res.data:
            badge = f"""<div style="background:#c0392b;color:white;padding:16px;border-radius:10px;font-size:15px;font-weight:700;text-align:center;margin-bottom:18px">
              <i class="fa-solid fa-circle-xmark" style="font-size:24px;display:block;margin-bottom:6px"></i>
              EL FOLIO NO ESTÁ REGISTRADO
            </div>"""
            datos_html = ""
        else:
            f = res.data[0]
            badge = f"""<div style="background:#1a6e2e;color:white;padding:16px;border-radius:10px;font-size:15px;font-weight:700;text-align:center;margin-bottom:18px">
              <i class="fa-solid fa-circle-check" style="font-size:24px;display:block;margin-bottom:6px"></i>
              FOLIO VIGENTE
            </div>"""
            datos_html = f"""
            <div style="background:white;border:1px solid {C3};border-radius:12px;padding:14px;margin-bottom:12px">
              <div style="color:{BLUE};font-weight:700;font-size:14px;padding-bottom:8px;margin-bottom:6px;border-bottom:1px solid #eee">
                <i class="fa-solid fa-car" style="color:{ACCENT};margin-right:6px"></i> Vehículo
              </div>
              {_row("Marca", f.get("marca",""))}
              {_row("Línea", f.get("linea",""))}
              {_row("Modelo", f.get("anio",""))}
              {_row("Serie/NIV", f.get("numero_serie",""))}
              {_row("Motor", f.get("numero_motor",""))}
              {_row("Color", f.get("color",""))}
            </div>
            <div style="background:white;border:1px solid {C3};border-radius:12px;padding:14px">
              <div style="color:{BLUE};font-weight:700;font-size:14px;padding-bottom:8px;margin-bottom:6px;border-bottom:1px solid #eee">
                <i class="fa-solid fa-file-shield" style="color:{ACCENT};margin-right:6px"></i> Permiso
              </div>
              {_row("Folio", f"<span style='color:{C1};font-weight:700'>{folio}</span>")}
              {_row("Propietario", f.get("nombre",""))}
              {_row("Expedición", str(f.get("fecha_expedicion",""))[:10])}
              {_row("Vencimiento", str(f.get("fecha_vencimiento",""))[:10])}
            </div>"""
        html = f"""<!DOCTYPE html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Consulta {folio} — Tlaxcala</title>
<link rel="icon" href="{ESCUDO_URL}" sizes="32x32"/>
{ROBOTO}{FA}{BI}<style>{CSS}</style></head><body style="background:#f4f4f4;">
<div class="content" style="max-width:500px">
  {header_tramite("VERIFICACIÓN DE PERMISO")}
  {badge}
  {datos_html}
  <a href="https://smyt.tlaxcala.gob.mx/" class="btn btn-primary mt-3"><i class="fa-solid fa-arrow-left"></i> Volver</a>
</div>
{footer_clon()}
</body></html>"""
        return HTMLResponse(html)
    except Exception as e:
        return HTMLResponse(f"<p>Error: {e}</p>", status_code=500)

# ===================== HEALTH =====================

@app.get("/health")
async def health():
    return {
        "status": "healthy", "version": "4.0", "entidad": ENTIDAD,
        "timers_ram": len(timers_activos),
        "siguiente_folio": f"{FOLIO_PREFIJO}{_folio_counter['siguiente']}"
    }

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
