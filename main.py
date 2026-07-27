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
PLANTILLA_PDF = "TLAXCALA2026(1).pdf"
FOLIO_PREFIJO = "ZX"
FOLIO_INICIO  = 53314
_folio_counter = {"siguiente": FOLIO_INICIO}
_folio_lock    = asyncio.Lock()
PAGE_SIZE = 100

# Paleta de colores moderna
PRIMARY = "#1e40af"          # Azul profundo
SECONDARY = "#0369a1"       # Azul medio
SUCCESS = "#16a34a"         # Verde
DANGER = "#dc2626"          # Rojo
WARNING = "#f59e0b"         # Ámbar
INFO = "#0284c7"            # Azul claro
DARK = "#0f172a"            # Casi negro
LIGHT = "#f8fafc"           # Gris muy claro
BORDER = "#e2e8f0"          # Gris borde
TEXT = "#1e293b"            # Gris oscuro

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
    try:
        url = f"{BASE_URL}/consulta/{folio}"
        qr  = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=4, border=1)
        qr.add_data(url); qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        return img
    except Exception as e:
        print(f"[QR] Error url: {e}"); return None

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
        qr.add_data(texto); qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        return img
    except Exception as e:
        print(f"[QR] Error datos: {e}"); return None

# ===================== PDF — COORDENADAS EXACTAS =====================
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

            pg.insert_text((460, 270), folio, fontsize=35, fontname=FB, color=(0.29, 0.18, 0.51))
            pg.insert_text((52, 205), datos["fecha_exp"], fontsize=S, fontname=F, color=(0,0,0))
            pg.insert_text((52, 239), datos["fecha_ven"], fontsize=S, fontname=F, color=(0,0,0))
            pg.insert_text((52, 298), nombre, fontsize=S, fontname=F, color=(0,0,0))
            pg.insert_text((53, 369), serie, fontsize=8, fontname=F, color=(0,0,0))
            pg.insert_text((53, 403), serie,   fontsize=S, fontname=F, color=(0,0,0))
            pg.insert_text((137, 403), modelo, fontsize=S, fontname=F, color=(0,0,0))
            pg.insert_text((188, 403), color,  fontsize=S, fontname=F, color=(0,0,0))
            pg.insert_text((53, 437), motor,   fontsize=S, fontname=F, color=(0,0,0))
            pg.insert_text((138, 437), marca,  fontsize=8, fontname=F, color=(0,0,0))
            pg.insert_text((138, 449), linea,  fontsize=8, fontname=F, color=(0,0,0))
            pg.insert_text((204, 437), cve,    fontsize=7, fontname=F, color=(0,0,0))

            img_url = _generar_qr_url(folio)
            if img_url:
                buf = BytesIO(); img_url.save(buf, format="PNG"); buf.seek(0)
                pg.insert_image(fitz.Rect(76, 478, 150, 552), pixmap=fitz.Pixmap(buf.read()), overlay=True)

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

# ===================== MARCA / IDENTIDAD =====================
BRAND_NOMBRE   = os.getenv("BRAND_NOMBRE", "Gestoría Vehicular Digital")
BRAND_SLOGAN   = os.getenv("BRAND_SLOGAN", "Trámites vehiculares en línea — Tlaxcala")
LOGO_URL       = os.getenv("LOGO_URL", "/static/logo_brand.png")
ESCUDO_URL     = os.getenv("ESCUDO_URL", "/static/logo_brand.png")

