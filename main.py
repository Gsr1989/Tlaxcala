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
PLANTILLA_PDF = "TLAXCALA2026(1).pdf"   # <-- sube el PDF a la raíz del repo con este nombre EXACTO (mayúsculas/minúsculas y paréntesis incluidos)
FOLIO_PREFIJO = "ZX"
FOLIO_INICIO  = 53314   # siguiente folio después del ZX53313 de tu ejemplo físico
_folio_counter = {"siguiente": FOLIO_INICIO}
_folio_lock    = asyncio.Lock()
PAGE_SIZE = 100

# Paleta propia del servicio (independiente de cualquier paleta oficial de gobierno)
C1 = "#2b3f6b"   # azul marino principal (topbar / sidebar)
C2 = "#1f2f52"   # variante oscura para hover
C3 = "#d8c98a"   # borde dorado de las tarjetas
ACCENT = "#8a1f4f"   # acento de iconos/secciones
GREEN  = "#4c8a12"   # botones de acción principal
BLUE   = "#2856ad"   # subtítulos de tarjeta

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

# ===================== TIMERS =====================
timers_activos       = {}
user_folios          = {}
pending_comprobantes = {}
TOTAL_MINUTOS_TIMER  = 36 * 60

async def eliminar_folio_automatico(folio: str):
    try:
        uid = timers_activos[folio]["user_id"] if folio in timers_activos else None
        await asyncio.to_thread(lambda: supabase.table("folios_registrados").delete().eq("folio", folio).execute())
        try:
            await asyncio.to_thread(lambda: supabase.storage.from_(BUCKET_NAME).remove([f"{folio}.pdf"]))
        except Exception as e: print(f"[STORAGE] Error borrando {folio}.pdf: {e}")
        ruta_local = os.path.join(OUTPUT_DIR, f"{folio}.pdf")
        if os.path.exists(ruta_local): os.remove(ruta_local)
        if uid:
            await bot.send_message(uid,
                f"⏰ TIEMPO AGOTADO - TLAXCALA\n\nEl folio {folio} fue eliminado por no completar el pago en 36 horas.\n\n📋 Use /tlaxcala para generar otro permiso.")
        limpiar_timer_folio(folio)
    except Exception as e: print(f"[ERROR] eliminando folio {folio}: {e}")

async def enviar_recordatorio(folio: str, minutos_restantes: int):
    try:
        if folio not in timers_activos: return
        uid = timers_activos[folio]["user_id"]
        await bot.send_message(uid,
            f"⚡ RECORDATORIO - TLAXCALA\n\nFolio: {folio}\nTiempo restante: {minutos_restantes} minutos\n\n📸 Envíe su comprobante de pago.\n\n📋 Use /tlaxcala para otro permiso.")
    except Exception as e: print(f"[ERROR] recordatorio {folio}: {e}")

async def iniciar_timer_36h(user_id: int, folio: str, nombre: str = ""):
    async def timer_task():
        await asyncio.sleep(34.5 * 3600)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 90)
        await asyncio.sleep(30 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 60)
        await asyncio.sleep(30 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 30)
        await asyncio.sleep(20 * 60)
        if folio not in timers_activos: return
        await enviar_recordatorio(folio, 10)
        await asyncio.sleep(10 * 60)
        if folio in timers_activos:
            await eliminar_folio_automatico(folio)
    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {"task": task, "user_id": user_id, "start_time": datetime.now(), "nombre": nombre}
    user_folios.setdefault(user_id, []).append(folio)
    print(f"[TIMER] Iniciado folio {folio} ({nombre})")

def cancelar_timer_folio(folio: str) -> bool:
    if folio not in timers_activos: return False
    timers_activos[folio]["task"].cancel()
    uid = timers_activos[folio]["user_id"]
    del timers_activos[folio]
    if uid in user_folios and folio in user_folios[uid]:
        user_folios[uid].remove(folio)
        if not user_folios[uid]: del user_folios[uid]
    return True

def limpiar_timer_folio(folio: str):
    if folio not in timers_activos: return
    uid = timers_activos[folio]["user_id"]
    del timers_activos[folio]
    if uid in user_folios and folio in user_folios[uid]:
        user_folios[uid].remove(folio)
        if not user_folios[uid]: del user_folios[uid]

def obtener_folios_usuario(user_id: int) -> list:
    return user_folios.get(user_id, [])

# ===================== FOLIOS (ZX + 5 dígitos) =====================
def _sb_leer_watermark():
    try:
        r = supabase.table("folio_watermark").select("ultimo_asignado").eq("prefijo", FOLIO_PREFIJO).execute()
        return r.data[0]["ultimo_asignado"] if r.data else None
    except: return None

def _sb_guardar_watermark(numero):
    try: supabase.table("folio_watermark").upsert({"prefijo": FOLIO_PREFIJO, "ultimo_asignado": numero}).execute()
    except Exception as e: print(f"[ERROR] guardar_watermark: {e}")

def _sb_inicializar_folio():
    wm = _sb_leer_watermark()
    if wm is not None:
        _folio_counter["siguiente"] = wm + 1; return
    try:
        resp = supabase.table("folios_registrados").select("folio").eq("entidad", ENTIDAD).like("folio", f"{FOLIO_PREFIJO}%").execute()
        nums = []
        for row in resp.data or []:
            f = row.get("folio","")
            if isinstance(f, str) and f.startswith(FOLIO_PREFIJO):
                suf = f[len(FOLIO_PREFIJO):]
                if suf.isdigit(): nums.append(int(suf))
        if nums:
            maximo = max(nums); _folio_counter["siguiente"] = maximo + 1; _sb_guardar_watermark(maximo)
        else:
            _folio_counter["siguiente"] = FOLIO_INICIO
    except Exception as e: print(f"[ERROR] inicializar_folio: {e}")

def _folio_existe(folio):
    try:
        r = supabase.table("folios_registrados").select("folio").eq("folio", folio).execute()
        return len(r.data) > 0
    except: return False

def _generar_folio_sync():
    candidato = _folio_counter["siguiente"]
    for _ in range(100_000):
        folio = f"{FOLIO_PREFIJO}{candidato}"
        if not _folio_existe(folio):
            _folio_counter["siguiente"] = candidato + 1
            _sb_guardar_watermark(candidato)
            print(f"[FOLIO] Asignado: {folio}"); return folio
        candidato += 1
    return f"{FOLIO_PREFIJO}{random.randint(90000,99999)}"

async def _generar_folio_async():
    async with _folio_lock:
        return await asyncio.to_thread(_generar_folio_sync)

def generar_folio(): return _generar_folio_sync()

# ===================== STORAGE =====================
def subir_pdf_a_storage(ruta_local: str, folio: str) -> str:
    try:
        if not os.path.exists(ruta_local): return ""
        with open(ruta_local, "rb") as f: contenido = f.read()
        nombre = f"{folio}.pdf"
        supabase.storage.from_(BUCKET_NAME).upload(path=nombre, file=contenido,
            file_options={"content-type": "application/pdf", "upsert": "true"})
        url = supabase.storage.from_(BUCKET_NAME).get_public_url(nombre)
        print(f"[STORAGE] ✅ Subido: {url}"); return url
    except Exception as e:
        print(f"[STORAGE] ❌ Error {folio}: {e}"); return ""

# ===================== QR (2 por permiso) =====================
def _generar_qr_url(folio: str):
    """QR izquierdo: apunta a nuestra página de consulta pública."""
    try:
        url = f"{BASE_URL}/consulta/{folio}"
        qr  = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=4, border=1)
        qr.add_data(url); qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        return img
    except Exception as e:
        print(f"[QR] Error url: {e}"); return None

def _generar_qr_datos(datos: dict):
    """QR derecho: texto plano con los datos capturados (no es una URL)."""
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
        qr.add_data(texto); qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        return img
    except Exception as e:
        print(f"[QR] Error datos: {e}"); return None

# ===================== PDF — COORDENADAS EXACTAS (plantilla 792x612 pts) =====================
def generar_pdf(datos: dict) -> str:
    folio = datos["folio"]
    out   = os.path.join(OUTPUT_DIR, f"{folio}.pdf")

    nombre = str(datos.get("nombre","")).upper()
    marca  = str(datos.get("marca","")).upper()
    linea  = str(datos.get("linea","")).upper()
    modelo = str(datos.get("anio",""))
    serie  = str(datos.get("serie","")).upper()
    motor  = str(datos.get("motor","")).upper()
    color  = str(datos.get("color","")).upper()
    cve    = str(datos.get("cve_vehicular","")).upper()

    F  = "helv"
    FB = "hebo"
    S  = 9

    try:
        if os.path.exists(PLANTILLA_PDF):
            doc = fitz.open(PLANTILLA_PDF)
            pg  = doc[0]

            # Folio gigante (espacio libre entre las dos franjas grises superiores)
            pg.insert_text((460, 270), folio, fontsize=35, fontname=FB, color=(0.29, 0.18, 0.51))

            # VIGENCIA — valores abajo de su rubro
            pg.insert_text((52, 205), datos["fecha_exp"], fontsize=S, fontname=F, color=(0,0,0))
            pg.insert_text((52, 239), datos["fecha_ven"], fontsize=S, fontname=F, color=(0,0,0))

            # PROPIETARIO — valor abajo del rubro
            pg.insert_text((52, 298), nombre, fontsize=S, fontname=F, color=(0,0,0))

            # VEHICULO — todos los valores abajo de su rubro
            pg.insert_text((53, 369), serie, fontsize=8, fontname=F, color=(0,0,0))            # NIV (2x1 con Serie)
            pg.insert_text((53, 403), serie,   fontsize=S, fontname=F, color=(0,0,0))          # Número de Serie
            pg.insert_text((137, 403), modelo, fontsize=S, fontname=F, color=(0,0,0))          # Modelo (año)
            pg.insert_text((188, 403), color,  fontsize=S, fontname=F, color=(0,0,0))          # Color
            pg.insert_text((53, 437), motor,   fontsize=S, fontname=F, color=(0,0,0))          # Número de Motor
            pg.insert_text((138, 437), marca,  fontsize=8, fontname=F, color=(0,0,0))          # Clase y Tipo = Marca
            pg.insert_text((138, 449), linea,  fontsize=8, fontname=F, color=(0,0,0))          # Clase y Tipo = Línea
            pg.insert_text((204, 437), cve,    fontsize=7, fontname=F, color=(0,0,0))          # Clave Vehicular

            # QR izquierdo — apunta a nuestra página
            img_url = _generar_qr_url(folio)
            if img_url:
                buf = BytesIO(); img_url.save(buf, format="PNG"); buf.seek(0)
                pg.insert_image(fitz.Rect(76, 478, 150, 552), pixmap=fitz.Pixmap(buf.read()), overlay=True)

            # QR derecho — texto plano con los datos capturados
            img_datos = _generar_qr_datos(datos)
            if img_datos:
                buf2 = BytesIO(); img_datos.save(buf2, format="PNG"); buf2.seek(0)
                pg.insert_image(fitz.Rect(650, 451, 717, 518), pixmap=fitz.Pixmap(buf2.read()), overlay=True)

            doc.save(out)
            doc.close()
            print(f"[PDF] ✅ {out}")
        else:
            print(f"[PDF] ⚠️ Plantilla no encontrada: {PLANTILLA_PDF}")
            doc = fitz.open(); pg = doc.new_page(width=792, height=612)
            pg.insert_text((50,50), f"PLANTILLA NO ENCONTRADA — Folio: {folio}", fontsize=10)
            doc.save(out); doc.close()
    except Exception as e:
        print(f"[PDF] ❌ Error: {e}")
        doc_fb = fitz.open()
        doc_fb.new_page().insert_text((50,50), f"ERROR - Folio: {folio}", fontsize=12)
        doc_fb.save(out); doc_fb.close()

    return out

def generar_subir_y_guardar_pdf(datos_pdf: dict) -> str:
    folio    = datos_pdf["folio"]
    ruta_pdf = generar_pdf(datos_pdf)
    url_pdf  = subir_pdf_a_storage(ruta_pdf, folio)
    if url_pdf:
        try: supabase.table("folios_registrados").update({"pdf_url": url_pdf}).eq("folio", folio).execute()
        except Exception as e: print(f"[DB] ❌ Error pdf_url: {e}")
    return url_pdf