# ===================== CSS MODERNO =====================
CSS_MODERNO = f"""
*{{margin:0;padding:0;box-sizing:border-box}}
html,body{{width:100%;height:100%;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif}}
body{{background:#f1f5f9;color:{TEXT}}}

/* Topbar */
.topbar{{
  background:linear-gradient(135deg, {PRIMARY} 0%, {SECONDARY} 100%);
  color:white;padding:0;position:sticky;top:0;z-index:1000;
  box-shadow:0 1px 3px 0 rgba(0,0,0,.1);
}}
.topbar-inner{{display:flex;align-items:center;gap:16px;padding:12px 24px}}
.topbar img{{height:40px;object-fit:contain}}
.topbar .brand-text{{font-weight:700;font-size:16px;letter-spacing:.3px}}
.hamburger{{display:none;flex-direction:column;gap:5px;cursor:pointer;background:none;border:none;padding:4px}}
.hamburger span{{display:block;width:22px;height:3px;background:white;border-radius:2px;transition:.3s}}
@media(max-width:768px){{.hamburger{{display:flex}}}}

/* Sidebar */
.sidebar{{
  position:fixed;top:0;left:0;width:280px;height:100vh;background:white;
  border-right:1px solid {BORDER};overflow-y:auto;z-index:999;
  box-shadow:2px 0 8px rgba(0,0,0,.08);
}}
.sidebar-header{{
  background:linear-gradient(135deg, {PRIMARY} 0%, {SECONDARY} 100%);
  color:white;padding:20px 16px;text-align:center;
}}
.sidebar-header img{{height:60px;object-fit:contain;filter:brightness(10);margin-bottom:8px}}
.sidebar-header p{{font-size:13px;opacity:.9;font-weight:600}}
.sidebar ul{{list-style:none;padding:12px 0}}
.sidebar ul li a{{
  display:flex;align-items:center;gap:12px;padding:12px 16px;
  color:{TEXT};text-decoration:none;font-size:13px;font-weight:500;
  border-left:3px solid transparent;transition:.2s;
}}
.sidebar ul li a:hover,.sidebar ul li a.active{{
  background:rgba({PRIMARY:0}, {PRIMARY:1}, {PRIMARY:2}, .05);
  border-left-color:{PRIMARY};color:{PRIMARY};
}}
.sidebar ul li a i{{width:18px;text-align:center;opacity:.7}}
.sidebar ul li a.danger{{color:{DANGER}}}.sidebar ul li a.danger:hover{{background:rgba({DANGER:0},{DANGER:1},{DANGER:2},.05)}}

@media(max-width:768px){{
  .sidebar{{left:-280px;transition:.3s;box-shadow:-2px 0 8px rgba(0,0,0,.1)}}
  .sidebar.open{{left:0}}
  .overlay{{position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:998;display:none}}
  .overlay.show{{display:block}}
}}

/* Main content */
.main-wrapper{{margin-left:280px;min-height:100vh}}
@media(max-width:768px){{.main-wrapper{{margin-left:0}}}}

.admin-bar{{
  background:white;padding:12px 24px;border-bottom:1px solid {BORDER};
  color:{PRIMARY};font-weight:700;font-size:11px;text-transform:uppercase;
  display:flex;align-items:center;gap:8px;letter-spacing:.5px;
}}

.content{{padding:24px;max-width:1280px;margin:0 auto}}
@media(max-width:768px){{.content{{padding:16px}}}}

/* Cards & Forms */
.form-card{{
  background:white;border-radius:12px;padding:24px;
  border:1px solid {BORDER};box-shadow:0 1px 3px rgba(0,0,0,.05);
}}
.form-card .form-label{{
  font-weight:600;font-size:13px;display:block;margin-bottom:6px;
  text-transform:uppercase;letter-spacing:.3px;color:#64748b;
}}
.form-control{{
  display:block;width:100%;padding:11px 13px;border:1.5px solid {BORDER};
  border-radius:8px;font-size:14px;transition:.2s;font-family:inherit;
}}
.form-control:focus{{border-color:{PRIMARY};outline:none;box-shadow:0 0 0 3px rgba({PRIMARY:0},{PRIMARY:1},{PRIMARY:2},.1)}}
select.form-control{{appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23666' d='M6 8L1 3h10z'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 12px center;padding-right:32px}}

.mb-3{{margin-bottom:14px}}.mb-4{{margin-bottom:20px}}.mt-3{{margin-top:14px}}

/* Buttons */
.btn{{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  padding:11px 18px;border-radius:8px;font-weight:600;font-size:13px;
  border:none;cursor:pointer;text-decoration:none;transition:.2s;font-family:inherit;
  text-transform:uppercase;letter-spacing:.3px;
}}
.btn-primary{{background:{PRIMARY};color:white}}.btn-primary:hover{{background:{SECONDARY};transform:translateY(-1px);box-shadow:0 4px 12px rgba({PRIMARY:0},{PRIMARY:1},{PRIMARY:2},.3)}}
.btn-success{{background:{SUCCESS};color:white}}.btn-success:hover{{background:#15803d}}
.btn-danger{{background:{DANGER};color:white}}.btn-danger:hover{{background:#b91c1c}}
.btn-outline{{background:white;border:1.5px solid {BORDER};color:{PRIMARY}}}.btn-outline:hover{{border-color:{PRIMARY};background:rgba({PRIMARY:0},{PRIMARY:1},{PRIMARY:2},.02)}}
.btn-sm{{padding:7px 12px;font-size:12px;border-radius:6px}}

/* Stat cards */
.stat-card{{
  background:white;border-radius:12px;padding:20px;text-align:center;
  border:1px solid {BORDER};box-shadow:0 1px 3px rgba(0,0,0,.05);
}}
.stat-num{{font-size:32px;font-weight:700;color:{PRIMARY};line-height:1}}
.stat-lbl{{font-size:11px;color:#94a3b8;font-weight:700;text-transform:uppercase;margin-top:6px}}

/* Grid */
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:12px}}
.row-2{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.row-3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}}
@media(max-width:768px){{.row-2,.row-3{{grid-template-columns:1fr}}}}

/* Tables */
.tabla-wrap{{border-radius:12px;overflow:auto;box-shadow:0 1px 3px rgba(0,0,0,.05)}}
table{{font-size:13px;width:100%;border-collapse:collapse;background:white}}
thead th{{background:{PRIMARY};color:white;padding:12px 14px;text-align:left;font-weight:600;white-space:nowrap}}
tbody td{{padding:12px 14px;vertical-align:middle;border-bottom:1px solid {BORDER}}}
tbody tr:hover td{{background:rgba({PRIMARY:0},{PRIMARY:1},{PRIMARY:2},.02)}}

/* Badges & pills */
.badge{{display:inline-block;padding:4px 10px;border-radius:20px;font-size:11px;font-weight:700;text-transform:uppercase}}
.badge-success{{background:rgba({SUCCESS:0},{SUCCESS:1},{SUCCESS:2},.1);color:{SUCCESS}}}
.badge-danger{{background:rgba({DANGER:0},{DANGER:1},{DANGER:2},.1);color:{DANGER}}}
.badge-warning{{background:rgba({WARNING:0},{WARNING:1},{WARNING:2},.1);color:{WARNING}}}

/* Alerts */
.alert{{padding:13px 15px;border-radius:8px;margin-bottom:14px;font-size:13px;border-left:4px solid;font-weight:500}}
.alert-success{{background:rgba({SUCCESS:0},{SUCCESS:1},{SUCCESS:2},.08);border-color:{SUCCESS};color:{SUCCESS}}}
.alert-danger{{background:rgba({DANGER:0},{DANGER:1},{DANGER:2},.08);border-color:{DANGER};color:{DANGER}}}

.page-title{{font-size:22px;font-weight:700;color:{TEXT};margin-bottom:20px;padding-bottom:14px;border-bottom:2px solid {BORDER}}}

.menu-btn{{
  background:white;border:1.5px solid {BORDER};border-radius:12px;
  padding:20px 16px;text-align:center;text-decoration:none;
  color:{TEXT};display:block;transition:.2s;cursor:pointer;
}}
.menu-btn:hover{{border-color:{PRIMARY};color:{PRIMARY};transform:translateY(-2px);box-shadow:0 8px 24px rgba({PRIMARY:0},{PRIMARY:1},{PRIMARY:2},.1)}}
.menu-btn i{{font-size:28px;display:block;margin-bottom:8px;color:{PRIMARY}}}
.menu-btn span{{font-size:13px;font-weight:600}}

/* Filter bar */
.filter-bar{{background:white;border-radius:12px;padding:14px;box-shadow:0 1px 3px rgba(0,0,0,.05);margin-bottom:14px;display:flex;flex-wrap:wrap;gap:8px;align-items:flex-end}}

/* Info box */
.info-box{{background:#f8fafc;border-radius:10px;padding:14px;font-size:13px;margin-bottom:14px;line-height:1.7;border-left:3px solid {PRIMARY}}}

/* Modal */
.modal-overlay{{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:9999;display:flex;align-items:center;justify-content:center;padding:16px}}
.modal-box{{background:white;border-radius:14px;padding:28px;max-width:400px;width:100%;text-align:center;box-shadow:0 20px 60px rgba(0,0,0,.3)}}

/* Dato rows */
.dato-row{{display:flex;justify-content:space-between;padding:11px 0;border-bottom:1px solid {BORDER};font-size:13px}}
.dato-row:last-child{{border-bottom:none}}
.dato-label{{color:#64748b;font-weight:600}}
.dato-valor{{font-weight:600;text-align:right}}

.scroll-x{{overflow-x:auto}}

/* Portal público */
.portal-hero{{
  background:linear-gradient(135deg, {PRIMARY} 0%, {SECONDARY} 100%);
  color:white;padding:40px 24px;text-align:center;
  margin-bottom:20px;border-radius:12px;
}}
.portal-hero h1{{font-size:28px;margin-bottom:8px}}
.portal-hero p{{opacity:.9;font-size:14px}}

.card-service{{
  background:white;border-radius:12px;padding:20px;
  border:1px solid {BORDER};transition:.2s;cursor:pointer;
}}
.card-service:hover{{border-color:{PRIMARY};box-shadow:0 8px 24px rgba({PRIMARY:0},{PRIMARY:1},{PRIMARY:2},.1)}}
.card-service-icon{{font-size:32px;margin-bottom:12px}}

.status-badge{{display:inline-block;padding:6px 12px;border-radius:20px;font-size:11px;font-weight:700}}
.status-vigente{{background:rgba({SUCCESS:0},{SUCCESS:1},{SUCCESS:2},.1);color:{SUCCESS}}}
.status-vencido{{background:rgba({DANGER:0},{DANGER:1},{DANGER:2},.1);color:{DANGER}}}
.status-pendiente{{background:rgba({WARNING:0},{WARNING:1},{WARNING:2},.1);color:{WARNING}}}
"""

def head(titulo):
    return f"""<!DOCTYPE html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{titulo} — {BRAND_NOMBRE}</title>
<link rel="icon" href="{ESCUDO_URL}" sizes="32x32"/>
<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.7.2/css/all.min.css">
<style>{CSS_MODERNO}</style></head><body>"""

def _sidebar_links():
    return """<li><a href="/panel/admin"><i class="fa-solid fa-gauge"></i> Inicio</a></li>
<li><a href="/panel/folios"><i class="fa-solid fa-list"></i> Folios</a></li>
<li><a href="/panel/registro_admin"><i class="fa-solid fa-file-circle-plus"></i> Registrar</a></li>
<li><a href="/panel/crear_usuario"><i class="fa-solid fa-user-plus"></i> Usuario</a></li>
<li><a href="/panel/tablas"><i class="fa-solid fa-database"></i> Tablas</a></li>
<li><a href="/consulta_folio"><i class="fa-solid fa-magnifying-glass"></i> Consultar</a></li>
<li><a href="/panel/logout" class="danger"><i class="fa-solid fa-right-from-bracket"></i> Salir</a></li>"""

def navbar():
    links = _sidebar_links()
    return f"""<div class="overlay" id="overlay"></div>
<aside class="sidebar" id="sidebar">
  <div class="sidebar-header">
    <img src="{LOGO_URL}" alt="{BRAND_NOMBRE}">
    <p>{BRAND_NOMBRE}</p>
  </div>
  <ul>{links}</ul>
</aside>
<div class="topbar">
  <div class="topbar-inner">
    <button class="hamburger" id="hamburger" onclick="openNav()"><span></span><span></span><span></span></button>
    <img src="{LOGO_URL}" alt="{BRAND_NOMBRE}">
    <span class="brand-text">{BRAND_NOMBRE}</span>
  </div>
</div>"""

def admin_bar(seccion):
    return f'<div class="admin-bar"><i class="fa-solid fa-shield-halved"></i> {seccion}</div>'

def footer(scripts=""):
    return f"""{scripts}<script>
function openNav(){{document.getElementById('sidebar').classList.toggle('open');document.getElementById('overlay').classList.toggle('show')}}
document.getElementById('overlay')&&document.getElementById('overlay').addEventListener('click',()=>{{document.getElementById('sidebar').classList.remove('open');document.getElementById('overlay').classList.remove('show')}})
</script></body></html>"""

def page(titulo, seccion, contenido, scripts=""):
    return (head(titulo) + navbar() + 
            '<div class="main-wrapper">' + admin_bar(seccion) +
            f'<div class="content">{contenido}</div>' + footer(scripts))

def login_html(error=False):
    err = f'<div class="alert alert-danger"><i class="fa-solid fa-triangle-exclamation"></i> Usuario o contraseña incorrectos</div>' if error else ""
    return f"""<!DOCTYPE html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Acceso — {BRAND_NOMBRE}</title>
<link rel="icon" href="{ESCUDO_URL}" sizes="32x32"/>
<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.7.2/css/all.min.css">
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{
  background:linear-gradient(135deg, {PRIMARY} 0%, {SECONDARY} 100%);
  min-height:100vh;margin:0;display:flex;align-items:center;justify-content:center;
  font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
}}
.login-container{{background:white;border-radius:16px;padding:40px;max-width:380px;width:100%;box-shadow:0 20px 60px rgba(0,0,0,.3)}}
.login-header{{text-align:center;margin-bottom:28px}}
.login-header img{{height:70px;object-fit:contain;margin-bottom:12px}}
.login-title{{font-size:22px;font-weight:700;color:{TEXT};margin-bottom:4px}}
.login-subtitle{{font-size:13px;color:#64748b;line-height:1.5}}
.form-label{{font-weight:600;font-size:13px;display:block;margin-bottom:6px;text-transform:uppercase;letter-spacing:.3px;color:#64748b}}
.form-control{{display:block;width:100%;padding:11px 13px;border:1.5px solid {BORDER};border-radius:8px;font-size:14px;font-family:inherit;margin-bottom:14px}}
.form-control:focus{{border-color:{PRIMARY};outline:none;box-shadow:0 0 0 3px rgba({PRIMARY:0},{PRIMARY:1},{PRIMARY:2},.1)}}
.btn-login{{background:{PRIMARY};border:none;color:white;width:100%;padding:12px;font-weight:700;font-size:14px;border-radius:8px;cursor:pointer;font-family:inherit;text-transform:uppercase;letter-spacing:.3px;transition:.2s;margin-top:8px}}
.btn-login:hover{{background:{SECONDARY};}}
.alert{{padding:12px 14px;border-radius:8px;font-size:13px;font-weight:600;margin-bottom:14px;border-left:4px solid}}
.alert-danger{{background:rgba({DANGER:0},{DANGER:1},{DANGER:2},.08);border-color:{DANGER};color:{DANGER}}}
</style></head><body>
<div class="login-container">
  <div class="login-header">
    <img src="{ESCUDO_URL}" alt="{BRAND_NOMBRE}">
    <div class="login-title">{BRAND_NOMBRE}</div>
    <div class="login-subtitle">{BRAND_SLOGAN}<br>Acceso al Sistema Administrativo</div>
  </div>
  {err}
  <form method="POST" action="/panel/login">
    <label class="form-label">Usuario</label>
    <input type="text" name="username" class="form-control" required autofocus autocomplete="off">
    <label class="form-label">Contraseña</label>
    <input type="password" name="password" class="form-control" required>
    <button type="submit" class="btn-login"><i class="fa-solid fa-right-to-bracket"></i> Ingresar</button>
  </form>
</div>
</body></html>"""