# ===================== BACKGROUND BOT =====================
async def generar_y_enviar_background(chat_id: int, datos: dict, user_id: int):
    folio = datos["folio"]; nombre = datos["nombre"]
    try:
        pdf_path = await asyncio.to_thread(generar_pdf, datos)
        pdf_url  = await asyncio.to_thread(subir_pdf_a_storage, pdf_path, folio)
        await asyncio.to_thread(lambda: supabase.table("folios_registrados").insert({
            "folio": folio, "marca": datos["marca"], "linea": datos["linea"],
            "anio": datos["anio"], "numero_serie": datos["serie"],
            "numero_motor": datos.get("motor",""),
            "color": datos.get("color",""), "nombre": nombre,
            "cve_vehicular": datos.get("cve_vehicular",""),
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
        try: await bot.send_message(user_id, f"❌ Error al generar el documento: {e}\n\nUse /tlaxcala para reintentar.")
        except Exception: pass

# ===================== BOT FSM — 9 pasos =====================
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
    folios_activos = obtener_folios_usuario(message.from_user.id)
    if folios_activos:
        texto = "📋 FOLIOS ACTIVOS\n" + "─"*28 + "\n\n"; botones = []
        for f in folios_activos:
            if f in timers_activos:
                seg  = max(0, int(TOTAL_MINUTOS_TIMER*60-(datetime.now()-timers_activos[f]["start_time"]).total_seconds()))
                h, m = divmod(seg//60, 60)
                texto += f"Folio: {f}\n{timers_activos[f].get('nombre','')}\n{h}h {m}min restantes\n\n"
            else: texto += f"Folio: {f}\n(sin timer)\n\n"
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
        await message.answer("⚠️ Año inválido. Usa 4 dígitos (ej. 2021):"); return
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
    tz = ZoneInfo(TZ); hoy = datetime.now(tz); ven = hoy + timedelta(days=30)
    datos["fecha_exp"]    = hoy.strftime("%d/%m/%Y")
    datos["fecha_ven"]    = ven.strftime("%d/%m/%Y")
    datos["fecha_exp_dt"] = hoy
    await state.clear()
    await message.answer(f"🔄 Generando permiso...\n📄 Folio: {datos['folio']}\n👤 Titular: {datos['nombre']}")
    asyncio.create_task(generar_y_enviar_background(message.chat.id, datos, message.from_user.id))

@dp.message(lambda m: m.text and m.text.strip().upper().startswith("SERO"))
async def codigo_admin(message: types.Message):
    texto = message.text.strip().upper(); folio = texto.replace("SERO","",1).strip()
    if not folio or not folio.startswith(FOLIO_PREFIJO):
        await message.answer(f"⚠️ Formato: SERO{FOLIO_PREFIJO}XXXXX\n\n📋 Use /tlaxcala para otro permiso."); return
    cancelado = cancelar_timer_folio(folio)
    with suppress(Exception):
        await asyncio.to_thread(lambda: supabase.table("folios_registrados").update({
            "estado_pago": "VALIDADO", "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", folio).execute())
    msg = f"✅ Validación admin\nFolio: {folio}\n" + ("⏹️ Timer cancelado" if cancelado else "⚠️ Timer ya inactivo")
    await message.answer(msg + "\n\n📋 Use /tlaxcala para otro permiso.")

@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    uid = message.from_user.id; folios = obtener_folios_usuario(uid)
    if not folios:
        await message.answer("ℹ️ No tienes folios pendientes.\n\n📋 Use /tlaxcala para generar un permiso."); return
    if len(folios) > 1:
        lista = "\n".join(f"• {f}" for f in folios); pending_comprobantes[uid] = "waiting_folio"
        await message.answer(f"📄 Varios folios activos:\n\n{lista}\n\nResponde con el NÚMERO DE FOLIO.\n\n📋 Use /tlaxcala para otro permiso."); return
    folio = folios[0]; cancelar_timer_folio(folio)
    with suppress(Exception):
        await asyncio.to_thread(lambda: supabase.table("folios_registrados").update({
            "estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", folio).execute())
    await message.answer(f"✅ Comprobante recibido\nFolio: {folio}\n⏹️ Timer detenido.\n\n📋 Use /tlaxcala para otro permiso.")

@dp.message(lambda m: m.from_user.id in pending_comprobantes and pending_comprobantes[m.from_user.id] == "waiting_folio")
async def especificar_folio_comprobante(message: types.Message):
    uid = message.from_user.id; fe = message.text.strip().upper(); fl = obtener_folios_usuario(uid)
    if fe not in fl:
        await message.answer("❌ Folio no en tu lista.\n\n📋 Use /tlaxcala para otro permiso."); return
    cancelar_timer_folio(fe); del pending_comprobantes[uid]
    with suppress(Exception):
        await asyncio.to_thread(lambda: supabase.table("folios_registrados").update({
            "estado": "COMPROBANTE_ENVIADO", "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", fe).execute())
    await message.answer(f"✅ Comprobante asociado.\nFolio: {fe}\n\n📋 Use /tlaxcala para otro permiso.")

@dp.callback_query(lambda c: c.data and c.data.startswith("validar_"))
async def callback_validar(callback: CallbackQuery):
    folio = callback.data.replace("validar_","")
    if folio in timers_activos:
        uid = timers_activos[folio]["user_id"]; nombre = timers_activos[folio].get("nombre","")
        cancelar_timer_folio(folio)
        with suppress(Exception):
            await asyncio.to_thread(lambda: supabase.table("folios_registrados").update({
                "estado_pago":"VALIDADO","fecha_comprobante":datetime.now().isoformat()
            }).eq("folio",folio).execute())
        await callback.answer("✅ Folio validado",show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        try: await bot.send_message(uid, f"✅ PAGO VALIDADO — TLAXCALA\nFolio: {folio}\nTitular: {nombre}\nTu permiso está activo.\n\n📋 Use /tlaxcala para otro permiso.")
        except Exception as e: print(f"[ERROR] notificando usuario: {e}")
    else: await callback.answer("❌ Folio no encontrado en timers activos",show_alert=True)

@dp.callback_query(lambda c: c.data and c.data.startswith("detener_"))
async def callback_detener(callback: CallbackQuery):
    folio = callback.data.replace("detener_","")
    if folio in timers_activos:
        nombre = timers_activos[folio].get("nombre",""); cancelar_timer_folio(folio)
        with suppress(Exception):
            await asyncio.to_thread(lambda: supabase.table("folios_registrados").update({"estado":"TIMER_DETENIDO"}).eq("folio",folio).execute())
        await callback.answer("⏹️ Timer detenido",show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"⏹️ TIMER DETENIDO\nFolio: {folio}\nTitular: {nombre}\n\n📋 Use /tlaxcala para otro permiso.")
    else: await callback.answer("❌ Timer ya no está activo",show_alert=True)

@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    uid = message.from_user.id; folios = obtener_folios_usuario(uid)
    if not folios:
        await message.answer("ℹ️ No hay folios activos.\n\n📋 Use /tlaxcala para generar uno."); return
    lista = []; botones = []
    for f in folios:
        if f in timers_activos:
            seg = max(0, int(TOTAL_MINUTOS_TIMER*60-(datetime.now()-timers_activos[f]["start_time"]).total_seconds()))
            h, m = divmod(seg//60, 60)
            lista.append(f"• {f} — {timers_activos[f].get('nombre','')}\n  {h}h {m}min restantes")
        else: lista.append(f"• {f} (sin timer)")
        botones.append([InlineKeyboardButton(text=f"⏹️ Detener {f}", callback_data=f"detener_{f}")])
    await message.answer(f"📋 FOLIOS ACTIVOS ({len(folios)})\n\n" + "\n\n".join(lista) + "\n\n📋 Use /tlaxcala para otro permiso.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=botones))

@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Gobierno del Estado de Tlaxcala — SMyT.\n\n📋 Use /tlaxcala para generar un permiso.")

# ===================== MARCA / IDENTIDAD PROPIA =====================
# Nombre e imagen del servicio: son propios, no reproducen el logo/escudo ni el
# nombre oficial del portal de gobierno. Cambia BRAND_NOMBRE y las rutas de
# imagen por las tuyas en /static.
BRAND_NOMBRE   = os.getenv("BRAND_NOMBRE", "Gestoría Vehicular Digital")
BRAND_SLOGAN   = os.getenv("BRAND_SLOGAN", "Trámites vehiculares en línea — Tlaxcala")
LOGO_URL       = os.getenv("LOGO_URL", "/static/logo_brand.png")
ESCUDO_URL     = os.getenv("ESCUDO_URL", "/static/logo_brand.png")

# ===================== HTML CSS =====================
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
.sidebar-header img{{height:70px;object-fit:contain;filter:brightness(10);margin-bottom:10px;}}
.sidebar-header p{{margin:6px 0 0;font-size:14px;opacity:.95;font-weight:600;}}
.sidebar ul{{list-style:none;margin:0;padding:10px 0;}}
.sidebar ul li a{{display:flex;align-items:center;gap:12px;padding:14px 20px;color:#000;text-decoration:none;font-size:14px;font-weight:600;transition:.15s;border-bottom:1px solid #f0f0f0;}}
.sidebar ul li a:hover{{background:rgba(138,31,79,.08);}}
.sidebar ul li a i{{color:{ACCENT};width:18px;text-align:center;}}
.sidebar ul li a.danger{{color:#c00;}}.sidebar ul li a.danger i{{color:#c00;}}
.topbar{{background:{C1};color:white;padding:12px 16px;display:flex;align-items:center;gap:14px;position:sticky;top:0;z-index:100;box-shadow:0 2px 8px rgba(0,0,0,.15);}}
.topbar img{{height:38px;object-fit:contain;}}
.topbar .brand-text{{font-weight:700;font-size:15px;letter-spacing:.2px;}}
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
.form-control:focus{{border-color:{C1};outline:none;box-shadow:0 0 0 3px rgba(43,63,107,.1);}}
select.form-control{{appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23666' d='M6 8L1 3h10z'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center;padding-right:32px;}}
.mb-3{{margin-bottom:14px;}}.mb-4{{margin-bottom:20px;}}.mt-3{{margin-top:14px;}}
.btn{{display:inline-flex;align-items:center;justify-content:center;gap:8px;padding:11px 20px;border-radius:8px;font-weight:700;font-size:14px;border:none;cursor:pointer;text-decoration:none;transition:.2s;font-family:inherit;}}
.btn-primary{{background:{GREEN};color:white;width:100%;}}
.btn-primary:hover{{background:#3f7310;}}
.btn-sm{{padding:5px 12px;font-size:11px;border-radius:6px;}}
.btn-outline{{background:white;border:1.5px solid #ddd;color:#444;}}
.btn-outline:hover{{border-color:{C1};color:{C1};}}
.btn-danger{{background:#dc3545;color:white;}}.btn-success{{background:#1a6e2e;color:white;}}
.alert{{padding:12px 14px;border-radius:8px;margin-bottom:14px;font-size:13px;font-weight:600;}}
.alert-ok{{background:#d4edda;color:#155724;border:1px solid #c3e6cb;}}
.alert-err{{background:#f8d7da;color:#721c24;border:1px solid #f5c6cb;}}
.barra-c{{width:100%;height:24px;background:rgba(43,63,107,.12);border-radius:12px;overflow:hidden;margin:8px 0;}}
.barra-p{{height:100%;background:{C1};border-radius:12px;display:flex;align-items:center;justify-content:center;color:white;font-size:11px;font-weight:700;}}
.info-box{{background:#f8f8f8;border-radius:8px;padding:14px;font-size:13px;margin-bottom:14px;line-height:1.7;}}
.cv{{display:inline-block;min-width:50px;max-width:180px;overflow:hidden;text-overflow:ellipsis;cursor:text;padding:2px 4px;border-radius:4px;border:1px solid transparent;color:#333;}}
.cv:hover{{border-color:#ccc;background:#fbf8ff;}}.cv.nv{{color:#ccc;font-style:italic;}}
.cell-input{{border:2px solid {C1};border-radius:4px;padding:3px 6px;font-size:12px;min-width:100px;max-width:220px;outline:none;background:#fbf8ff;}}
.del-btn{{background:#fff;border:1px solid #ccc;color:#c00;border-radius:4px;padding:2px 7px;font-size:11px;cursor:pointer;}}
.del-btn:hover{{background:#c00;color:#fff;}}
.toast-f{{position:fixed;bottom:20px;right:16px;z-index:999;padding:10px 16px;border-radius:8px;font-size:13px;opacity:0;transition:opacity .25s;pointer-events:none;border:1px solid transparent;max-width:260px;}}
.toast-f.show{{opacity:1;}}.toast-f.ok{{background:#e6ffee;border-color:#060;color:#060;}}.toast-f.err{{background:#fff0f0;border-color:#c00;color:#c00;}}
.row-2{{display:grid;grid-template-columns:1fr 1fr;gap:12px;}}
.row-3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;}}
.filter-bar{{background:white;border-radius:12px;padding:14px;box-shadow:0 2px 8px rgba(0,0,0,.08);margin-bottom:14px;display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end;}}
.page-title{{font-size:20px;font-weight:700;color:#1d1d1b;margin-bottom:20px;padding-bottom:14px;border-bottom:1px solid #eee;}}
.modal-overlay{{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:9999;display:flex;align-items:center;justify-content:center;padding:16px;}}
.modal-box{{background:white;border-radius:16px;padding:28px;max-width:360px;width:100%;text-align:center;}}
.dato-row{{display:flex;justify-content:space-between;padding:9px 0;border-bottom:1px solid #f0f0f0;font-size:13px;}}
.dato-row:last-child{{border-bottom:none;}}
.dato-label{{color:#888;font-weight:600;}}
.dato-valor{{font-weight:600;text-align:right;max-width:60%;}}

/* ===== Portal público (menú de servicios + ficha de trámite) ===== */
.tittle-menu{{font-size:22px;font-weight:700;color:#1d1d1b;display:flex;align-items:center;gap:8px;}}
.tittle-sub-menu{{color:{BLUE};font-weight:700;font-size:16px;margin-bottom:6px;}}
.costo-color-text{{color:{GREEN};}}
.card-menu-principal{{border:1px solid {C3};border-radius:14px;background:#f8f9fa;box-shadow:0 4px 16px rgba(0,0,0,.06);}}
.card-menu-principal .card-body{{padding:20px;}}
.text-parrafo{{color:#222;line-height:1.6;}}
.container-fluid-portal{{max-width:1100px;margin:0 auto;padding:24px 16px;}}
.row-portal{{display:flex;flex-wrap:wrap;gap:20px;margin-top:14px;}}
.col-portal-4{{flex:1 1 300px;}}
.col-portal-8{{flex:2 1 480px;}}
.proc-list li{{margin-bottom:10px;}}
"""

FA    = '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.7.2/css/all.min.css">'
ROBOTO = '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap" rel="stylesheet">'
JS_NAV = """<script>
function openNav(){document.getElementById('offsb').classList.add('open');document.getElementById('overlay').classList.add('show');}
function closeNav(){document.getElementById('offsb').classList.remove('open');document.getElementById('overlay').classList.remove('show');}
document.addEventListener('DOMContentLoaded',function(){document.getElementById('overlay').addEventListener('click',closeNav);});
</script>"""

def head(titulo):
    return f"""<!DOCTYPE html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{titulo} — {BRAND_NOMBRE}</title>
<link rel="icon" href="{ESCUDO_URL}" sizes="32x32"/>
{ROBOTO}{FA}<style>{CSS}</style></head><body>"""

def _sidebar_links():
    return """
    <li><a href="/panel/admin"><i class="fa-solid fa-house"></i>Inicio</a></li>
    <li><a href="/panel/folios"><i class="fa-solid fa-list-check"></i>Ver Folios</a></li>
    <li><a href="/panel/registro_admin"><i class="fa-solid fa-file-circle-plus"></i>Registrar Permiso</a></li>
    <li><a href="/panel/crear_usuario"><i class="fa-solid fa-user-plus"></i>Crear Usuario</a></li>
    <li><a href="/panel/tablas"><i class="fa-solid fa-database"></i>Tablas BD</a></li>
    <li><a href="/consulta_folio"><i class="fa-solid fa-magnifying-glass"></i>Consultar Folio</a></li>
    <li><a href="/panel/logout" class="danger"><i class="fa-solid fa-right-from-bracket"></i>Cerrar Sesión</a></li>"""

def navbar():
    links = _sidebar_links()
    return f"""<nav class="sidebar">
  <div class="sidebar-header">
    <img src="{LOGO_URL}" alt="{BRAND_NOMBRE}">
    <p>{BRAND_NOMBRE}</p>
  </div>
  <ul>{links}</ul>
</nav>
<div class="overlay" id="overlay"></div>
<nav class="offcanvas-sb" id="offsb">
  <div class="sidebar-header">
    <img src="{LOGO_URL}" alt="{BRAND_NOMBRE}">
    <p>{BRAND_NOMBRE}</p>
  </div>
  <ul>{links}</ul>
</nav>
<div class="topbar">
  <button class="hamburger" onclick="openNav()"><span></span><span></span><span></span></button>
  <img src="{LOGO_URL}" alt="{BRAND_NOMBRE}">
  <span class="brand-text">{BRAND_NOMBRE}</span>
</div>"""

def admin_bar(seccion):
    return f'<div class="admin-bar"><i class="fa-solid fa-shield-halved"></i> {seccion}</div>'

def footer(scripts=""):
    return f"""{scripts}{JS_NAV}</div></body></html>"""

def page(titulo, seccion, contenido, scripts=""):
    return (head(titulo) + '<div class="layout-container">' + navbar()
            + '<div class="main-content-wrapper">' + admin_bar(seccion)
            + f'<div class="content">{contenido}</div>' + footer(scripts))

def login_html(error=False):
    err = '<div class="alert alert-err"><i class="fa-solid fa-triangle-exclamation"></i> Usuario o contraseña incorrectos</div>' if error else ""
    return f"""<!DOCTYPE html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Acceso — {BRAND_NOMBRE}</title>
<link rel="icon" href="{ESCUDO_URL}" sizes="32x32"/>
{ROBOTO}{FA}
<style>
*{{font-family:'Roboto',sans-serif;box-sizing:border-box;}}
body{{background:{C1};min-height:100vh;margin:0;display:flex;flex-direction:column;}}
.lh{{background:white;padding:12px 20px;text-align:center;border-bottom:4px solid {C3};}}
.lh img{{height:60px;object-fit:contain;}}
.lw{{flex:1;display:flex;align-items:center;justify-content:center;padding:30px 15px;}}
.lc{{background:white;border-radius:16px;padding:32px;max-width:380px;width:100%;box-shadow:0 12px 40px rgba(0,0,0,.3);}}
.le{{text-align:center;margin-bottom:16px;}}.le img{{height:65px;}}
.lt{{text-align:center;font-size:20px;font-weight:700;color:{C1};margin-bottom:4px;}}
.ls{{text-align:center;font-size:12px;color:#777;margin-bottom:22px;}}
.form-label{{font-weight:600;font-size:14px;display:block;margin-bottom:4px;}}
.form-control{{display:block;width:100%;padding:11px 13px;border:1.5px solid #ddd;border-radius:8px;font-size:14px;font-family:inherit;}}
.form-control:focus{{border-color:{C1};outline:none;box-shadow:0 0 0 3px rgba(43,63,107,.1);}}
.mb-3{{margin-bottom:14px;}}.mb-4{{margin-bottom:20px;}}
.alert{{padding:11px 13px;border-radius:8px;font-size:13px;font-weight:600;margin-bottom:14px;}}
.alert-err{{background:#f8d7da;color:#721c24;border:1px solid #f5c6cb;}}
.btn-in{{background:{C1};border:none;color:white;width:100%;padding:13px;font-weight:700;font-size:15px;border-radius:8px;cursor:pointer;font-family:inherit;}}
.btn-in:hover{{background:{C2};}}
.lf{{background:rgba(0,0,0,.2);color:rgba(255,255,255,.7);text-align:center;padding:14px;font-size:12px;}}
</style></head><body>
<div class="lh"><img src="{LOGO_URL}" alt="{BRAND_NOMBRE}"></div>
<div class="lw"><div class="lc">
  <div class="le"><img src="{ESCUDO_URL}" alt="{BRAND_NOMBRE}"></div>
  <div class="lt">{BRAND_NOMBRE}</div>
  <div class="ls">{BRAND_SLOGAN}<br>Sistema Administrativo</div>
  {err}
  <form method="POST" action="/panel/login">
    <div class="mb-3"><label class="form-label">Usuario</label><input type="text" name="username" class="form-control" required autofocus autocomplete="off"></div>
    <div class="mb-4"><label class="form-label">Contraseña</label><input type="password" name="password" class="form-control" required></div>
    <button type="submit" class="btn-in"><i class="fa-solid fa-right-to-bracket"></i> &nbsp;Ingresar al Sistema</button>
  </form>
</div></div>
<div class="lf">{BRAND_NOMBRE} © 2026</div>
</body></html>"""

# ===================== PORTAL PÚBLICO (menú + ficha de trámite) =====================
PORTAL_ITEMS = [
    {"icono": "fa-car-front", "titulo": "Permiso Provisional de Circulación", "ruta": "/portal/permiso-provisional-de-circulacion", "activo": True},
    {"icono": "fa-file-circle-check", "titulo": "Refrendo y/o Tenencia", "ruta": "#", "activo": False},
    {"icono": "fa-file-circle-minus", "titulo": "Baja de Vehículos", "ruta": "#", "activo": False},
    {"icono": "fa-magnifying-glass", "titulo": "Consultar / Validar Folio", "ruta": "/consulta_folio", "activo": True},
]

def portal_navbar():
    items_html = "".join(
        f'<li><a href="{it["ruta"]}"><i class="fa-solid {it["icono"]}"></i>{it["titulo"]}</a></li>'
        for it in PORTAL_ITEMS
    )
    return f"""<nav class="sidebar">
  <div class="sidebar-header">
    <img src="{LOGO_URL}" alt="{BRAND_NOMBRE}">
    <p>{BRAND_NOMBRE}</p>
  </div>
  <ul>
    <li><a href="/portal"><i class="fa-solid fa-house"></i>Inicio</a></li>
    {items_html}
    <li><a href="/panel/login"><i class="fa-solid fa-right-to-bracket"></i>Acceso al Sistema</a></li>
  </ul>
</nav>
<div class="overlay" id="overlay"></div>
<nav class="offcanvas-sb" id="offsb">
  <div class="sidebar-header">
    <img src="{LOGO_URL}" alt="{BRAND_NOMBRE}">
    <p>{BRAND_NOMBRE}</p>
  </div>
  <ul>
    <li><a href="/portal"><i class="fa-solid fa-house"></i>Inicio</a></li>
    {items_html}
    <li><a href="/panel/login"><i class="fa-solid fa-right-to-bracket"></i>Acceso al Sistema</a></li>
  </ul>
</nav>
<div class="topbar">
  <button class="hamburger" onclick="openNav()"><span></span><span></span><span></span></button>
  <img src="{LOGO_URL}" alt="{BRAND_NOMBRE}">
  <span class="brand-text">{BRAND_NOMBRE}</span>
</div>"""

def portal_page(titulo, contenido):
    return (head(titulo) + '<div class="layout-container">' + portal_navbar()
            + '<div class="main-content-wrapper">'
            + f'<div class="container-fluid-portal">{contenido}</div>' + footer())

# ===================== LIFESPAN =====================
_keep_task = None
async def keep_alive():
    while True:
        await asyncio.sleep(600)
        print("[HEARTBEAT] Tlaxcala activo")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    await asyncio.to_thread(_sb_inicializar_folio)
    await bot.delete_webhook(drop_pending_updates=True)
    webhook_url = f"{BASE_URL}/webhook"
    await bot.set_webhook(webhook_url, allowed_updates=["message", "callback_query"])
    _keep_task = asyncio.create_task(keep_alive())
    print(f"[SISTEMA] Tlaxcala v1.0 listo — siguiente folio: {FOLIO_PREFIJO}{_folio_counter['siguiente']}")
    yield
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError): await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Trámites Vehiculares", version="1.0")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "tlaxcala_clave_super_segura_cambiar"))
try: app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
except Exception: pass

# ===================== PORTAL PÚBLICO — RUTAS =====================
@app.get("/portal", response_class=HTMLResponse)
async def portal_home():
    cards = "".join(f"""<a href="{it['ruta']}" class="menu-btn" style="text-align:left;display:flex;align-items:center;gap:14px;padding:18px">
        <i class="fa-solid {it['icono']}" style="font-size:26px"></i>
        <span style="font-size:14px">{it['titulo']}</span>
      </a>""" for it in PORTAL_ITEMS)
    contenido = f"""
    <h4 class="tittle-menu"><i class="fa-solid fa-clipboard-list"></i> Trámites y Servicios</h4>
    <p class="text-parrafo" style="color:#666">{BRAND_SLOGAN}</p>
    <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr))">{cards}</div>
    """
    return HTMLResponse(portal_page("Trámites y Servicios", contenido))

@app.get("/portal/permiso-provisional-de-circulacion", response_class=HTMLResponse)
async def portal_permiso_provisional():
    contenido = f"""
    <div class="row-portal">
      <div class="col-portal-4">
        <div class="card-menu-principal">
          <div class="card-body">
            <h4 class="tittle-menu" style="font-size:18px"><i class="fa-solid fa-car-front"></i> Permiso Provisional de Circulación</h4>
            <hr>
            <h4 class="tittle-sub-menu">Descripción</h4>
            <p class="text-parrafo">Permiso provisional de circulación para vehículos nuevos y usados, mientras se realiza el trámite de placas definitivas.</p>
            <h4 class="tittle-sub-menu">Costo</h4>
            <p class="costo-color-text"><b>$352.00 MXN</b></p>
            <h4 class="tittle-sub-menu">Documento a recibir</h4>
            <p class="text-parrafo">Permiso provisional de circulación en PDF, con folio y código QR de verificación.</p>
            <h4 class="tittle-sub-menu">Vigencia</h4>
            <p class="text-parrafo">30 días naturales a partir de la fecha de expedición.</p>
          </div>
        </div>
      </div>
      <div class="col-portal-8">
        <div class="card-menu-principal mb-3">
          <div class="card-body" style="text-align:center">
            <p class="text-parrafo mb-2">Si deseas iniciar tu trámite, ingresa al sistema y captura los datos de tu vehículo.</p>
            <a href="/panel/login" class="btn btn-primary" style="width:auto;display:inline-flex"><i class="fa-solid fa-right-to-bracket"></i> Iniciar Trámite</a>
          </div>
        </div>
        <div class="card-menu-principal">
          <div class="card-body">
            <h4 class="tittle-sub-menu">Procedimiento</h4>
            <ol class="text-parrafo proc-list">
              <li>Ingresa al sistema con tu usuario y contraseña.</li>
              <li>Captura los datos del vehículo: marca, línea, año, número de serie, número de motor, color y clave vehicular.</li>
              <li>Captura el nombre completo del propietario.</li>
              <li>El sistema genera tu folio y tu permiso en PDF de manera automática.</li>
              <li>Realiza el pago correspondiente y envía tu comprobante dentro de las 36 horas siguientes.</li>
              <li>Una vez validado el pago, tu permiso queda activo y puedes descargarlo o consultarlo con tu folio.</li>
            </ol>
          </div>
        </div>
      </div>
    </div>
    <div class="mt-3"><a href="/portal" class="btn btn-outline btn-sm">← Volver a Trámites y Servicios</a></div>
    """
    return HTMLResponse(portal_page("Permiso Provisional de Circulación", contenido))

# ===================== WEBHOOK =====================
@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        await dp.feed_webhook_update(bot, types.Update(**data))
        return {"ok": True}
    except Exception as e:
        print(f"[WEBHOOK] Error: {e}"); return {"ok": False, "error": str(e)}

# ===================== AUTH =====================
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if request.session.get("admin"): return RedirectResponse(url="/panel/admin", status_code=303)
    if request.session.get("username"): return RedirectResponse(url="/registro_usuario", status_code=303)
    return RedirectResponse(url="/portal", status_code=303)

@app.get("/panel/login", response_class=HTMLResponse)
async def login_get(request: Request):
    if request.session.get("admin"): return RedirectResponse(url="/panel/admin", status_code=303)
    return HTMLResponse(login_html(bool(request.query_params.get("error",""))))

@app.post("/panel/login")
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == ADMIN_USER and password == ADMIN_PASS:
        request.session["admin"] = True; request.session["username"] = username
        return RedirectResponse(url="/panel/admin", status_code=303)
    try:
        res = supabase.table("verificacion_tlaxcala").select("*").eq("username",username).eq("password",password).execute()
        if res.data:
            u = res.data[0]; request.session["admin"] = False
            request.session["username"] = u["username"]; request.session["user_id"] = u.get("id")
            return RedirectResponse(url="/registro_usuario", status_code=303)
    except Exception as e: print(f"[LOGIN] Error: {e}")
    return RedirectResponse(url="/panel/login?error=1", status_code=303)

@app.get("/panel/logout")
async def logout(request: Request):
    request.session.clear(); return RedirectResponse(url="/panel/login", status_code=303)

# ===================== PANEL ADMIN =====================
@app.get("/panel/admin", response_class=HTMLResponse)
async def panel_admin(request: Request):
    if not request.session.get("admin"): return RedirectResponse(url="/panel/login", status_code=303)
    pendientes = 0
    try:
        r = supabase.table("folios_registrados").select("folio").eq("estado_pago","PENDIENTE_PAGO").eq("entidad",ENTIDAD).execute()
        pendientes = len(r.data or [])
    except Exception: pass
    color_pend = "#dc3545" if pendientes else "#1a6e2e"
    contenido = f"""
    <div class="row-2 mb-3">
      <div class="stat-card"><div class="stat-num">{len(timers_activos)}</div><div class="stat-lbl">Timers Activos</div></div>
      <div class="stat-card"><div class="stat-num" style="color:{color_pend}">{pendientes}</div><div class="stat-lbl">Pendientes Pago</div></div>
    </div>
    <div class="stat-card mb-3"><div class="stat-num">{FOLIO_PREFIJO}{_folio_counter['siguiente']}</div><div class="stat-lbl">Siguiente Folio</div></div>
    <div class="grid">
      <a href="/panel/folios" class="menu-btn"><i class="fa-solid fa-list-check"></i><span>Ver Folios</span></a>
      <a href="/panel/registro_admin" class="menu-btn"><i class="fa-solid fa-file-circle-plus"></i><span>Registrar Permiso</span></a>
      <a href="/panel/crear_usuario" class="menu-btn"><i class="fa-solid fa-user-plus"></i><span>Crear Usuario</span></a>
      <a href="/panel/tablas" class="menu-btn"><i class="fa-solid fa-database"></i><span>Tablas BD</span></a>
      <a href="/consulta_folio" class="menu-btn"><i class="fa-solid fa-magnifying-glass"></i><span>Consultar Folio</span></a>
      <a href="/portal" class="menu-btn"><i class="fa-solid fa-globe"></i><span>Ver Portal Público</span></a>
      <a href="/panel/logout" class="menu-btn danger grid-full"><i class="fa-solid fa-right-from-bracket"></i><span>Cerrar Sesión</span></a>
    </div>"""
    return HTMLResponse(page("Panel Admin","Panel de Administración", contenido))

# ===================== FOLIOS =====================
@app.get("/panel/folios", response_class=HTMLResponse)
async def admin_folios(request: Request):
    if not request.session.get("admin"): return RedirectResponse(url="/panel/login", status_code=303)
    filtro  = request.query_params.get("filtro","").strip()
    crit    = request.query_params.get("criterio","folio")
    ep_fil  = request.query_params.get("estado_pago","todos")
    ev_fil  = request.query_params.get("estado_vigencia","todos")
    msg     = request.query_params.get("msg","")
    pdf_url = request.query_params.get("pdf","")
    modal_html = ""
    if pdf_url:
        modal_html = f"""<div class="modal-overlay" id="mD">
  <div class="modal-box">
    <div style="font-size:48px;margin-bottom:12px">📄</div>
    <h2 style="color:{C1};font-size:18px;font-weight:700;margin-bottom:8px">Permiso Generado</h2>
    <p style="color:#666;font-size:13px;margin-bottom:20px">¿Deseas descargar el PDF?</p>
    <div style="display:flex;gap:8px;justify-content:center">
      <a href="{pdf_url}" target="_blank" class="btn btn-primary btn-sm" onclick="document.getElementById('mD').remove()" style="width:auto"><i class="fa-solid fa-download"></i> Descargar</a>
      <button class="btn btn-outline btn-sm" onclick="document.getElementById('mD').remove()">Cerrar</button>
    </div>
  </div>
</div>"""
    try:
        q = supabase.table("folios_registrados").select("*").eq("entidad",ENTIDAD)
        if filtro: q = q.ilike(crit, f"%{filtro}%")
        if ep_fil != "todos": q = q.eq("estado_pago", ep_fil)
        folios = q.order("fecha_expedicion", desc=True).execute().data or []
        tz = ZoneInfo(TZ); hoy = datetime.now(tz).date()
        for f in folios:
            try:
                fv = datetime.fromisoformat(f["fecha_vencimiento"]).date()
                f["estado_calc"] = "VIGENTE" if hoy <= fv else "VENCIDO"
            except: f["estado_calc"] = "ERROR"
        if ev_fil != "todos": folios = [f for f in folios if f.get("estado_calc","") == ev_fil]
    except Exception as e: folios = []; print(f"[FOLIOS] Error: {e}")
    msg_html = f'<div class="alert alert-ok">{msg}</div>' if msg else ""
    filas = ""
    for f in folios:
        pago = f.get("estado_pago","VALIDADO") or "VALIDADO"
        ec   = f.get("estado_calc","")
        bp   = f'<span class="bp bp-p">PEND</span>' if pago=="PENDIENTE_PAGO" else f'<span class="bp bp-v">OK</span>'
        be   = f'<span class="bp bp-vig">VIG</span>' if ec=="VIGENTE" else f'<span class="bp bp-ven">VEN</span>'
        bval = f'<form method="POST" action="/panel/validar/{f["folio"]}" style="display:inline"><button class="btn btn-success btn-sm" onclick="return confirm(\'¿Validar?\')">✅</button></form> ' if pago=="PENDIENTE_PAGO" else ""
        pdf  = f.get("pdf_url","")
        bpdf = f'<a href="{pdf}" target="_blank" class="btn btn-sm" style="background:{C1};color:white">📄</a> ' if pdf else ""
        filas += f"""<tr>
          <td><strong style="color:{C1}">{f.get("folio","")}</strong><br><small style="color:#999">{f.get("creado_por","")}</small></td>
          <td>{f.get("nombre","")[:18]}</td>
          <td>{f.get("marca","")} {f.get("linea","")}<br><small>{f.get("anio","")}</small></td>
          <td>{str(f.get("fecha_expedicion",""))[:10]}<br>{str(f.get("fecha_vencimiento",""))[:10]}</td>
          <td>{be} {bp}</td>
          <td>{bval}{bpdf}<a href="/consulta/{f.get('folio','')}" target="_blank" class="btn btn-sm btn-outline">🔗</a></td>
        </tr>"""
    filtros = f"""<div class="filter-bar">
      <form method="GET" style="display:contents">
        <input type="text" name="filtro" class="form-control" value="{filtro}" placeholder="Buscar...">
        <select name="criterio" class="form-control" style="max-width:100px">
          <option value="folio" {"selected" if crit=="folio" else ""}>Folio</option>
          <option value="nombre" {"selected" if crit=="nombre" else ""}>Nombre</option>
          <option value="numero_serie" {"selected" if crit=="numero_serie" else ""}>Serie</option>
        </select>
        <select name="estado_pago" class="form-control" style="max-width:100px">
          <option value="todos" {"selected" if ep_fil=="todos" else ""}>Todos</option>
          <option value="PENDIENTE_PAGO" {"selected" if ep_fil=="PENDIENTE_PAGO" else ""}>Pendiente</option>
          <option value="VALIDADO" {"selected" if ep_fil=="VALIDADO" else ""}>Validado</option>
        </select>
        <select name="estado_vigencia" class="form-control" style="max-width:100px">
          <option value="todos" {"selected" if ev_fil=="todos" else ""}>Todos</option>
          <option value="VIGENTE" {"selected" if ev_fil=="VIGENTE" else ""}>Vigente</option>
          <option value="VENCIDO" {"selected" if ev_fil=="VENCIDO" else ""}>Vencido</option>
        </select>
        <button type="submit" class="btn btn-primary btn-sm" style="width:auto">Filtrar</button>
        <a href="/panel/folios" class="btn btn-outline btn-sm">✕</a>
      </form>
      <span style="font-size:12px;color:#888">{len(folios)} resultados</span>
    </div>"""
    contenido = f"""{modal_html}
    <p class="page-title">Folios Registrados</p>
    {msg_html}{filtros}
    <div class="tabla-wrap"><table>
      <thead><tr><th>Folio</th><th>Titular</th><th>Vehículo</th><th>Fechas</th><th>Estado</th><th>Acc.</th></tr></thead>
      <tbody>{filas or '<tr><td colspan="6" style="text-align:center;color:#999;padding:20px">Sin folios</td></tr>'}</tbody>
    </table></div>"""
    return HTMLResponse(page("Folios","Folios Registrados", contenido))

@app.post("/panel/validar/{folio}")
async def validar_pago(request: Request, folio: str):
    if not request.session.get("admin"): return RedirectResponse(url="/panel/login", status_code=303)
    folio = folio.strip().upper()
    try:
        supabase.table("folios_registrados").update({"estado_pago":"VALIDADO"}).eq("folio",folio).execute()
        if folio in timers_activos:
            uid = timers_activos[folio]["user_id"]; nombre = timers_activos[folio].get("nombre","")
            cancelar_timer_folio(folio)
            try: await bot.send_message(uid, f"✅ PAGO VALIDADO — TLAXCALA\nFolio: {folio}\nTitular: {nombre}\nTu permiso está activo.\n\n📋 Use /tlaxcala para otro permiso.")
            except Exception: pass
    except Exception as e: print(f"[VALIDAR] Error: {e}")
    from urllib.parse import quote
    return RedirectResponse(url=f"/panel/folios?msg={quote(f'Folio {folio} validado ✅')}", status_code=303)

@app.get("/panel/pdf/{folio}")
async def descargar_pdf_panel(folio: str, request: Request):
    if not request.session.get("admin"): return RedirectResponse(url="/panel/login", status_code=303)
    folio = folio.strip().upper()
    try:
        res = supabase.table("folios_registrados").select("pdf_url").eq("folio",folio).execute()
        if res.data and res.data[0].get("pdf_url"): return RedirectResponse(url=res.data[0]["pdf_url"])
    except Exception: pass
    ruta = os.path.join(OUTPUT_DIR, f"{folio}.pdf")
    if os.path.exists(ruta):
        from fastapi.responses import FileResponse
        return FileResponse(ruta, media_type="application/pdf", filename=f"{folio}_tlaxcala.pdf")
    return HTMLResponse(f"<p>PDF no encontrado.</p><a href='/panel/folios'>← Volver</a>", status_code=404)

# ===================== REGISTRO ADMIN =====================
@app.get("/panel/registro_admin", response_class=HTMLResponse)
async def registro_admin_get(request: Request):
    if not request.session.get("admin"): return RedirectResponse(url="/panel/login", status_code=303)
    tz = ZoneInfo(TZ); hoy = datetime.now(tz).strftime("%Y-%m-%d")
    err = request.query_params.get("error","")
    err_html = f'<div class="alert alert-err">{err}</div>' if err else ""
    contenido = f"""
    <p class="page-title">Registrar Permiso</p>{err_html}
    <div class="form-card">
      <form method="POST" action="/panel/registro_admin">
        <div class="mb-3"><label class="form-label">Folio manual <small style="color:#999;font-weight:400">(vacío = auto)</small></label>
          <input type="text" name="folio" class="form-control" placeholder="{FOLIO_PREFIJO}53314" style="text-transform:uppercase"></div>
        <div class="row-2">
          <div class="mb-3"><label class="form-label">Marca *</label><input type="text" name="marca" class="form-control" required style="text-transform:uppercase"></div>
          <div class="mb-3"><label class="form-label">Línea *</label><input type="text" name="linea" class="form-control" required style="text-transform:uppercase"></div>
        </div>
        <div class="row-2">
          <div class="mb-3"><label class="form-label">Año *</label><input type="text" name="anio" class="form-control" maxlength="4" required></div>
          <div class="mb-3"><label class="form-label">Color *</label><input type="text" name="color" class="form-control" required style="text-transform:uppercase"></div>
        </div>
        <div class="mb-3"><label class="form-label">Núm. Serie / NIV * <small style="color:#999;font-weight:400">(se usa para ambos campos)</small></label><input type="text" name="numero_serie" class="form-control" required style="text-transform:uppercase"></div>
        <div class="mb-3"><label class="form-label">Núm. Motor *</label><input type="text" name="numero_motor" class="form-control" required style="text-transform:uppercase"></div>
        <div class="mb-3"><label class="form-label">Clave Vehicular *</label><input type="text" name="cve_vehicular" class="form-control" required style="text-transform:uppercase"></div>
        <div class="mb-3"><label class="form-label">Nombre del propietario *</label><input type="text" name="nombre" class="form-control" required style="text-transform:uppercase"></div>
        <div class="row-2">
          <div class="mb-3"><label class="form-label">Fecha expedición</label><input type="date" name="fecha_expedicion" class="form-control" value="{hoy}"></div>
          <div class="mb-3"><label class="form-label">Vencimiento <small style="color:#999">(vacío=+30d)</small></label><input type="date" name="fecha_vencimiento" class="form-control"></div>
        </div>
        <button type="submit" class="btn btn-primary mt-3"><i class="fa-solid fa-file-circle-plus"></i> Generar Permiso</button>
      </form>
    </div>"""
    return HTMLResponse(page("Registrar Permiso","Registrar Permiso", contenido))

@app.post("/panel/registro_admin")
async def registro_admin_post(request: Request,
    folio: str = Form(None), marca: str = Form(...), linea: str = Form(...),
    anio: str = Form(...), color: str = Form(""), numero_serie: str = Form(...),
    numero_motor: str = Form(""), cve_vehicular: str = Form(""),
    nombre: str = Form(...), fecha_expedicion: str = Form(None), fecha_vencimiento: str = Form(None)):
    if not request.session.get("admin"): return RedirectResponse(url="/panel/login", status_code=303)
    from urllib.parse import quote
    try:
        tz = ZoneInfo(TZ)
        fg = folio.strip().upper() if folio and folio.strip() else generar_folio()
        fe = datetime.fromisoformat(fecha_expedicion).date() if fecha_expedicion and fecha_expedicion.strip() else datetime.now(tz).date()
        fv = datetime.fromisoformat(fecha_vencimiento).date() if fecha_vencimiento and fecha_vencimiento.strip() else fe + timedelta(days=30)
        datos_pdf = {"folio": fg, "marca": marca.upper(), "linea": linea.upper(), "anio": anio,
            "serie": numero_serie.upper(), "motor": numero_motor.upper(),
            "color": color.upper(), "nombre": nombre.upper(), "cve_vehicular": cve_vehicular.upper(),
            "fecha_exp": fe.strftime("%d/%m/%Y"), "fecha_ven": fv.strftime("%d/%m/%Y"),
            "fecha_exp_dt": datetime.combine(fe, datetime.min.time()).replace(tzinfo=tz)}
        supabase.table("folios_registrados").insert({"folio": fg, "marca": marca.upper(), "linea": linea.upper(),
            "anio": anio, "numero_serie": numero_serie.upper(), "numero_motor": numero_motor.upper(),
            "color": color.upper(), "nombre": nombre.upper(), "cve_vehicular": cve_vehicular.upper(),
            "fecha_expedicion": fe.isoformat(), "fecha_vencimiento": fv.isoformat(), "entidad": ENTIDAD,
            "estado": "ACTIVO", "estado_pago": "VALIDADO", "creado_por": request.session.get("username","admin")}).execute()
        pdf_url = await asyncio.to_thread(generar_subir_y_guardar_pdf, datos_pdf)
        return RedirectResponse(url=f"/panel/folios?msg={quote(f'Permiso {fg} generado ✅')}&pdf={quote(pdf_url)}", status_code=303)
    except Exception as e:
        print(f"[REGISTRO ADMIN] Error: {e}")
        return RedirectResponse(url=f"/panel/registro_admin?error={quote(str(e))}", status_code=303)

# ===================== CREAR USUARIO =====================
@app.get("/panel/crear_usuario", response_class=HTMLResponse)
async def crear_usuario_get(request: Request):
    if not request.session.get("admin"): return RedirectResponse(url="/panel/login", status_code=303)
    msg = request.query_params.get("msg",""); err = request.query_params.get("error","")
    contenido = f"""
    <p class="page-title">Crear Usuario</p>
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
    return HTMLResponse(page("Crear Usuario","Crear Usuario", contenido))

@app.post("/panel/crear_usuario")
async def crear_usuario_post(request: Request,
    username: str = Form(...), password: str = Form(...), folios: int = Form(...)):
    if not request.session.get("admin"): return RedirectResponse(url="/panel/login", status_code=303)
    from urllib.parse import quote
    try:
        existe = supabase.table("verificacion_tlaxcala").select("id").eq("username", username).execute()
        if existe.data: return RedirectResponse(url=f"/panel/crear_usuario?error={quote('El usuario ya existe')}", status_code=303)
        supabase.table("verificacion_tlaxcala").insert({"username": username, "password": password, "folios_asignac": folios, "folios_usados": 0}).execute()
        return RedirectResponse(url=f"/panel/crear_usuario?msg={quote(f'Usuario {username} creado con {folios} folios ✅')}", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/panel/crear_usuario?error={quote(str(e))}", status_code=303)

# ===================== REGISTRO USUARIO 3RO =====================
@app.get("/registro_usuario", response_class=HTMLResponse)
async def registro_usuario_get(request: Request):
    if not request.session.get("username") or request.session.get("admin"): return RedirectResponse(url="/panel/login", status_code=303)
    ud = supabase.table("verificacion_tlaxcala").select("*").eq("username", request.session["username"]).limit(1).execute()
    if not ud.data: return RedirectResponse(url="/panel/login", status_code=303)
    u = ud.data[0]; asig = int(u.get("folios_asignac",0)); usad = int(u.get("folios_usados",0))
    disp = asig - usad; porc = round((usad/asig*100) if asig else 0, 1)
    tz = ZoneInfo(TZ); hoy = datetime.now(tz).strftime("%Y-%m-%d")
    msg = request.query_params.get("msg",""); err = request.query_params.get("error","")
    form_html = f"""<div class="form-card">
      <form method="POST" action="/registro_usuario">
        <div class="row-2">
          <div class="mb-3"><label class="form-label">Marca *</label><input type="text" name="marca" class="form-control" required style="text-transform:uppercase"></div>
          <div class="mb-3"><label class="form-label">Línea *</label><input type="text" name="linea" class="form-control" required style="text-transform:uppercase"></div>
        </div>
        <div class="row-3">
          <div class="mb-3"><label class="form-label">Año *</label><input type="number" name="anio" class="form-control" required></div>
          <div class="mb-3" style="grid-column:span 2"><label class="form-label">Color *</label><input type="text" name="color" class="form-control" required style="text-transform:uppercase"></div>
        </div>
        <div class="mb-3"><label class="form-label">Núm. Serie / NIV * <small style="color:#999;font-weight:400">(se usa para ambos campos)</small></label><input type="text" name="serie" class="form-control" required style="text-transform:uppercase"></div>
        <div class="mb-3"><label class="form-label">Núm. Motor *</label><input type="text" name="motor" class="form-control" required style="text-transform:uppercase"></div>
        <div class="mb-3"><label class="form-label">Clave Vehicular *</label><input type="text" name="cve_vehicular" class="form-control" required style="text-transform:uppercase"></div>
        <div class="mb-3"><label class="form-label">Nombre del propietario *</label><input type="text" name="nombre" class="form-control" required style="text-transform:uppercase"></div>
        <div class="mb-4"><label class="form-label">Fecha inicio vigencia</label><input type="date" name="fecha_inicio" class="form-control" value="{hoy}" min="{hoy}"></div>
        <button type="submit" id="btnReg" class="btn btn-primary">Registrar Folio</button>
      </form>
    </div>""" if disp > 0 else '<div class="alert alert-err">Sin folios disponibles. Contacta al administrador.</div>'
    contenido = f"""
    <p class="page-title">Registrar Permiso</p>
    <div class="form-card mb-3">
      <div style="display:flex;justify-content:space-between;margin-bottom:8px">
        <span style="font-weight:700;font-size:14px">Mis Folios</span>
        <span style="font-size:12px;color:#888">{usad} / {asig}</span>
      </div>
      <div class="barra-c"><div class="barra-p" style="width:{porc}%">{porc}%</div></div>
      <div style="display:flex;justify-content:space-between;font-size:11px;color:#888;margin-top:4px">
        <span>Usados: <strong>{usad}</strong></span><span>Total: <strong>{asig}</strong></span><span>Disponibles: <strong style="color:{C1}">{disp}</strong></span>
      </div>
    </div>
    {"<div class='alert alert-ok'>"+msg+"</div>" if msg else ""}
    {"<div class='alert alert-err'>"+err+"</div>" if err else ""}
    {form_html}
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">
      <a href="/mis_permisos" class="btn btn-outline btn-sm">📋 Mis Permisos</a>
      <a href="/consulta_folio" class="btn btn-outline btn-sm">🔍 Consultar</a>
      <a href="/panel/logout" class="btn btn-danger btn-sm">🚪 Salir</a>
    </div>"""
    scripts = """<script>
document.querySelector('form[action="/registro_usuario"]')&&document.querySelector('form[action="/registro_usuario"]').addEventListener('submit',function(){
  const btn=document.getElementById('btnReg');
  if(btn){btn.disabled=true;btn.textContent='⏳ Generando...';}
  setTimeout(()=>{if(btn){btn.disabled=false;btn.textContent='Registrar Folio';}},12000);
});
</script>"""
    return HTMLResponse(page("Registrar Permiso","Registro de Permisos", contenido, scripts))

@app.post("/registro_usuario")
async def registro_usuario_post(request: Request,
    marca: str = Form(...), linea: str = Form(...),
    anio: str = Form(...), color: str = Form(""),
    serie: str = Form(...), motor: str = Form(""), cve_vehicular: str = Form(""),
    nombre: str = Form(...), fecha_inicio: str = Form(None)):
    if not request.session.get("username") or request.session.get("admin"): return RedirectResponse(url="/panel/login", status_code=303)
    from urllib.parse import quote
    try:
        ud = supabase.table("verificacion_tlaxcala").select("*").eq("username", request.session["username"]).limit(1).execute()
        if not ud.data: return RedirectResponse(url="/panel/login", status_code=303)
        u = ud.data[0]; asig = int(u.get("folios_asignac",0)); usad = int(u.get("folios_usados",0))
        if asig - usad <= 0: return RedirectResponse(url=f"/registro_usuario?error={quote('Sin folios disponibles')}", status_code=303)
        tz = ZoneInfo(TZ)
        fe = datetime.strptime(fecha_inicio, "%Y-%m-%d").replace(tzinfo=tz) if fecha_inicio else datetime.now(tz)
        fv = fe + timedelta(days=30); fg = generar_folio()
        supabase.table("folios_registrados").insert({"folio": fg, "marca": marca.upper(), "linea": linea.upper(),
            "anio": anio, "numero_serie": serie.upper(), "numero_motor": motor.upper(),
            "color": color.upper(), "nombre": nombre.upper(), "cve_vehicular": cve_vehicular.upper(),
            "fecha_expedicion": fe.date().isoformat(), "fecha_vencimiento": fv.date().isoformat(),
            "entidad": ENTIDAD, "estado": "ACTIVO", "estado_pago": "VALIDADO",
            "user_id": request.session.get("user_id"), "creado_por": request.session["username"]}).execute()
        datos_pdf = {"folio": fg, "marca": marca.upper(), "linea": linea.upper(), "anio": anio,
            "serie": serie.upper(), "motor": motor.upper(), "cve_vehicular": cve_vehicular.upper(),
            "color": color.upper(), "nombre": nombre.upper(),
            "fecha_exp": fe.strftime("%d/%m/%Y"), "fecha_ven": fv.strftime("%d/%m/%Y"), "fecha_exp_dt": fe}
        pdf_url = await asyncio.to_thread(generar_subir_y_guardar_pdf, datos_pdf)
        supabase.table("verificacion_tlaxcala").update({"folios_usados": usad+1}).eq("username", request.session["username"]).execute()
        contenido = f"""
        <p class="page-title">✅ Permiso Generado</p>
        <div class="form-card" style="text-align:center">
          <div style="font-size:52px;margin-bottom:12px">📄</div>
          <h2 style="color:{C1};font-size:24px;font-weight:700;margin-bottom:4px">{fg}</h2>
          <div class="info-box" style="text-align:left">
            <strong>Vehículo:</strong> {marca.upper()} {linea.upper()} {anio}<br>
            <strong>Serie/NIV:</strong> {serie.upper()} · <strong>Motor:</strong> {motor.upper()}<br>
            <strong>Clave Vehicular:</strong> {cve_vehicular.upper()}<br>
            <strong>Color:</strong> {color.upper()}<br>
            <strong>Propietario:</strong> {nombre.upper()}<br>
            <strong>Vigencia:</strong> {fe.strftime("%d/%m/%Y")} — {fv.strftime("%d/%m/%Y")}
          </div>
          {"<a href='"+pdf_url+"' target='_blank' class='btn btn-primary mb-3'><i class='fa-solid fa-download'></i> Descargar PDF</a>" if pdf_url else ""}
          <div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap">
            <a href="/mis_permisos" class="btn btn-outline btn-sm">📋 Mis Permisos</a>
            <a href="/registro_usuario" class="btn btn-primary btn-sm" style="width:auto">+ Nuevo</a>
          </div>
        </div>"""
        return HTMLResponse(page("Permiso Generado","Registro Exitoso", contenido))
    except Exception as e:
        print(f"[REG USUARIO] Error: {e}")
        return RedirectResponse(url=f"/registro_usuario?error={quote(str(e))}", status_code=303)

@app.get("/mis_permisos", response_class=HTMLResponse)
async def mis_permisos(request: Request):
    if not request.session.get("username") or request.session.get("admin"): return RedirectResponse(url="/panel/login", status_code=303)
    permisos = supabase.table("folios_registrados").select("*").eq("creado_por", request.session["username"]).order("fecha_expedicion", desc=True).execute().data or []
    tz = ZoneInfo(TZ); hoy = datetime.now(tz).date()
    for p in permisos:
        try:
            fv = datetime.fromisoformat(p["fecha_vencimiento"]).date()
            fe = datetime.fromisoformat(p["fecha_expedicion"]).date()
            p["fe_fmt"] = fe.strftime("%d/%m/%Y"); p["estado_calc"] = "VIGENTE" if hoy <= fv else "VENCIDO"
        except: p["fe_fmt"] = p["estado_calc"] = "ERROR"
    ud = supabase.table("verificacion_tlaxcala").select("folios_asignac,folios_usados").eq("username", request.session["username"]).limit(1).execute().data
    ud = ud[0] if ud else {"folios_asignac":0,"folios_usados":0}
    asig = int(ud.get("folios_asignac",0)); usad = int(ud.get("folios_usados",0))
    vig  = len([p for p in permisos if p.get("estado_calc")=="VIGENTE"])
    filas = ""
    for p in permisos:
        ec  = p.get("estado_calc","")
        be  = f'<span class="bp bp-vig">VIG</span>' if ec=="VIGENTE" else f'<span class="bp bp-ven">VEN</span>'
        pdf = p.get("pdf_url","")
        btn = f'<a href="{pdf}" target="_blank" class="btn btn-sm" style="background:{C1};color:white">📥</a> ' if pdf else ""
        filas += f"""<tr>
          <td><strong style="color:{C1}">{p.get("folio","")}</strong></td>
          <td>{p.get("marca","")} {p.get("linea","")}<br><small>{p.get("anio","")}</small></td>
          <td style="font-size:11px">{p.get("numero_serie","")}</td>
          <td>{p.get("fe_fmt","")}</td><td>{be}</td>
          <td>{btn}<a href="/consulta/{p.get('folio','')}" target="_blank" class="btn btn-sm btn-outline">🔗</a></td>
        </tr>"""
    contenido = f"""
    <p class="page-title">📋 Mis Permisos</p>
    <div class="grid mb-3">
      <div class="stat-card"><div class="stat-num">{asig}</div><div class="stat-lbl">Asignados</div></div>
      <div class="stat-card"><div class="stat-num">{asig-usad}</div><div class="stat-lbl">Disponibles</div></div>
      <div class="stat-card"><div class="stat-num" style="color:#1a6e2e">{vig}</div><div class="stat-lbl">Vigentes</div></div>
      <div class="stat-card"><div class="stat-num" style="color:{C1}">{len(permisos)}</div><div class="stat-lbl">Total</div></div>
    </div>
    <div class="tabla-wrap"><table>
      <thead><tr><th>Folio</th><th>Vehículo</th><th>Serie</th><th>Fecha</th><th>Estado</th><th>Acc.</th></tr></thead>
      <tbody>{filas or '<tr><td colspan="6" style="text-align:center;color:#999;padding:20px">Sin permisos</td></tr>'}</tbody>
    </table></div>
    <div style="display:flex;gap:8px;margin-top:12px">
      <a href="/registro_usuario" class="btn btn-primary btn-sm" style="width:auto">+ Nuevo</a>
      <a href="/panel/logout" class="btn btn-danger btn-sm">🚪 Salir</a>
    </div>"""
    return HTMLResponse(page("Mis Permisos","Mis Permisos", contenido))

# ===================== CONSULTA PÚBLICA =====================
@app.get("/consulta_folio", response_class=HTMLResponse)
async def consulta_folio_form(request: Request):
    contenido = f"""
    <p class="page-title">🔍 Consultar Folio</p>
    <div class="form-card">
      <form method="POST" action="/consulta_folio">
        <div class="mb-3"><label class="form-label">Número de Folio</label>
          <input type="text" name="folio" class="form-control" placeholder="{FOLIO_PREFIJO}53314" required autofocus style="text-transform:uppercase"></div>
        <button type="submit" class="btn btn-primary"><i class="fa-solid fa-magnifying-glass"></i> Buscar</button>
      </form>
    </div>"""
    return HTMLResponse(page("Consultar Folio","Consultar Folio", contenido))

@app.post("/consulta_folio")
async def consulta_folio_post(request: Request, folio: str = Form(...)):
    return RedirectResponse(url=f"/consulta/{folio.strip().upper()}", status_code=303)

@app.get("/consulta/{folio}", response_class=HTMLResponse)
async def consulta_publica(folio: str):
    folio = folio.strip().upper()

    def _row(label, valor):
        return f'<div class="dato-row"><span class="dato-label">{label}</span><span class="dato-valor">{valor or "—"}</span></div>'

    try:
        res = supabase.table("folios_registrados").select("*").eq("folio", folio).execute()

        if not res.data:
            badge = f"""<div style="background:#c0392b;color:white;padding:16px 18px;border-radius:10px;font-size:15px;font-weight:700;text-align:center;margin-bottom:18px">
              <i class="fa-solid fa-circle-xmark" style="font-size:24px;display:block;margin-bottom:6px"></i>
              EL FOLIO {folio} NO ESTÁ REGISTRADO
            </div>"""
            datos_html = ""
        else:
            f = res.data[0]; tz = ZoneInfo(TZ); hoy = datetime.now(tz).date()
            try:
                fv = datetime.fromisoformat(f["fecha_vencimiento"]).date()
                fe = datetime.fromisoformat(f["fecha_expedicion"]).date()
                vigente = hoy <= fv
            except:
                vigente = False; fe = fv = None

            if vigente:
                badge = f"""<div style="background:#1a6e2e;color:white;padding:16px 18px;border-radius:10px;font-size:15px;font-weight:700;text-align:center;margin-bottom:18px">
                  <i class="fa-solid fa-circle-check" style="font-size:24px;display:block;margin-bottom:6px"></i>
                  EL FOLIO {folio} ESTÁ VIGENTE
                </div>"""
            else:
                badge = f"""<div style="background:#b38b00;color:white;padding:16px 18px;border-radius:10px;font-size:15px;font-weight:700;text-align:center;margin-bottom:18px">
                  <i class="fa-solid fa-clock" style="font-size:24px;display:block;margin-bottom:6px"></i>
                  EL FOLIO {folio} ESTÁ VENCIDO
                </div>"""

            datos_html = f"""
            <div style="background:white;border:1px solid {C3};border-radius:14px;padding:16px;box-shadow:0 4px 16px rgba(0,0,0,.06);margin-bottom:14px">
              <div style="color:{BLUE};font-weight:700;font-size:15px;padding-bottom:10px;margin-bottom:8px;border-bottom:1px solid #eee">
                <i class="fa-solid fa-car" style="color:{ACCENT}"></i> Datos del Vehículo
              </div>
              <div>
                {_row("Marca",  f.get("marca",""))}
                {_row("Línea",  f.get("linea",""))}
                {_row("Modelo (Año)", f.get("anio",""))}
                {_row("Núm. Serie / NIV",  f.get("numero_serie",""))}
                {_row("Núm. Motor",  f.get("numero_motor",""))}
                {_row("Clave Vehicular", f.get("cve_vehicular",""))}
                {_row("Color",  f.get("color",""))}
              </div>
            </div>
            <div style="background:white;border:1px solid {C3};border-radius:14px;padding:16px;box-shadow:0 4px 16px rgba(0,0,0,.06);margin-bottom:14px">
              <div style="color:{BLUE};font-weight:700;font-size:15px;padding-bottom:10px;margin-bottom:8px;border-bottom:1px solid #eee">
                <i class="fa-solid fa-file-shield" style="color:{ACCENT}"></i> Datos del Permiso
              </div>
              <div>
                {_row("Folio",  f'<span style="color:{C1};font-weight:700">{folio}</span>')}
                {_row("Propietario", f.get("nombre",""))}
                {_row("Fecha de Expedición",  fe.strftime("%d/%m/%Y") if fe else "—")}
                {_row("Fecha de Vencimiento", fv.strftime("%d/%m/%Y") if fv else "—")}
              </div>
            </div>"""

        html = f"""<!DOCTYPE html><html lang="en" data-beasties-container><head><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <meta charset="utf-8">
  <title>GOB TLAX Oficina virtual de TrÃ¡mites y Servicios</title>
  <base href="/">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link rel="icon" type="image/x-icon" href="logo.ico">
  <style>@font-face{font-family:'Roboto';font-style:normal;font-weight:300;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3GUBGEe.woff2) format('woff2');unicode-range:U+0460-052F, U+1C80-1C8A, U+20B4, U+2DE0-2DFF, U+A640-A69F, U+FE2E-FE2F;}@font-face{font-family:'Roboto';font-style:normal;font-weight:300;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3iUBGEe.woff2) format('woff2');unicode-range:U+0301, U+0400-045F, U+0490-0491, U+04B0-04B1, U+2116;}@font-face{font-family:'Roboto';font-style:normal;font-weight:300;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3CUBGEe.woff2) format('woff2');unicode-range:U+1F00-1FFF;}@font-face{font-family:'Roboto';font-style:normal;font-weight:300;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3-UBGEe.woff2) format('woff2');unicode-range:U+0370-0377, U+037A-037F, U+0384-038A, U+038C, U+038E-03A1, U+03A3-03FF;}@font-face{font-family:'Roboto';font-style:normal;font-weight:300;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMawCUBGEe.woff2) format('woff2');unicode-range:U+0302-0303, U+0305, U+0307-0308, U+0310, U+0312, U+0315, U+031A, U+0326-0327, U+032C, U+032F-0330, U+0332-0333, U+0338, U+033A, U+0346, U+034D, U+0391-03A1, U+03A3-03A9, U+03B1-03C9, U+03D1, U+03D5-03D6, U+03F0-03F1, U+03F4-03F5, U+2016-2017, U+2034-2038, U+203C, U+2040, U+2043, U+2047, U+2050, U+2057, U+205F, U+2070-2071, U+2074-208E, U+2090-209C, U+20D0-20DC, U+20E1, U+20E5-20EF, U+2100-2112, U+2114-2115, U+2117-2121, U+2123-214F, U+2190, U+2192, U+2194-21AE, U+21B0-21E5, U+21F1-21F2, U+21F4-2211, U+2213-2214, U+2216-22FF, U+2308-230B, U+2310, U+2319, U+231C-2321, U+2336-237A, U+237C, U+2395, U+239B-23B7, U+23D0, U+23DC-23E1, U+2474-2475, U+25AF, U+25B3, U+25B7, U+25BD, U+25C1, U+25CA, U+25CC, U+25FB, U+266D-266F, U+27C0-27FF, U+2900-2AFF, U+2B0E-2B11, U+2B30-2B4C, U+2BFE, U+3030, U+FF5B, U+FF5D, U+1D400-1D7FF, U+1EE00-1EEFF;}@font-face{font-family:'Roboto';font-style:normal;font-weight:300;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMaxKUBGEe.woff2) format('woff2');unicode-range:U+0001-000C, U+000E-001F, U+007F-009F, U+20DD-20E0, U+20E2-20E4, U+2150-218F, U+2190, U+2192, U+2194-2199, U+21AF, U+21E6-21F0, U+21F3, U+2218-2219, U+2299, U+22C4-22C6, U+2300-243F, U+2440-244A, U+2460-24FF, U+25A0-27BF, U+2800-28FF, U+2921-2922, U+2981, U+29BF, U+29EB, U+2B00-2BFF, U+4DC0-4DFF, U+FFF9-FFFB, U+10140-1018E, U+10190-1019C, U+101A0, U+101D0-101FD, U+102E0-102FB, U+10E60-10E7E, U+1D2C0-1D2D3, U+1D2E0-1D37F, U+1F000-1F0FF, U+1F100-1F1AD, U+1F1E6-1F1FF, U+1F30D-1F30F, U+1F315, U+1F31C, U+1F31E, U+1F320-1F32C, U+1F336, U+1F378, U+1F37D, U+1F382, U+1F393-1F39F, U+1F3A7-1F3A8, U+1F3AC-1F3AF, U+1F3C2, U+1F3C4-1F3C6, U+1F3CA-1F3CE, U+1F3D4-1F3E0, U+1F3ED, U+1F3F1-1F3F3, U+1F3F5-1F3F7, U+1F408, U+1F415, U+1F41F, U+1F426, U+1F43F, U+1F441-1F442, U+1F444, U+1F446-1F449, U+1F44C-1F44E, U+1F453, U+1F46A, U+1F47D, U+1F4A3, U+1F4B0, U+1F4B3, U+1F4B9, U+1F4BB, U+1F4BF, U+1F4C8-1F4CB, U+1F4D6, U+1F4DA, U+1F4DF, U+1F4E3-1F4E6, U+1F4EA-1F4ED, U+1F4F7, U+1F4F9-1F4FB, U+1F4FD-1F4FE, U+1F503, U+1F507-1F50B, U+1F50D, U+1F512-1F513, U+1F53E-1F54A, U+1F54F-1F5FA, U+1F610, U+1F650-1F67F, U+1F687, U+1F68D, U+1F691, U+1F694, U+1F698, U+1F6AD, U+1F6B2, U+1F6B9-1F6BA, U+1F6BC, U+1F6C6-1F6CF, U+1F6D3-1F6D7, U+1F6E0-1F6EA, U+1F6F0-1F6F3, U+1F6F7-1F6FC, U+1F700-1F7FF, U+1F800-1F80B, U+1F810-1F847, U+1F850-1F859, U+1F860-1F887, U+1F890-1F8AD, U+1F8B0-1F8BB, U+1F8C0-1F8C1, U+1F900-1F90B, U+1F93B, U+1F946, U+1F984, U+1F996, U+1F9E9, U+1FA00-1FA6F, U+1FA70-1FA7C, U+1FA80-1FA89, U+1FA8F-1FAC6, U+1FACE-1FADC, U+1FADF-1FAE9, U+1FAF0-1FAF8, U+1FB00-1FBFF;}@font-face{font-family:'Roboto';font-style:normal;font-weight:300;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3OUBGEe.woff2) format('woff2');unicode-range:U+0102-0103, U+0110-0111, U+0128-0129, U+0168-0169, U+01A0-01A1, U+01AF-01B0, U+0300-0301, U+0303-0304, U+0308-0309, U+0323, U+0329, U+1EA0-1EF9, U+20AB;}@font-face{font-family:'Roboto';font-style:normal;font-weight:300;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3KUBGEe.woff2) format('woff2');unicode-range:U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF;}@font-face{font-family:'Roboto';font-style:normal;font-weight:300;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3yUBA.woff2) format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD;}@font-face{font-family:'Roboto';font-style:normal;font-weight:400;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3GUBGEe.woff2) format('woff2');unicode-range:U+0460-052F, U+1C80-1C8A, U+20B4, U+2DE0-2DFF, U+A640-A69F, U+FE2E-FE2F;}@font-face{font-family:'Roboto';font-style:normal;font-weight:400;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3iUBGEe.woff2) format('woff2');unicode-range:U+0301, U+0400-045F, U+0490-0491, U+04B0-04B1, U+2116;}@font-face{font-family:'Roboto';font-style:normal;font-weight:400;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3CUBGEe.woff2) format('woff2');unicode-range:U+1F00-1FFF;}@font-face{font-family:'Roboto';font-style:normal;font-weight:400;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3-UBGEe.woff2) format('woff2');unicode-range:U+0370-0377, U+037A-037F, U+0384-038A, U+038C, U+038E-03A1, U+03A3-03FF;}@font-face{font-family:'Roboto';font-style:normal;font-weight:400;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMawCUBGEe.woff2) format('woff2');unicode-range:U+0302-0303, U+0305, U+0307-0308, U+0310, U+0312, U+0315, U+031A, U+0326-0327, U+032C, U+032F-0330, U+0332-0333, U+0338, U+033A, U+0346, U+034D, U+0391-03A1, U+03A3-03A9, U+03B1-03C9, U+03D1, U+03D5-03D6, U+03F0-03F1, U+03F4-03F5, U+2016-2017, U+2034-2038, U+203C, U+2040, U+2043, U+2047, U+2050, U+2057, U+205F, U+2070-2071, U+2074-208E, U+2090-209C, U+20D0-20DC, U+20E1, U+20E5-20EF, U+2100-2112, U+2114-2115, U+2117-2121, U+2123-214F, U+2190, U+2192, U+2194-21AE, U+21B0-21E5, U+21F1-21F2, U+21F4-2211, U+2213-2214, U+2216-22FF, U+2308-230B, U+2310, U+2319, U+231C-2321, U+2336-237A, U+237C, U+2395, U+239B-23B7, U+23D0, U+23DC-23E1, U+2474-2475, U+25AF, U+25B3, U+25B7, U+25BD, U+25C1, U+25CA, U+25CC, U+25FB, U+266D-266F, U+27C0-27FF, U+2900-2AFF, U+2B0E-2B11, U+2B30-2B4C, U+2BFE, U+3030, U+FF5B, U+FF5D, U+1D400-1D7FF, U+1EE00-1EEFF;}@font-face{font-family:'Roboto';font-style:normal;font-weight:400;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMaxKUBGEe.woff2) format('woff2');unicode-range:U+0001-000C, U+000E-001F, U+007F-009F, U+20DD-20E0, U+20E2-20E4, U+2150-218F, U+2190, U+2192, U+2194-2199, U+21AF, U+21E6-21F0, U+21F3, U+2218-2219, U+2299, U+22C4-22C6, U+2300-243F, U+2440-244A, U+2460-24FF, U+25A0-27BF, U+2800-28FF, U+2921-2922, U+2981, U+29BF, U+29EB, U+2B00-2BFF, U+4DC0-4DFF, U+FFF9-FFFB, U+10140-1018E, U+10190-1019C, U+101A0, U+101D0-101FD, U+102E0-102FB, U+10E60-10E7E, U+1D2C0-1D2D3, U+1D2E0-1D37F, U+1F000-1F0FF, U+1F100-1F1AD, U+1F1E6-1F1FF, U+1F30D-1F30F, U+1F315, U+1F31C, U+1F31E, U+1F320-1F32C, U+1F336, U+1F378, U+1F37D, U+1F382, U+1F393-1F39F, U+1F3A7-1F3A8, U+1F3AC-1F3AF, U+1F3C2, U+1F3C4-1F3C6, U+1F3CA-1F3CE, U+1F3D4-1F3E0, U+1F3ED, U+1F3F1-1F3F3, U+1F3F5-1F3F7, U+1F408, U+1F415, U+1F41F, U+1F426, U+1F43F, U+1F441-1F442, U+1F444, U+1F446-1F449, U+1F44C-1F44E, U+1F453, U+1F46A, U+1F47D, U+1F4A3, U+1F4B0, U+1F4B3, U+1F4B9, U+1F4BB, U+1F4BF, U+1F4C8-1F4CB, U+1F4D6, U+1F4DA, U+1F4DF, U+1F4E3-1F4E6, U+1F4EA-1F4ED, U+1F4F7, U+1F4F9-1F4FB, U+1F4FD-1F4FE, U+1F503, U+1F507-1F50B, U+1F50D, U+1F512-1F513, U+1F53E-1F54A, U+1F54F-1F5FA, U+1F610, U+1F650-1F67F, U+1F687, U+1F68D, U+1F691, U+1F694, U+1F698, U+1F6AD, U+1F6B2, U+1F6B9-1F6BA, U+1F6BC, U+1F6C6-1F6CF, U+1F6D3-1F6D7, U+1F6E0-1F6EA, U+1F6F0-1F6F3, U+1F6F7-1F6FC, U+1F700-1F7FF, U+1F800-1F80B, U+1F810-1F847, U+1F850-1F859, U+1F860-1F887, U+1F890-1F8AD, U+1F8B0-1F8BB, U+1F8C0-1F8C1, U+1F900-1F90B, U+1F93B, U+1F946, U+1F984, U+1F996, U+1F9E9, U+1FA00-1FA6F, U+1FA70-1FA7C, U+1FA80-1FA89, U+1FA8F-1FAC6, U+1FACE-1FADC, U+1FADF-1FAE9, U+1FAF0-1FAF8, U+1FB00-1FBFF;}@font-face{font-family:'Roboto';font-style:normal;font-weight:400;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3OUBGEe.woff2) format('woff2');unicode-range:U+0102-0103, U+0110-0111, U+0128-0129, U+0168-0169, U+01A0-01A1, U+01AF-01B0, U+0300-0301, U+0303-0304, U+0308-0309, U+0323, U+0329, U+1EA0-1EF9, U+20AB;}@font-face{font-family:'Roboto';font-style:normal;font-weight:400;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3KUBGEe.woff2) format('woff2');unicode-range:U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF;}@font-face{font-family:'Roboto';font-style:normal;font-weight:400;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3yUBA.woff2) format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD;}@font-face{font-family:'Roboto';font-style:normal;font-weight:500;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3GUBGEe.woff2) format('woff2');unicode-range:U+0460-052F, U+1C80-1C8A, U+20B4, U+2DE0-2DFF, U+A640-A69F, U+FE2E-FE2F;}@font-face{font-family:'Roboto';font-style:normal;font-weight:500;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3iUBGEe.woff2) format('woff2');unicode-range:U+0301, U+0400-045F, U+0490-0491, U+04B0-04B1, U+2116;}@font-face{font-family:'Roboto';font-style:normal;font-weight:500;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3CUBGEe.woff2) format('woff2');unicode-range:U+1F00-1FFF;}@font-face{font-family:'Roboto';font-style:normal;font-weight:500;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3-UBGEe.woff2) format('woff2');unicode-range:U+0370-0377, U+037A-037F, U+0384-038A, U+038C, U+038E-03A1, U+03A3-03FF;}@font-face{font-family:'Roboto';font-style:normal;font-weight:500;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMawCUBGEe.woff2) format('woff2');unicode-range:U+0302-0303, U+0305, U+0307-0308, U+0310, U+0312, U+0315, U+031A, U+0326-0327, U+032C, U+032F-0330, U+0332-0333, U+0338, U+033A, U+0346, U+034D, U+0391-03A1, U+03A3-03A9, U+03B1-03C9, U+03D1, U+03D5-03D6, U+03F0-03F1, U+03F4-03F5, U+2016-2017, U+2034-2038, U+203C, U+2040, U+2043, U+2047, U+2050, U+2057, U+205F, U+2070-2071, U+2074-208E, U+2090-209C, U+20D0-20DC, U+20E1, U+20E5-20EF, U+2100-2112, U+2114-2115, U+2117-2121, U+2123-214F, U+2190, U+2192, U+2194-21AE, U+21B0-21E5, U+21F1-21F2, U+21F4-2211, U+2213-2214, U+2216-22FF, U+2308-230B, U+2310, U+2319, U+231C-2321, U+2336-237A, U+237C, U+2395, U+239B-23B7, U+23D0, U+23DC-23E1, U+2474-2475, U+25AF, U+25B3, U+25B7, U+25BD, U+25C1, U+25CA, U+25CC, U+25FB, U+266D-266F, U+27C0-27FF, U+2900-2AFF, U+2B0E-2B11, U+2B30-2B4C, U+2BFE, U+3030, U+FF5B, U+FF5D, U+1D400-1D7FF, U+1EE00-1EEFF;}@font-face{font-family:'Roboto';font-style:normal;font-weight:500;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMaxKUBGEe.woff2) format('woff2');unicode-range:U+0001-000C, U+000E-001F, U+007F-009F, U+20DD-20E0, U+20E2-20E4, U+2150-218F, U+2190, U+2192, U+2194-2199, U+21AF, U+21E6-21F0, U+21F3, U+2218-2219, U+2299, U+22C4-22C6, U+2300-243F, U+2440-244A, U+2460-24FF, U+25A0-27BF, U+2800-28FF, U+2921-2922, U+2981, U+29BF, U+29EB, U+2B00-2BFF, U+4DC0-4DFF, U+FFF9-FFFB, U+10140-1018E, U+10190-1019C, U+101A0, U+101D0-101FD, U+102E0-102FB, U+10E60-10E7E, U+1D2C0-1D2D3, U+1D2E0-1D37F, U+1F000-1F0FF, U+1F100-1F1AD, U+1F1E6-1F1FF, U+1F30D-1F30F, U+1F315, U+1F31C, U+1F31E, U+1F320-1F32C, U+1F336, U+1F378, U+1F37D, U+1F382, U+1F393-1F39F, U+1F3A7-1F3A8, U+1F3AC-1F3AF, U+1F3C2, U+1F3C4-1F3C6, U+1F3CA-1F3CE, U+1F3D4-1F3E0, U+1F3ED, U+1F3F1-1F3F3, U+1F3F5-1F3F7, U+1F408, U+1F415, U+1F41F, U+1F426, U+1F43F, U+1F441-1F442, U+1F444, U+1F446-1F449, U+1F44C-1F44E, U+1F453, U+1F46A, U+1F47D, U+1F4A3, U+1F4B0, U+1F4B3, U+1F4B9, U+1F4BB, U+1F4BF, U+1F4C8-1F4CB, U+1F4D6, U+1F4DA, U+1F4DF, U+1F4E3-1F4E6, U+1F4EA-1F4ED, U+1F4F7, U+1F4F9-1F4FB, U+1F4FD-1F4FE, U+1F503, U+1F507-1F50B, U+1F50D, U+1F512-1F513, U+1F53E-1F54A, U+1F54F-1F5FA, U+1F610, U+1F650-1F67F, U+1F687, U+1F68D, U+1F691, U+1F694, U+1F698, U+1F6AD, U+1F6B2, U+1F6B9-1F6BA, U+1F6BC, U+1F6C6-1F6CF, U+1F6D3-1F6D7, U+1F6E0-1F6EA, U+1F6F0-1F6F3, U+1F6F7-1F6FC, U+1F700-1F7FF, U+1F800-1F80B, U+1F810-1F847, U+1F850-1F859, U+1F860-1F887, U+1F890-1F8AD, U+1F8B0-1F8BB, U+1F8C0-1F8C1, U+1F900-1F90B, U+1F93B, U+1F946, U+1F984, U+1F996, U+1F9E9, U+1FA00-1FA6F, U+1FA70-1FA7C, U+1FA80-1FA89, U+1FA8F-1FAC6, U+1FACE-1FADC, U+1FADF-1FAE9, U+1FAF0-1FAF8, U+1FB00-1FBFF;}@font-face{font-family:'Roboto';font-style:normal;font-weight:500;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3OUBGEe.woff2) format('woff2');unicode-range:U+0102-0103, U+0110-0111, U+0128-0129, U+0168-0169, U+01A0-01A1, U+01AF-01B0, U+0300-0301, U+0303-0304, U+0308-0309, U+0323, U+0329, U+1EA0-1EF9, U+20AB;}@font-face{font-family:'Roboto';font-style:normal;font-weight:500;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3KUBGEe.woff2) format('woff2');unicode-range:U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF, U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB, U+20AD-20C0, U+2113, U+2C60-2C7F, U+A720-A7FF;}@font-face{font-family:'Roboto';font-style:normal;font-weight:500;font-stretch:100%;font-display:swap;src:url(https://fonts.gstatic.com/s/roboto/v50/KFO7CnqEu92Fr1ME7kSn66aGLdTylUAMa3yUBA.woff2) format('woff2');unicode-range:U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC, U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215, U+FEFF, U+FFFD;}</style>
  <style>@font-face{font-family:'Material Icons';font-style:normal;font-weight:400;src:url(https://fonts.gstatic.com/s/materialicons/v145/flUhRq6tzZclQEJ-Vdg-IuiaDsNc.woff2) format('woff2');}.material-icons{font-family:'Material Icons';font-weight:normal;font-style:normal;font-size:24px;line-height:1;letter-spacing:normal;text-transform:none;display:inline-block;white-space:nowrap;word-wrap:normal;direction:ltr;-webkit-font-feature-settings:'liga';-webkit-font-smoothing:antialiased;}</style>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">

<style>html{--mat-sys-background: #faf9fd;--mat-sys-error: #ba1a1a;--mat-sys-error-container: #ffdad6;--mat-sys-inverse-on-surface: #f2f0f4;--mat-sys-inverse-primary: #abc7ff;--mat-sys-inverse-surface: #2f3033;--mat-sys-on-background: #1a1b1f;--mat-sys-on-error: #ffffff;--mat-sys-on-error-container: #93000a;--mat-sys-on-primary: #ffffff;--mat-sys-on-primary-container: #00458f;--mat-sys-on-primary-fixed: #001b3f;--mat-sys-on-primary-fixed-variant: #00458f;--mat-sys-on-secondary: #ffffff;--mat-sys-on-secondary-container: #3e4759;--mat-sys-on-secondary-fixed: #131c2b;--mat-sys-on-secondary-fixed-variant: #3e4759;--mat-sys-on-surface: #1a1b1f;--mat-sys-on-surface-variant: #44474e;--mat-sys-on-tertiary: #ffffff;--mat-sys-on-tertiary-container: #0000ef;--mat-sys-on-tertiary-fixed: #00006e;--mat-sys-on-tertiary-fixed-variant: #0000ef;--mat-sys-outline: #74777f;--mat-sys-outline-variant: #c4c6d0;--mat-sys-primary: #005cbb;--mat-sys-primary-container: #d7e3ff;--mat-sys-primary-fixed: #d7e3ff;--mat-sys-primary-fixed-dim: #abc7ff;--mat-sys-scrim: #000000;--mat-sys-secondary: #565e71;--mat-sys-secondary-container: #dae2f9;--mat-sys-secondary-fixed: #dae2f9;--mat-sys-secondary-fixed-dim: #bec6dc;--mat-sys-shadow: #000000;--mat-sys-surface: #faf9fd;--mat-sys-surface-bright: #faf9fd;--mat-sys-surface-container: #efedf0;--mat-sys-surface-container-high: #e9e7eb;--mat-sys-surface-container-highest: #e3e2e6;--mat-sys-surface-container-low: #f4f3f6;--mat-sys-surface-container-lowest: #ffffff;--mat-sys-surface-dim: #dbd9dd;--mat-sys-surface-tint: #005cbb;--mat-sys-surface-variant: #e0e2ec;--mat-sys-tertiary: #343dff;--mat-sys-tertiary-container: #e0e0ff;--mat-sys-tertiary-fixed: #e0e0ff;--mat-sys-tertiary-fixed-dim: #bec2ff;--mat-sys-neutral-variant20: #2d3038;--mat-sys-neutral10: #1a1b1f}html{--mat-sys-level0: 0px 0px 0px 0px rgba(0, 0, 0, .2), 0px 0px 0px 0px rgba(0, 0, 0, .14), 0px 0px 0px 0px rgba(0, 0, 0, .12)}html{--mat-sys-level1: 0px 2px 1px -1px rgba(0, 0, 0, .2), 0px 1px 1px 0px rgba(0, 0, 0, .14), 0px 1px 3px 0px rgba(0, 0, 0, .12)}html{--mat-sys-level2: 0px 3px 3px -2px rgba(0, 0, 0, .2), 0px 3px 4px 0px rgba(0, 0, 0, .14), 0px 1px 8px 0px rgba(0, 0, 0, .12)}html{--mat-sys-level3: 0px 3px 5px -1px rgba(0, 0, 0, .2), 0px 6px 10px 0px rgba(0, 0, 0, .14), 0px 1px 18px 0px rgba(0, 0, 0, .12)}html{--mat-sys-level4: 0px 5px 5px -3px rgba(0, 0, 0, .2), 0px 8px 10px 1px rgba(0, 0, 0, .14), 0px 3px 14px 2px rgba(0, 0, 0, .12)}html{--mat-sys-level5: 0px 7px 8px -4px rgba(0, 0, 0, .2), 0px 12px 17px 2px rgba(0, 0, 0, .14), 0px 5px 22px 4px rgba(0, 0, 0, .12)}html{--mat-sys-body-large: 400 1rem / 1.5rem Roboto;--mat-sys-body-large-font: Roboto;--mat-sys-body-large-line-height: 1.5rem;--mat-sys-body-large-size: 1rem;--mat-sys-body-large-tracking: .031rem;--mat-sys-body-large-weight: 400;--mat-sys-body-medium: 400 .875rem / 1.25rem Roboto;--mat-sys-body-medium-font: Roboto;--mat-sys-body-medium-line-height: 1.25rem;--mat-sys-body-medium-size: .875rem;--mat-sys-body-medium-tracking: .016rem;--mat-sys-body-medium-weight: 400;--mat-sys-body-small: 400 .75rem / 1rem Roboto;--mat-sys-body-small-font: Roboto;--mat-sys-body-small-line-height: 1rem;--mat-sys-body-small-size: .75rem;--mat-sys-body-small-tracking: .025rem;--mat-sys-body-small-weight: 400;--mat-sys-display-large: 400 3.562rem / 4rem Roboto;--mat-sys-display-large-font: Roboto;--mat-sys-display-large-line-height: 4rem;--mat-sys-display-large-size: 3.562rem;--mat-sys-display-large-tracking: -.016rem;--mat-sys-display-large-weight: 400;--mat-sys-display-medium: 400 2.812rem / 3.25rem Roboto;--mat-sys-display-medium-font: Roboto;--mat-sys-display-medium-line-height: 3.25rem;--mat-sys-display-medium-size: 2.812rem;--mat-sys-display-medium-tracking: 0;--mat-sys-display-medium-weight: 400;--mat-sys-display-small: 400 2.25rem / 2.75rem Roboto;--mat-sys-display-small-font: Roboto;--mat-sys-display-small-line-height: 2.75rem;--mat-sys-display-small-size: 2.25rem;--mat-sys-display-small-tracking: 0;--mat-sys-display-small-weight: 400;--mat-sys-headline-large: 400 2rem / 2.5rem Roboto;--mat-sys-headline-large-font: Roboto;--mat-sys-headline-large-line-height: 2.5rem;--mat-sys-headline-large-size: 2rem;--mat-sys-headline-large-tracking: 0;--mat-sys-headline-large-weight: 400;--mat-sys-headline-medium: 400 1.75rem / 2.25rem Roboto;--mat-sys-headline-medium-font: Roboto;--mat-sys-headline-medium-line-height: 2.25rem;--mat-sys-headline-medium-size: 1.75rem;--mat-sys-headline-medium-tracking: 0;--mat-sys-headline-medium-weight: 400;--mat-sys-headline-small: 400 1.5rem / 2rem Roboto;--mat-sys-headline-small-font: Roboto;--mat-sys-headline-small-line-height: 2rem;--mat-sys-headline-small-size: 1.5rem;--mat-sys-headline-small-tracking: 0;--mat-sys-headline-small-weight: 400;--mat-sys-label-large: 500 .875rem / 1.25rem Roboto;--mat-sys-label-large-font: Roboto;--mat-sys-label-large-line-height: 1.25rem;--mat-sys-label-large-size: .875rem;--mat-sys-label-large-tracking: .006rem;--mat-sys-label-large-weight: 500;--mat-sys-label-large-weight-prominent: 700;--mat-sys-label-medium: 500 .75rem / 1rem Roboto;--mat-sys-label-medium-font: Roboto;--mat-sys-label-medium-line-height: 1rem;--mat-sys-label-medium-size: .75rem;--mat-sys-label-medium-tracking: .031rem;--mat-sys-label-medium-weight: 500;--mat-sys-label-medium-weight-prominent: 700;--mat-sys-label-small: 500 .688rem / 1rem Roboto;--mat-sys-label-small-font: Roboto;--mat-sys-label-small-line-height: 1rem;--mat-sys-label-small-size: .688rem;--mat-sys-label-small-tracking: .031rem;--mat-sys-label-small-weight: 500;--mat-sys-title-large: 400 1.375rem / 1.75rem Roboto;--mat-sys-title-large-font: Roboto;--mat-sys-title-large-line-height: 1.75rem;--mat-sys-title-large-size: 1.375rem;--mat-sys-title-large-tracking: 0;--mat-sys-title-large-weight: 400;--mat-sys-title-medium: 500 1rem / 1.5rem Roboto;--mat-sys-title-medium-font: Roboto;--mat-sys-title-medium-line-height: 1.5rem;--mat-sys-title-medium-size: 1rem;--mat-sys-title-medium-tracking: .009rem;--mat-sys-title-medium-weight: 500;--mat-sys-title-small: 500 .875rem / 1.25rem Roboto;--mat-sys-title-small-font: Roboto;--mat-sys-title-small-line-height: 1.25rem;--mat-sys-title-small-size: .875rem;--mat-sys-title-small-tracking: .006rem;--mat-sys-title-small-weight: 500}html{--mat-sys-corner-extra-large: 28px;--mat-sys-corner-extra-large-top: 28px 28px 0 0;--mat-sys-corner-extra-small: 4px;--mat-sys-corner-extra-small-top: 4px 4px 0 0;--mat-sys-corner-full: 9999px;--mat-sys-corner-large: 16px;--mat-sys-corner-large-end: 0 16px 16px 0;--mat-sys-corner-large-start: 16px 0 0 16px;--mat-sys-corner-large-top: 16px 16px 0 0;--mat-sys-corner-medium: 12px;--mat-sys-corner-none: 0;--mat-sys-corner-small: 8px}html{--mat-sys-dragged-state-layer-opacity: .16;--mat-sys-focus-state-layer-opacity: .12;--mat-sys-hover-state-layer-opacity: .08;--mat-sys-pressed-state-layer-opacity: .12}:root{--bs-blue:#0d6efd;--bs-indigo:#6610f2;--bs-purple:#6f42c1;--bs-pink:#d63384;--bs-red:#dc3545;--bs-orange:#fd7e14;--bs-yellow:#ffc107;--bs-green:#198754;--bs-teal:#20c997;--bs-cyan:#0dcaf0;--bs-black:#000;--bs-white:#fff;--bs-gray:#6c757d;--bs-gray-dark:#343a40;--bs-gray-100:#f8f9fa;--bs-gray-200:#e9ecef;--bs-gray-300:#dee2e6;--bs-gray-400:#ced4da;--bs-gray-500:#adb5bd;--bs-gray-600:#6c757d;--bs-gray-700:#495057;--bs-gray-800:#343a40;--bs-gray-900:#212529;--bs-primary:#0d6efd;--bs-secondary:#6c757d;--bs-success:#198754;--bs-info:#0dcaf0;--bs-warning:#ffc107;--bs-danger:#dc3545;--bs-light:#f8f9fa;--bs-dark:#212529;--bs-primary-rgb:13,110,253;--bs-secondary-rgb:108,117,125;--bs-success-rgb:25,135,84;--bs-info-rgb:13,202,240;--bs-warning-rgb:255,193,7;--bs-danger-rgb:220,53,69;--bs-light-rgb:248,249,250;--bs-dark-rgb:33,37,41;--bs-primary-text-emphasis:#052c65;--bs-secondary-text-emphasis:#2b2f32;--bs-success-text-emphasis:#0a3622;--bs-info-text-emphasis:#055160;--bs-warning-text-emphasis:#664d03;--bs-danger-text-emphasis:#58151c;--bs-light-text-emphasis:#495057;--bs-dark-text-emphasis:#495057;--bs-primary-bg-subtle:#cfe2ff;--bs-secondary-bg-subtle:#e2e3e5;--bs-success-bg-subtle:#d1e7dd;--bs-info-bg-subtle:#cff4fc;--bs-warning-bg-subtle:#fff3cd;--bs-danger-bg-subtle:#f8d7da;--bs-light-bg-subtle:#fcfcfd;--bs-dark-bg-subtle:#ced4da;--bs-primary-border-subtle:#9ec5fe;--bs-secondary-border-subtle:#c4c8cb;--bs-success-border-subtle:#a3cfbb;--bs-info-border-subtle:#9eeaf9;--bs-warning-border-subtle:#ffe69c;--bs-danger-border-subtle:#f1aeb5;--bs-light-border-subtle:#e9ecef;--bs-dark-border-subtle:#adb5bd;--bs-white-rgb:255,255,255;--bs-black-rgb:0,0,0;--bs-font-sans-serif:system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue","Noto Sans","Liberation Sans",Arial,sans-serif,"Apple Color Emoji","Segoe UI Emoji","Segoe UI Symbol","Noto Color Emoji";--bs-font-monospace:SFMono-Regular,Menlo,Monaco,Consolas,"Liberation Mono","Courier New",monospace;--bs-gradient:linear-gradient(180deg, rgba(255, 255, 255, .15), rgba(255, 255, 255, 0));--bs-body-font-family:var(--bs-font-sans-serif);--bs-body-font-size:1rem;--bs-body-font-weight:400;--bs-body-line-height:1.5;--bs-body-color:#212529;--bs-body-color-rgb:33,37,41;--bs-body-bg:#fff;--bs-body-bg-rgb:255,255,255;--bs-emphasis-color:#000;--bs-emphasis-color-rgb:0,0,0;--bs-secondary-color:rgba(33, 37, 41, .75);--bs-secondary-color-rgb:33,37,41;--bs-secondary-bg:#e9ecef;--bs-secondary-bg-rgb:233,236,239;--bs-tertiary-color:rgba(33, 37, 41, .5);--bs-tertiary-color-rgb:33,37,41;--bs-tertiary-bg:#f8f9fa;--bs-tertiary-bg-rgb:248,249,250;--bs-heading-color:inherit;--bs-link-color:#0d6efd;--bs-link-color-rgb:13,110,253;--bs-link-decoration:underline;--bs-link-hover-color:#0a58ca;--bs-link-hover-color-rgb:10,88,202;--bs-code-color:#d63384;--bs-highlight-color:#212529;--bs-highlight-bg:#fff3cd;--bs-border-width:1px;--bs-border-style:solid;--bs-border-color:#dee2e6;--bs-border-color-translucent:rgba(0, 0, 0, .175);--bs-border-radius:.375rem;--bs-border-radius-sm:.25rem;--bs-border-radius-lg:.5rem;--bs-border-radius-xl:1rem;--bs-border-radius-xxl:2rem;--bs-border-radius-2xl:var(--bs-border-radius-xxl);--bs-border-radius-pill:50rem;--bs-box-shadow:0 .5rem 1rem rgba(0, 0, 0, .15);--bs-box-shadow-sm:0 .125rem .25rem rgba(0, 0, 0, .075);--bs-box-shadow-lg:0 1rem 3rem rgba(0, 0, 0, .175);--bs-box-shadow-inset:inset 0 1px 2px rgba(0, 0, 0, .075);--bs-focus-ring-width:.25rem;--bs-focus-ring-opacity:.25;--bs-focus-ring-color:rgba(13, 110, 253, .25);--bs-form-valid-color:#198754;--bs-form-valid-border-color:#198754;--bs-form-invalid-color:#dc3545;--bs-form-invalid-border-color:#dc3545}*,:after,:before{box-sizing:border-box}@media (prefers-reduced-motion:no-preference){:root{scroll-behavior:smooth}}body{margin:0;font-family:var(--bs-body-font-family);font-size:var(--bs-body-font-size);font-weight:var(--bs-body-font-weight);line-height:var(--bs-body-line-height);color:var(--bs-body-color);text-align:var(--bs-body-text-align);background-color:var(--bs-body-bg);-webkit-text-size-adjust:100%;-webkit-tap-highlight-color:transparent}hr{margin:1rem 0;color:inherit;border:0;border-top:var(--bs-border-width) solid;opacity:.25}h4,h5,h6{margin-top:0;margin-bottom:.5rem;font-weight:500;line-height:1.2;color:var(--bs-heading-color)}h4{font-size:calc(1.275rem + .3vw)}@media (min-width:1200px){h4{font-size:1.5rem}}h5{font-size:1.25rem}h6{font-size:1rem}p{margin-top:0;margin-bottom:1rem}ol,ul{padding-left:2rem}ol,ul{margin-top:0;margin-bottom:1rem}ol ul,ul ul{margin-bottom:0}b{font-weight:bolder}mark{padding:.1875em;color:var(--bs-highlight-color);background-color:var(--bs-highlight-bg)}a{color:rgba(var(--bs-link-color-rgb),var(--bs-link-opacity,1));text-decoration:underline}a:hover{--bs-link-color-rgb:var(--bs-link-hover-color-rgb)}a:not([href]):not([class]),a:not([href]):not([class]):hover{color:inherit;text-decoration:none}img{vertical-align:middle}button{border-radius:0}button:focus:not(:focus-visible){outline:0}button,input{margin:0;font-family:inherit;font-size:inherit;line-height:inherit}button{text-transform:none}[role=button]{cursor:pointer}[type=button],button{-webkit-appearance:button}[type=button]:not(:disabled),button:not(:disabled){cursor:pointer}.img-fluid{max-width:100%;height:auto}.container-fluid{--bs-gutter-x:1.5rem;--bs-gutter-y:0;width:100%;padding-right:calc(var(--bs-gutter-x) * .5);padding-left:calc(var(--bs-gutter-x) * .5);margin-right:auto;margin-left:auto}:root{--bs-breakpoint-xs:0;--bs-breakpoint-sm:576px;--bs-breakpoint-md:768px;--bs-breakpoint-lg:992px;--bs-breakpoint-xl:1200px;--bs-breakpoint-xxl:1400px}.row{--bs-gutter-x:1.5rem;--bs-gutter-y:0;display:flex;flex-wrap:wrap;margin-top:calc(-1 * var(--bs-gutter-y));margin-right:calc(-.5 * var(--bs-gutter-x));margin-left:calc(-.5 * var(--bs-gutter-x))}.row>*{flex-shrink:0;width:100%;max-width:100%;padding-right:calc(var(--bs-gutter-x) * .5);padding-left:calc(var(--bs-gutter-x) * .5);margin-top:var(--bs-gutter-y)}.col-12{flex:0 0 auto;width:100%}.g-3{--bs-gutter-x:1rem}.g-3{--bs-gutter-y:1rem}@media (min-width:768px){.col-md-4{flex:0 0 auto;width:33.33333333%}.col-md-8{flex:0 0 auto;width:66.66666667%}}@media (min-width:992px){.col-lg-4{flex:0 0 auto;width:33.33333333%}.col-lg-8{flex:0 0 auto;width:66.66666667%}.col-lg-12{flex:0 0 auto;width:100%}}.form-control{display:block;width:100%;padding:.375rem .75rem;font-size:1rem;font-weight:400;line-height:1.5;color:var(--bs-body-color);-webkit-appearance:none;-moz-appearance:none;appearance:none;background-color:var(--bs-body-bg);background-clip:padding-box;border:var(--bs-border-width) solid var(--bs-border-color);border-radius:var(--bs-border-radius);transition:border-color .15s ease-in-out,box-shadow .15s ease-in-out}@media (prefers-reduced-motion:reduce){.form-control{transition:none}}.form-control:focus{color:var(--bs-body-color);background-color:var(--bs-body-bg);border-color:#86b7fe;outline:0;box-shadow:0 0 0 .25rem #0d6efd40}.form-control::-webkit-date-and-time-value{min-width:85px;height:1.5em;margin:0}.form-control::-webkit-datetime-edit{display:block;padding:0}.form-control::placeholder{color:var(--bs-secondary-color);opacity:1}.form-control:disabled{background-color:var(--bs-secondary-bg);opacity:1}.form-control::-webkit-file-upload-button{padding:.375rem .75rem;margin:-.375rem -.75rem;-webkit-margin-end:.75rem;margin-inline-end:.75rem;color:var(--bs-body-color);background-color:var(--bs-tertiary-bg);pointer-events:none;border-color:inherit;border-style:solid;border-width:0;border-inline-end-width:var(--bs-border-width);border-radius:0;-webkit-transition:color .15s ease-in-out,background-color .15s ease-in-out,border-color .15s ease-in-out,box-shadow .15s ease-in-out;transition:color .15s ease-in-out,background-color .15s ease-in-out,border-color .15s ease-in-out,box-shadow .15s ease-in-out}.form-control::file-selector-button{padding:.375rem .75rem;margin:-.375rem -.75rem;-webkit-margin-end:.75rem;margin-inline-end:.75rem;color:var(--bs-body-color);background-color:var(--bs-tertiary-bg);pointer-events:none;border-color:inherit;border-style:solid;border-width:0;border-inline-end-width:var(--bs-border-width);border-radius:0;transition:color .15s ease-in-out,background-color .15s ease-in-out,border-color .15s ease-in-out,box-shadow .15s ease-in-out}@media (prefers-reduced-motion:reduce){.form-control::-webkit-file-upload-button{-webkit-transition:none;transition:none}.form-control::file-selector-button{transition:none}}.form-control:hover:not(:disabled):not([readonly])::-webkit-file-upload-button{background-color:var(--bs-secondary-bg)}.form-control:hover:not(:disabled):not([readonly])::file-selector-button{background-color:var(--bs-secondary-bg)}.btn{--bs-btn-padding-x:.75rem;--bs-btn-padding-y:.375rem;--bs-btn-font-family: ;--bs-btn-font-size:1rem;--bs-btn-font-weight:400;--bs-btn-line-height:1.5;--bs-btn-color:var(--bs-body-color);--bs-btn-bg:transparent;--bs-btn-border-width:var(--bs-border-width);--bs-btn-border-color:transparent;--bs-btn-border-radius:var(--bs-border-radius);--bs-btn-hover-border-color:transparent;--bs-btn-box-shadow:inset 0 1px 0 rgba(255, 255, 255, .15),0 1px 1px rgba(0, 0, 0, .075);--bs-btn-disabled-opacity:.65;--bs-btn-focus-box-shadow:0 0 0 .25rem rgba(var(--bs-btn-focus-shadow-rgb), .5);display:inline-block;padding:var(--bs-btn-padding-y) var(--bs-btn-padding-x);font-family:var(--bs-btn-font-family);font-size:var(--bs-btn-font-size);font-weight:var(--bs-btn-font-weight);line-height:var(--bs-btn-line-height);color:var(--bs-btn-color);text-align:center;text-decoration:none;vertical-align:middle;cursor:pointer;-webkit-user-select:none;-moz-user-select:none;user-select:none;border:var(--bs-btn-border-width) solid var(--bs-btn-border-color);border-radius:var(--bs-btn-border-radius);background-color:var(--bs-btn-bg);transition:color .15s ease-in-out,background-color .15s ease-in-out,border-color .15s ease-in-out,box-shadow .15s ease-in-out}@media (prefers-reduced-motion:reduce){.btn{transition:none}}.btn:hover{color:var(--bs-btn-hover-color);background-color:var(--bs-btn-hover-bg);border-color:var(--bs-btn-hover-border-color)}.btn:focus-visible{color:var(--bs-btn-hover-color);background-color:var(--bs-btn-hover-bg);border-color:var(--bs-btn-hover-border-color);outline:0;box-shadow:var(--bs-btn-focus-box-shadow)}.btn:first-child:active{color:var(--bs-btn-active-color);background-color:var(--bs-btn-active-bg);border-color:var(--bs-btn-active-border-color)}.btn:first-child:active:focus-visible{box-shadow:var(--bs-btn-focus-box-shadow)}.btn:disabled{color:var(--bs-btn-disabled-color);pointer-events:none;background-color:var(--bs-btn-disabled-bg);border-color:var(--bs-btn-disabled-border-color);opacity:var(--bs-btn-disabled-opacity)}.btn-outline-light{--bs-btn-color:#f8f9fa;--bs-btn-border-color:#f8f9fa;--bs-btn-hover-color:#000;--bs-btn-hover-bg:#f8f9fa;--bs-btn-hover-border-color:#f8f9fa;--bs-btn-focus-shadow-rgb:248,249,250;--bs-btn-active-color:#000;--bs-btn-active-bg:#f8f9fa;--bs-btn-active-border-color:#f8f9fa;--bs-btn-active-shadow:inset 0 3px 5px rgba(0, 0, 0, .125);--bs-btn-disabled-color:#f8f9fa;--bs-btn-disabled-bg:transparent;--bs-btn-disabled-border-color:#f8f9fa;--bs-gradient:none}.btn-sm{--bs-btn-padding-y:.25rem;--bs-btn-padding-x:.5rem;--bs-btn-font-size:.875rem;--bs-btn-border-radius:var(--bs-border-radius-sm)}.collapse:not(.show){display:none}.nav{--bs-nav-link-padding-x:1rem;--bs-nav-link-padding-y:.5rem;--bs-nav-link-font-weight: ;--bs-nav-link-color:var(--bs-link-color);--bs-nav-link-hover-color:var(--bs-link-hover-color);--bs-nav-link-disabled-color:var(--bs-secondary-color);display:flex;flex-wrap:wrap;padding-left:0;margin-bottom:0;list-style:none}.nav-link{display:block;padding:var(--bs-nav-link-padding-y) var(--bs-nav-link-padding-x);font-size:var(--bs-nav-link-font-size);font-weight:var(--bs-nav-link-font-weight);color:var(--bs-nav-link-color);text-decoration:none;background:0 0;border:0;transition:color .15s ease-in-out,background-color .15s ease-in-out,border-color .15s ease-in-out}@media (prefers-reduced-motion:reduce){.nav-link{transition:none}}.nav-link:focus,.nav-link:hover{color:var(--bs-nav-link-hover-color)}.nav-link:focus-visible{outline:0;box-shadow:0 0 0 .25rem #0d6efd40}.nav-link:disabled{color:var(--bs-nav-link-disabled-color);pointer-events:none;cursor:default}.nav-pills{--bs-nav-pills-border-radius:var(--bs-border-radius);--bs-nav-pills-link-active-color:#fff;--bs-nav-pills-link-active-bg:#0d6efd}.nav-pills .nav-link{border-radius:var(--bs-nav-pills-border-radius)}.nav-pills .nav-link.active{color:var(--bs-nav-pills-link-active-color);background-color:var(--bs-nav-pills-link-active-bg)}.navbar{--bs-navbar-padding-x:0;--bs-navbar-padding-y:.5rem;--bs-navbar-color:rgba(var(--bs-emphasis-color-rgb), .65);--bs-navbar-hover-color:rgba(var(--bs-emphasis-color-rgb), .8);--bs-navbar-disabled-color:rgba(var(--bs-emphasis-color-rgb), .3);--bs-navbar-active-color:rgba(var(--bs-emphasis-color-rgb), 1);--bs-navbar-brand-padding-y:.3125rem;--bs-navbar-brand-margin-end:1rem;--bs-navbar-brand-font-size:1.25rem;--bs-navbar-brand-color:rgba(var(--bs-emphasis-color-rgb), 1);--bs-navbar-brand-hover-color:rgba(var(--bs-emphasis-color-rgb), 1);--bs-navbar-nav-link-padding-x:.5rem;--bs-navbar-toggler-padding-y:.25rem;--bs-navbar-toggler-padding-x:.75rem;--bs-navbar-toggler-font-size:1.25rem;--bs-navbar-toggler-icon-bg:url("data:image/svg+xml,%3csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 30 30'%3e%3cpath stroke='rgba%2833, 37, 41, 0.75%29' stroke-linecap='round' stroke-miterlimit='10' stroke-width='2' d='M4 7h22M4 15h22M4 23h22'/%3e%3c/svg%3e");--bs-navbar-toggler-border-color:rgba(var(--bs-emphasis-color-rgb), .15);--bs-navbar-toggler-border-radius:var(--bs-border-radius);--bs-navbar-toggler-focus-width:.25rem;--bs-navbar-toggler-transition:box-shadow .15s ease-in-out;position:relative;display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;padding:var(--bs-navbar-padding-y) var(--bs-navbar-padding-x)}.navbar>.container-fluid{display:flex;flex-wrap:inherit;align-items:center;justify-content:space-between}.navbar-brand{padding-top:var(--bs-navbar-brand-padding-y);padding-bottom:var(--bs-navbar-brand-padding-y);margin-right:var(--bs-navbar-brand-margin-end);font-size:var(--bs-navbar-brand-font-size);color:var(--bs-navbar-brand-color);text-decoration:none;white-space:nowrap}.navbar-brand:focus,.navbar-brand:hover{color:var(--bs-navbar-brand-hover-color)}.navbar-nav{--bs-nav-link-padding-x:0;--bs-nav-link-padding-y:.5rem;--bs-nav-link-font-weight: ;--bs-nav-link-color:var(--bs-navbar-color);--bs-nav-link-hover-color:var(--bs-navbar-hover-color);--bs-nav-link-disabled-color:var(--bs-navbar-disabled-color);display:flex;flex-direction:column;padding-left:0;margin-bottom:0;list-style:none}.navbar-collapse{flex-grow:1;flex-basis:100%;align-items:center}.navbar-toggler{padding:var(--bs-navbar-toggler-padding-y) var(--bs-navbar-toggler-padding-x);font-size:var(--bs-navbar-toggler-font-size);line-height:1;color:var(--bs-navbar-color);background-color:transparent;border:var(--bs-border-width) solid var(--bs-navbar-toggler-border-color);border-radius:var(--bs-navbar-toggler-border-radius);transition:var(--bs-navbar-toggler-transition)}@media (prefers-reduced-motion:reduce){.navbar-toggler{transition:none}}.navbar-toggler:hover{text-decoration:none}.navbar-toggler:focus{text-decoration:none;outline:0;box-shadow:0 0 0 var(--bs-navbar-toggler-focus-width)}.navbar-nav-scroll{max-height:var(--bs-scroll-height,75vh);overflow-y:auto}@media (min-width:992px){.navbar-expand-lg{flex-wrap:nowrap;justify-content:flex-start}.navbar-expand-lg .navbar-nav{flex-direction:row}.navbar-expand-lg .navbar-nav-scroll{overflow:visible}.navbar-expand-lg .navbar-collapse{display:flex!important;flex-basis:auto}.navbar-expand-lg .navbar-toggler{display:none}}.card{--bs-card-spacer-y:1rem;--bs-card-spacer-x:1rem;--bs-card-title-spacer-y:.5rem;--bs-card-title-color: ;--bs-card-subtitle-color: ;--bs-card-border-width:var(--bs-border-width);--bs-card-border-color:var(--bs-border-color-translucent);--bs-card-border-radius:var(--bs-border-radius);--bs-card-box-shadow: ;--bs-card-inner-border-radius:calc(var(--bs-border-radius) - (var(--bs-border-width)));--bs-card-cap-padding-y:.5rem;--bs-card-cap-padding-x:1rem;--bs-card-cap-bg:rgba(var(--bs-body-color-rgb), .03);--bs-card-cap-color: ;--bs-card-height: ;--bs-card-color: ;--bs-card-bg:var(--bs-body-bg);--bs-card-img-overlay-padding:1rem;--bs-card-group-margin:.75rem;position:relative;display:flex;flex-direction:column;min-width:0;height:var(--bs-card-height);color:var(--bs-body-color);word-wrap:break-word;background-color:var(--bs-card-bg);background-clip:border-box;border:var(--bs-card-border-width) solid var(--bs-card-border-color);border-radius:var(--bs-card-border-radius)}.card-body{flex:1 1 auto;padding:var(--bs-card-spacer-y) var(--bs-card-spacer-x);color:var(--bs-card-color)}.btn-close{--bs-btn-close-color:#000;--bs-btn-close-bg:url("data:image/svg+xml,%3csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16' fill='%23000'%3e%3cpath d='M.293.293a1 1 0 0 1 1.414 0L8 6.586 14.293.293a1 1 0 1 1 1.414 1.414L9.414 8l6.293 6.293a1 1 0 0 1-1.414 1.414L8 9.414l-6.293 6.293a1 1 0 0 1-1.414-1.414L6.586 8 .293 1.707a1 1 0 0 1 0-1.414'/%3e%3c/svg%3e");--bs-btn-close-opacity:.5;--bs-btn-close-hover-opacity:.75;--bs-btn-close-focus-shadow:0 0 0 .25rem rgba(13, 110, 253, .25);--bs-btn-close-focus-opacity:1;--bs-btn-close-disabled-opacity:.25;box-sizing:content-box;width:1em;height:1em;padding:.25em;color:var(--bs-btn-close-color);background:transparent var(--bs-btn-close-bg) center/1em auto no-repeat;filter:var(--bs-btn-close-filter);border:0;border-radius:.375rem;opacity:var(--bs-btn-close-opacity)}.btn-close:hover{color:var(--bs-btn-close-color);text-decoration:none;opacity:var(--bs-btn-close-hover-opacity)}.btn-close:focus{outline:0;box-shadow:var(--bs-btn-close-focus-shadow);opacity:var(--bs-btn-close-focus-opacity)}.btn-close:disabled{pointer-events:none;-webkit-user-select:none;-moz-user-select:none;user-select:none;opacity:var(--bs-btn-close-disabled-opacity)}.btn-close-white{--bs-btn-close-filter:invert(1) grayscale(100%) brightness(200%)}:root{--bs-btn-close-filter: }:root{--bs-carousel-indicator-active-bg:#fff;--bs-carousel-caption-color:#fff;--bs-carousel-control-icon-filter: }.offcanvas{--bs-offcanvas-zindex:1045;--bs-offcanvas-width:400px;--bs-offcanvas-height:30vh;--bs-offcanvas-padding-x:1rem;--bs-offcanvas-padding-y:1rem;--bs-offcanvas-color:var(--bs-body-color);--bs-offcanvas-bg:var(--bs-body-bg);--bs-offcanvas-border-width:var(--bs-border-width);--bs-offcanvas-border-color:var(--bs-border-color-translucent);--bs-offcanvas-box-shadow:var(--bs-box-shadow-sm);--bs-offcanvas-transition:transform .3s ease-in-out;--bs-offcanvas-title-line-height:1.5}.offcanvas{position:fixed;bottom:0;z-index:var(--bs-offcanvas-zindex);display:flex;flex-direction:column;max-width:100%;color:var(--bs-offcanvas-color);visibility:hidden;background-color:var(--bs-offcanvas-bg);background-clip:padding-box;outline:0;transition:var(--bs-offcanvas-transition)}@media (prefers-reduced-motion:reduce){.offcanvas{transition:none}}.offcanvas.offcanvas-start{top:0;left:0;width:var(--bs-offcanvas-width);border-right:var(--bs-offcanvas-border-width) solid var(--bs-offcanvas-border-color);transform:translate(-100%)}.offcanvas-header{display:flex;align-items:center;padding:var(--bs-offcanvas-padding-y) var(--bs-offcanvas-padding-x)}.offcanvas-header .btn-close{padding:calc(var(--bs-offcanvas-padding-y) * .5) calc(var(--bs-offcanvas-padding-x) * .5);margin-top:calc(-.5 * var(--bs-offcanvas-padding-y));margin-right:calc(-.5 * var(--bs-offcanvas-padding-x));margin-bottom:calc(-.5 * var(--bs-offcanvas-padding-y));margin-left:auto}.offcanvas-body{flex-grow:1;padding:var(--bs-offcanvas-padding-y) var(--bs-offcanvas-padding-x);overflow-y:auto}.d-flex{display:flex!important}.d-none{display:none!important}.shadow-sm{box-shadow:var(--bs-box-shadow-sm)!important}.position-relative{position:relative!important}.border{border:var(--bs-border-width) var(--bs-border-style) var(--bs-border-color)!important}.border-top{border-top:var(--bs-border-width) var(--bs-border-style) var(--bs-border-color)!important}.border-warning{--bs-border-opacity:1;border-color:rgba(var(--bs-warning-rgb),var(--bs-border-opacity))!important}.w-50{width:50%!important}.w-100{width:100%!important}.h-100{height:100%!important}.min-vh-100{min-height:100vh!important}.flex-column{flex-direction:column!important}.flex-grow-1{flex-grow:1!important}.justify-content-center{justify-content:center!important}.align-items-center{align-items:center!important}.order-1{order:1!important}.order-2{order:2!important}.mx-auto{margin-right:auto!important;margin-left:auto!important}.my-2{margin-top:.5rem!important;margin-bottom:.5rem!important}.mt-1{margin-top:.25rem!important}.mt-2{margin-top:.5rem!important}.mt-3{margin-top:1rem!important}.mt-4{margin-top:1.5rem!important}.me-2{margin-right:.5rem!important}.me-3{margin-right:1rem!important}.me-auto{margin-right:auto!important}.mb-0{margin-bottom:0!important}.mb-1{margin-bottom:.25rem!important}.mb-2{margin-bottom:.5rem!important}.mb-3{margin-bottom:1rem!important}.mb-4{margin-bottom:1.5rem!important}.ms-auto{margin-left:auto!important}.p-3{padding:1rem!important}.pt-2{padding-top:.5rem!important}.pb-2{padding-bottom:.5rem!important}.ps-4{padding-left:1.5rem!important}.fs-4{font-size:calc(1.275rem + .3vw)!important}.text-start{text-align:left!important}.text-center{text-align:center!important}.text-light{--bs-text-opacity:1;color:rgba(var(--bs-light-rgb),var(--bs-text-opacity))!important}.text-white{--bs-text-opacity:1;color:rgba(var(--bs-white-rgb),var(--bs-text-opacity))!important}.text-reset{--bs-text-opacity:1;color:inherit!important}.link-offset-2{text-underline-offset:.25em!important}.link-underline{--bs-link-underline-opacity:1;-webkit-text-decoration-color:rgba(var(--bs-link-color-rgb),var(--bs-link-underline-opacity,1))!important;text-decoration-color:rgba(var(--bs-link-color-rgb),var(--bs-link-underline-opacity,1))!important}.link-underline-opacity-0{--bs-link-underline-opacity:0}.bg-danger{--bs-bg-opacity:1;background-color:rgba(var(--bs-danger-rgb),var(--bs-bg-opacity))!important}.rounded{border-radius:var(--bs-border-radius)!important}.rounded-circle{border-radius:50%!important}@media (min-width:768px){.mx-md-0{margin-right:0!important;margin-left:0!important}.mb-md-0{margin-bottom:0!important}.text-md-start{text-align:left!important}.text-md-end{text-align:right!important}}@media (min-width:992px){.d-lg-block{display:block!important}.d-lg-none{display:none!important}.order-lg-1{order:1!important}.order-lg-2{order:2!important}.my-lg-0{margin-top:0!important;margin-bottom:0!important}}@media (min-width:1200px){.fs-4{font-size:1.5rem!important}}:root{--animate-duration:1s;--animate-delay:1s;--animate-repeat:1}@-webkit-keyframes bounce{0%,20%,53%,to{-webkit-animation-timing-function:cubic-bezier(.215,.61,.355,1);animation-timing-function:cubic-bezier(.215,.61,.355,1);-webkit-transform:translateZ(0);transform:translateZ(0)}40%,43%{-webkit-animation-timing-function:cubic-bezier(.755,.05,.855,.06);animation-timing-function:cubic-bezier(.755,.05,.855,.06);-webkit-transform:translate3d(0,-30px,0) scaleY(1.1);transform:translate3d(0,-30px,0) scaleY(1.1)}70%{-webkit-animation-timing-function:cubic-bezier(.755,.05,.855,.06);animation-timing-function:cubic-bezier(.755,.05,.855,.06);-webkit-transform:translate3d(0,-15px,0) scaleY(1.05);transform:translate3d(0,-15px,0) scaleY(1.05)}80%{-webkit-transition-timing-function:cubic-bezier(.215,.61,.355,1);transition-timing-function:cubic-bezier(.215,.61,.355,1);-webkit-transform:translateZ(0) scaleY(.95);transform:translateZ(0) scaleY(.95)}90%{-webkit-transform:translate3d(0,-4px,0) scaleY(1.02);transform:translate3d(0,-4px,0) scaleY(1.02)}}@-webkit-keyframes flash{0%,50%,to{opacity:1}25%,75%{opacity:0}}@-webkit-keyframes pulse{0%{-webkit-transform:scaleX(1);transform:scaleX(1)}50%{-webkit-transform:scale3d(1.05,1.05,1.05);transform:scale3d(1.05,1.05,1.05)}to{-webkit-transform:scaleX(1);transform:scaleX(1)}}@-webkit-keyframes rubberBand{0%{-webkit-transform:scaleX(1);transform:scaleX(1)}30%{-webkit-transform:scale3d(1.25,.75,1);transform:scale3d(1.25,.75,1)}40%{-webkit-transform:scale3d(.75,1.25,1);transform:scale3d(.75,1.25,1)}50%{-webkit-transform:scale3d(1.15,.85,1);transform:scale3d(1.15,.85,1)}65%{-webkit-transform:scale3d(.95,1.05,1);transform:scale3d(.95,1.05,1)}75%{-webkit-transform:scale3d(1.05,.95,1);transform:scale3d(1.05,.95,1)}to{-webkit-transform:scaleX(1);transform:scaleX(1)}}@-webkit-keyframes shakeX{0%,to{-webkit-transform:translateZ(0);transform:translateZ(0)}10%,30%,50%,70%,90%{-webkit-transform:translate3d(-10px,0,0);transform:translate3d(-10px,0,0)}20%,40%,60%,80%{-webkit-transform:translate3d(10px,0,0);transform:translate3d(10px,0,0)}}@-webkit-keyframes shakeY{0%,to{-webkit-transform:translateZ(0);transform:translateZ(0)}10%,30%,50%,70%,90%{-webkit-transform:translate3d(0,-10px,0);transform:translate3d(0,-10px,0)}20%,40%,60%,80%{-webkit-transform:translate3d(0,10px,0);transform:translate3d(0,10px,0)}}@-webkit-keyframes headShake{0%{-webkit-transform:translateX(0);transform:translate(0)}6.5%{-webkit-transform:translateX(-6px) rotateY(-9deg);transform:translate(-6px) rotateY(-9deg)}18.5%{-webkit-transform:translateX(5px) rotateY(7deg);transform:translate(5px) rotateY(7deg)}31.5%{-webkit-transform:translateX(-3px) rotateY(-5deg);transform:translate(-3px) rotateY(-5deg)}43.5%{-webkit-transform:translateX(2px) rotateY(3deg);transform:translate(2px) rotateY(3deg)}50%{-webkit-transform:translateX(0);transform:translate(0)}}@-webkit-keyframes swing{20%{-webkit-transform:rotate(15deg);transform:rotate(15deg)}40%{-webkit-transform:rotate(-10deg);transform:rotate(-10deg)}60%{-webkit-transform:rotate(5deg);transform:rotate(5deg)}80%{-webkit-transform:rotate(-5deg);transform:rotate(-5deg)}to{-webkit-transform:rotate(0deg);transform:rotate(0)}}@-webkit-keyframes tada{0%{-webkit-transform:scaleX(1);transform:scaleX(1)}10%,20%{-webkit-transform:scale3d(.9,.9,.9) rotate(-3deg);transform:scale3d(.9,.9,.9) rotate(-3deg)}30%,50%,70%,90%{-webkit-transform:scale3d(1.1,1.1,1.1) rotate(3deg);transform:scale3d(1.1,1.1,1.1) rotate(3deg)}40%,60%,80%{-webkit-transform:scale3d(1.1,1.1,1.1) rotate(-3deg);transform:scale3d(1.1,1.1,1.1) rotate(-3deg)}to{-webkit-transform:scaleX(1);transform:scaleX(1)}}@-webkit-keyframes wobble{0%{-webkit-transform:translateZ(0);transform:translateZ(0)}15%{-webkit-transform:translate3d(-25%,0,0) rotate(-5deg);transform:translate3d(-25%,0,0) rotate(-5deg)}30%{-webkit-transform:translate3d(20%,0,0) rotate(3deg);transform:translate3d(20%,0,0) rotate(3deg)}45%{-webkit-transform:translate3d(-15%,0,0) rotate(-3deg);transform:translate3d(-15%,0,0) rotate(-3deg)}60%{-webkit-transform:translate3d(10%,0,0) rotate(2deg);transform:translate3d(10%,0,0) rotate(2deg)}75%{-webkit-transform:translate3d(-5%,0,0) rotate(-1deg);transform:translate3d(-5%,0,0) rotate(-1deg)}to{-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes jello{0%,11.1%,to{-webkit-transform:translateZ(0);transform:translateZ(0)}22.2%{-webkit-transform:skewX(-12.5deg) skewY(-12.5deg);transform:skew(-12.5deg) skewY(-12.5deg)}33.3%{-webkit-transform:skewX(6.25deg) skewY(6.25deg);transform:skew(6.25deg) skewY(6.25deg)}44.4%{-webkit-transform:skewX(-3.125deg) skewY(-3.125deg);transform:skew(-3.125deg) skewY(-3.125deg)}55.5%{-webkit-transform:skewX(1.5625deg) skewY(1.5625deg);transform:skew(1.5625deg) skewY(1.5625deg)}66.6%{-webkit-transform:skewX(-.78125deg) skewY(-.78125deg);transform:skew(-.78125deg) skewY(-.78125deg)}77.7%{-webkit-transform:skewX(.390625deg) skewY(.390625deg);transform:skew(.390625deg) skewY(.390625deg)}88.8%{-webkit-transform:skewX(-.1953125deg) skewY(-.1953125deg);transform:skew(-.1953125deg) skewY(-.1953125deg)}}@-webkit-keyframes heartBeat{0%{-webkit-transform:scale(1);transform:scale(1)}14%{-webkit-transform:scale(1.3);transform:scale(1.3)}28%{-webkit-transform:scale(1);transform:scale(1)}42%{-webkit-transform:scale(1.3);transform:scale(1.3)}70%{-webkit-transform:scale(1);transform:scale(1)}}@-webkit-keyframes backInDown{0%{-webkit-transform:translateY(-1200px) scale(.7);transform:translateY(-1200px) scale(.7);opacity:.7}80%{-webkit-transform:translateY(0) scale(.7);transform:translateY(0) scale(.7);opacity:.7}to{-webkit-transform:scale(1);transform:scale(1);opacity:1}}@-webkit-keyframes backInLeft{0%{-webkit-transform:translateX(-2000px) scale(.7);transform:translate(-2000px) scale(.7);opacity:.7}80%{-webkit-transform:translateX(0) scale(.7);transform:translate(0) scale(.7);opacity:.7}to{-webkit-transform:scale(1);transform:scale(1);opacity:1}}@-webkit-keyframes backInRight{0%{-webkit-transform:translateX(2000px) scale(.7);transform:translate(2000px) scale(.7);opacity:.7}80%{-webkit-transform:translateX(0) scale(.7);transform:translate(0) scale(.7);opacity:.7}to{-webkit-transform:scale(1);transform:scale(1);opacity:1}}@-webkit-keyframes backInUp{0%{-webkit-transform:translateY(1200px) scale(.7);transform:translateY(1200px) scale(.7);opacity:.7}80%{-webkit-transform:translateY(0) scale(.7);transform:translateY(0) scale(.7);opacity:.7}to{-webkit-transform:scale(1);transform:scale(1);opacity:1}}@-webkit-keyframes backOutDown{0%{-webkit-transform:scale(1);transform:scale(1);opacity:1}20%{-webkit-transform:translateY(0) scale(.7);transform:translateY(0) scale(.7);opacity:.7}to{-webkit-transform:translateY(700px) scale(.7);transform:translateY(700px) scale(.7);opacity:.7}}@-webkit-keyframes backOutLeft{0%{-webkit-transform:scale(1);transform:scale(1);opacity:1}20%{-webkit-transform:translateX(0) scale(.7);transform:translate(0) scale(.7);opacity:.7}to{-webkit-transform:translateX(-2000px) scale(.7);transform:translate(-2000px) scale(.7);opacity:.7}}@-webkit-keyframes backOutRight{0%{-webkit-transform:scale(1);transform:scale(1);opacity:1}20%{-webkit-transform:translateX(0) scale(.7);transform:translate(0) scale(.7);opacity:.7}to{-webkit-transform:translateX(2000px) scale(.7);transform:translate(2000px) scale(.7);opacity:.7}}@-webkit-keyframes backOutUp{0%{-webkit-transform:scale(1);transform:scale(1);opacity:1}20%{-webkit-transform:translateY(0) scale(.7);transform:translateY(0) scale(.7);opacity:.7}to{-webkit-transform:translateY(-700px) scale(.7);transform:translateY(-700px) scale(.7);opacity:.7}}@-webkit-keyframes bounceIn{0%,20%,40%,60%,80%,to{-webkit-animation-timing-function:cubic-bezier(.215,.61,.355,1);animation-timing-function:cubic-bezier(.215,.61,.355,1)}0%{opacity:0;-webkit-transform:scale3d(.3,.3,.3);transform:scale3d(.3,.3,.3)}20%{-webkit-transform:scale3d(1.1,1.1,1.1);transform:scale3d(1.1,1.1,1.1)}40%{-webkit-transform:scale3d(.9,.9,.9);transform:scale3d(.9,.9,.9)}60%{opacity:1;-webkit-transform:scale3d(1.03,1.03,1.03);transform:scale3d(1.03,1.03,1.03)}80%{-webkit-transform:scale3d(.97,.97,.97);transform:scale3d(.97,.97,.97)}to{opacity:1;-webkit-transform:scaleX(1);transform:scaleX(1)}}@-webkit-keyframes bounceInDown{0%,60%,75%,90%,to{-webkit-animation-timing-function:cubic-bezier(.215,.61,.355,1);animation-timing-function:cubic-bezier(.215,.61,.355,1)}0%{opacity:0;-webkit-transform:translate3d(0,-3000px,0) scaleY(3);transform:translate3d(0,-3000px,0) scaleY(3)}60%{opacity:1;-webkit-transform:translate3d(0,25px,0) scaleY(.9);transform:translate3d(0,25px,0) scaleY(.9)}75%{-webkit-transform:translate3d(0,-10px,0) scaleY(.95);transform:translate3d(0,-10px,0) scaleY(.95)}90%{-webkit-transform:translate3d(0,5px,0) scaleY(.985);transform:translate3d(0,5px,0) scaleY(.985)}to{-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes bounceInLeft{0%,60%,75%,90%,to{-webkit-animation-timing-function:cubic-bezier(.215,.61,.355,1);animation-timing-function:cubic-bezier(.215,.61,.355,1)}0%{opacity:0;-webkit-transform:translate3d(-3000px,0,0) scaleX(3);transform:translate3d(-3000px,0,0) scaleX(3)}60%{opacity:1;-webkit-transform:translate3d(25px,0,0) scaleX(1);transform:translate3d(25px,0,0) scaleX(1)}75%{-webkit-transform:translate3d(-10px,0,0) scaleX(.98);transform:translate3d(-10px,0,0) scaleX(.98)}90%{-webkit-transform:translate3d(5px,0,0) scaleX(.995);transform:translate3d(5px,0,0) scaleX(.995)}to{-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes bounceInRight{0%,60%,75%,90%,to{-webkit-animation-timing-function:cubic-bezier(.215,.61,.355,1);animation-timing-function:cubic-bezier(.215,.61,.355,1)}0%{opacity:0;-webkit-transform:translate3d(3000px,0,0) scaleX(3);transform:translate3d(3000px,0,0) scaleX(3)}60%{opacity:1;-webkit-transform:translate3d(-25px,0,0) scaleX(1);transform:translate3d(-25px,0,0) scaleX(1)}75%{-webkit-transform:translate3d(10px,0,0) scaleX(.98);transform:translate3d(10px,0,0) scaleX(.98)}90%{-webkit-transform:translate3d(-5px,0,0) scaleX(.995);transform:translate3d(-5px,0,0) scaleX(.995)}to{-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes bounceInUp{0%,60%,75%,90%,to{-webkit-animation-timing-function:cubic-bezier(.215,.61,.355,1);animation-timing-function:cubic-bezier(.215,.61,.355,1)}0%{opacity:0;-webkit-transform:translate3d(0,3000px,0) scaleY(5);transform:translate3d(0,3000px,0) scaleY(5)}60%{opacity:1;-webkit-transform:translate3d(0,-20px,0) scaleY(.9);transform:translate3d(0,-20px,0) scaleY(.9)}75%{-webkit-transform:translate3d(0,10px,0) scaleY(.95);transform:translate3d(0,10px,0) scaleY(.95)}90%{-webkit-transform:translate3d(0,-5px,0) scaleY(.985);transform:translate3d(0,-5px,0) scaleY(.985)}to{-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes bounceOut{20%{-webkit-transform:scale3d(.9,.9,.9);transform:scale3d(.9,.9,.9)}50%,55%{opacity:1;-webkit-transform:scale3d(1.1,1.1,1.1);transform:scale3d(1.1,1.1,1.1)}to{opacity:0;-webkit-transform:scale3d(.3,.3,.3);transform:scale3d(.3,.3,.3)}}@-webkit-keyframes bounceOutDown{20%{-webkit-transform:translate3d(0,10px,0) scaleY(.985);transform:translate3d(0,10px,0) scaleY(.985)}40%,45%{opacity:1;-webkit-transform:translate3d(0,-20px,0) scaleY(.9);transform:translate3d(0,-20px,0) scaleY(.9)}to{opacity:0;-webkit-transform:translate3d(0,2000px,0) scaleY(3);transform:translate3d(0,2000px,0) scaleY(3)}}@-webkit-keyframes bounceOutLeft{20%{opacity:1;-webkit-transform:translate3d(20px,0,0) scaleX(.9);transform:translate3d(20px,0,0) scaleX(.9)}to{opacity:0;-webkit-transform:translate3d(-2000px,0,0) scaleX(2);transform:translate3d(-2000px,0,0) scaleX(2)}}@-webkit-keyframes bounceOutRight{20%{opacity:1;-webkit-transform:translate3d(-20px,0,0) scaleX(.9);transform:translate3d(-20px,0,0) scaleX(.9)}to{opacity:0;-webkit-transform:translate3d(2000px,0,0) scaleX(2);transform:translate3d(2000px,0,0) scaleX(2)}}@-webkit-keyframes bounceOutUp{20%{-webkit-transform:translate3d(0,-10px,0) scaleY(.985);transform:translate3d(0,-10px,0) scaleY(.985)}40%,45%{opacity:1;-webkit-transform:translate3d(0,20px,0) scaleY(.9);transform:translate3d(0,20px,0) scaleY(.9)}to{opacity:0;-webkit-transform:translate3d(0,-2000px,0) scaleY(3);transform:translate3d(0,-2000px,0) scaleY(3)}}@-webkit-keyframes fadeIn{0%{opacity:0}to{opacity:1}}@-webkit-keyframes fadeInDown{0%{opacity:0;-webkit-transform:translate3d(0,-100%,0);transform:translate3d(0,-100%,0)}to{opacity:1;-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes fadeInDownBig{0%{opacity:0;-webkit-transform:translate3d(0,-2000px,0);transform:translate3d(0,-2000px,0)}to{opacity:1;-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes fadeInLeft{0%{opacity:0;-webkit-transform:translate3d(-100%,0,0);transform:translate3d(-100%,0,0)}to{opacity:1;-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes fadeInLeftBig{0%{opacity:0;-webkit-transform:translate3d(-2000px,0,0);transform:translate3d(-2000px,0,0)}to{opacity:1;-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes fadeInRight{0%{opacity:0;-webkit-transform:translate3d(100%,0,0);transform:translate3d(100%,0,0)}to{opacity:1;-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes fadeInRightBig{0%{opacity:0;-webkit-transform:translate3d(2000px,0,0);transform:translate3d(2000px,0,0)}to{opacity:1;-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes fadeInUp{0%{opacity:0;-webkit-transform:translate3d(0,100%,0);transform:translate3d(0,100%,0)}to{opacity:1;-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes fadeInUpBig{0%{opacity:0;-webkit-transform:translate3d(0,2000px,0);transform:translate3d(0,2000px,0)}to{opacity:1;-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes fadeInTopLeft{0%{opacity:0;-webkit-transform:translate3d(-100%,-100%,0);transform:translate3d(-100%,-100%,0)}to{opacity:1;-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes fadeInTopRight{0%{opacity:0;-webkit-transform:translate3d(100%,-100%,0);transform:translate3d(100%,-100%,0)}to{opacity:1;-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes fadeInBottomLeft{0%{opacity:0;-webkit-transform:translate3d(-100%,100%,0);transform:translate3d(-100%,100%,0)}to{opacity:1;-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes fadeInBottomRight{0%{opacity:0;-webkit-transform:translate3d(100%,100%,0);transform:translate3d(100%,100%,0)}to{opacity:1;-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes fadeOut{0%{opacity:1}to{opacity:0}}@-webkit-keyframes fadeOutDown{0%{opacity:1}to{opacity:0;-webkit-transform:translate3d(0,100%,0);transform:translate3d(0,100%,0)}}@-webkit-keyframes fadeOutDownBig{0%{opacity:1}to{opacity:0;-webkit-transform:translate3d(0,2000px,0);transform:translate3d(0,2000px,0)}}@-webkit-keyframes fadeOutLeft{0%{opacity:1}to{opacity:0;-webkit-transform:translate3d(-100%,0,0);transform:translate3d(-100%,0,0)}}@-webkit-keyframes fadeOutLeftBig{0%{opacity:1}to{opacity:0;-webkit-transform:translate3d(-2000px,0,0);transform:translate3d(-2000px,0,0)}}@-webkit-keyframes fadeOutRight{0%{opacity:1}to{opacity:0;-webkit-transform:translate3d(100%,0,0);transform:translate3d(100%,0,0)}}@-webkit-keyframes fadeOutRightBig{0%{opacity:1}to{opacity:0;-webkit-transform:translate3d(2000px,0,0);transform:translate3d(2000px,0,0)}}@-webkit-keyframes fadeOutUp{0%{opacity:1}to{opacity:0;-webkit-transform:translate3d(0,-100%,0);transform:translate3d(0,-100%,0)}}@-webkit-keyframes fadeOutUpBig{0%{opacity:1}to{opacity:0;-webkit-transform:translate3d(0,-2000px,0);transform:translate3d(0,-2000px,0)}}@-webkit-keyframes fadeOutTopLeft{0%{opacity:1;-webkit-transform:translateZ(0);transform:translateZ(0)}to{opacity:0;-webkit-transform:translate3d(-100%,-100%,0);transform:translate3d(-100%,-100%,0)}}@-webkit-keyframes fadeOutTopRight{0%{opacity:1;-webkit-transform:translateZ(0);transform:translateZ(0)}to{opacity:0;-webkit-transform:translate3d(100%,-100%,0);transform:translate3d(100%,-100%,0)}}@-webkit-keyframes fadeOutBottomRight{0%{opacity:1;-webkit-transform:translateZ(0);transform:translateZ(0)}to{opacity:0;-webkit-transform:translate3d(100%,100%,0);transform:translate3d(100%,100%,0)}}@-webkit-keyframes fadeOutBottomLeft{0%{opacity:1;-webkit-transform:translateZ(0);transform:translateZ(0)}to{opacity:0;-webkit-transform:translate3d(-100%,100%,0);transform:translate3d(-100%,100%,0)}}@-webkit-keyframes flip{0%{-webkit-transform:perspective(400px) scaleX(1) translateZ(0) rotateY(-1turn);transform:perspective(400px) scaleX(1) translateZ(0) rotateY(-1turn);-webkit-animation-timing-function:ease-out;animation-timing-function:ease-out}40%{-webkit-transform:perspective(400px) scaleX(1) translateZ(150px) rotateY(-190deg);transform:perspective(400px) scaleX(1) translateZ(150px) rotateY(-190deg);-webkit-animation-timing-function:ease-out;animation-timing-function:ease-out}50%{-webkit-transform:perspective(400px) scaleX(1) translateZ(150px) rotateY(-170deg);transform:perspective(400px) scaleX(1) translateZ(150px) rotateY(-170deg);-webkit-animation-timing-function:ease-in;animation-timing-function:ease-in}80%{-webkit-transform:perspective(400px) scale3d(.95,.95,.95) translateZ(0) rotateY(0deg);transform:perspective(400px) scale3d(.95,.95,.95) translateZ(0) rotateY(0);-webkit-animation-timing-function:ease-in;animation-timing-function:ease-in}to{-webkit-transform:perspective(400px) scaleX(1) translateZ(0) rotateY(0deg);transform:perspective(400px) scaleX(1) translateZ(0) rotateY(0);-webkit-animation-timing-function:ease-in;animation-timing-function:ease-in}}@-webkit-keyframes flipInX{0%{-webkit-transform:perspective(400px) rotateX(90deg);transform:perspective(400px) rotateX(90deg);-webkit-animation-timing-function:ease-in;animation-timing-function:ease-in;opacity:0}40%{-webkit-transform:perspective(400px) rotateX(-20deg);transform:perspective(400px) rotateX(-20deg);-webkit-animation-timing-function:ease-in;animation-timing-function:ease-in}60%{-webkit-transform:perspective(400px) rotateX(10deg);transform:perspective(400px) rotateX(10deg);opacity:1}80%{-webkit-transform:perspective(400px) rotateX(-5deg);transform:perspective(400px) rotateX(-5deg)}to{-webkit-transform:perspective(400px);transform:perspective(400px)}}@-webkit-keyframes flipInY{0%{-webkit-transform:perspective(400px) rotateY(90deg);transform:perspective(400px) rotateY(90deg);-webkit-animation-timing-function:ease-in;animation-timing-function:ease-in;opacity:0}40%{-webkit-transform:perspective(400px) rotateY(-20deg);transform:perspective(400px) rotateY(-20deg);-webkit-animation-timing-function:ease-in;animation-timing-function:ease-in}60%{-webkit-transform:perspective(400px) rotateY(10deg);transform:perspective(400px) rotateY(10deg);opacity:1}80%{-webkit-transform:perspective(400px) rotateY(-5deg);transform:perspective(400px) rotateY(-5deg)}to{-webkit-transform:perspective(400px);transform:perspective(400px)}}@-webkit-keyframes flipOutX{0%{-webkit-transform:perspective(400px);transform:perspective(400px)}30%{-webkit-transform:perspective(400px) rotateX(-20deg);transform:perspective(400px) rotateX(-20deg);opacity:1}to{-webkit-transform:perspective(400px) rotateX(90deg);transform:perspective(400px) rotateX(90deg);opacity:0}}@-webkit-keyframes flipOutY{0%{-webkit-transform:perspective(400px);transform:perspective(400px)}30%{-webkit-transform:perspective(400px) rotateY(-15deg);transform:perspective(400px) rotateY(-15deg);opacity:1}to{-webkit-transform:perspective(400px) rotateY(90deg);transform:perspective(400px) rotateY(90deg);opacity:0}}@-webkit-keyframes lightSpeedInRight{0%{-webkit-transform:translate3d(100%,0,0) skewX(-30deg);transform:translate3d(100%,0,0) skew(-30deg);opacity:0}60%{-webkit-transform:skewX(20deg);transform:skew(20deg);opacity:1}80%{-webkit-transform:skewX(-5deg);transform:skew(-5deg)}to{-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes lightSpeedInLeft{0%{-webkit-transform:translate3d(-100%,0,0) skewX(30deg);transform:translate3d(-100%,0,0) skew(30deg);opacity:0}60%{-webkit-transform:skewX(-20deg);transform:skew(-20deg);opacity:1}80%{-webkit-transform:skewX(5deg);transform:skew(5deg)}to{-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes lightSpeedOutRight{0%{opacity:1}to{-webkit-transform:translate3d(100%,0,0) skewX(30deg);transform:translate3d(100%,0,0) skew(30deg);opacity:0}}@-webkit-keyframes lightSpeedOutLeft{0%{opacity:1}to{-webkit-transform:translate3d(-100%,0,0) skewX(-30deg);transform:translate3d(-100%,0,0) skew(-30deg);opacity:0}}@-webkit-keyframes rotateIn{0%{-webkit-transform:rotate(-200deg);transform:rotate(-200deg);opacity:0}to{-webkit-transform:translateZ(0);transform:translateZ(0);opacity:1}}@-webkit-keyframes rotateInDownLeft{0%{-webkit-transform:rotate(-45deg);transform:rotate(-45deg);opacity:0}to{-webkit-transform:translateZ(0);transform:translateZ(0);opacity:1}}@-webkit-keyframes rotateInDownRight{0%{-webkit-transform:rotate(45deg);transform:rotate(45deg);opacity:0}to{-webkit-transform:translateZ(0);transform:translateZ(0);opacity:1}}@-webkit-keyframes rotateInUpLeft{0%{-webkit-transform:rotate(45deg);transform:rotate(45deg);opacity:0}to{-webkit-transform:translateZ(0);transform:translateZ(0);opacity:1}}@-webkit-keyframes rotateInUpRight{0%{-webkit-transform:rotate(-90deg);transform:rotate(-90deg);opacity:0}to{-webkit-transform:translateZ(0);transform:translateZ(0);opacity:1}}@-webkit-keyframes rotateOut{0%{opacity:1}to{-webkit-transform:rotate(200deg);transform:rotate(200deg);opacity:0}}@-webkit-keyframes rotateOutDownLeft{0%{opacity:1}to{-webkit-transform:rotate(45deg);transform:rotate(45deg);opacity:0}}@-webkit-keyframes rotateOutDownRight{0%{opacity:1}to{-webkit-transform:rotate(-45deg);transform:rotate(-45deg);opacity:0}}@-webkit-keyframes rotateOutUpLeft{0%{opacity:1}to{-webkit-transform:rotate(-45deg);transform:rotate(-45deg);opacity:0}}@-webkit-keyframes rotateOutUpRight{0%{opacity:1}to{-webkit-transform:rotate(90deg);transform:rotate(90deg);opacity:0}}@-webkit-keyframes hinge{0%{-webkit-animation-timing-function:ease-in-out;animation-timing-function:ease-in-out}20%,60%{-webkit-transform:rotate(80deg);transform:rotate(80deg);-webkit-animation-timing-function:ease-in-out;animation-timing-function:ease-in-out}40%,80%{-webkit-transform:rotate(60deg);transform:rotate(60deg);-webkit-animation-timing-function:ease-in-out;animation-timing-function:ease-in-out;opacity:1}to{-webkit-transform:translate3d(0,700px,0);transform:translate3d(0,700px,0);opacity:0}}@-webkit-keyframes jackInTheBox{0%{opacity:0;-webkit-transform:scale(.1) rotate(30deg);transform:scale(.1) rotate(30deg);-webkit-transform-origin:center bottom;transform-origin:center bottom}50%{-webkit-transform:rotate(-10deg);transform:rotate(-10deg)}70%{-webkit-transform:rotate(3deg);transform:rotate(3deg)}to{opacity:1;-webkit-transform:scale(1);transform:scale(1)}}@-webkit-keyframes rollIn{0%{opacity:0;-webkit-transform:translate3d(-100%,0,0) rotate(-120deg);transform:translate3d(-100%,0,0) rotate(-120deg)}to{opacity:1;-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes rollOut{0%{opacity:1}to{opacity:0;-webkit-transform:translate3d(100%,0,0) rotate(120deg);transform:translate3d(100%,0,0) rotate(120deg)}}@-webkit-keyframes zoomIn{0%{opacity:0;-webkit-transform:scale3d(.3,.3,.3);transform:scale3d(.3,.3,.3)}50%{opacity:1}}@-webkit-keyframes zoomInDown{0%{opacity:0;-webkit-transform:scale3d(.1,.1,.1) translate3d(0,-1000px,0);transform:scale3d(.1,.1,.1) translate3d(0,-1000px,0);-webkit-animation-timing-function:cubic-bezier(.55,.055,.675,.19);animation-timing-function:cubic-bezier(.55,.055,.675,.19)}60%{opacity:1;-webkit-transform:scale3d(.475,.475,.475) translate3d(0,60px,0);transform:scale3d(.475,.475,.475) translate3d(0,60px,0);-webkit-animation-timing-function:cubic-bezier(.175,.885,.32,1);animation-timing-function:cubic-bezier(.175,.885,.32,1)}}@-webkit-keyframes zoomInLeft{0%{opacity:0;-webkit-transform:scale3d(.1,.1,.1) translate3d(-1000px,0,0);transform:scale3d(.1,.1,.1) translate3d(-1000px,0,0);-webkit-animation-timing-function:cubic-bezier(.55,.055,.675,.19);animation-timing-function:cubic-bezier(.55,.055,.675,.19)}60%{opacity:1;-webkit-transform:scale3d(.475,.475,.475) translate3d(10px,0,0);transform:scale3d(.475,.475,.475) translate3d(10px,0,0);-webkit-animation-timing-function:cubic-bezier(.175,.885,.32,1);animation-timing-function:cubic-bezier(.175,.885,.32,1)}}@-webkit-keyframes zoomInRight{0%{opacity:0;-webkit-transform:scale3d(.1,.1,.1) translate3d(1000px,0,0);transform:scale3d(.1,.1,.1) translate3d(1000px,0,0);-webkit-animation-timing-function:cubic-bezier(.55,.055,.675,.19);animation-timing-function:cubic-bezier(.55,.055,.675,.19)}60%{opacity:1;-webkit-transform:scale3d(.475,.475,.475) translate3d(-10px,0,0);transform:scale3d(.475,.475,.475) translate3d(-10px,0,0);-webkit-animation-timing-function:cubic-bezier(.175,.885,.32,1);animation-timing-function:cubic-bezier(.175,.885,.32,1)}}@-webkit-keyframes zoomInUp{0%{opacity:0;-webkit-transform:scale3d(.1,.1,.1) translate3d(0,1000px,0);transform:scale3d(.1,.1,.1) translate3d(0,1000px,0);-webkit-animation-timing-function:cubic-bezier(.55,.055,.675,.19);animation-timing-function:cubic-bezier(.55,.055,.675,.19)}60%{opacity:1;-webkit-transform:scale3d(.475,.475,.475) translate3d(0,-60px,0);transform:scale3d(.475,.475,.475) translate3d(0,-60px,0);-webkit-animation-timing-function:cubic-bezier(.175,.885,.32,1);animation-timing-function:cubic-bezier(.175,.885,.32,1)}}@-webkit-keyframes zoomOut{0%{opacity:1}50%{opacity:0;-webkit-transform:scale3d(.3,.3,.3);transform:scale3d(.3,.3,.3)}to{opacity:0}}@-webkit-keyframes zoomOutDown{40%{opacity:1;-webkit-transform:scale3d(.475,.475,.475) translate3d(0,-60px,0);transform:scale3d(.475,.475,.475) translate3d(0,-60px,0);-webkit-animation-timing-function:cubic-bezier(.55,.055,.675,.19);animation-timing-function:cubic-bezier(.55,.055,.675,.19)}to{opacity:0;-webkit-transform:scale3d(.1,.1,.1) translate3d(0,2000px,0);transform:scale3d(.1,.1,.1) translate3d(0,2000px,0);-webkit-animation-timing-function:cubic-bezier(.175,.885,.32,1);animation-timing-function:cubic-bezier(.175,.885,.32,1)}}@-webkit-keyframes zoomOutLeft{40%{opacity:1;-webkit-transform:scale3d(.475,.475,.475) translate3d(42px,0,0);transform:scale3d(.475,.475,.475) translate3d(42px,0,0)}to{opacity:0;-webkit-transform:scale(.1) translate3d(-2000px,0,0);transform:scale(.1) translate3d(-2000px,0,0)}}@-webkit-keyframes zoomOutRight{40%{opacity:1;-webkit-transform:scale3d(.475,.475,.475) translate3d(-42px,0,0);transform:scale3d(.475,.475,.475) translate3d(-42px,0,0)}to{opacity:0;-webkit-transform:scale(.1) translate3d(2000px,0,0);transform:scale(.1) translate3d(2000px,0,0)}}@-webkit-keyframes zoomOutUp{40%{opacity:1;-webkit-transform:scale3d(.475,.475,.475) translate3d(0,60px,0);transform:scale3d(.475,.475,.475) translate3d(0,60px,0);-webkit-animation-timing-function:cubic-bezier(.55,.055,.675,.19);animation-timing-function:cubic-bezier(.55,.055,.675,.19)}to{opacity:0;-webkit-transform:scale3d(.1,.1,.1) translate3d(0,-2000px,0);transform:scale3d(.1,.1,.1) translate3d(0,-2000px,0);-webkit-animation-timing-function:cubic-bezier(.175,.885,.32,1);animation-timing-function:cubic-bezier(.175,.885,.32,1)}}@-webkit-keyframes slideInDown{0%{-webkit-transform:translate3d(0,-100%,0);transform:translate3d(0,-100%,0);visibility:visible}to{-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes slideInLeft{0%{-webkit-transform:translate3d(-100%,0,0);transform:translate3d(-100%,0,0);visibility:visible}to{-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes slideInRight{0%{-webkit-transform:translate3d(100%,0,0);transform:translate3d(100%,0,0);visibility:visible}to{-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes slideInUp{0%{-webkit-transform:translate3d(0,100%,0);transform:translate3d(0,100%,0);visibility:visible}to{-webkit-transform:translateZ(0);transform:translateZ(0)}}@-webkit-keyframes slideOutDown{0%{-webkit-transform:translateZ(0);transform:translateZ(0)}to{visibility:hidden;-webkit-transform:translate3d(0,100%,0);transform:translate3d(0,100%,0)}}@-webkit-keyframes slideOutLeft{0%{-webkit-transform:translateZ(0);transform:translateZ(0)}to{visibility:hidden;-webkit-transform:translate3d(-100%,0,0);transform:translate3d(-100%,0,0)}}@-webkit-keyframes slideOutRight{0%{-webkit-transform:translateZ(0);transform:translateZ(0)}to{visibility:hidden;-webkit-transform:translate3d(100%,0,0);transform:translate3d(100%,0,0)}}@-webkit-keyframes slideOutUp{0%{-webkit-transform:translateZ(0);transform:translateZ(0)}to{visibility:hidden;-webkit-transform:translate3d(0,-100%,0);transform:translate3d(0,-100%,0)}}:root{--background-img: linear-gradient( to right, rgba(255, 255, 255, .884), rgba(255, 255, 255, .884) ), url("./media/fondo-gris-claro-DN6623XU.png");--text-color: #000}.main-content{background:var(--background-img);background-repeat:no-repeat;background-attachment:fixed;color:var(--text-color);transition:background .3s ease,color .3s ease;margin:0;font-family:Arial,sans-serif}.text-parrafo{color:#000}.contenedor-menu-principal{background-color:#fff;border:1px solid #e6d194;border-radius:16px;box-shadow:0 8px 24px #0000000f;padding:30px}.img-dark{display:none}.img-light{display:block}*{scrollbar-color:#7c7c7c #f0f0f000}.arrow-icon-section{color:#a11a5c}.costo-color-text{color:#64ad0b}ol li::marker{color:#64ad0b!important;font-weight:700;font-size:1.1em}ul li::marker{color:#64ad0b!important;font-weight:700;font-size:1.1em}.text-justify-custom{text-align:justify}@media (max-width: 767px){.back-to-top{bottom:70px;left:10px;margin-bottom:50px}}
</style><link rel="stylesheet" href="styles-KDTWX2PR.css" media="print" onload="this.media='all'"><noscript><link rel="stylesheet" href="styles-KDTWX2PR.css"></noscript><style ng-app-id="ng">.theme-switch-wrapper[_ngcontent-ng-c660411309]{cursor:pointer;display:flex;align-items:center;height:32px}.theme-switch[_ngcontent-ng-c660411309]{width:60px;height:28px;background-color:#ccc;border-radius:50px;position:relative;transition:background-color .3s ease}.theme-switch-wrapper.dark[_ngcontent-ng-c660411309]   .theme-switch[_ngcontent-ng-c660411309]{background-color:#4c4c4c}.switch-track[_ngcontent-ng-c660411309]{width:100%;height:100%;position:relative}.switch-thumb[_ngcontent-ng-c660411309]{width:24px;height:24px;background-color:#fff;border-radius:50%;position:absolute;top:2px;left:2px;display:flex;justify-content:center;align-items:center;transition:left .3s ease,background-color .3s ease;font-size:14px;color:#333}.theme-switch-wrapper.dark[_ngcontent-ng-c660411309]   .switch-thumb[_ngcontent-ng-c660411309]{left:34px;background-color:#f1c40f;color:#000}.home-icon[_ngcontent-ng-c660411309]{color:#dbdbdb!important;transition:transform .2s ease,color .2s ease}.home-icon[_ngcontent-ng-c660411309]:hover{transform:scale(1.2);color:#fff!important}img.rounded-circle[_ngcontent-ng-c660411309]{border:3px solid #dfe3e8;background-color:#f8f9fa;padding:1px}.background-menu-tramites[_ngcontent-ng-c660411309]{background-color:#a11a5c;color:#fff}.main-content[_ngcontent-ng-c660411309]{overflow-y:auto;height:100vh}.navbar-color[_ngcontent-ng-c660411309]{background-color:#422b7c;color:#fff}.layout-container[_ngcontent-ng-c660411309]{height:100vh;overflow:hidden}.sidebar[_ngcontent-ng-c660411309]{width:300px;height:100vh;position:fixed;top:0;left:0;z-index:1020;overflow-y:auto;box-shadow:0 4px 12px #0000009b}.main-content-wrapper[_ngcontent-ng-c660411309]{height:100vh;overflow-y:auto;display:flex;flex-direction:column;margin-left:0}@media (min-width: 992px){.main-content-wrapper[_ngcontent-ng-c660411309]{margin-left:300px}}.nav-link[_ngcontent-ng-c660411309]{color:#000!important;transition:background-color .3s ease,color .3s ease}.nav-link[aria-expanded=true][_ngcontent-ng-c660411309]{color:#000!important;font-weight:600}.nav-link[aria-expanded=true][_ngcontent-ng-c660411309]:before{transform:rotate(80deg)}.nav-link[_ngcontent-ng-c660411309]:hover{background-color:#a11a5b10;color:#000;transition:background-color .1s ease-in-out;border-radius:5px}.nav-link.active[_ngcontent-ng-c660411309]{background-color:#a11a5b17;color:#000!important;font-weight:500}.arrow-icon-section[_ngcontent-ng-c660411309]{color:#a11a5c}.arrow-icon-sub-secticon[_ngcontent-ng-c660411309]{color:#6fb41b}.rotate-icon[_ngcontent-ng-c660411309]{transition:transform .3s ease}.collapsed[_ngcontent-ng-c660411309]   .rotate-icon[_ngcontent-ng-c660411309]{transform:rotate(0)}.rotate-icon[_ngcontent-ng-c660411309]:not(.collapsed){transform:rotate(180deg)}.list-group-item[_ngcontent-ng-c660411309]{background-color:#fff;color:#000;cursor:pointer;font-weight:400}.list-group-item[_ngcontent-ng-c660411309]:hover, .list-group-item.active[_ngcontent-ng-c660411309]{background-color:#a11a5c;color:#fff;cursor:pointer;font-weight:400}.not-search[_ngcontent-ng-c660411309]{background-color:#fff;color:#000;cursor:pointer;font-weight:400}.no-borders[_ngcontent-ng-c660411309]   .list-group-item[_ngcontent-ng-c660411309], .no-borders[_ngcontent-ng-c660411309]{border:none!important}.social-bar[_ngcontent-ng-c660411309]{position:fixed;top:50%;right:1rem;transform:translateY(-50%);display:flex;flex-direction:column;gap:1.2rem;padding:.4rem .6rem;border-radius:8px;z-index:1000;width:56px}@media (max-width: 768px){.social-bar[_ngcontent-ng-c660411309]{flex-direction:row;bottom:1rem;top:auto;right:50%;transform:translate(50%);width:auto;padding:.5rem 1rem;border-radius:10px}}.social-bar[_ngcontent-ng-c660411309]   a[_ngcontent-ng-c660411309]{color:#fff;font-size:1.8rem;text-decoration:none;display:flex;align-items:center;justify-content:center;width:40px;height:40px;border-radius:6px;box-shadow:0 2px 5px #00000026;border:none;cursor:pointer;transition:transform .3s ease,box-shadow .3s ease}.social-bar[_ngcontent-ng-c660411309]   a[_ngcontent-ng-c660411309]:hover{transform:translateY(-4px) scale(1.1);box-shadow:0 6px 12px #0000004d}.social-bar[_ngcontent-ng-c660411309]   a.facebook[_ngcontent-ng-c660411309]{background-color:#3b5998}.social-bar[_ngcontent-ng-c660411309]   a.twitter[_ngcontent-ng-c660411309]{background-color:#000}.social-bar[_ngcontent-ng-c660411309]   a.instagram[_ngcontent-ng-c660411309]{background-color:#e4405f}</style><style ng-app-id="ng">.footer-bg[_ngcontent-ng-c2808021136]{background-color:#f7f9f9;color:#000}.back-to-top[_ngcontent-ng-c2808021136]{background:linear-gradient(145deg,#2b5ebb,#1e4fa9);color:#fff;border:none;box-shadow:0 4px 8px #2b5ebb4d;transition:all .3s ease;width:42px;height:42px;font-size:18px;display:flex;align-items:center;justify-content:center;animation:_ngcontent-ng-c2808021136_pulse 2s infinite}.back-to-top[_ngcontent-ng-c2808021136]:hover{transform:translateY(-4px) scale(1.1);background:linear-gradient(145deg,#1e4fa9,#163f8a);box-shadow:0 6px 12px #1e4fa966;color:#fff}@keyframes _ngcontent-ng-c2808021136_pulse{0%{box-shadow:0 0 #2b5ebb66}70%{box-shadow:0 0 0 10px #2b5ebb00}to{box-shadow:0 0 #2b5ebb00}}@media (max-width: 767px){.back-to-top[_ngcontent-ng-c2808021136]{bottom:70px;left:10px;margin-bottom:50px}}</style><style ng-app-id="ng">.btn-custom-of[_ngcontent-ng-c3386797310]{background-color:#64ad0b!important;color:#fff!important}.btn-custom-of[_ngcontent-ng-c3386797310]:hover{background-color:#5a9e07!important;color:#fff!important}.tittle-sub-menu[_ngcontent-ng-c3386797310]{color:#2856ad!important;font-weight:600}.card-menu-principal[_ngcontent-ng-c3386797310]{border:none;border-radius:14px;background-color:#f8f9fa;box-shadow:0 4px 16px #0000000f;transition:transform .2s ease,box-shadow .2s ease;border:1px solid #e6d194}</style></head>

<body class="mat-typography"><!--nghm--><script type="text/javascript" id="ng-event-dispatch-contract">(()=>{function p(t,n,r,o,e,i,f,m){return{eventType:t,event:n,targetElement:r,eic:o,timeStamp:e,eia:i,eirp:f,eiack:m}}function u(t){let n=[],r=e=>{n.push(e)};return{c:t,q:n,et:[],etc:[],d:r,h:e=>{r(p(e.type,e,e.target,t,Date.now()))}}}function s(t,n,r){for(let o=0;o<n.length;o++){let e=n[o];(r?t.etc:t.et).push(e),t.c.addEventListener(e,t.h,r)}}function c(t,n,r,o,e=window){let i=u(t);e._ejsas||(e._ejsas={}),e._ejsas[n]=i,s(i,r),s(i,o,!0)}window.__jsaction_bootstrap=c;})();
</script><script>window.__jsaction_bootstrap(document.body,"ng",["click","submit","input","compositionstart","compositionend"],["blur"]);</script>

  <script src="https://cdn.botpress.cloud/webchat/v3.0/inject.js"></script>
  <script src="https://files.bpcontent.cloud/2025/07/04/16/20250704163022-VVZRU8NA.js"></script>

  <app-root ng-version="19.2.6" _nghost-ng-c660411309 ngh="1" ng-server-context="ssg"><div _ngcontent-ng-c660411309 class="layout-container d-flex"><nav _ngcontent-ng-c660411309 class="sidebar sidebar-color d-none d-lg-block"><div _ngcontent-ng-c660411309 class="text-center background-menu-tramites pt-2"><img _ngcontent-ng-c660411309 src="assets/img/imagenesCuerpo/perfil-admin-SMyT.png" alt="Foto de perfil" class="rounded-circle" style="width: 80px; height: 80px; object-fit: cover;"><h6 _ngcontent-ng-c660411309 class="mt-2 pb-2">MenÃº de TrÃ¡mites y Servicios</h6></div><ul _ngcontent-ng-c660411309 class="nav nav-pills flex-column mb-3"><li _ngcontent-ng-c660411309 class="nav-item mb-2"><a _ngcontent-ng-c660411309 routerlink="/ventanilla-unica" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-center" href="/ventanilla-unica" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-house-door-fill me-2 arrow-icon-section"></i> Menu Principal </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-2"><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="collapse" data-bs-target="#collapseOficialiaMayorGobierno" aria-expanded="false" aria-controls="collapseOficialiaMayorGobierno" class="nav-link d-flex align-items-center w-100 text-start collapsed"><i _ngcontent-ng-c660411309 class="bi bi-building-fill me-2 arrow-icon-section"></i> OficialÃ­a Mayor de Gobierno <i _ngcontent-ng-c660411309 class="bi bi-chevron-down ms-auto rotate-icon"></i></button><div _ngcontent-ng-c660411309 id="collapseOficialiaMayorGobierno" class="collapse ps-4 mt-1"><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/bases-licitaciones-adquisiciones" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/bases-licitaciones-adquisiciones" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Pago de Bases de LicitaciÃ³n para Adquisiciones </a></li><li _ngcontent-ng-c660411309 class="nav-item"><a _ngcontent-ng-c660411309 routerlink="/bases-licitaciones-obra-publica" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/bases-licitaciones-obra-publica" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Pago de Bases de LicitaciÃ³n para ContrataciÃ³n de Obra PÃºblica </a></li><li _ngcontent-ng-c660411309 class="nav-item"><a _ngcontent-ng-c660411309 routerlink="/pago-inscripcion-padron-proveedores" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/pago-inscripcion-padron-proveedores" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Pago de InscripciÃ³n al PadrÃ³n de Proveedores </a></li></ul></div></li><li _ngcontent-ng-c660411309 class="nav-item mb-2"><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="collapse" data-bs-target="#collapseSecretariaDeBienestar" aria-expanded="false" aria-controls="collapseSecretariaDeBienestar" class="nav-link d-flex align-items-center w-100 text-start collapsed"><i _ngcontent-ng-c660411309 class="bi bi-box2-heart-fill me-2 arrow-icon-section"></i> SecretarÃ­a de Bienestar <i _ngcontent-ng-c660411309 class="bi bi-chevron-down ms-auto rotate-icon"></i></button><div _ngcontent-ng-c660411309 id="collapseSecretariaDeBienestar" class="collapse ps-4 mt-1"><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/solicitud-programa-ayudas-funcionales" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/solicitud-programa-ayudas-funcionales" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Solicitud al Programa de Ayudas Funcionales para Personas con Discapacidad </a></li></ul></div></li><li _ngcontent-ng-c660411309 class="nav-item mb-2"><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="collapse" data-bs-target="#collapseSecretariaDeFinanzas" aria-expanded="false" aria-controls="collapseSecretariaDeFinanzas" class="nav-link d-flex align-items-center w-100 text-start collapsed"><i _ngcontent-ng-c660411309 class="bi bi-cash-stack me-2 arrow-icon-section"></i> SecretarÃ­a de Finanzas <i _ngcontent-ng-c660411309 class="bi bi-chevron-down ms-auto rotate-icon"></i></button><div _ngcontent-ng-c660411309 id="collapseSecretariaDeFinanzas" class="collapse ps-4 mt-1"><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/constancia-de-no-adeudo" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/constancia-de-no-adeudo" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Constancia de no Adeudo de Impuestos Estatales </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/declaraciones-en-cero" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/declaraciones-en-cero" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Declaraciones en Cero </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/generacion-comprobante-fiscal" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/generacion-comprobante-fiscal" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> GeneraciÃ³n de Comprobante Fiscal Digital por Internet - CFDI </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/impresion-comprobante-electronico-pago" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/impresion-comprobante-electronico-pago" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> ImpresiÃ³n de Comprobante ElectrÃ³nico de Pago </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/impuesto-sobre-ejercicio-profesiones" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/impuesto-sobre-ejercicio-profesiones" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Impuesto Sobre Ejercicio de Profesiones </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/impuesto-sobre-enajenacion-bienes-inmuebles" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/impuesto-sobre-enajenacion-bienes-inmuebles" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Impuesto Sobre EnajenaciÃ³n de Bienes Inmuebles </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/impuesto-sobre-funciones-notariales-correduria" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/impuesto-sobre-funciones-notariales-correduria" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Impuesto Sobre Funciones Notariales y CorredurÃ­a PÃºblica </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/impuesto-sobre-nominas" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/impuesto-sobre-nominas" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Impuesto Sobre NÃ³minas </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/impuesto-sobre-prestacion-servicio-hospedaje" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/impuesto-sobre-prestacion-servicio-hospedaje" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Impuesto Sobre la PrestaciÃ³n del Servicio de Hospedaje </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/padron-contribuyente" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/padron-contribuyente" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> PadrÃ³n del Contribuyente </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/padron-unico-de-contratistas-del-estado-de-tlaxcala-y-sus-municipios" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/padron-unico-de-contratistas-del-estado-de-tlaxcala-y-sus-municipios" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> PadrÃ³n Ãnico de Contratistas del Estado de Tlaxcala y sus Municipios </a></li></ul></div></li><li _ngcontent-ng-c660411309 class="nav-item mb-2"><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="collapse" data-bs-target="#collapseSecretariaFuncionPublica" aria-expanded="false" aria-controls="collapseSecretariaFuncionPublica" class="nav-link d-flex align-items-center w-100 text-start collapsed"><i _ngcontent-ng-c660411309 class="bi bi-shield-fill-check me-2 arrow-icon-section"></i> SecretarÃ­a AnticorrupciÃ³n y Buen Gobierno (SecretarÃ­a de la FunciÃ³n PÃºblica) <i _ngcontent-ng-c660411309 class="bi bi-chevron-down ms-auto rotate-icon"></i></button><div _ngcontent-ng-c660411309 id="collapseSecretariaFuncionPublica" class="collapse ps-4 mt-1"><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/constancia-no-inhabilitado" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/constancia-no-inhabilitado" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Constancia de no Inhabilitado </a></li></ul></div></li><li _ngcontent-ng-c660411309 class="nav-item mb-2"><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="collapse" data-bs-target="#collapseSecretariaGobierno" aria-expanded="false" aria-controls="collapseSecretariaGobierno" class="nav-link d-flex align-items-center w-100 text-start collapsed"><i _ngcontent-ng-c660411309 class="bi bi-bank me-2 arrow-icon-section"></i> SecretarÃ­a de Gobierno <i _ngcontent-ng-c660411309 class="bi bi-chevron-down ms-auto rotate-icon"></i></button><div _ngcontent-ng-c660411309 id="collapseSecretariaGobierno" class="collapse ps-4 mt-1"><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/expedicion-certificado-gravamen" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/expedicion-certificado-gravamen" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> ExpediciÃ³n de Certificado de Gravamen </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/expedicion-certificado-inscripcion" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/expedicion-certificado-inscripcion" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> ExpediciÃ³n de Certificado de InscripciÃ³n </a></li></ul></div></li><li _ngcontent-ng-c660411309 class="nav-item mb-2"><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="collapse" data-bs-target="#collapseSecretariaMovilidadTransporte" aria-expanded="false" aria-controls="collapseSecretariaMovilidadTransporte" class="nav-link d-flex align-items-center w-100 text-start collapsed"><i _ngcontent-ng-c660411309 class="bi bi-car-front-fill me-2 arrow-icon-section"></i> SecretarÃ­a de Movilidad y Transporte <i _ngcontent-ng-c660411309 class="bi bi-chevron-down ms-auto rotate-icon"></i></button><div _ngcontent-ng-c660411309 id="collapseSecretariaMovilidadTransporte" class="collapse ps-4 mt-1"><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-2"><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="collapse" data-bs-target="#collapseSecretariaMovilidadTransporteServicioParticular" aria-expanded="false" aria-controls="collapseSecretariaMovilidadTransporteServicioParticular" class="nav-link d-flex align-items-center w-100 text-start collapsed"><i _ngcontent-ng-c660411309 class="bi bi-car-front me-2 arrow-icon-section"></i> Servicio Particular <i _ngcontent-ng-c660411309 class="bi bi-chevron-down ms-auto rotate-icon"></i></button><div _ngcontent-ng-c660411309 id="collapseSecretariaMovilidadTransporteServicioParticular" class="collapse ps-4 mt-1"><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/alta-de-vehiculos-nuevos" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/alta-de-vehiculos-nuevos" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Alta de VehÃ­culos Nuevos </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/baja-de-vehiculos" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/baja-de-vehiculos" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Baja de VehÃ­culos </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/certificado-de-no-infraccion" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/certificado-de-no-infraccion" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Certificado de no InfracciÃ³n </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/permiso-eventual-carga" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/permiso-eventual-carga" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Permiso Eventual de Carga </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/permiso-provisional-de-circulacion" routerlinkactive="active" class="nav-link d-flex align-items-left active" href="/permiso-provisional-de-circulacion" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Permiso Provisional de CirculaciÃ³n </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/refrendo-tenencia" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/refrendo-tenencia" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Refrendo y/o Tenencia </a></li></ul></div></li></ul><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-2"><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="collapse" data-bs-target="#collapseSecretariaMovilidadTransporteServicioPublico" aria-expanded="false" aria-controls="collapseSecretariaMovilidadTransporteServicioPublico" class="nav-link d-flex align-items-center w-100 text-start collapsed"><i _ngcontent-ng-c660411309 class="bi bi-bus-front-fill me-2 arrow-icon-section"></i> Servicio PÃºblico <i _ngcontent-ng-c660411309 class="bi bi-chevron-down ms-auto rotate-icon"></i></button><div _ngcontent-ng-c660411309 id="collapseSecretariaMovilidadTransporteServicioPublico" class="collapse ps-4 mt-1"><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/baja-de-unidad-al-padron-del-servicio-publico" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/baja-de-unidad-al-padron-del-servicio-publico" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Baja de Unidad al PadrÃ³n del Servicio PÃºblico </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/baja-unidad-al-padron-del-servicio-publico-por-robo-vehicular" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/baja-unidad-al-padron-del-servicio-publico-por-robo-vehicular" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Baja de Unidad al PadrÃ³n del Servicio PÃºblico por Robo Vehicular </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/permiso-fuera-de-ruta" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/permiso-fuera-de-ruta" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Permiso Fuera de Ruta </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/refrendo-anual-concesiones" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/refrendo-anual-concesiones" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Refrendo Anual de Concesiones </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/reposicion-de-tarjeta-de-circulacion" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/reposicion-de-tarjeta-de-circulacion" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> ReposiciÃ³n de Tarjeta de CirculaciÃ³n </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/reposicion-de-tarjeta-de-circulacion-por-cambio-de-combustible" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/reposicion-de-tarjeta-de-circulacion-por-cambio-de-combustible" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> ReposiciÃ³n de Tarjeta de CirculaciÃ³n por Cambio de Combustible </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/revista-vehicular" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/revista-vehicular" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Revista Vehicular </a></li></ul></div></li></ul></div></li><li _ngcontent-ng-c660411309 class="nav-item mb-2"><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="collapse" data-bs-target="#collapseServiciosExternos" aria-expanded="false" aria-controls="collapseServiciosExternos" class="nav-link d-flex align-items-center w-100 text-start collapsed"><i _ngcontent-ng-c660411309 class="bi bi-file-earmark-text-fill me-2 arrow-icon-section"></i> Externos <i _ngcontent-ng-c660411309 class="bi bi-chevron-down ms-auto rotate-icon"></i></button><div _ngcontent-ng-c660411309 id="collapseServiciosExternos" class="collapse ps-4 mt-1"><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/acta-nacimiento" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/acta-nacimiento" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Acta de Nacimiento </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/consultar-CURP" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/consultar-CURP" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Obtener e Imprimir CURP </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/consultar-RFC" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/consultar-RFC" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Consultar RFC </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/cedula-identificacion-fiscal" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/cedula-identificacion-fiscal" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Obtener CÃ©dula de IdentificaciÃ³n Fiscal </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/constancia-situacion-fiscal" routerlinkactive="active" class="nav-link d-flex align-items-left" href="/constancia-situacion-fiscal" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Generar Constancia de SituaciÃ³n Fiscal </a></li></ul></div></li></ul></nav><div _ngcontent-ng-c660411309 tabindex="-1" id="mobileSidebar" aria-labelledby="offcanvasLabel" class="offcanvas offcanvas-start sidebar-color"><div _ngcontent-ng-c660411309 class="offcanvas-header background-menu-tramites"><button _ngcontent-ng-c660411309 type="button" data-bs-dismiss="offcanvas" class="btn-close btn-close-white text-reset"></button></div><div _ngcontent-ng-c660411309 class="offcanvas-body d-flex flex-column h-100"><div _ngcontent-ng-c660411309 class="text-center background-menu-tramites pt-2 mb-4"><img _ngcontent-ng-c660411309 src="assets/img/imagenesCuerpo/perfil-admin-SMyT.png" alt="Foto de perfil" class="rounded-circle" style="width: 60px; height: 60px; object-fit: cover;"><h6 _ngcontent-ng-c660411309 class="mt-2 pb-2">MenÃº de TrÃ¡mites y Servicios</h6></div><ul _ngcontent-ng-c660411309 class="nav nav-pills flex-column mb-3"><li _ngcontent-ng-c660411309 class="nav-item mb-2"><a _ngcontent-ng-c660411309 routerlink="/ventanilla-unica" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-center" href="/ventanilla-unica" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-house-door-fill me-2 arrow-icon-section"></i> Menu Principal </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-2"><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="collapse" data-bs-target="#collapseOficialiaMayorGobierno" aria-expanded="false" aria-controls="collapseOficialiaMayorGobierno" class="nav-link d-flex align-items-center w-100 text-start collapsed"><i _ngcontent-ng-c660411309 class="bi bi-building-fill me-2 arrow-icon-section"></i> OficialÃ­a Mayor de Gobierno <i _ngcontent-ng-c660411309 class="bi bi-chevron-down ms-auto rotate-icon"></i></button><div _ngcontent-ng-c660411309 id="collapseOficialiaMayorGobierno" class="collapse ps-4 mt-1"><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/bases-licitaciones-adquisiciones" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/bases-licitaciones-adquisiciones" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Pago de Bases de LicitaciÃ³n para Adquisiciones </a></li><li _ngcontent-ng-c660411309 class="nav-item"><a _ngcontent-ng-c660411309 routerlink="/bases-licitaciones-obra-publica" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/bases-licitaciones-obra-publica" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Pago de Bases de LicitaciÃ³n para ContrataciÃ³n de Obra PÃºblica </a></li><li _ngcontent-ng-c660411309 class="nav-item"><a _ngcontent-ng-c660411309 routerlink="/pago-inscripcion-padron-proveedores" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/pago-inscripcion-padron-proveedores" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Pago de InscripciÃ³n al PadrÃ³n de Proveedores </a></li></ul></div></li><li _ngcontent-ng-c660411309 class="nav-item mb-2"><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="collapse" data-bs-target="#collapseSecretariaDeBienestar" aria-expanded="false" aria-controls="collapseSecretariaDeBienestar" class="nav-link d-flex align-items-center w-100 text-start collapsed"><i _ngcontent-ng-c660411309 class="bi bi-box2-heart-fill me-2 arrow-icon-section"></i> SecretarÃ­a de Bienestar <i _ngcontent-ng-c660411309 class="bi bi-chevron-down ms-auto rotate-icon"></i></button><div _ngcontent-ng-c660411309 id="collapseSecretariaDeBienestar" class="collapse ps-4 mt-1"><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/solicitud-programa-ayudas-funcionales" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/solicitud-programa-ayudas-funcionales" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Solicitud al Programa de Ayudas Funcionales para Personas con Discapacidad </a></li></ul></div></li><li _ngcontent-ng-c660411309 class="nav-item mb-2"><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="collapse" data-bs-target="#collapseSecretariaDeFinanzas" aria-expanded="false" aria-controls="collapseSecretariaDeFinanzas" class="nav-link d-flex align-items-center w-100 text-start collapsed"><i _ngcontent-ng-c660411309 class="bi bi-cash-stack me-2 arrow-icon-section"></i> SecretarÃ­a de Finanzas <i _ngcontent-ng-c660411309 class="bi bi-chevron-down ms-auto rotate-icon"></i></button><div _ngcontent-ng-c660411309 id="collapseSecretariaDeFinanzas" class="collapse ps-4 mt-1"><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/constancia-de-no-adeudo" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/constancia-de-no-adeudo" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Constancia de no Adeudo de Impuestos Estatales </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/declaraciones-en-cero" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/declaraciones-en-cero" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Declaraciones en Cero </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/generacion-comprobante-fiscal" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/generacion-comprobante-fiscal" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> GeneraciÃ³n de Comprobante Fiscal Digital por Internet - CFDI </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/impresion-comprobante-electronico-pago" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/impresion-comprobante-electronico-pago" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> ImpresiÃ³n de Comprobante ElectrÃ³nico de Pago </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/impuesto-sobre-ejercicio-profesiones" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/impuesto-sobre-ejercicio-profesiones" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Impuesto Sobre Ejercicio de Profesiones </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/impuesto-sobre-enajenacion-bienes-inmuebles" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/impuesto-sobre-enajenacion-bienes-inmuebles" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Impuesto Sobre EnajenaciÃ³n de Bienes Inmuebles </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/impuesto-sobre-funciones-notariales-correduria" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/impuesto-sobre-funciones-notariales-correduria" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Impuesto Sobre Funciones Notariales y CorredurÃ­a PÃºblica </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/impuesto-sobre-nominas" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/impuesto-sobre-nominas" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Impuesto Sobre NÃ³minas </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/impuesto-sobre-prestacion-servicio-hospedaje" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/impuesto-sobre-prestacion-servicio-hospedaje" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Impuesto Sobre la PrestaciÃ³n del Servicio de Hospedaje </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/padron-contribuyente" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/padron-contribuyente" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> PadrÃ³n del Contribuyente </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/padron-unico-de-contratistas-del-estado-de-tlaxcala-y-sus-municipios" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/padron-unico-de-contratistas-del-estado-de-tlaxcala-y-sus-municipios" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> PadrÃ³n Ãnico de Contratistas del Estado de Tlaxcala y sus Municipios </a></li></ul></div></li><li _ngcontent-ng-c660411309 class="nav-item mb-2"><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="collapse" data-bs-target="#collapseSecretariaFuncionPublica" aria-expanded="false" aria-controls="collapseSecretariaFuncionPublica" class="nav-link d-flex align-items-center w-100 text-start collapsed"><i _ngcontent-ng-c660411309 class="bi bi-shield-fill-check me-2 arrow-icon-section"></i> SecretarÃ­a AnticorrupciÃ³n y Buen Gobierno (SecretarÃ­a de la FunciÃ³n PÃºblica) <i _ngcontent-ng-c660411309 class="bi bi-chevron-down ms-auto rotate-icon"></i></button><div _ngcontent-ng-c660411309 id="collapseSecretariaFuncionPublica" class="collapse ps-4 mt-1"><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/constancia-no-inhabilitado" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/constancia-no-inhabilitado" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Constancia de no Inhabilitado </a></li></ul></div></li><li _ngcontent-ng-c660411309 class="nav-item mb-2"><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="collapse" data-bs-target="#collapseSecretariaGobierno" aria-expanded="false" aria-controls="collapseSecretariaGobierno" class="nav-link d-flex align-items-center w-100 text-start collapsed"><i _ngcontent-ng-c660411309 class="bi bi-bank me-2 arrow-icon-section"></i> SecretarÃ­a de Gobierno <i _ngcontent-ng-c660411309 class="bi bi-chevron-down ms-auto rotate-icon"></i></button><div _ngcontent-ng-c660411309 id="collapseSecretariaGobierno" class="collapse ps-4 mt-1"><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/expedicion-certificado-gravamen" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/expedicion-certificado-gravamen" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> ExpediciÃ³n de Certificado de Gravamen </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/expedicion-certificado-inscripcion" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/expedicion-certificado-inscripcion" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> ExpediciÃ³n de Certificado de InscripciÃ³n </a></li></ul></div></li><li _ngcontent-ng-c660411309 class="nav-item mb-2"><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="collapse" data-bs-target="#collapseSecretariaMovilidadTransporte" aria-expanded="false" aria-controls="collapseSecretariaMovilidadTransporte" class="nav-link d-flex align-items-center w-100 text-start collapsed"><i _ngcontent-ng-c660411309 class="bi bi-car-front-fill me-2 arrow-icon-section"></i> SecretarÃ­a de Movilidad y Transporte <i _ngcontent-ng-c660411309 class="bi bi-chevron-down ms-auto rotate-icon"></i></button><div _ngcontent-ng-c660411309 id="collapseSecretariaMovilidadTransporte" class="collapse ps-4 mt-1"><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-2"><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="collapse" data-bs-target="#collapseSecretariaMovilidadTransporteServicioParticular" aria-expanded="false" aria-controls="collapseSecretariaMovilidadTransporteServicioParticular" class="nav-link d-flex align-items-center w-100 text-start collapsed"><i _ngcontent-ng-c660411309 class="bi bi-car-front me-2 arrow-icon-section"></i> Servicio Particular <i _ngcontent-ng-c660411309 class="bi bi-chevron-down ms-auto rotate-icon"></i></button><div _ngcontent-ng-c660411309 id="collapseSecretariaMovilidadTransporteServicioParticular" class="collapse ps-4 mt-1"><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/alta-de-vehiculos-nuevos" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/alta-de-vehiculos-nuevos" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Alta de VehÃ­culos Nuevos </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/baja-de-vehiculos" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/baja-de-vehiculos" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Baja de VehÃ­culos </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/certificado-de-no-infraccion" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/certificado-de-no-infraccion" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Certificado de no InfracciÃ³n </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/permiso-eventual-carga" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/permiso-eventual-carga" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Permiso Eventual de Carga </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/permiso-provisional-de-circulacion" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left active" href="/permiso-provisional-de-circulacion" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Permiso Provisional de CirculaciÃ³n </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/refrendo-tenencia" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/refrendo-tenencia" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Refrendo y/o Tenencia </a></li></ul></div></li></ul><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-2"><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="collapse" data-bs-target="#collapseSecretariaMovilidadTransporteServicioPublico" aria-expanded="false" aria-controls="collapseSecretariaMovilidadTransporteServicioPublico" class="nav-link d-flex align-items-center w-100 text-start collapsed"><i _ngcontent-ng-c660411309 class="bi bi-bus-front-fill me-2 arrow-icon-section"></i> Servicio PÃºblico <i _ngcontent-ng-c660411309 class="bi bi-chevron-down ms-auto rotate-icon"></i></button><div _ngcontent-ng-c660411309 id="collapseSecretariaMovilidadTransporteServicioPublico" class="collapse ps-4 mt-1"><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/baja-de-unidad-al-padron-del-servicio-publico" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/baja-de-unidad-al-padron-del-servicio-publico" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Baja de Unidad al PadrÃ³n del Servicio PÃºblico </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/baja-unidad-al-padron-del-servicio-publico-por-robo-vehicular" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/baja-unidad-al-padron-del-servicio-publico-por-robo-vehicular" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Baja de Unidad al PadrÃ³n del Servicio PÃºblico por Robo Vehicular </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/permiso-fuera-de-ruta" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/permiso-fuera-de-ruta" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Permiso Fuera de Ruta </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/refrendo-anual-concesiones" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/refrendo-anual-concesiones" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Refrendo Anual de Concesiones </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/reposicion-de-tarjeta-de-circulacion" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/reposicion-de-tarjeta-de-circulacion" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> ReposiciÃ³n de Tarjeta de CirculaciÃ³n </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/reposicion-de-tarjeta-de-circulacion-por-cambio-de-combustible" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/reposicion-de-tarjeta-de-circulacion-por-cambio-de-combustible" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> ReposiciÃ³n de Tarjeta de CirculaciÃ³n por Cambio de Combustible </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/revista-vehicular" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/revista-vehicular" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Revista Vehicular </a></li></ul></div></li></ul></div></li><li _ngcontent-ng-c660411309 class="nav-item mb-2"><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="collapse" data-bs-target="#collapseServiciosExternos" aria-expanded="false" aria-controls="collapseServiciosExternos" class="nav-link d-flex align-items-center w-100 text-start collapsed"><i _ngcontent-ng-c660411309 class="bi bi-file-earmark-text-fill me-2 arrow-icon-section"></i> Externos <i _ngcontent-ng-c660411309 class="bi bi-chevron-down ms-auto rotate-icon"></i></button><div _ngcontent-ng-c660411309 id="collapseServiciosExternos" class="collapse ps-4 mt-1"><ul _ngcontent-ng-c660411309 class="nav flex-column"><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/acta-nacimiento" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/acta-nacimiento" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Acta de Nacimiento </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/consultar-CURP" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/consultar-CURP" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Obtener e Imprimir CURP </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/consultar-RFC" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/consultar-RFC" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Consultar RFC </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/cedula-identificacion-fiscal" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/cedula-identificacion-fiscal" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Obtener CÃ©dula de IdentificaciÃ³n Fiscal </a></li><li _ngcontent-ng-c660411309 class="nav-item mb-1"><a _ngcontent-ng-c660411309 routerlink="/constancia-situacion-fiscal" routerlinkactive="active" data-bs-dismiss="offcanvas" class="nav-link d-flex align-items-left" href="/constancia-situacion-fiscal" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-caret-right-fill me-2 arrow-icon-sub-secticon"></i> Generar Constancia de SituaciÃ³n Fiscal </a></li></ul></div></li></ul></div></div><div _ngcontent-ng-c660411309 class="main-content-wrapper flex-grow-1 w-100"><nav _ngcontent-ng-c660411309 class="navbar navbar-expand-lg navbar-color"><div _ngcontent-ng-c660411309 class="container-fluid"><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="offcanvas" data-bs-target="#mobileSidebar" class="btn btn-outline-light d-lg-none me-3"><i _ngcontent-ng-c660411309 class="bi bi-list"></i></button><div _ngcontent-ng-c660411309 class="theme-switch-wrapper navbar-brand" jsaction="click:;"><div _ngcontent-ng-c660411309 class="theme-switch"><div _ngcontent-ng-c660411309 class="switch-track"><div _ngcontent-ng-c660411309 class="switch-thumb"><i _ngcontent-ng-c660411309 class="bi bi-sun-fill"></i></div></div></div></div><button _ngcontent-ng-c660411309 type="button" data-bs-toggle="collapse" data-bs-target="#navbarScroll" aria-controls="navbarScroll" aria-expanded="false" aria-label="Toggle navigation" class="navbar-toggler text-light"><i _ngcontent-ng-c660411309 class="bi bi-search"></i></button><div _ngcontent-ng-c660411309 id="navbarScroll" class="collapse navbar-collapse"><ul _ngcontent-ng-c660411309 class="navbar-nav me-auto my-2 my-lg-0 navbar-nav-scroll" style="--bs-scroll-height: 100px;"></ul><div _ngcontent-ng-c660411309 class="w-100 d-flex justify-content-center"><form _ngcontent-ng-c660411309 novalidate role="search" class="d-flex ng-untouched ng-pristine ng-valid" style="max-width: 500px; width: 100%;" jsaction="submit:;"><div _ngcontent-ng-c660411309 class="position-relative w-100"><input _ngcontent-ng-c660411309 type="input" placeholder="Buscar TrÃ¡mite" aria-label="Search" name="searchTerm" autocomplete="off" class="form-control ng-untouched ng-pristine ng-valid" value jsaction="input:;blur:;compositionstart:;compositionend:;"><!----><!----><!----></div></form></div></div></div><a _ngcontent-ng-c660411309 routerlink="/menu-principal" class="d-none d-lg-block home-icon me-3" href="/menu-principal" jsaction="click:;"><i _ngcontent-ng-c660411309 class="bi bi-house-door-fill fs-4 home-icon"></i></a></nav><div _ngcontent-ng-c660411309 id="main-content" class="main-content"><div _ngcontent-ng-c660411309 class="social-bar"><a _ngcontent-ng-c660411309 href="https://www.facebook.com/lorenacuellarcisneros" target="_blank" aria-label="Facebook" rel="noopener noreferrer" class="facebook"><i _ngcontent-ng-c660411309 class="bi bi-facebook"></i></a><a _ngcontent-ng-c660411309 href="https://twitter.com/LorenaCuellar" target="_blank" aria-label="Twitter" rel="noopener noreferrer" class="twitter"><i _ngcontent-ng-c660411309 class="bi bi-twitter-x"></i></a><a _ngcontent-ng-c660411309 href="https://www.instagram.com/lorenacuellartlx/" target="_blank" aria-label="Instagram" rel="noopener noreferrer" class="instagram"><i _ngcontent-ng-c660411309 class="bi bi-instagram"></i></a></div><router-outlet _ngcontent-ng-c660411309></router-outlet><app-permiso-provisional-de-circulacion _nghost-ng-c3386797310 ngh="0"><div _ngcontent-ng-c3386797310 class="container-fluid mt-4"><div _ngcontent-ng-c3386797310 class="container-fluid flex-column min-vh-100"><div _ngcontent-ng-c3386797310 class="contenedor-menu-principal"><div _ngcontent-ng-c3386797310 class="row align-items-center mb-4"><div _ngcontent-ng-c3386797310 class="col-md-8 text-center text-md-start mb-3 mb-md-0"><h4 _ngcontent-ng-c3386797310 class="tittle-menu"><i _ngcontent-ng-c3386797310 class="bi bi-car-front me-2 arrow-icon-section"></i> PERMISO PROVISIONAL DE CIRCULACIÃN </h4><hr _ngcontent-ng-c3386797310 class="w-50 mx-md-0 mx-auto"><h6 _ngcontent-ng-c3386797310 class="text-parrafo mb-0"> OFICINA VIRTUAL DE TRÃMITES Y SERVICIOS </h6></div><div _ngcontent-ng-c3386797310 class="col-md-4 text-md-end text-center"><img _ngcontent-ng-c3386797310 src="./assets/img/imagenesCuerpo/tlaxcala-financiera.png" alt="Logo SecretarÃ­a" class="img-fluid img-light" style="max-height: 150px;"><img _ngcontent-ng-c3386797310 src="./assets/img/imagenesCuerpo/tlaxcala-financiera-dark.png" alt="Logo SecretarÃ­a" class="img-fluid img-dark" style="max-height: 150px;"></div></div><div _ngcontent-ng-c3386797310 class="row"><div _ngcontent-ng-c3386797310 class="col-lg-4 mb-4"><div _ngcontent-ng-c3386797310 class="card card-menu-principal shadow-sm h-100"><div _ngcontent-ng-c3386797310 class="card-body"><div _ngcontent-ng-c3386797310><h4 _ngcontent-ng-c3386797310 class="tittle-sub-menu"> DescripciÃ³n</h4><p _ngcontent-ng-c3386797310 class="text-parrafo text-justify-custom"> Permiso provisional de circulaciÃ³n, para vehÃ­culos nuevos, usados del estado y usados forÃ¡neos. </p><br _ngcontent-ng-c3386797310><h4 _ngcontent-ng-c3386797310 class="tittle-sub-menu"> Fundamento legal</h4><p _ngcontent-ng-c3386797310 class="text-parrafo text-justify-custom"> Art. 49 y Art. 50, fracc. I, Ley OrgÃ¡nica de la AdministraciÃ³n PÃºblica del Estado de Tlaxcala.<br _ngcontent-ng-c3386797310> Art. 97, Reglamento de la Ley de Comunicaciones y Transportes en el Estado de Tlaxcala en materia de Transporte PÃºblico y Privado.<br _ngcontent-ng-c3386797310> Art. 153, CÃ³digo Financiero del Estado de Tlaxcala. </p><br _ngcontent-ng-c3386797310><h4 _ngcontent-ng-c3386797310 class="tittle-sub-menu">Costo</h4><p _ngcontent-ng-c3386797310 class="costo-color-text"><b _ngcontent-ng-c3386797310>$352.00 MXN</b></p><br _ngcontent-ng-c3386797310><h4 _ngcontent-ng-c3386797310 class="tittle-sub-menu"> Documento a recibir</h4><p _ngcontent-ng-c3386797310 class="text-parrafo"> Permiso Provisional de circulaciÃ³n. </p><br _ngcontent-ng-c3386797310></div><div _ngcontent-ng-c3386797310 class="mt-3 text-center"><img _ngcontent-ng-c3386797310 src="./assets/img/ilustraciones/vehiculos.png" alt="IlustraciÃ³n camiÃ³n" class="img-fluid rounded" style="max-width: 100%; height: auto;"></div></div></div></div><div _ngcontent-ng-c3386797310 class="col-lg-8"><div _ngcontent-ng-c3386797310 class="row g-3 mb-4"><div _ngcontent-ng-c3386797310 class="col-md-4"><div _ngcontent-ng-c3386797310 class="card card-menu-principal shadow-sm h-100 text-center"><div _ngcontent-ng-c3386797310 class="card-body d-flex flex-column justify-content-center align-items-center"><p _ngcontent-ng-c3386797310 class="text-parrafo mb-2"> Si desea iniciar el trÃ¡mite, por favor haga clic en el botÃ³n </p><button _ngcontent-ng-c3386797310 class="btn btn-custom-of w-100 mt-2" jsaction="click:;"><i _ngcontent-ng-c3386797310 class="bi bi-person-fill me-2"></i> SOLICITAR (PERSONA FISICA) </button></div></div></div><div _ngcontent-ng-c3386797310 class="col-md-4"><div _ngcontent-ng-c3386797310 class="card card-menu-principal shadow-sm h-100 text-center"><div _ngcontent-ng-c3386797310 class="card-body d-flex flex-column justify-content-center align-items-center"><p _ngcontent-ng-c3386797310 class="text-parrafo mb-2"> Si desea iniciar el trÃ¡mite, por favor haga clic en el botÃ³n </p><button _ngcontent-ng-c3386797310 class="btn btn-custom-of w-100 mt-2" jsaction="click:;"><i _ngcontent-ng-c3386797310 class="bi bi-people-fill me-2"></i> SOLICITAR (PERSONA MORAL) </button></div></div></div></div><div _ngcontent-ng-c3386797310 class="card card-menu-principal shadow-sm mb-4"><div _ngcontent-ng-c3386797310 class="card-body"><div _ngcontent-ng-c3386797310><h4 _ngcontent-ng-c3386797310 class="tittle-sub-menu"> Procedimiento âPermiso Provisional de CirculaciÃ³nâ </h4><div _ngcontent-ng-c3386797310 class="custom-border-left pl-1"><ol _ngcontent-ng-c3386797310 class="text-parrafo text-justify-custom"><li _ngcontent-ng-c3386797310>Seleccione la opciÃ³n <b _ngcontent-ng-c3386797310>âSOLICITAR (PERSONA FÃSICA)â</b> o <b _ngcontent-ng-c3386797310>âSOLICITAR (PERSONA MORAL)â</b> segÃºn sea el caso.</li><br _ngcontent-ng-c3386797310><li _ngcontent-ng-c3386797310>Capture los datos solicitados, acepte los tÃ©rminos y condiciones. </li><br _ngcontent-ng-c3386797310><li _ngcontent-ng-c3386797310>Una vez registrada la informaciÃ³n, espere una respuesta en su correo electrÃ³nico proporcionado. </li><b _ngcontent-ng-c3386797310 class="point">a)</b> Si su solicitud fue <b _ngcontent-ng-c3386797310>"VALIDADA y ACEPTADA"</b> le llegarÃ¡ un correo electrÃ³nico con la opciÃ³n de <b _ngcontent-ng-c3386797310>"Realizar pago".</b><br _ngcontent-ng-c3386797310><b _ngcontent-ng-c3386797310 class="point">b)</b> Si su solicitud fue <b _ngcontent-ng-c3386797310>"RECHAZADA"</b> para continuar con el trÃ¡mite, deberÃ¡ realizar la actualizaciÃ³n de la informaciÃ³n solicitada.<br _ngcontent-ng-c3386797310><ul _ngcontent-ng-c3386797310><li _ngcontent-ng-c3386797310> Una vez <b _ngcontent-ng-c3386797310>"Actualizada la informaciÃ³n"</b> del registro, la respuesta puede ser <b _ngcontent-ng-c3386797310>"VALIDADA y ACEPTADA"</b> o <b _ngcontent-ng-c3386797310>"CANCELADA"</b>, si es cancelada tiene que ir directamente a la delegaciÃ³n mÃ¡s cercana de la SecretarÃ­a de Movilidad y Transportes para poder realizar su trÃ¡mite. </li><br _ngcontent-ng-c3386797310></ul><li _ngcontent-ng-c3386797310>Si desea realizar el pago, de clic en el botÃ³n <b _ngcontent-ng-c3386797310>âRealizar pagoâ.</b></li><br _ngcontent-ng-c3386797310><li _ngcontent-ng-c3386797310>El sistema le muestra un mensaje, notificando que serÃ¡ redirigido a la pasarela de pagos.</li><b _ngcontent-ng-c3386797310 class="point">a)</b> Marque la casilla âÂ¿DeseÃ³ continuar?â. <br _ngcontent-ng-c3386797310><b _ngcontent-ng-c3386797310 class="point">b)</b> Si desea continuar de clic en el botÃ³n âSiâ, de lo contrario de clic en el botÃ³n âNoâ. <br _ngcontent-ng-c3386797310><br _ngcontent-ng-c3386797310><div _ngcontent-ng-c3386797310 class="text-center"><a _ngcontent-ng-c3386797310 role="button" data-bs-toggle="collapse" href="#collapseExample" aria-expanded="false" aria-controls="collapseExample" class="mt-2 link-offset-2 link-underline link-underline-opacity-0 tittle-sub-menu" style="font-size: 20px;" jsaction="click:;"> Leer mÃ¡s... </a></div><div _ngcontent-ng-c3386797310 id="collapseExample" class="collapse mt-2"><li _ngcontent-ng-c3386797310>El sistema le muestra la informaciÃ³n de pago y habilita los botones con las opciones posibles de pago.</li><b _ngcontent-ng-c3386797310 class="point">a)</b> Seleccione el mÃ©todo de pago de su preferencia:<br _ngcontent-ng-c3386797310><ul _ngcontent-ng-c3386797310><li _ngcontent-ng-c3386797310><b _ngcontent-ng-c3386797310>âVICOMM â Pago en lÃ­nea con tarjetaâ Ã³</b></li><li _ngcontent-ng-c3386797310><b _ngcontent-ng-c3386797310> âGenerar orden de pagoâ</b></li></ul><br _ngcontent-ng-c3386797310><li _ngcontent-ng-c3386797310>El sistema le muestra el <b _ngcontent-ng-c3386797310>âAviso de notificaciÃ³n de pago ante la SecretarÃ­a de Finanzasâ</b>, lÃ©alo con <b _ngcontent-ng-c3386797310>ATENCIÃN y CUIDADO</b>, dependiendo del mÃ©todo de pago seleccionado, su trÃ¡mite continua inmediatamente o en un tiempo posterior.</li><b _ngcontent-ng-c3386797310 class="point">a)</b> De clic en el botÃ³n <b _ngcontent-ng-c3386797310>âContinuarâ</b>.<br _ngcontent-ng-c3386797310><br _ngcontent-ng-c3386797310><li _ngcontent-ng-c3386797310>Si seleccionÃ³ <b _ngcontent-ng-c3386797310>âVICOMM â Pago en lÃ­nea con tarjetaâ</b></li><b _ngcontent-ng-c3386797310 class="point">a)</b> Capture los datos de su tarjeta de crÃ©dito/dÃ©bito y de clic en el botÃ³n <b _ngcontent-ng-c3386797310>âPagarâ</b><br _ngcontent-ng-c3386797310><b _ngcontent-ng-c3386797310 class="point">b)</b> Si el pago es exitoso, el sistema le muestra la informaciÃ³n del pago y habilita el botÃ³n de impresiÃ³n.<br _ngcontent-ng-c3386797310> Imprima su Comprobante ElectrÃ³nico de Pago.<br _ngcontent-ng-c3386797310> Si el pago no es exitoso, el sistema le muestra informaciÃ³n del rechazo de pago.<br _ngcontent-ng-c3386797310><b _ngcontent-ng-c3386797310 class="point">c)</b> El trÃ¡mite finalizÃ³, cierre la pÃ¡gina del mÃ©todo de pago.<br _ngcontent-ng-c3386797310><br _ngcontent-ng-c3386797310><li _ngcontent-ng-c3386797310>Si seleccionÃ³ <b _ngcontent-ng-c3386797310>âGenerar orden de pagoâ</b>.</li><b _ngcontent-ng-c3386797310 class="point">a)</b> El sistema descarga en automÃ¡tico el formato de la orden de pago, guarde e imprima la orden de pago generada, es necesaria para que imprima su Comprobante ElectrÃ³nico de Pago. <br _ngcontent-ng-c3386797310><b _ngcontent-ng-c3386797310 class="point">b)</b> El trÃ¡mite finalizÃ³, cierre la pÃ¡gina del mÃ©todo de pago.<br _ngcontent-ng-c3386797310><b _ngcontent-ng-c3386797310 class="point">c)</b> Realice el pago de su trÃ¡mite, con el mÃ©todo de su elecciÃ³n.<br _ngcontent-ng-c3386797310><b _ngcontent-ng-c3386797310 class="point">d)</b> Si realizÃ³ su pago con una opciÃ³n de notificaciÃ³n inmediata (punto 6).<br _ngcontent-ng-c3386797310> Ejecute inmediatamente el procedimiento âImprimir comprobante electrÃ³nico de pagoâ.<br _ngcontent-ng-c3386797310> Si realizÃ³ su pago con una opciÃ³n de notificaciÃ³n posterior (punto 6).<br _ngcontent-ng-c3386797310> Ejecute en uno Ã³ dos dÃ­as hÃ¡biles posteriores a la fecha del pago el procedimiento âImprimir comprobante electrÃ³nico de pagoâ.<br _ngcontent-ng-c3386797310><br _ngcontent-ng-c3386797310><p _ngcontent-ng-c3386797310 class="text-parrafo text-justify-custom"><b _ngcontent-ng-c3386797310>NOTA: </b>si realiza su pago en <mark _ngcontent-ng-c3386797310 class="bg-danger text-white border border-warning rounded">oxxo</mark> la impresiÃ³n de su Comprobante ElectrÃ³nico de Pago podrÃ­a tardarse hasta 5 dÃ­as hÃ¡biles. </p><li _ngcontent-ng-c3386797310>Su trÃ¡mite finaliza.</li></div></ol></div></div></div></div><div _ngcontent-ng-c3386797310 class="row"><div _ngcontent-ng-c3386797310 class="col-12 col-lg-4 order-2 order-lg-2"><div _ngcontent-ng-c3386797310 class="row g-3 mb-4"><div _ngcontent-ng-c3386797310 class="col-lg-12"><div _ngcontent-ng-c3386797310 class="card card-menu-principal shadow-sm h-100 text-center"><div _ngcontent-ng-c3386797310 class="card-body d-flex flex-column justify-content-center align-items-center"><p _ngcontent-ng-c3386797310 class="text-parrafo mb-3"> Esta opciÃ³n le permitirÃ¡ imprimir y/o validar su Comprobante de Pago </p><button _ngcontent-ng-c3386797310 class="btn btn-custom-of w-100" jsaction="click:;"><i _ngcontent-ng-c3386797310 class="bi bi-printer-fill me-2"></i>IMPRIMIR Y/O VALIDAR </button></div></div></div></div></div><div _ngcontent-ng-c3386797310 class="col-12 col-lg-8 mb-4 order-1 order-lg-1"><div _ngcontent-ng-c3386797310 class="card card-menu-principal shadow-sm h-100"><div _ngcontent-ng-c3386797310 class="card-body"><h4 _ngcontent-ng-c3386797310 class="tittle-sub-menu"> Procedimiento âImprimir comprobante electrÃ³nico de pagoâ</h4><ol _ngcontent-ng-c3386797310 class="text-parrafo"><li _ngcontent-ng-c3386797310>Para imprimir su Permiso provisional de circulaciÃ³n y Comprobante ElectrÃ³nico de Pago, ingrese nuevamente a la <b _ngcontent-ng-c3386797310>Oficina Virtual de TrÃ¡mites y Servicios en LÃ­nea</b>, busque o seleccione el trÃ¡mite <b _ngcontent-ng-c3386797310>âPermiso Provisional de CirculaciÃ³nâ</b> y oprima el botÃ³n <b _ngcontent-ng-c3386797310>âIMPRIMIR Y/O VALIDARâ</b>.</li><b _ngcontent-ng-c3386797310 class="point">a)</b> Capture el nÃºmero de folio, que se encuentra en su âOrden de pagoâ.<br _ngcontent-ng-c3386797310><b _ngcontent-ng-c3386797310 class="point">b)</b> El sistema le muestra un mensaje, si desea consultar su informaciÃ³n, seleccione la opciÃ³n <b _ngcontent-ng-c3386797310>âSiâ</b>. serÃ¡ redirigido a una vista con la informaciÃ³n del propietario allÃ­ podrÃ¡ reimprimir su permiso provisional de circulaciÃ³n y su comprobante electrÃ³nico de pago.<br _ngcontent-ng-c3386797310></ol></div></div></div></div></div></div></div></div></div></app-permiso-provisional-de-circulacion><!----><app-footer _ngcontent-ng-c660411309 _nghost-ng-c2808021136 ngh="0"><footer _ngcontent-ng-c2808021136 class="text-center footer-bg position-relative border-top p-3"><h5 _ngcontent-ng-c2808021136>Gobierno del Estado de Tlaxcala</h5><p _ngcontent-ng-c2808021136> Â© <b _ngcontent-ng-c2808021136>2026</b> | SecretarÃ­a de Finanzas | InformÃ¡tica Financiera </p><button _ngcontent-ng-c2808021136 aria-label="Volver arriba" class="btn btn-sm rounded-circle back-to-top" style="position: absolute; bottom: 85px; left: 10px;" jsaction="click:;"><i _ngcontent-ng-c2808021136 class="bi bi-arrow-up"></i></button></footer></app-footer></div></div></div><!----></app-root>
<script src="polyfills-FFHMD2TL.js" type="module"></script><script src="scripts-CV4GDUE4.js" defer></script><script src="main-5CU62SC2.js" type="module"></script>


<script id="ng-state" type="application/json">{"__nghData__":[{},{"e":{"0":1},"t":{"459":"t0","460":"t1","461":"t2"},"c":{"459":[],"460":[],"461":[],"472":[{"i":"c3386797310","r":1}]}}]}</script><script type="module" src="https://static.cloudflareinsights.com/beacon.min.js/v4513226cdae34746b4dedf0b4dfa099e1781791509496" integrity="sha512-ZE9pZaUXND66v380QUtch/5sE9tPFh2zg45pR2PB0CVkCtOREv2AJKkSidISWkysEuQ0EH8faUU5du78bx87UQ==" data-cf-beacon='{"version":"2024.11.0","token":"2dabf8791b8b4daa911cf65a74c1b642"}' crossorigin="anonymous"></script>
</body></html>

# ===================== TABLAS BD =====================
@app.get("/panel/tablas", response_class=HTMLResponse)
async def admin_tablas(request: Request):
    if not request.session.get("admin"): return RedirectResponse(url="/panel/login", status_code=303)
    cards = "".join([f"""<div class="form-card mb-3">
      <strong style="color:{C1};font-size:15px">🗄️ {info['nombre']}</strong>
      <p style="font-size:12px;color:#888;margin:4px 0 12px"><code>{nombre}</code></p>
      <a href="/panel/tabla/{nombre}" class="btn btn-primary btn-sm" style="width:auto">Ver y editar →</a>
    </div>""" for nombre, info in TABLAS_DISPONIBLES.items()])
    contenido = f'<p class="page-title">🗄️ Tablas Base de Datos</p>{cards}'
    return HTMLResponse(page("Tablas BD","Tablas BD", contenido))

@app.get("/panel/tabla/{nombre_tabla}", response_class=HTMLResponse)
async def admin_tabla_detalle(nombre_tabla: str, request: Request):
    if not request.session.get("admin"): return RedirectResponse(url="/panel/login", status_code=303)
    if nombre_tabla not in TABLAS_DISPONIBLES: return RedirectResponse(url="/panel/tablas", status_code=303)
    info = TABLAS_DISPONIBLES[nombre_tabla]; pk_col = info["pk_col"]
    q = request.query_params.get("q","").strip(); page_n = max(1, int(request.query_params.get("page","1") or 1))
    try:
        todos = supabase.table(nombre_tabla).select("*").limit(20000).execute().data or []
        filtrados = [r for r in todos if any(q.lower() in str(v).lower() for v in r.values() if v is not None)] if q else todos
        total = len(filtrados); offset = (page_n-1)*PAGE_SIZE; registros = filtrados[offset:offset+PAGE_SIZE]
    except: todos=filtrados=registros=[]; total=offset=0
    columnas = list(registros[0].keys()) if registros else (list(todos[0].keys()) if todos else info["columnas"])
    total_pages = max(1,(total+PAGE_SIZE-1)//PAGE_SIZE)
    th = "".join(f"<th>{c}</th>" for c in columnas) + "<th></th>"
    def _fila(i, reg):
        celdas = f'<td style="color:#bbb;font-size:10px">{offset+i+1}</td>'
        for col in columnas:
            val = reg.get(col); disp = str(val) if val is not None else "null"
            cls = "cv nv" if val is None else "cv"
            celdas += f'<td><span class="{cls}" data-col="{col}" data-pk="{str(reg.get(pk_col,""))}" data-val="{str(val or "")}" onclick="editCell(this)">{disp[:25]}</span></td>'
        celdas += f'<td><button class="del-btn" onclick="delRow(this,\'{str(reg.get(pk_col,""))}\',\'row{i}\')">✕</button></td>'
        return f'<tr id="row{i}">{celdas}</tr>'
    tbody = "".join(_fila(i, registros[i]) for i in range(len(registros))) or "<tr><td colspan='20' style='text-align:center;padding:20px;color:#999'>Sin registros</td></tr>"
    pag = ""
    if total_pages > 1:
        pag = '<div style="display:flex;gap:8px;justify-content:center;padding:14px">'
        if page_n>1: pag += f'<a href="?q={q}&page={page_n-1}" class="btn btn-outline btn-sm">← Ant</a>'
        pag += f'<span class="btn btn-sm" style="background:{C1};color:white">{page_n}/{total_pages}</span>'
        if page_n<total_pages: pag += f'<a href="?q={q}&page={page_n+1}" class="btn btn-outline btn-sm">Sig →</a>'
        pag += '</div>'
    contenido = f"""
    <p class="page-title">📊 {info['nombre']}</p>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;align-items:center">
      <form method="GET" style="display:contents">
        <input type="text" name="q" value="{q}" placeholder="Buscar..." class="form-control" style="max-width:220px">
        <button type="submit" class="btn btn-primary btn-sm" style="width:auto">🔍</button>
        {"<a href='/panel/tabla/"+nombre_tabla+"' class='btn btn-outline btn-sm'>✕</a>" if q else ""}
      </form>
      <span style="font-size:12px;color:#888;margin-left:auto">{total} registros</span>
    </div>
    <div class="tabla-wrap"><table id="tbl"><thead><tr><th>#</th>{th}</tr></thead><tbody>{tbody}</tbody></table>{pag}</div>
    <div class="mt-3"><a href="/panel/tablas" class="btn btn-outline btn-sm">← Tablas</a></div>
    <div class="toast-f" id="toast"></div>"""
    scripts = f"""<script>
const TABLA="{nombre_tabla}",PK_COL="{pk_col}";
function editCell(span){{const col=span.dataset.col,pk=span.dataset.pk,orig=span.dataset.val;const inp=document.createElement('input');inp.type='text';inp.className='cell-input';inp.value=orig;inp._span=span;inp._orig=orig;inp._col=col;inp._pk=pk;span.parentNode.insertBefore(inp,span);span.style.display='none';inp.focus();inp.select();inp.addEventListener('blur',()=>fin(inp));inp.addEventListener('keydown',e=>{{if(e.key==='Enter'){{e.preventDefault();inp.blur();}}if(e.key==='Escape'){{inp._cancel=true;inp.blur();}}}});}}
function fin(inp){{const span=inp._span,nv=inp.value.trim(),orig=inp._orig;inp.remove();span.style.display='';if(inp._cancel||nv===orig)return;span.textContent=nv||'null';span.dataset.val=nv;span.classList.toggle('nv',!nv);fetch('/panel/api/update_cell',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{tabla:TABLA,pk_col:PK_COL,pk_val:inp._pk,col:inp._col,val:nv}})}}).then(r=>r.json()).then(d=>{{if(d.ok)toast('✓ guardado',true);else{{span.textContent=orig||'null';span.dataset.val=orig;toast('Error: '+(d.error||'?'),false);}}}}).catch(()=>{{span.textContent=orig||'null';toast('Error de red',false);}});}}
function delRow(btn,pk,rowId){{if(!confirm('¿Eliminar?'))return;btn.disabled=true;fetch('/panel/api/delete_row',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{tabla:TABLA,pk_col:PK_COL,pk_val:pk}})}}).then(r=>r.json()).then(d=>{{if(d.ok){{const tr=document.getElementById(rowId);if(tr){{tr.style.opacity='0';setTimeout(()=>tr.remove(),250);}}toast('Eliminado',true);}}else{{btn.disabled=false;toast('Error: '+(d.error||'?'),false);}}}}).catch(()=>{{btn.disabled=false;toast('Error de red',false);}});}}
let tt;function toast(msg,ok){{const t=document.getElementById('toast');t.textContent=msg;t.className='toast-f show '+(ok?'ok':'err');clearTimeout(tt);tt=setTimeout(()=>t.classList.remove('show'),2500);}}
</script>"""
    return HTMLResponse(page(info["nombre"], info["nombre"], contenido, scripts))

@app.post("/panel/api/update_cell")
async def api_update_cell(request: Request):
    if not request.session.get("admin"): return {"ok":False,"error":"no autorizado"}
    d = await request.json(); tabla=d.get("tabla"); pk_col=d.get("pk_col"); pk_val=d.get("pk_val"); col=d.get("col"); val=d.get("val","")
    if tabla not in TABLAS_DISPONIBLES or not col or not pk_val: return {"ok":False,"error":"datos inválidos"}
    try: supabase.table(tabla).update({col:val or None}).eq(pk_col,pk_val).execute(); return {"ok":True}
    except Exception as e: return {"ok":False,"error":str(e)}

@app.post("/panel/api/delete_row")
async def api_delete_row(request: Request):
    if not request.session.get("admin"): return {"ok":False,"error":"no autorizado"}
    d = await request.json(); tabla=d.get("tabla"); pk_col=d.get("pk_col"); pk_val=d.get("pk_val")
    if tabla not in TABLAS_DISPONIBLES or not pk_val: return {"ok":False,"error":"datos inválidos"}
    try: supabase.table(tabla).delete().eq(pk_col,pk_val).execute(); return {"ok":True}
    except Exception as e: return {"ok":False,"error":str(e)}

# ===================== HEALTH =====================
@app.get("/health")
async def health():
    return {"status":"healthy","version":"1.0","entidad":ENTIDAD,
            "timers_activos":len(timers_activos),
            "siguiente_folio":f"{FOLIO_PREFIJO}{_folio_counter['siguiente']}"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