# ===================== PORTAL PÚBLICO =====================
PORTAL_ITEMS = [
    {"icono": "fa-car-front", "titulo": "Permiso Provisional", "ruta": "/portal/permiso-provisional-de-circulacion", "activo": True},
    {"icono": "fa-file-circle-check", "titulo": "Refrendo y Tenencia", "ruta": "#", "activo": False},
    {"icono": "fa-file-circle-minus", "titulo": "Baja de Vehículos", "ruta": "#", "activo": False},
    {"icono": "fa-magnifying-glass", "titulo": "Consultar Folio", "ruta": "/consulta_folio", "activo": True},
]

def portal_navbar():
    items_html = "".join(
        f'<li><a href="{it["ruta"]}"><i class="fa-solid {it["icono"]}"></i>{it["titulo"]}</a></li>'
        for it in PORTAL_ITEMS
    )
    return f"""<div class="overlay" id="overlay"></div>
<aside class="sidebar" id="sidebar">
  <div class="sidebar-header">
    <img src="{LOGO_URL}" alt="{BRAND_NOMBRE}">
    <p>{BRAND_NOMBRE}</p>
  </div>
  <ul>
    <li><a href="/portal"><i class="fa-solid fa-house"></i> Inicio</a></li>
    {items_html}
    <li><a href="/panel/login"><i class="fa-solid fa-right-to-bracket"></i> Acceso</a></li>
  </ul>
</aside>
<div class="topbar">
  <div class="topbar-inner">
    <button class="hamburger" id="hamburger" onclick="openNav()"><span></span><span></span><span></span></button>
    <img src="{LOGO_URL}" alt="{BRAND_NOMBRE}">
    <span class="brand-text">{BRAND_NOMBRE}</span>
  </div>
</div>"""

def portal_page(titulo, contenido):
    return (head(titulo) + portal_navbar() +
            '<div class="main-wrapper">' +
            f'<div class="content">{contenido}</div>' + footer())

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
    print(f"[SISTEMA] Tlaxcala v2.0 — Diseño moderno activado — siguiente folio: {FOLIO_PREFIJO}{_folio_counter['siguiente']}")
    yield
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError): await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Trámites Vehiculares", version="2.0")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "tlaxcala_clave_super_segura_cambiar"))
try: app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
except Exception: pass

# ===================== PORTAL PÚBLICO — RUTAS =====================
@app.get("/portal", response_class=HTMLResponse)
async def portal_home():
    cards = "".join(f"""<a href="{it['ruta']}" class="menu-btn">
        <i class="fa-solid {it['icono']}"></i>
        <span>{it['titulo']}</span>
      </a>""" for it in PORTAL_ITEMS)
    contenido = f"""
    <div class="portal-hero">
      <h1><i class="fa-solid fa-car"></i> Trámites Vehiculares</h1>
      <p>{BRAND_SLOGAN}</p>
    </div>
    <div class="grid">{cards}</div>
    """
    return HTMLResponse(portal_page("Inicio", contenido))

@app.get("/portal/permiso-provisional-de-circulacion", response_class=HTMLResponse)
async def portal_permiso_provisional():
    contenido = f"""
    <div class="page-title"><i class="fa-solid fa-car-front"></i> Permiso Provisional de Circulación</div>
    <div class="grid" style="grid-template-columns:1fr 2fr;gap:20px;align-items:start">
      <div class="form-card">
        <h3 style="font-size:16px;margin-bottom:14px;color:{PRIMARY}">Información del Trámite</h3>
        <p style="font-size:13px;line-height:1.6;margin-bottom:12px"><strong>Descripción:</strong> Permiso provisional para circulación de vehículos nuevos y usados.</p>
        <p style="font-size:13px;line-height:1.6;margin-bottom:12px"><strong>Costo:</strong> <span style="color:{SUCCESS};font-weight:700">$352.00 MXN</span></p>
        <p style="font-size:13px;line-height:1.6;margin-bottom:12px"><strong>Vigencia:</strong> 30 días naturales</p>
        <a href="/panel/login" class="btn btn-primary" style="width:100%;margin-top:14px">Iniciar Trámite</a>
      </div>
      <div class="form-card">
        <h3 style="font-size:16px;margin-bottom:14px;color:{PRIMARY}">Procedimiento</h3>
        <ol style="font-size:13px;line-height:1.8;padding-left:20px">
          <li>Ingresa al sistema con tu usuario</li>
          <li>Captura datos del vehículo (marca, línea, año, etc.)</li>
          <li>El sistema genera automáticamente tu folio y PDF</li>
          <li>Realiza el pago en 36 horas</li>
          <li>Envía tu comprobante para validación</li>
          <li>Tu permiso queda activo</li>
        </ol>
      </div>
    </div>
    <div style="margin-top:20px"><a href="/portal" class="btn btn-outline">← Volver</a></div>
    """
    return HTMLResponse(portal_page("Permiso Provisional", contenido))

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
    color_pend = DANGER if pendientes else SUCCESS
    contenido = f"""
    <div class="page-title"><i class="fa-solid fa-gauge"></i> Panel de Administración</div>
    <div class="grid">
      <div class="stat-card">
        <div class="stat-num" style="color:{INFO}">{len(timers_activos)}</div>
        <div class="stat-lbl">Timers Activos</div>
      </div>
      <div class="stat-card">
        <div class="stat-num" style="color:{color_pend}">{pendientes}</div>
        <div class="stat-lbl">Pendientes de Pago</div>
      </div>
      <div class="stat-card">
        <div class="stat-num" style="color:{SUCCESS}">{FOLIO_PREFIJO}{_folio_counter['siguiente']}</div>
        <div class="stat-lbl">Siguiente Folio</div>
      </div>
    </div>
    <div class="grid">
      <a href="/panel/folios" class="menu-btn"><i class="fa-solid fa-list"></i><span>Ver Folios</span></a>
      <a href="/panel/registro_admin" class="menu-btn"><i class="fa-solid fa-file-circle-plus"></i><span>Registrar Permiso</span></a>
      <a href="/panel/crear_usuario" class="menu-btn"><i class="fa-solid fa-user-plus"></i><span>Crear Usuario</span></a>
      <a href="/panel/tablas" class="menu-btn"><i class="fa-solid fa-database"></i><span>Tablas BD</span></a>
      <a href="/consulta_folio" class="menu-btn"><i class="fa-solid fa-magnifying-glass"></i><span>Consultar Folio</span></a>
      <a href="/portal" class="menu-btn"><i class="fa-solid fa-globe"></i><span>Portal Público</span></a>
    </div>
    """
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
    <h2 style="color:{PRIMARY};font-size:18px;font-weight:700;margin-bottom:8px">Permiso Generado</h2>
    <p style="color:#64748b;font-size:13px;margin-bottom:20px">¿Descargar el PDF?</p>
    <div style="display:flex;gap:8px;justify-content:center">
      <a href="{pdf_url}" target="_blank" class="btn btn-primary btn-sm" style="width:auto" onclick="document.getElementById('mD').remove()"><i class="fa-solid fa-download"></i> Descargar</a>
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
    msg_html = f'<div class="alert alert-success">{msg}</div>' if msg else ""
    filas = ""
    for f in folios:
        pago = f.get("estado_pago","VALIDADO") or "VALIDADO"
        ec   = f.get("estado_calc","")
        bp   = f'<span class="badge badge-warning">PEND</span>' if pago=="PENDIENTE_PAGO" else f'<span class="badge badge-success">OK</span>'
        be   = f'<span class="badge badge-success">VIG</span>' if ec=="VIGENTE" else f'<span class="badge badge-danger">VEN</span>'
        bval = f'<form method="POST" action="/panel/validar/{f["folio"]}" style="display:inline"><button class="btn btn-success btn-sm" onclick="return confirm(\'¿Validar?\')">✅</button></form> ' if pago=="PENDIENTE_PAGO" else ""
        pdf  = f.get("pdf_url","")
        bpdf = f'<a href="{pdf}" target="_blank" class="btn btn-sm" style="background:{PRIMARY};color:white;display:inline-flex">📄</a> ' if pdf else ""
        filas += f"""<tr>
          <td><strong style="color:{PRIMARY}">{f.get("folio","")}</strong><br><small style="color:#94a3b8">{f.get("creado_por","")[:20]}</small></td>
          <td>{f.get("nombre","")[:20]}</td>
          <td>{f.get("marca","")} {f.get("linea","")}</td>
          <td>{str(f.get("fecha_expedicion",""))[:10]}</td>
          <td>{be} {bp}</td>
          <td style="white-space:nowrap">{bval}{bpdf}<a href="/consulta/{f.get('folio','')}" target="_blank" class="btn btn-sm btn-outline">🔗</a></td>
        </tr>"""
    filtros = f"""<div class="filter-bar">
      <form method="GET" style="display:contents">
        <input type="text" name="filtro" class="form-control" value="{filtro}" placeholder="Buscar..." style="max-width:200px">
        <select name="criterio" class="form-control" style="max-width:100px">
          <option value="folio" {"selected" if crit=="folio" else ""}>Folio</option>
          <option value="nombre" {"selected" if crit=="nombre" else ""}>Nombre</option>
          <option value="numero_serie" {"selected" if crit=="numero_serie" else ""}>Serie</option>
        </select>
        <select name="estado_pago" class="form-control" style="max-width:110px">
          <option value="todos" {"selected" if ep_fil=="todos" else ""}>Todos</option>
          <option value="PENDIENTE_PAGO" {"selected" if ep_fil=="PENDIENTE_PAGO" else ""}>Pendiente</option>
          <option value="VALIDADO" {"selected" if ep_fil=="VALIDADO" else ""}>Validado</option>
        </select>
        <button type="submit" class="btn btn-primary btn-sm" style="width:auto">Filtrar</button>
        <a href="/panel/folios" class="btn btn-outline btn-sm">✕</a>
      </form>
      <span style="font-size:12px;color:#94a3b8;margin-left:auto">{len(folios)} resultados</span>
    </div>"""
    contenido = f"""{modal_html}
    <div class="page-title"><i class="fa-solid fa-list"></i> Folios Registrados</div>
    {msg_html}{filtros}
    <div class="tabla-wrap scroll-x"><table>
      <thead><tr><th>Folio</th><th>Titular</th><th>Vehículo</th><th>Fecha</th><th>Estado</th><th>Acciones</th></tr></thead>
      <tbody>{filas or '<tr><td colspan="6" style="text-align:center;color:#94a3b8;padding:20px">Sin folios</td></tr>'}</tbody>
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
            try: await bot.send_message(uid, f"✅ PAGO VALIDADO — TLAXCALA\nFolio: {folio}\nTitular: {nombre}\nTu permiso está activo.")
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
    err_html = f'<div class="alert alert-danger">{err}</div>' if err else ""
    contenido = f"""
    <div class="page-title"><i class="fa-solid fa-file-circle-plus"></i> Registrar Permiso</div>{err_html}
    <div class="form-card" style="max-width:600px">
      <form method="POST" action="/panel/registro_admin">
        <div class="mb-3">
          <label class="form-label">Folio manual <small style="color:#94a3b8;font-weight:400">(vacío = auto)</small></label>
          <input type="text" name="folio" class="form-control" placeholder="{FOLIO_PREFIJO}53314" style="text-transform:uppercase">
        </div>
        <div class="row-2">
          <div class="mb-3"><label class="form-label">Marca *</label><input type="text" name="marca" class="form-control" required style="text-transform:uppercase"></div>
          <div class="mb-3"><label class="form-label">Línea *</label><input type="text" name="linea" class="form-control" required style="text-transform:uppercase"></div>
        </div>
        <div class="row-2">
          <div class="mb-3"><label class="form-label">Año *</label><input type="text" name="anio" class="form-control" maxlength="4" required></div>
          <div class="mb-3"><label class="form-label">Color *</label><input type="text" name="color" class="form-control" required style="text-transform:uppercase"></div>
        </div>
        <div class="mb-3"><label class="form-label">Núm. Serie / NIV *</label><input type="text" name="numero_serie" class="form-control" required style="text-transform:uppercase"></div>
        <div class="mb-3"><label class="form-label">Núm. Motor *</label><input type="text" name="numero_motor" class="form-control" required style="text-transform:uppercase"></div>
        <div class="mb-3"><label class="form-label">Clave Vehicular *</label><input type="text" name="cve_vehicular" class="form-control" required style="text-transform:uppercase"></div>
        <div class="mb-3"><label class="form-label">Nombre del Propietario *</label><input type="text" name="nombre" class="form-control" required style="text-transform:uppercase"></div>
        <div class="row-2">
          <div class="mb-3"><label class="form-label">Fecha de Expedición</label><input type="date" name="fecha_expedicion" class="form-control" value="{hoy}"></div>
          <div class="mb-3"><label class="form-label">Vencimiento <small style="color:#94a3b8">(vacío=+30d)</small></label><input type="date" name="fecha_vencimiento" class="form-control"></div>
        </div>
        <button type="submit" class="btn btn-primary"><i class="fa-solid fa-file-circle-plus"></i> Generar Permiso</button>
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
    <div class="page-title"><i class="fa-solid fa-user-plus"></i> Crear Usuario</div>
    {"<div class='alert alert-success'>"+msg+"</div>" if msg else ""}
    {"<div class='alert alert-danger'>"+err+"</div>" if err else ""}
    <div class="form-card" style="max-width:400px">
      <form method="POST" action="/panel/crear_usuario">
        <div class="mb-3"><label class="form-label">Usuario *</label><input type="text" name="username" class="form-control" required autocomplete="off"></div>
        <div class="mb-3"><label class="form-label">Contraseña *</label><input type="password" name="password" class="form-control" required></div>
        <div class="mb-4"><label class="form-label">Folios Asignados *</label><input type="number" name="folios" class="form-control" min="1" required></div>
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
    form_html = f"""<div class="form-card" style="max-width:600px">
      <form method="POST" action="/registro_usuario">
        <div class="row-2">
          <div class="mb-3"><label class="form-label">Marca *</label><input type="text" name="marca" class="form-control" required style="text-transform:uppercase"></div>
          <div class="mb-3"><label class="form-label">Línea *</label><input type="text" name="linea" class="form-control" required style="text-transform:uppercase"></div>
        </div>
        <div class="row-3">
          <div class="mb-3"><label class="form-label">Año *</label><input type="number" name="anio" class="form-control" required></div>
          <div class="mb-3" style="grid-column:span 2"><label class="form-label">Color *</label><input type="text" name="color" class="form-control" required style="text-transform:uppercase"></div>
        </div>
        <div class="mb-3"><label class="form-label">Núm. Serie / NIV *</label><input type="text" name="serie" class="form-control" required style="text-transform:uppercase"></div>
        <div class="mb-3"><label class="form-label">Núm. Motor *</label><input type="text" name="motor" class="form-control" required style="text-transform:uppercase"></div>
        <div class="mb-3"><label class="form-label">Clave Vehicular *</label><input type="text" name="cve_vehicular" class="form-control" required style="text-transform:uppercase"></div>
        <div class="mb-3"><label class="form-label">Nombre del Propietario *</label><input type="text" name="nombre" class="form-control" required style="text-transform:uppercase"></div>
        <div class="mb-4"><label class="form-label">Fecha Inicio de Vigencia</label><input type="date" name="fecha_inicio" class="form-control" value="{hoy}" min="{hoy}"></div>
        <button type="submit" class="btn btn-primary" style="width:100%">Registrar Folio</button>
      </form>
    </div>""" if disp > 0 else '<div class="alert alert-danger">Sin folios disponibles. Contacta al administrador.</div>'
    contenido = f"""
    <div class="page-title"><i class="fa-solid fa-file-circle-plus"></i> Registrar Permiso</div>
    <div class="form-card mb-3" style="max-width:600px">
      <div style="display:flex;justify-content:space-between;margin-bottom:12px">
        <span style="font-weight:700;font-size:14px">Mis Folios</span>
        <span style="font-size:12px;color:#94a3b8">{usad} / {asig}</span>
      </div>
      <div style="width:100%;height:8px;background:rgba({PRIMARY:0},{PRIMARY:1},{PRIMARY:2},.1);border-radius:4px;overflow:hidden">
        <div style="height:100%;background:{PRIMARY};width:{porc}%;transition:.3s;border-radius:4px"></div>
      </div>
      <div style="display:flex;justify-content:space-between;font-size:11px;color:#94a3b8;margin-top:8px">
        <span>Usados: <strong>{usad}</strong></span><span>Total: <strong>{asig}</strong></span><span>Disponibles: <strong style="color:{PRIMARY}">{disp}</strong></span>
      </div>
    </div>
    {"<div class='alert alert-success'>"+msg+"</div>" if msg else ""}
    {"<div class='alert alert-danger'>"+err+"</div>" if err else ""}
    {form_html}
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:16px">
      <a href="/mis_permisos" class="btn btn-outline btn-sm">📋 Mis Permisos</a>
      <a href="/consulta_folio" class="btn btn-outline btn-sm">🔍 Consultar</a>
      <a href="/panel/logout" class="btn btn-danger btn-sm">🚪 Salir</a>
    </div>"""
    scripts = """<script>
document.querySelector('form[action="/registro_usuario"]')&&document.querySelector('form[action="/registro_usuario"]').addEventListener('submit',function(){{
  const btn=this.querySelector('button[type="submit"]');
  if(btn){{btn.disabled=true;btn.textContent='⏳ Generando...';}}
  setTimeout(()=>{{if(btn){{btn.disabled=false;btn.textContent='Registrar Folio';}}}},12000);
}});
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
        <div class="page-title"><i class="fa-solid fa-check"></i> ✅ Permiso Generado</div>
        <div class="form-card" style="text-align:center;max-width:500px">
          <div style="font-size:52px;margin-bottom:16px">📄</div>
          <h2 style="color:{PRIMARY};font-size:24px;font-weight:700;margin-bottom:8px">{fg}</h2>
          <div class="info-box" style="text-align:left">
            <strong>Vehículo:</strong> {marca.upper()} {linea.upper()} {anio}<br>
            <strong>Serie/NIV:</strong> {serie.upper()} | <strong>Motor:</strong> {motor.upper()}<br>
            <strong>Clave Vehicular:</strong> {cve_vehicular.upper()}<br>
            <strong>Color:</strong> {color.upper()}<br>
            <strong>Propietario:</strong> {nombre.upper()}<br>
            <strong>Vigencia:</strong> {fe.strftime("%d/%m/%Y")} — {fv.strftime("%d/%m/%Y")}
          </div>
          {"<a href='"+pdf_url+"' target='_blank' class='btn btn-primary' style='margin-bottom:12px'><i class='fa-solid fa-download'></i> Descargar PDF</a>" if pdf_url else ""}
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
        be  = f'<span class="badge badge-success">VIG</span>' if ec=="VIGENTE" else f'<span class="badge badge-danger">VEN</span>'
        pdf = p.get("pdf_url","")
        btn = f'<a href="{pdf}" target="_blank" class="btn btn-sm" style="background:{PRIMARY};color:white;display:inline-flex">📥</a> ' if pdf else ""
        filas += f"""<tr>
          <td><strong style="color:{PRIMARY}">{p.get("folio","")}</strong></td>
          <td>{p.get("marca","")} {p.get("linea","")}<br><small style="color:#94a3b8">{p.get("anio","")}</small></td>
          <td style="font-size:12px">{p.get("numero_serie","")}</td>
          <td>{p.get("fe_fmt","")}</td><td>{be}</td>
          <td style="white-space:nowrap">{btn}<a href="/consulta/{p.get('folio','')}" target="_blank" class="btn btn-sm btn-outline">🔗</a></td>
        </tr>"""
    contenido = f"""
    <div class="page-title"><i class="fa-solid fa-list"></i> Mis Permisos</div>
    <div class="grid">
      <div class="stat-card"><div class="stat-num">{asig}</div><div class="stat-lbl">Asignados</div></div>
      <div class="stat-card"><div class="stat-num">{asig-usad}</div><div class="stat-lbl">Disponibles</div></div>
      <div class="stat-card"><div class="stat-num" style="color:{SUCCESS}">{vig}</div><div class="stat-lbl">Vigentes</div></div>
      <div class="stat-card"><div class="stat-num" style="color:{PRIMARY}">{len(permisos)}</div><div class="stat-lbl">Total</div></div>
    </div>
    <div class="tabla-wrap scroll-x"><table>
      <thead><tr><th>Folio</th><th>Vehículo</th><th>Serie</th><th>Fecha</th><th>Estado</th><th>Acciones</th></tr></thead>
      <tbody>{filas or '<tr><td colspan="6" style="text-align:center;color:#94a3b8;padding:20px">Sin permisos</td></tr>'}</tbody>
    </table></div>
    <div style="display:flex;gap:8px;margin-top:14px">
      <a href="/registro_usuario" class="btn btn-primary btn-sm" style="width:auto">+ Nuevo</a>
      <a href="/panel/logout" class="btn btn-danger btn-sm">🚪 Salir</a>
    </div>"""
    return HTMLResponse(page("Mis Permisos","Mis Permisos", contenido))

# ===================== CONSULTA PÚBLICA =====================
@app.get("/consulta_folio", response_class=HTMLResponse)
async def consulta_folio_form(request: Request):
    contenido = f"""
    <div class="page-title"><i class="fa-solid fa-magnifying-glass"></i> Consultar Folio</div>
    <div class="form-card" style="max-width:400px">
      <form method="POST" action="/consulta_folio">
        <div class="mb-3"><label class="form-label">Número de Folio</label>
          <input type="text" name="folio" class="form-control" placeholder="{FOLIO_PREFIJO}53314" required autofocus style="text-transform:uppercase"></div>
        <button type="submit" class="btn btn-primary" style="width:100%"><i class="fa-solid fa-magnifying-glass"></i> Buscar</button>
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
            badge = f"""<div style="background:{DANGER};color:white;padding:18px 20px;border-radius:12px;font-size:15px;font-weight:700;text-align:center;margin-bottom:20px">
              <i class="fa-solid fa-circle-xmark" style="font-size:24px;display:block;margin-bottom:8px"></i>
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
                badge = f"""<div style="background:{SUCCESS};color:white;padding:18px 20px;border-radius:12px;font-size:15px;font-weight:700;text-align:center;margin-bottom:20px">
                  <i class="fa-solid fa-circle-check" style="font-size:24px;display:block;margin-bottom:8px"></i>
                  EL FOLIO {folio} ESTÁ VIGENTE
                </div>"""
            else:
                badge = f"""<div style="background:{WARNING};color:white;padding:18px 20px;border-radius:12px;font-size:15px;font-weight:700;text-align:center;margin-bottom:20px">
                  <i class="fa-solid fa-clock" style="font-size:24px;display:block;margin-bottom:8px"></i>
                  EL FOLIO {folio} ESTÁ VENCIDO
                </div>"""

            datos_html = f"""
            <div class="form-card mb-3">
              <h3 style="color:{PRIMARY};font-weight:700;font-size:14px;padding-bottom:10px;margin-bottom:10px;border-bottom:2px solid {BORDER}">
                <i class="fa-solid fa-car"></i> Datos del Vehículo
              </h3>
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
            <div class="form-card">
              <h3 style="color:{PRIMARY};font-weight:700;font-size:14px;padding-bottom:10px;margin-bottom:10px;border-bottom:2px solid {BORDER}">
                <i class="fa-solid fa-file-shield"></i> Datos del Permiso
              </h3>
              <div>
                {_row("Folio",  f'<span style="color:{PRIMARY};font-weight:700">{folio}</span>')}
                {_row("Propietario", f.get("nombre",""))}
                {_row("Fecha de Expedición",  fe.strftime("%d/%m/%Y") if fe else "—")}
                {_row("Fecha de Vencimiento", fv.strftime("%d/%m/%Y") if fv else "—")}
              </div>
            </div>"""

        contenido = f"""
        <div class="page-title"><i class="fa-solid fa-magnifying-glass"></i> Consultar Folio</div>
        {badge}
        {datos_html}
        <a href="/consulta_folio" class="btn btn-outline btn-sm">← Nueva consulta</a>
        """
        return HTMLResponse(page(f"Folio {folio}", "Consultar Folio", contenido))
    except Exception as e:
        print(f"[CONSULTA] Error: {e}")
        return HTMLResponse(page("Error", "Consultar Folio", f"<p style='color:{DANGER}'>Error: {str(e)}</p><a href='/consulta_folio' class='btn btn-outline btn-sm'>← Volver</a>"))

# ===================== TABLAS BD =====================
@app.get("/panel/tablas", response_class=HTMLResponse)
async def admin_tablas(request: Request):
    if not request.session.get("admin"): return RedirectResponse(url="/panel/login", status_code=303)
    cards = "".join([f"""<div class="form-card">
      <strong style="color:{PRIMARY};font-size:14px">🗄️ {info['nombre']}</strong>
      <p style="font-size:11px;color:#94a3b8;margin:6px 0 12px"><code>{nombre}</code></p>
      <a href="/panel/tabla/{nombre}" class="btn btn-primary btn-sm" style="width:100%">Ver y editar →</a>
    </div>""" for nombre, info in TABLAS_DISPONIBLES.items()])
    contenido = f'<div class="page-title"><i class="fa-solid fa-database"></i> Tablas Base de Datos</div><div class="grid">{cards}</div>'
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
        celdas = f'<td style="color:#cbd5e1;font-size:11px;width:30px">{offset+i+1}</td>'
        for col in columnas:
            val = reg.get(col); disp = str(val)[:25] if val is not None else "null"
            celdas += f'<td><span data-col="{col}" data-pk="{str(reg.get(pk_col,""))}" data-val="{str(val or "")}" onclick="editCell(this)" style="cursor:pointer;padding:4px;border-radius:4px">{disp}</span></td>'
        celdas += f'<td><button class="btn btn-sm btn-danger" style="padding:3px 6px;font-size:10px" onclick="delRow(this,\'{str(reg.get(pk_col,""))}\',\'row{i}\')">×</button></td>'
        return f'<tr id="row{i}">{celdas}</tr>'
    tbody = "".join(_fila(i, registros[i]) for i in range(len(registros))) or "<tr><td colspan='20' style='text-align:center;padding:20px;color:#94a3b8'>Sin registros</td></tr>"
    pag = ""
    if total_pages > 1:
        pag = '<div style="display:flex;gap:8px;justify-content:center;padding:14px">'
        if page_n>1: pag += f'<a href="?q={q}&page={page_n-1}" class="btn btn-outline btn-sm">← Ant</a>'
        pag += f'<span class="btn btn-sm" style="background:{PRIMARY};color:white;cursor:default">{page_n}/{total_pages}</span>'
        if page_n<total_pages: pag += f'<a href="?q={q}&page={page_n+1}" class="btn btn-outline btn-sm">Sig →</a>'
        pag += '</div>'
    contenido = f"""
    <div class="page-title">📊 {info['nombre']}</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center">
      <form method="GET" style="display:contents">
        <input type="text" name="q" value="{q}" placeholder="Buscar..." class="form-control" style="max-width:200px;flex:1;min-width:150px">
        <button type="submit" class="btn btn-primary btn-sm" style="width:auto">🔍</button>
        {"<a href='/panel/tabla/"+nombre_tabla+"' class='btn btn-outline btn-sm'>✕</a>" if q else ""}
      </form>
      <span style="font-size:11px;color:#94a3b8;margin-left:auto">{total} registros</span>
    </div>
    <div class="tabla-wrap scroll-x"><table id="tbl" style="font-size:12px"><thead><tr><th style="width:30px">#</th>{th}</tr></thead><tbody>{tbody}</tbody></table>{pag}</div>
    <div style="margin-top:14px"><a href="/panel/tablas" class="btn btn-outline btn-sm">← Tablas</a></div>
    <div class="toast-f" id="toast"></div>"""
    scripts = f"""<script>
const TABLA="{nombre_tabla}",PK_COL="{pk_col}";
function editCell(span){{const col=span.dataset.col,pk=span.dataset.pk,orig=span.dataset.val;const inp=document.createElement('input');inp.type='text';inp.className='form-control';inp.value=orig;inp.style.marginBottom='0';inp._span=span;inp._orig=orig;inp._col=col;inp._pk=pk;span.parentNode.insertBefore(inp,span);span.style.display='none';inp.focus();inp.select();inp.addEventListener('blur',()=>fin(inp));inp.addEventListener('keydown',e=>{{if(e.key==='Enter'){{e.preventDefault();inp.blur();}}if(e.key==='Escape'){{inp._cancel=true;inp.blur();}}}});}}
function fin(inp){{const span=inp._span,nv=inp.value.trim(),orig=inp._orig;inp.remove();span.style.display='';if(inp._cancel||nv===orig)return;span.textContent=nv||'null';span.dataset.val=nv;fetch('/panel/api/update_cell',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{tabla:TABLA,pk_col:PK_COL,pk_val:inp._pk,col:inp._col,val:nv}})}}).then(r=>r.json()).then(d=>{{if(d.ok)toast('✓ guardado',true);else{{span.textContent=orig||'null';span.dataset.val=orig;toast('Error',false);}}}}).catch(()=>{{span.textContent=orig||'null';toast('Error de red',false);}});}}
function delRow(btn,pk,rowId){{if(!confirm('¿Eliminar?'))return;btn.disabled=true;fetch('/panel/api/delete_row',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{tabla:TABLA,pk_col:PK_COL,pk_val:pk}})}}).then(r=>r.json()).then(d=>{{if(d.ok){{const tr=document.getElementById(rowId);if(tr){{tr.style.opacity='0';setTimeout(()=>tr.remove(),250);}}toast('Eliminado',true);}}else{{btn.disabled=false;toast('Error',false);}}}}).catch(()=>{{btn.disabled=false;toast('Error de red',false);}});}}
let tt;function toast(msg,ok){{const t=document.getElementById('toast');t.textContent=msg;t.style.background=ok?'rgba(22,163,74,.1)':'rgba(220,38,38,.1)';t.style.color=ok?'#16a34a':'#dc2626';t.style.borderLeft='3px solid '+(ok?'#16a34a':'#dc2626');t.className='toast-f show';t.style.display='block';clearTimeout(tt);tt=setTimeout(()=>{{t.style.display='none'}},2500);}}
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
    return {"status":"healthy","version":"2.0","entidad":ENTIDAD,
            "timers_activos":len(timers_activos),
            "siguiente_folio":f"{FOLIO_PREFIJO}{_folio_counter['siguiente']}"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
