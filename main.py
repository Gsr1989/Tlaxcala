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
PLANTILLA_PDF = "TLAXCALA2026(1).pdf"   # <-- sube el PDF a la raíz del repo con este nombre EXACTO
FOLIO_PREFIJO = "ZX"
FOLIO_INICIO  = 53314
_folio_counter = {"siguiente": FOLIO_INICIO}
_folio_lock    = asyncio.Lock()
PAGE_SIZE = 100

# Paleta real extraída del portal oficial tlaxcaladigital.gob.mx (Angular/Bootstrap)
C1 = "#422b7c"   # navbar-color (morado oscuro, real del portal)
C2 = "#341f63"   # variante oscura para hover
C3 = "#e6d194"   # borde dorado de las tarjetas (card-menu-principal)
ACCENT = "#a11a5c"   # magenta de iconos/secciones (arrow-icon-section)
GREEN  = "#64ad0b"   # verde de botones de acción (btn-custom-of)
BLUE   = "#2856ad"   # azul de subtítulos de tarjeta (tittle-sub-menu)

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
                     "estado_pago","creado_por","pdf_url","timer_activo","timer_expira_en",
                     "timer_detenido_en","timer_motivo"],
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

# ===================== TIMERS — FUENTE DE VERDAD: SUPABASE =====================
#
# El timer NO vive en RAM. La RAM sólo tiene la tarea asyncio que "despierta".
# Antes de CUALQUIER borrado se consulta Supabase. Si la BD dice que el timer
# está detenido (validado / comprobante / detenido manual), NO se borra nada.
#
# Detener desde Telegram y detener desde el panel web llaman a la MISMA
# función: detener_timer_global(). Por eso quedan sincronizados siempre.
#
# Columnas necesarias en folios_registrados (correr el SQL del README):
#   timer_activo boolean default false
#   timer_expira_en timestamptz
#   timer_detenido_en timestamptz
#   timer_motivo text
# ==============================================================================

timers_activos       = {}   # sólo cache local de tareas asyncio
user_folios          = {}
pending_comprobantes = {}
TOTAL_MINUTOS_TIMER  = 36 * 60

ESTADOS_STOP_PAGO = {"VALIDADO"}
ESTADOS_STOP_GRAL = {"COMPROBANTE_ENVIADO", "TIMER_DETENIDO"}


def _leer_estado_folio(folio: str):
    """Lee el estado real del folio desde Supabase. Devuelve dict, None o 'ERROR'."""
    try:
        r = supabase.table("folios_registrados") \
            .select("folio,estado,estado_pago,user_id,nombre,timer_activo") \
            .eq("folio", folio).limit(1).execute()
        return r.data[0] if r.data else None
    except Exception as e:
        print(f"[TIMER] Error leyendo {folio}: {e}")
        return "ERROR"


async def _timer_debe_seguir(folio: str) -> bool:
    """¿El timer sigue vivo según la BD? Ante error de red NO se borra (devuelve True)."""
    row = await asyncio.to_thread(_leer_estado_folio, folio)
    if row == "ERROR":
        return True          # error de red: NUNCA borrar por las dudas
    if row is None:
        return False         # ya no existe
    if (row.get("estado_pago") or "") in ESTADOS_STOP_PAGO:
        return False
    if (row.get("estado") or "") in ESTADOS_STOP_GRAL:
        return False
    if row.get("timer_activo") is False:
        return False
    return True


async def detener_timer_global(folio: str, motivo: str,
                               estado_pago: str = None, estado: str = None) -> dict:
    """
    ÚNICA vía para detener un timer. La usan el bot de Telegram Y el panel web.
    Escribe en Supabase (fuente de verdad) y cancela la tarea local si existe.
    """
    folio = folio.strip().upper()
    row = await asyncio.to_thread(_leer_estado_folio, folio)
    if row in (None, "ERROR"):
        cancelar_timer_folio(folio)
        return {"ok": False, "motivo": "folio no encontrado", "folio": folio,
                "user_id": None, "nombre": "", "cancelado_mem": False}

    parches = {
        "timer_activo": False,
        "timer_detenido_en": datetime.now().isoformat(),
        "timer_motivo": motivo,
    }
    if estado_pago:
        parches["estado_pago"] = estado_pago
    if estado:
        parches["estado"] = estado

    ok_db = True
    try:
        await asyncio.to_thread(
            lambda: supabase.table("folios_registrados").update(parches).eq("folio", folio).execute()
        )
    except Exception as e:
        ok_db = False
        print(f"[TIMER] Error deteniendo {folio} en BD: {e}")

    cancelado_mem = cancelar_timer_folio(folio)
    print(f"[TIMER] STOP {folio} motivo={motivo} db={ok_db} mem={cancelado_mem}")
    return {
        "ok": ok_db,
        "folio": folio,
        "user_id": row.get("user_id"),
        "nombre": row.get("nombre", "") or "",
        "cancelado_mem": cancelado_mem,
    }


async def eliminar_folio_automatico(folio: str):
    """Borra el folio SOLO si la BD confirma que el timer sigue activo."""
    if not await _timer_debe_seguir(folio):
        print(f"[TIMER] {folio} ya fue detenido — NO se borra")
        limpiar_timer_folio(folio)
        return
    try:
        row = await asyncio.to_thread(_leer_estado_folio, folio)
        uid = None
        if isinstance(row, dict):
            uid = row.get("user_id")
        if not uid and folio in timers_activos:
            uid = timers_activos[folio].get("user_id")

        await asyncio.to_thread(
            lambda: supabase.table("folios_registrados").delete().eq("folio", folio).execute()
        )
        try:
            await asyncio.to_thread(lambda: supabase.storage.from_(BUCKET_NAME).remove([f"{folio}.pdf"]))
        except Exception as e:
            print(f"[STORAGE] Error borrando {folio}.pdf: {e}")
        ruta_local = os.path.join(OUTPUT_DIR, f"{folio}.pdf")
        if os.path.exists(ruta_local):
            os.remove(ruta_local)
        if uid:
            with suppress(Exception):
                await bot.send_message(uid,
                    f"⏰ TIEMPO AGOTADO - TLAXCALA\n\nEl folio {folio} fue eliminado por no completar el pago en 36 horas.\n\n📋 Use /tlaxcala para generar otro permiso.")
        limpiar_timer_folio(folio)
        print(f"[TIMER] {folio} eliminado por vencimiento")
    except Exception as e:
        print(f"[ERROR] eliminando folio {folio}: {e}")


async def enviar_recordatorio(folio: str, minutos_restantes: int):
    try:
        row = await asyncio.to_thread(_leer_estado_folio, folio)
        uid = None
        if isinstance(row, dict):
            uid = row.get("user_id")
        if not uid and folio in timers_activos:
            uid = timers_activos[folio].get("user_id")
        if not uid:
            return
        await bot.send_message(uid,
            f"⚡ RECORDATORIO - TLAXCALA\n\nFolio: {folio}\nTiempo restante: {minutos_restantes} minutos\n\n📸 Envíe su comprobante de pago.\n\n📋 Use /tlaxcala para otro permiso.")
    except Exception as e:
        print(f"[ERROR] recordatorio {folio}: {e}")


async def iniciar_timer_36h(user_id: int, folio: str, nombre: str = "",
                            segundos_restantes: int = None, marcar_db: bool = True):
    """
    Arranca el timer. Marca timer_activo=True + timer_expira_en en Supabase,
    para que sobreviva a reinicios de Render (ver rehidratar_timers).
    """
    total = segundos_restantes if segundos_restantes is not None else TOTAL_MINUTOS_TIMER * 60

    if marcar_db:
        expira = datetime.now() + timedelta(seconds=total)
        try:
            await asyncio.to_thread(lambda: supabase.table("folios_registrados").update({
                "timer_activo": True,
                "timer_expira_en": expira.isoformat(),
                "timer_detenido_en": None,
                "timer_motivo": None,
            }).eq("folio", folio).execute())
        except Exception as e:
            print(f"[TIMER] No se pudo marcar activo {folio}: {e}")

    async def timer_task():
        restante = total
        # Avisos a 90 / 60 / 30 / 10 min del final; cada uno revalida contra la BD
        for falta, minutos in [(90 * 60, 90), (60 * 60, 60), (30 * 60, 30), (10 * 60, 10)]:
            espera = restante - falta
            if espera > 0:
                await asyncio.sleep(espera)
                restante = falta
                if not await _timer_debe_seguir(folio):
                    print(f"[TIMER] {folio} detenido durante recordatorios — task termina")
                    limpiar_timer_folio(folio)
                    return
                await enviar_recordatorio(folio, minutos)
        if restante > 0:
            await asyncio.sleep(restante)
        await eliminar_folio_automatico(folio)

    task = asyncio.create_task(timer_task())
    timers_activos[folio] = {
        "task": task,
        "user_id": user_id,
        "start_time": datetime.now() - timedelta(seconds=(TOTAL_MINUTOS_TIMER * 60 - total)),
        "nombre": nombre,
    }
    user_folios.setdefault(user_id, []).append(folio)
    print(f"[TIMER] Iniciado {folio} ({nombre}) — {int(total/60)} min restantes")


async def rehidratar_timers():
    """Al arrancar el servicio, revive los timers que la BD dice que siguen vivos."""
    try:
        r = await asyncio.to_thread(lambda: supabase.table("folios_registrados")
            .select("folio,user_id,nombre,timer_expira_en")
            .eq("entidad", ENTIDAD).eq("timer_activo", True).execute())
        filas = r.data or []
        ahora = datetime.now()
        revividos = 0
        for row in filas:
            folio = row.get("folio")
            exp   = row.get("timer_expira_en")
            if not folio or not exp:
                continue
            try:
                expira = datetime.fromisoformat(str(exp).replace("Z", "+00:00")).replace(tzinfo=None)
            except Exception:
                continue
            seg = (expira - ahora).total_seconds()
            if seg <= 0:
                await eliminar_folio_automatico(folio)
                continue
            await iniciar_timer_36h(row.get("user_id") or 0, folio, row.get("nombre", "") or "",
                                    segundos_restantes=int(seg), marcar_db=False)
            revividos += 1
        print(f"[TIMER] Rehidratados {revividos}/{len(filas)} timers desde la BD")
    except Exception as e:
        print(f"[TIMER] Error rehidratando: {e}")


def cancelar_timer_folio(folio: str) -> bool:
    """Cancela SOLO la tarea local. No toca la BD (eso lo hace detener_timer_global)."""
    if folio not in timers_activos:
        return False
    with suppress(Exception):
        timers_activos[folio]["task"].cancel()
    uid = timers_activos[folio]["user_id"]
    del timers_activos[folio]
    if uid in user_folios and folio in user_folios[uid]:
        user_folios[uid].remove(folio)
        if not user_folios[uid]:
            del user_folios[uid]
    return True


def limpiar_timer_folio(folio: str):
    if folio not in timers_activos:
        return
    uid = timers_activos[folio]["user_id"]
    del timers_activos[folio]
    if uid in user_folios and folio in user_folios[uid]:
        user_folios[uid].remove(folio)
        if not user_folios[uid]:
            del user_folios[uid]


def obtener_folios_usuario(user_id: int) -> list:
    return user_folios.get(user_id, [])


def _folios_activos_db(user_id: int) -> list:
    """Folios con timer vivo según la BD (no depende de la RAM)."""
    try:
        r = supabase.table("folios_registrados") \
            .select("folio,nombre,timer_expira_en") \
            .eq("entidad", ENTIDAD).eq("user_id", user_id).eq("timer_activo", True) \
            .execute()
        return r.data or []
    except Exception as e:
        print(f"[TIMER] Error listando folios de {user_id}: {e}")
        return []


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
    """QR izquierdo: apunta a nuestra página de consulta pública."""
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
        qr.add_data(texto)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
        return img
    except Exception as e:
        print(f"[QR] Error datos: {e}")
        return None

# ===================== PDF — COORDENADAS EXACTAS (plantilla 792x612 pts) =====================

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

            # Folio gigante
            pg.insert_text((405, 280), folio, fontsize=80, fontname=FB, color=(0, 0, 0))

            # VIGENCIA
            pg.insert_text((52, 205), datos["fecha_exp"], fontsize=S, fontname=F, color=(0, 0, 0))
            pg.insert_text((52, 239), datos["fecha_ven"], fontsize=S, fontname=F, color=(0, 0, 0))

            # PROPIETARIO
            pg.insert_text((92, 298), nombre, fontsize=S, fontname=F, color=(0, 0, 0))

            # VEHICULO
            pg.insert_text((53, 369), serie, fontsize=8, fontname=F, color=(0, 0, 0))
            pg.insert_text((53, 403), serie, fontsize=S, fontname=F, color=(0, 0, 0))
            pg.insert_text((185, 403), modelo, fontsize=S, fontname=F, color=(0, 0, 0))
            pg.insert_text((238, 403), color, fontsize=S, fontname=F, color=(0, 0, 0))
            pg.insert_text((53, 437), motor, fontsize=S, fontname=F, color=(0, 0, 0))
            pg.insert_text((168, 437), marca, fontsize=8, fontname=F, color=(0, 0, 0))
            pg.insert_text((168, 449), linea, fontsize=8, fontname=F, color=(0, 0, 0))
            pg.insert_text((264, 437), cve, fontsize=7, fontname=F, color=(0, 0, 0))

            # QR izquierdo
            img_url = _generar_qr_url(folio)
            if img_url:
                buf = BytesIO()
                img_url.save(buf, format="PNG")
                buf.seek(0)
                pg.insert_image(fitz.Rect(76, 478, 150, 552), pixmap=fitz.Pixmap(buf.read()), overlay=True)

            # QR derecho
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
        expira   = datetime.now() + timedelta(hours=36)
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
            "timer_activo": True,
            "timer_expira_en": expira.isoformat(),
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
        # marcar_db=False porque el INSERT de arriba ya dejó timer_activo/expira
        await iniciar_timer_36h(user_id, folio, nombre, marcar_db=False)
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

# ===================== BOT FSM — 8 pasos =====================

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
    # Lee los folios activos desde la BD (no de la RAM) para que coincida con el panel web
    activos_db = await asyncio.to_thread(_folios_activos_db, message.from_user.id)
    if activos_db:
        texto = "📋 FOLIOS ACTIVOS\n" + "─" * 28 + "\n\n"
        botones = []
        ahora = datetime.now()
        for row in activos_db:
            f = row["folio"]
            exp = row.get("timer_expira_en")
            try:
                expira = datetime.fromisoformat(str(exp).replace("Z", "+00:00")).replace(tzinfo=None)
                seg = max(0, int((expira - ahora).total_seconds()))
                h, m = divmod(seg // 60, 60)
                texto += f"Folio: {f}\n{row.get('nombre','') or ''}\n{h}h {m}min restantes\n\n"
            except Exception:
                texto += f"Folio: {f}\n{row.get('nombre','') or ''}\n(sin fecha de expiración)\n\n"
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
    r = await detener_timer_global(folio, "admin_telegram_SERO", estado_pago="VALIDADO")
    if not r["ok"]:
        await message.answer(f"⚠️ Folio {folio} no encontrado en la base.\n\n📋 Use /tlaxcala para otro permiso.")
        return
    await message.answer(
        f"✅ Validación admin\nFolio: {folio}\n⏹️ Timer detenido (sincronizado con el panel web)"
        f"\n\n📋 Use /tlaxcala para otro permiso.")

@dp.message(lambda m: m.content_type == ContentType.PHOTO)
async def recibir_comprobante(message: types.Message):
    uid = message.from_user.id
    activos_db = await asyncio.to_thread(_folios_activos_db, uid)
    folios = [r["folio"] for r in activos_db]
    if not folios:
        await message.answer("ℹ️ No tienes folios pendientes.\n\n📋 Use /tlaxcala para generar un permiso.")
        return
    if len(folios) > 1:
        lista = "\n".join(f"• {f}" for f in folios)
        pending_comprobantes[uid] = "waiting_folio"
        await message.answer(f"📄 Varios folios activos:\n\n{lista}\n\nResponde con el NÚMERO DE FOLIO.\n\n📋 Use /tlaxcala para otro permiso.")
        return
    folio = folios[0]
    await detener_timer_global(folio, "comprobante_telegram", estado="COMPROBANTE_ENVIADO")
    with suppress(Exception):
        await asyncio.to_thread(lambda: supabase.table("folios_registrados").update({
            "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", folio).execute())
    await message.answer(f"✅ Comprobante recibido\nFolio: {folio}\n⏹️ Timer detenido.\n\n📋 Use /tlaxcala para otro permiso.")

@dp.message(lambda m: m.from_user.id in pending_comprobantes and pending_comprobantes[m.from_user.id] == "waiting_folio")
async def especificar_folio_comprobante(message: types.Message):
    uid = message.from_user.id
    fe = message.text.strip().upper()
    activos_db = await asyncio.to_thread(_folios_activos_db, uid)
    fl = [r["folio"] for r in activos_db]
    if fe not in fl:
        await message.answer("❌ Folio no en tu lista.\n\n📋 Use /tlaxcala para otro permiso.")
        return
    await detener_timer_global(fe, "comprobante_telegram", estado="COMPROBANTE_ENVIADO")
    with suppress(Exception):
        await asyncio.to_thread(lambda: supabase.table("folios_registrados").update({
            "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", fe).execute())
    pending_comprobantes.pop(uid, None)
    await message.answer(f"✅ Comprobante asociado.\nFolio: {fe}\n⏹️ Timer detenido.\n\n📋 Use /tlaxcala para otro permiso.")

@dp.callback_query(lambda c: c.data and c.data.startswith("validar_"))
async def callback_validar(callback: CallbackQuery):
    folio = callback.data.replace("validar_", "")
    r = await detener_timer_global(folio, "callback_validar_telegram", estado_pago="VALIDADO")
    if not r["ok"]:
        await callback.answer("❌ Folio no encontrado en la base", show_alert=True)
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
    r = await detener_timer_global(folio, "callback_detener_telegram", estado="TIMER_DETENIDO")
    if not r["ok"]:
        await callback.answer("❌ Folio no encontrado en la base", show_alert=True)
        return
    await callback.answer("⏹️ Timer detenido", show_alert=True)
    with suppress(Exception):
        await callback.message.edit_reply_markup(reply_markup=None)
    await callback.message.answer(
        f"⏹️ TIMER DETENIDO\nFolio: {r['folio']}\nTitular: {r.get('nombre','')}\n"
        f"(sincronizado con el panel web)\n\n📋 Use /tlaxcala para otro permiso.")

@dp.message(Command("folios"))
async def ver_folios_activos(message: types.Message):
    uid = message.from_user.id
    activos_db = await asyncio.to_thread(_folios_activos_db, uid)
    if not activos_db:
        await message.answer("ℹ️ No hay folios activos.\n\n📋 Use /tlaxcala para generar uno.")
        return
    lista, botones = [], []
    ahora = datetime.now()
    for row in activos_db:
        f = row["folio"]
        exp = row.get("timer_expira_en")
        try:
            expira = datetime.fromisoformat(str(exp).replace("Z", "+00:00")).replace(tzinfo=None)
            seg = max(0, int((expira - ahora).total_seconds()))
            h, m = divmod(seg // 60, 60)
            lista.append(f"• {f} — {row.get('nombre','') or ''}\n  {h}h {m}min restantes")
        except Exception:
            lista.append(f"• {f} — {row.get('nombre','') or ''}")
        botones.append([InlineKeyboardButton(text=f"⏹️ Detener {f}", callback_data=f"detener_{f}")])
    await message.answer(f"📋 FOLIOS ACTIVOS ({len(activos_db)})\n\n" + "\n\n".join(lista) + "\n\n📋 Use /tlaxcala para otro permiso.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=botones))

@dp.message()
async def fallback(message: types.Message):
    await message.answer("🏛️ Gobierno del Estado de Tlaxcala — SMyT.\n\n📋 Use /tlaxcala para generar un permiso.")

# ===================== HTML / CSS (tema clonado del portal oficial) =====================

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
.bp-t{{background:#e67e22;}}.bp-td{{background:#7f8c8d;}}
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
.barra-c{{width:100%;height:24px;background:rgba(75,46,131,.12);border-radius:12px;overflow:hidden;margin:8px 0;}}
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

/* ---------- TEMA CLONADO DEL PORTAL OFICIAL tlaxcaladigital.gob.mx ---------- */
.contenedor-menu-principal{{background:#fff;border:1px solid {C3};border-radius:16px;box-shadow:0 8px 24px rgba(0,0,0,.06);padding:30px;}}
.card-tramite{{background:#fff;border:1px solid {C3};border-radius:12px;transition:transform .5s ease,box-shadow .5s ease;height:100%;}}
.card-tramite:hover{{transform:translateY(-10px);box-shadow:0 8px 16px {C3};}}
.card-tramite .icon-card{{background:#ebebeb;border-radius:50%;padding:4px;}}
.tittle-sub-menu{{color:{BLUE} !important;font-weight:600;}}
.tittle-text{{color:{BLUE};font-weight:600;}}
.arrow-icon-section{{color:{ACCENT};}}
.social-bar-clon{{position:fixed;top:50%;right:1rem;transform:translateY(-50%);display:flex;flex-direction:column;gap:1rem;z-index:1000;}}
.social-bar-clon a{{color:#fff;width:40px;height:40px;border-radius:6px;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 5px rgba(0,0,0,.15);transition:transform .3s ease,box-shadow .3s ease;text-decoration:none;}}
.social-bar-clon a:hover{{transform:translateY(-4px) scale(1.1);box-shadow:0 6px 12px rgba(0,0,0,.3);}}
.social-bar-clon a.facebook{{background:#3b5998;}}.social-bar-clon a.twitter{{background:#000;}}.social-bar-clon a.instagram{{background:#e4405f;}}
.back-to-top-clon{{background:linear-gradient(145deg,#2b5ebb,#1e4fa9);color:#fff;border:none;border-radius:50%;width:42px;height:42px;font-size:18px;display:flex;align-items:center;justify-content:center;box-shadow:0 4px 8px rgba(43,94,187,.3);animation:pulseClon 2s infinite;cursor:pointer;}}
.back-to-top-clon:hover{{transform:translateY(-4px) scale(1.1);background:linear-gradient(145deg,#1e4fa9,#163f8a);}}
@keyframes pulseClon{{0%{{box-shadow:0 0 0 0 rgba(43,94,187,.4);}}70%{{box-shadow:0 0 0 10px rgba(43,94,187,0);}}100%{{box-shadow:0 0 0 0 rgba(43,94,187,0);}}}}
.footer-clon{{background:#f7f9f9;color:#000;text-align:center;padding:14px;position:relative;border-top:1px solid #dee2e6;}}
.anim-scroll{{opacity:0;transition:opacity .8s ease-out,transform .8s ease-out;will-change:transform;}}
.anim-scroll.IzqDer{{transform:translateX(-50px);}}
.anim-scroll.DerIzq{{transform:translateX(50px);}}
.anim-scroll.AbajoArriba{{transform:translateY(50px);}}
.anim-scroll.ArribaAbajo{{transform:translateY(-50px);}}
.anim-scroll.is-visible{{opacity:1;transform:translate(0);}}

/* ---------- HEADER DE TRÁMITE (clon exacto de la ficha oficial) ---------- */
.tramite-header{{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:16px;margin-bottom:20px;}}
.tramite-header-text{{flex:1;min-width:220px;}}
.tramite-header-text h4{{font-size:19px;font-weight:700;color:#1d1d1b;margin:0 0 10px;display:flex;align-items:center;gap:8px;}}
.tramite-header-text hr{{border:none;border-top:1px solid #ddd;width:50%;margin:0 0 8px;}}
.tramite-header-text h6{{font-size:13px;color:#555;margin:0;font-weight:400;}}
.tramite-header-logos{{display:flex;align-items:center;gap:10px;flex-shrink:0;}}
.tramite-header-logos img{{max-height:60px;object-fit:contain;}}
@media (max-width:767px){{
  .tramite-header{{flex-direction:column;text-align:center;}}
  .tramite-header-text hr{{margin:0 auto 8px;}}
  .tramite-header-logos{{justify-content:center;}}
}}
"""

FA     = '<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.7.2/css/all.min.css">'
BI     = '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">'
ROBOTO = '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin><link href="https://fonts.googleapis.com/css2?family=Roboto:wght@300;400;500;700&display=swap" rel="stylesheet">'

JS_NAV = """<script>
function openNav(){document.getElementById('offsb').classList.add('open');document.getElementById('overlay').classList.add('show');}
function closeNav(){document.getElementById('offsb').classList.remove('open');document.getElementById('overlay').classList.remove('show');}
document.addEventListener('DOMContentLoaded',function(){
  document.getElementById('overlay').addEventListener('click',closeNav);
  const targets = document.querySelectorAll('.anim-scroll');
  const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => { if (entry.isIntersecting) entry.target.classList.add('is-visible'); });
  }, { threshold: 0.15 });
  targets.forEach(el => observer.observe(el));
});
</script>"""

LOGO_URL   = "/static/logo_brand.png"
ESCUDO_URL = "/static/logo_brand.png"
LOGO_SECRETARIA_URL = "/static/tlaxcala-financiera.png"

def header_tramite(titulo_html: str, subtitulo: str = "OFICINA VIRTUAL DE TRÁMITES Y SERVICIOS", icono: str = "fa-solid fa-car") -> str:
    return f"""<div class="tramite-header">
      <div class="tramite-header-text">
        <h4><i class="{icono} arrow-icon-section"></i> {titulo_html}</h4>
        <hr>
        <h6>{subtitulo}</h6>
      </div>
      <div class="tramite-header-logos">
        <img src="{LOGO_URL}" alt="Tlaxcala — Secretaría de Finanzas">
      </div>
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
    </div>
    <div class="social-bar-clon">
      <a href="#" class="facebook" aria-label="Facebook"><i class="bi bi-facebook"></i></a>
      <a href="#" class="twitter" aria-label="Twitter"><i class="bi bi-twitter-x"></i></a>
      <a href="#" class="instagram" aria-label="Instagram"><i class="bi bi-instagram"></i></a>
    </div>"""

def admin_bar(seccion):
    return f'<div class="admin-bar"><i class="fa-solid fa-shield-halved"></i> {seccion}</div>'

def footer_clon():
    return """<footer class="footer-clon">
      <p style="margin:0;font-size:12px;">Secretaría de Movilidad y Transporte — Gobierno del Estado de Tlaxcala © 2026</p>
      <button class="back-to-top-clon" style="position:absolute;bottom:14px;left:14px;" onclick="window.scrollTo({top:0,behavior:'smooth'})">
        <i class="bi bi-arrow-up"></i>
      </button>
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
        print(f"[HEARTBEAT] Tlaxcala activo — timers en memoria: {len(timers_activos)}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _keep_task
    await asyncio.to_thread(_sb_inicializar_folio)
    # Revive los timers que quedaron vivos en la BD antes del reinicio
    await rehidratar_timers()
    await bot.delete_webhook(drop_pending_updates=True)
    webhook_url = f"{BASE_URL}/webhook"
    await bot.set_webhook(webhook_url, allowed_updates=["message", "callback_query"])
    _keep_task = asyncio.create_task(keep_alive())
    print(f"[SISTEMA] Tlaxcala v1.1 listo — siguiente folio: {FOLIO_PREFIJO}{_folio_counter['siguiente']}")
    yield
    if _keep_task:
        _keep_task.cancel()
        with suppress(asyncio.CancelledError):
            await _keep_task
    await bot.session.close()

app = FastAPI(lifespan=lifespan, title="Tránsito Tlaxcala", version="1.1")
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
    timers_db = 0
    try:
        r = supabase.table("folios_registrados").select("folio").eq("estado_pago", "PENDIENTE_PAGO").eq("entidad", ENTIDAD).execute()
        pendientes = len(r.data or [])
    except Exception:
        pass
    try:
        r2 = supabase.table("folios_registrados").select("folio").eq("timer_activo", True).eq("entidad", ENTIDAD).execute()
        timers_db = len(r2.data or [])
    except Exception:
        pass
    color_pend = "#dc3545" if pendientes else "#1a6e2e"
    contenido = f"""
    {header_tramite("PANEL DE ADMINISTRACIÓN", icono="fa-solid fa-house")}
    <div class="row-2 mb-3">
      <div class="stat-card"><div class="stat-num">{timers_db}</div><div class="stat-lbl">Timers Activos (BD)</div></div>
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

# ===================== TIMERS (panel web, sincronizado con Telegram) =====================

@app.get("/panel/timers", response_class=HTMLResponse)
async def panel_timers(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    msg = request.query_params.get("msg", "")
    try:
        r = supabase.table("folios_registrados") \
            .select("folio,nombre,marca,linea,anio,estado,estado_pago,timer_expira_en,user_id") \
            .eq("entidad", ENTIDAD).eq("timer_activo", True) \
            .order("timer_expira_en", desc=False).execute()
        filas_db = r.data or []
    except Exception as e:
        filas_db = []
        print(f"[TIMERS] Error: {e}")

    ahora = datetime.now()
    filas = ""
    for row in filas_db:
        f = row["folio"]
        exp = row.get("timer_expira_en")
        try:
            expira = datetime.fromisoformat(str(exp).replace("Z", "+00:00")).replace(tzinfo=None)
            seg = max(0, int((expira - ahora).total_seconds()))
            h, m = divmod(seg // 60, 60)
            restante = f"{h}h {m}min"
            urgente = seg < 3600
        except Exception:
            restante = "—"
            urgente = False
        color = "#dc3545" if urgente else C1
        en_mem = "🟢" if f in timers_activos else "⚪"
        filas += f"""<tr>
          <td><strong style="color:{C1}">{f}</strong><br><small style="color:#999">{en_mem} {row.get('nombre','') or ''}</small></td>
          <td>{row.get('marca','')} {row.get('linea','')}<br><small>{row.get('anio','')}</small></td>
          <td><strong style="color:{color}">{restante}</strong></td>
          <td>
            <form method="POST" action="/panel/timer/detener/{f}" style="display:inline">
              <button class="btn btn-warn btn-sm" onclick="return confirm('¿Detener el timer de {f}? El folio NO se borrará.')">⏹️ Detener</button>
            </form>
            <form method="POST" action="/panel/validar/{f}" style="display:inline">
              <button class="btn btn-success btn-sm" onclick="return confirm('¿Validar pago de {f}?')">✅ Validar</button>
            </form>
          </td>
        </tr>"""

    contenido = f"""
    {header_tramite("TIMERS ACTIVOS", icono="fa-solid fa-stopwatch")}
    {"<div class='alert alert-ok'>"+msg+"</div>" if msg else ""}
    <div class="info-box">
      <strong>🔄 Sincronización total:</strong> detener un timer aquí o desde el bot de Telegram
      produce exactamente el mismo efecto — ambos escriben en Supabase, que es la única fuente
      de verdad. Un folio con el timer detenido <strong>nunca</strong> se borra automáticamente.<br>
      🟢 = tarea viva en este proceso · ⚪ = sólo en la BD (se revive al reiniciar)
    </div>
    <div class="tabla-wrap"><table>
      <thead><tr><th>Folio / Titular</th><th>Vehículo</th><th>Restante</th><th>Acciones</th></tr></thead>
      <tbody>{filas or '<tr><td colspan="4" style="text-align:center;color:#999;padding:20px">Sin timers activos</td></tr>'}</tbody>
    </table></div>
    <div class="mt-3"><a href="/panel/admin" class="btn btn-outline btn-sm">← Panel</a></div>"""
    return HTMLResponse(page("Timers", "Timers Activos — Tlaxcala", contenido))


@app.post("/panel/timer/detener/{folio}")
async def panel_detener_timer(request: Request, folio: str):
    """Detiene el timer desde la web. Mismo efecto que el botón de Telegram."""
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    r = await detener_timer_global(folio, "panel_web_detener", estado="TIMER_DETENIDO")
    if not r["ok"]:
        return RedirectResponse(url=f"/panel/timers?msg={quote(f'⚠️ Folio {folio.upper()} no encontrado')}", status_code=303)
    if r.get("user_id"):
        with suppress(Exception):
            await bot.send_message(r["user_id"],
                f"⏹️ TIMER DETENIDO — TLAXCALA\nFolio: {r['folio']}\nTitular: {r.get('nombre','')}\n"
                f"Un administrador detuvo el timer. Tu folio NO será eliminado.\n\n📋 Use /tlaxcala para otro permiso.")
    aviso = "Timer de " + r["folio"] + " detenido ⏹️ (sincronizado con Telegram)"
    return RedirectResponse(url=f"/panel/timers?msg={quote(aviso)}", status_code=303)


@app.post("/panel/timer/reiniciar/{folio}")
async def panel_reiniciar_timer(request: Request, folio: str):
    """Vuelve a arrancar un timer de 36h para un folio."""
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    folio = folio.strip().upper()
    row = await asyncio.to_thread(_leer_estado_folio, folio)
    if row in (None, "ERROR"):
        return RedirectResponse(url=f"/panel/timers?msg={quote(f'⚠️ Folio {folio} no encontrado')}", status_code=303)
    await iniciar_timer_36h(row.get("user_id") or 0, folio, row.get("nombre", "") or "")
    return RedirectResponse(url=f"/panel/timers?msg={quote(f'Timer de {folio} reiniciado (36h)')}", status_code=303)

# ===================== FOLIOS =====================

@app.get("/panel/folios", response_class=HTMLResponse)
async def admin_folios(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    filtro  = request.query_params.get("filtro", "").strip()
    crit    = request.query_params.get("criterio", "folio")
    ep_fil  = request.query_params.get("estado_pago", "todos")
    ev_fil  = request.query_params.get("estado_vigencia", "todos")
    msg     = request.query_params.get("msg", "")
    pdf_url = request.query_params.get("pdf", "")
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
        q = supabase.table("folios_registrados").select("*").eq("entidad", ENTIDAD)
        if filtro:
            q = q.ilike(crit, f"%{filtro}%")
        if ep_fil != "todos":
            q = q.eq("estado_pago", ep_fil)
        folios = q.order("fecha_expedicion", desc=True).execute().data or []
        tz = ZoneInfo(TZ)
        hoy = datetime.now(tz).date()
        for f in folios:
            try:
                fv = datetime.fromisoformat(f["fecha_vencimiento"]).date()
                f["estado_calc"] = "VIGENTE" if hoy <= fv else "VENCIDO"
            except:
                f["estado_calc"] = "ERROR"
        if ev_fil != "todos":
            folios = [f for f in folios if f.get("estado_calc", "") == ev_fil]
    except Exception as e:
        folios = []
        print(f"[FOLIOS] Error: {e}")
    msg_html = f'<div class="alert alert-ok">{msg}</div>' if msg else ""
    filas = ""
    for f in folios:
        folio_id = f.get("folio", "")
        pago = f.get("estado_pago", "VALIDADO") or "VALIDADO"
        ec   = f.get("estado_calc", "")
        bp   = '<span class="bp bp-p">PEND</span>' if pago == "PENDIENTE_PAGO" else '<span class="bp bp-v">OK</span>'
        be   = '<span class="bp bp-vig">VIG</span>' if ec == "VIGENTE" else '<span class="bp bp-ven">VEN</span>'
        # Badge del timer leído desde la BD
        if f.get("timer_activo"):
            bt = '<span class="bp bp-t">⏱ ON</span>'
            btn_timer = f'<form method="POST" action="/panel/timer/detener/{folio_id}" style="display:inline"><button class="btn btn-warn btn-sm" onclick="return confirm(\'¿Detener timer? El folio NO se borra.\')">⏹️</button></form> '
        else:
            bt = '<span class="bp bp-td">⏱ OFF</span>'
            btn_timer = ""
        bval = f'<form method="POST" action="/panel/validar/{folio_id}" style="display:inline"><button class="btn btn-success btn-sm" onclick="return confirm(\'¿Validar?\')">✅</button></form> ' if pago == "PENDIENTE_PAGO" else ""
        pdf  = f.get("pdf_url", "")
        bpdf = f'<a href="{pdf}" target="_blank" class="btn btn-sm" style="background:{C1};color:white">📄</a> ' if pdf else ""
        filas += f"""<tr>
          <td><strong style="color:{C1}">{folio_id}</strong><br><small style="color:#999">{f.get("creado_por","")}</small></td>
          <td>{(f.get("nombre","") or "")[:18]}</td>
          <td>{f.get("marca","")} {f.get("linea","")}<br><small>{f.get("anio","")}</small></td>
          <td>{str(f.get("fecha_expedicion",""))[:10]}<br>{str(f.get("fecha_vencimiento",""))[:10]}</td>
          <td>{be} {bp}<br>{bt}</td>
          <td>{btn_timer}{bval}{bpdf}<a href="/consulta/{folio_id}" target="_blank" class="btn btn-sm btn-outline">🔗</a></td>
        </tr>"""
    filtros = f"""<div class="filter-bar">
      <form method="GET" style="display:contents">
        <input type="text" name="filtro" class="form-control" value="{filtro}" placeholder="Buscar...">
        <select name="criterio" class="form-control" style="max-width:100px">
          <option value="folio" {"selected" if crit == "folio" else ""}>Folio</option>
          <option value="nombre" {"selected" if crit == "nombre" else ""}>Nombre</option>
          <option value="numero_serie" {"selected" if crit == "numero_serie" else ""}>Serie</option>
        </select>
        <select name="estado_pago" class="form-control" style="max-width:100px">
          <option value="todos" {"selected" if ep_fil == "todos" else ""}>Todos</option>
          <option value="PENDIENTE_PAGO" {"selected" if ep_fil == "PENDIENTE_PAGO" else ""}>Pendiente</option>
          <option value="VALIDADO" {"selected" if ep_fil == "VALIDADO" else ""}>Validado</option>
        </select>
        <select name="estado_vigencia" class="form-control" style="max-width:100px">
          <option value="todos" {"selected" if ev_fil == "todos" else ""}>Todos</option>
          <option value="VIGENTE" {"selected" if ev_fil == "VIGENTE" else ""}>Vigente</option>
          <option value="VENCIDO" {"selected" if ev_fil == "VENCIDO" else ""}>Vencido</option>
        </select>
        <button type="submit" class="btn btn-primary btn-sm" style="width:auto">Filtrar</button>
        <a href="/panel/folios" class="btn btn-outline btn-sm">✕</a>
      </form>
      <span style="font-size:12px;color:#888">{len(folios)} resultados</span>
    </div>"""
    contenido = f"""{modal_html}
    {header_tramite("FOLIOS REGISTRADOS", icono="fa-solid fa-list-check")}
    {msg_html}{filtros}
    <div class="tabla-wrap"><table>
      <thead><tr><th>Folio</th><th>Titular</th><th>Vehículo</th><th>Fechas</th><th>Estado</th><th>Acc.</th></tr></thead>
      <tbody>{filas or '<tr><td colspan="6" style="text-align:center;color:#999;padding:20px">Sin folios</td></tr>'}</tbody>
    </table></div>"""
    return HTMLResponse(page("Folios", "Folios Registrados — Tlaxcala", contenido))

@app.post("/panel/validar/{folio}")
async def validar_pago(request: Request, folio: str):
    """Valida el pago desde la web. Detiene el timer igual que el bot de Telegram."""
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    r = await detener_timer_global(folio, "panel_web_validar", estado_pago="VALIDADO")
    if not r["ok"]:
        return RedirectResponse(url=f"/panel/folios?msg={quote(f'⚠️ Folio {folio.upper()} no encontrado')}", status_code=303)
    with suppress(Exception):
        await asyncio.to_thread(lambda: supabase.table("folios_registrados").update({
            "fecha_comprobante": datetime.now().isoformat()
        }).eq("folio", r["folio"]).execute())
    if r.get("user_id"):
        with suppress(Exception):
            await bot.send_message(r["user_id"],
                f"✅ PAGO VALIDADO — TLAXCALA\nFolio: {r['folio']}\nTitular: {r.get('nombre','')}\n"
                f"Tu permiso está activo.\n\n📋 Use /tlaxcala para otro permiso.")
    return RedirectResponse(url=f"/panel/folios?msg={quote('Folio ' + r['folio'] + ' validado ✅ (timer detenido en Telegram también)')}", status_code=303)

@app.get("/panel/pdf/{folio}")
async def descargar_pdf_panel(folio: str, request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    folio = folio.strip().upper()
    try:
        res = supabase.table("folios_registrados").select("pdf_url").eq("folio", folio).execute()
        if res.data and res.data[0].get("pdf_url"):
            return RedirectResponse(url=res.data[0]["pdf_url"])
    except Exception:
        pass
    ruta = os.path.join(OUTPUT_DIR, f"{folio}.pdf")
    if os.path.exists(ruta):
        from fastapi.responses import FileResponse
        return FileResponse(ruta, media_type="application/pdf", filename=f"{folio}_tlaxcala.pdf")
    return HTMLResponse("<p>PDF no encontrado.</p><a href='/panel/folios'>← Volver</a>", status_code=404)

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
    return HTMLResponse(page("Registrar Permiso", "Registrar Permiso — Tlaxcala", contenido))

@app.post("/panel/registro_admin")
async def registro_admin_post(request: Request,
    folio: str = Form(None), marca: str = Form(...), linea: str = Form(...),
    anio: str = Form(...), color: str = Form(""), numero_serie: str = Form(...),
    numero_motor: str = Form(""), cve_vehicular: str = Form(""),
    nombre: str = Form(...), fecha_expedicion: str = Form(None), fecha_vencimiento: str = Form(None)):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
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
        # Permiso de admin: nace VALIDADO y SIN timer
        supabase.table("folios_registrados").insert({"folio": fg, "marca": marca.upper(), "linea": linea.upper(),
            "anio": anio, "numero_serie": numero_serie.upper(), "numero_motor": numero_motor.upper(),
            "color": color.upper(), "nombre": nombre.upper(), "cve_vehicular": cve_vehicular.upper(),
            "fecha_expedicion": fe.isoformat(), "fecha_vencimiento": fv.isoformat(), "entidad": ENTIDAD,
            "estado": "ACTIVO", "estado_pago": "VALIDADO", "timer_activo": False,
            "creado_por": request.session.get("username", "admin")}).execute()
        pdf_url = await asyncio.to_thread(generar_subir_y_guardar_pdf, datos_pdf)
        return RedirectResponse(url=f"/panel/folios?msg={quote(f'Permiso {fg} generado ✅')}&pdf={quote(pdf_url)}", status_code=303)
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
        supabase.table("verificacion_tlaxcala").insert({"username": username, "password": password, "folios_asignac": folios, "folios_usados": 0}).execute()
        return RedirectResponse(url=f"/panel/crear_usuario?msg={quote(f'Usuario {username} creado con {folios} folios ✅')}", status_code=303)
    except Exception as e:
        return RedirectResponse(url=f"/panel/crear_usuario?error={quote(str(e))}", status_code=303)

# ===================== REGISTRO USUARIO 3RO =====================

@app.get("/registro_usuario", response_class=HTMLResponse)
async def registro_usuario_get(request: Request):
    if not request.session.get("username") or request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    ud = supabase.table("verificacion_tlaxcala").select("*").eq("username", request.session["username"]).limit(1).execute()
    if not ud.data:
        return RedirectResponse(url="/panel/login", status_code=303)
    u = ud.data[0]
    asig = int(u.get("folios_asignac", 0))
    usad = int(u.get("folios_usados", 0))
    disp = asig - usad
    porc = round((usad / asig * 100) if asig else 0, 1)
    tz = ZoneInfo(TZ)
    hoy = datetime.now(tz).strftime("%Y-%m-%d")
    msg = request.query_params.get("msg", "")
    err = request.query_params.get("error", "")
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
    {header_tramite("PERMISO PROVISIONAL DE CIRCULACIÓN")}
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
    return HTMLResponse(page("Registrar Permiso", "Registro de Permisos", contenido, scripts))

@app.post("/registro_usuario")
async def registro_usuario_post(request: Request,
    marca: str = Form(...), linea: str = Form(...),
    anio: str = Form(...), color: str = Form(""),
    serie: str = Form(...), motor: str = Form(""), cve_vehicular: str = Form(""),
    nombre: str = Form(...), fecha_inicio: str = Form(None)):
    if not request.session.get("username") or request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    try:
        ud = supabase.table("verificacion_tlaxcala").select("*").eq("username", request.session["username"]).limit(1).execute()
        if not ud.data:
            return RedirectResponse(url="/panel/login", status_code=303)
        u = ud.data[0]
        asig = int(u.get("folios_asignac", 0))
        usad = int(u.get("folios_usados", 0))
        if asig - usad <= 0:
            return RedirectResponse(url=f"/registro_usuario?error={quote('Sin folios disponibles')}", status_code=303)
        tz = ZoneInfo(TZ)
        fe = datetime.strptime(fecha_inicio, "%Y-%m-%d").replace(tzinfo=tz) if fecha_inicio else datetime.now(tz)
        fv = fe + timedelta(days=30)
        fg = generar_folio()
        # Folio de usuario con cupo: nace VALIDADO y SIN timer
        supabase.table("folios_registrados").insert({"folio": fg, "marca": marca.upper(), "linea": linea.upper(),
            "anio": anio, "numero_serie": serie.upper(), "numero_motor": motor.upper(),
            "color": color.upper(), "nombre": nombre.upper(), "cve_vehicular": cve_vehicular.upper(),
            "fecha_expedicion": fe.date().isoformat(), "fecha_vencimiento": fv.date().isoformat(),
            "entidad": ENTIDAD, "estado": "ACTIVO", "estado_pago": "VALIDADO", "timer_activo": False,
            "user_id": request.session.get("user_id"), "creado_por": request.session["username"]}).execute()
        datos_pdf = {"folio": fg, "marca": marca.upper(), "linea": linea.upper(), "anio": anio,
                     "serie": serie.upper(), "motor": motor.upper(), "cve_vehicular": cve_vehicular.upper(),
                     "color": color.upper(), "nombre": nombre.upper(),
                     "fecha_exp": fe.strftime("%d/%m/%Y"), "fecha_ven": fv.strftime("%d/%m/%Y"), "fecha_exp_dt": fe}
        pdf_url = await asyncio.to_thread(generar_subir_y_guardar_pdf, datos_pdf)
        supabase.table("verificacion_tlaxcala").update({"folios_usados": usad + 1}).eq("username", request.session["username"]).execute()
        contenido = f"""
        {header_tramite("✅ PERMISO GENERADO")}
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
        return HTMLResponse(page("Permiso Generado", "Registro Exitoso", contenido))
    except Exception as e:
        print(f"[REG USUARIO] Error: {e}")
        return RedirectResponse(url=f"/registro_usuario?error={quote(str(e))}", status_code=303)

@app.get("/mis_permisos", response_class=HTMLResponse)
async def mis_permisos(request: Request):
    if not request.session.get("username") or request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    permisos = supabase.table("folios_registrados").select("*").eq("creado_por", request.session["username"]).order("fecha_expedicion", desc=True).execute().data or []
    tz = ZoneInfo(TZ)
    hoy = datetime.now(tz).date()
    for p in permisos:
        try:
            fv = datetime.fromisoformat(p["fecha_vencimiento"]).date()
            fe = datetime.fromisoformat(p["fecha_expedicion"]).date()
            p["fe_fmt"] = fe.strftime("%d/%m/%Y")
            p["estado_calc"] = "VIGENTE" if hoy <= fv else "VENCIDO"
        except:
            p["fe_fmt"] = p["estado_calc"] = "ERROR"
    ud = supabase.table("verificacion_tlaxcala").select("folios_asignac,folios_usados").eq("username", request.session["username"]).limit(1).execute().data
    ud = ud[0] if ud else {"folios_asignac": 0, "folios_usados": 0}
    asig = int(ud.get("folios_asignac", 0))
    usad = int(ud.get("folios_usados", 0))
    vig  = len([p for p in permisos if p.get("estado_calc") == "VIGENTE"])
    filas = ""
    for p in permisos:
        ec  = p.get("estado_calc", "")
        be  = '<span class="bp bp-vig">VIG</span>' if ec == "VIGENTE" else '<span class="bp bp-ven">VEN</span>'
        pdf = p.get("pdf_url", "")
        btn = f'<a href="{pdf}" target="_blank" class="btn btn-sm" style="background:{C1};color:white">📥</a> ' if pdf else ""
        filas += f"""<tr>
          <td><strong style="color:{C1}">{p.get("folio","")}</strong></td>
          <td>{p.get("marca","")} {p.get("linea","")}<br><small>{p.get("anio","")}</small></td>
          <td style="font-size:11px">{p.get("numero_serie","")}</td>
          <td>{p.get("fe_fmt","")}</td><td>{be}</td>
          <td>{btn}<a href="/consulta/{p.get('folio','')}" target="_blank" class="btn btn-sm btn-outline">🔗</a></td>
        </tr>"""
    contenido = f"""
    {header_tramite("MIS PERMISOS", icono="fa-solid fa-folder-open")}
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
    return HTMLResponse(page("Mis Permisos", "Mis Permisos — Tlaxcala", contenido))

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
            f = res.data[0]
            tz = ZoneInfo(TZ)
            hoy = datetime.now(tz).date()
            try:
                fv = datetime.fromisoformat(f["fecha_vencimiento"]).date()
                fe = datetime.fromisoformat(f["fecha_expedicion"]).date()
                vigente = hoy <= fv
            except:
                vigente = False
                fe = fv = None

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
                {_row("Marca", f.get("marca",""))}
                {_row("Línea", f.get("linea",""))}
                {_row("Modelo (Año)", f.get("anio",""))}
                {_row("Núm. Serie / NIV", f.get("numero_serie",""))}
                {_row("Núm. Motor", f.get("numero_motor",""))}
                {_row("Clave Vehicular", f.get("cve_vehicular",""))}
                {_row("Color", f.get("color",""))}
              </div>
            </div>
            <div style="background:white;border:1px solid {C3};border-radius:14px;padding:16px;box-shadow:0 4px 16px rgba(0,0,0,.06);margin-bottom:14px">
              <div style="color:{BLUE};font-weight:700;font-size:15px;padding-bottom:10px;margin-bottom:8px;border-bottom:1px solid #eee">
                <i class="fa-solid fa-file-shield" style="color:{ACCENT}"></i> Datos del Permiso
              </div>
              <div>
                {_row("Folio", f'<span style="color:{C1};font-weight:700">{folio}</span>')}
                {_row("Propietario", f.get("nombre",""))}
                {_row("Fecha de Expedición", fe.strftime("%d/%m/%Y") if fe else "—")}
                {_row("Fecha de Vencimiento", fv.strftime("%d/%m/%Y") if fv else "—")}
              </div>
            </div>"""

        html = f"""<!DOCTYPE html><html lang="es"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Consulta Folio {folio} — Tlaxcala</title>
<link rel="icon" href="{ESCUDO_URL}" sizes="32x32"/>
{ROBOTO}{FA}{BI}<style>{CSS}</style></head><body style="background:#f4f4f4;">
<div class="content" style="max-width:600px">
  {header_tramite("VERIFICACIÓN DE PERMISO", subtitulo="TLAXCALA SMYT", icono="fa-solid fa-shield-halved")}
  {badge}
  {datos_html}
  <a href="https://smyt.tlaxcala.gob.mx/" class="btn btn-primary mt-3">
    <i class="fa-solid fa-arrow-left"></i> Volver a SMyT Tlaxcala
  </a>
</div>
{footer_clon()}
</body></html>"""
        return HTMLResponse(html)
    except Exception as e:
        return HTMLResponse(f"<p>Error: {e}</p>", status_code=500)

# ===================== TABLAS BD =====================

@app.get("/panel/tablas", response_class=HTMLResponse)
async def admin_tablas(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    cards = "".join([f"""<div class="form-card mb-3">
      <strong style="color:{C1};font-size:15px">🗄️ {info['nombre']}</strong>
      <p style="font-size:12px;color:#888;margin:4px 0 12px"><code>{nombre}</code></p>
      <a href="/panel/tabla/{nombre}" class="btn btn-primary btn-sm" style="width:auto">Ver y editar →</a>
    </div>""" for nombre, info in TABLAS_DISPONIBLES.items()])
    contenido = f'{header_tramite("TABLAS BASE DE DATOS", icono="fa-solid fa-database")}{cards}'
    return HTMLResponse(page("Tablas BD", "Tablas BD — Tlaxcala", contenido))

@app.get("/panel/tabla/{nombre_tabla}", response_class=HTMLResponse)
async def admin_tabla_detalle(nombre_tabla: str, request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login", status_code=303)
    if nombre_tabla not in TABLAS_DISPONIBLES:
        return RedirectResponse(url="/panel/tablas", status_code=303)
    info = TABLAS_DISPONIBLES[nombre_tabla]
    pk_col = info["pk_col"]
    q = request.query_params.get("q", "").strip()
    page_n = max(1, int(request.query_params.get("page", "1") or 1))
    try:
        todos = supabase.table(nombre_tabla).select("*").limit(20000).execute().data or []
        filtrados = [r for r in todos if any(q.lower() in str(v).lower() for v in r.values() if v is not None)] if q else todos
        total = len(filtrados)
        offset = (page_n - 1) * PAGE_SIZE
        registros = filtrados[offset:offset + PAGE_SIZE]
    except:
        todos = filtrados = registros = []
        total = offset = 0
    columnas = list(registros[0].keys()) if registros else (list(todos[0].keys()) if todos else info["columnas"])
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    th = "".join(f"<th>{c}</th>" for c in columnas) + "<th></th>"

    def _fila(i, reg):
        celdas = f'<td style="color:#bbb;font-size:10px">{offset+i+1}</td>'
        for col in columnas:
            val = reg.get(col)
            disp = str(val) if val is not None else "null"
            cls = "cv nv" if val is None else "cv"
            celdas += f'<td><span class="{cls}" data-col="{col}" data-pk="{str(reg.get(pk_col,""))}" data-val="{str(val or "")}" onclick="editCell(this)">{disp[:25]}</span></td>'
        celdas += f'<td><button class="del-btn" onclick="delRow(this,\'{str(reg.get(pk_col,""))}\',\'row{i}\')">✕</button></td>'
        return f'<tr id="row{i}">{celdas}</tr>'

    tbody = "".join(_fila(i, registros[i]) for i in range(len(registros))) or "<tr><td colspan='20' style='text-align:center;padding:20px;color:#999'>Sin registros</td></tr>"
    pag = ""
    if total_pages > 1:
        pag = '<div style="display:flex;gap:8px;justify-content:center;padding:14px">'
        if page_n > 1:
            pag += f'<a href="?q={q}&page={page_n-1}" class="btn btn-outline btn-sm">← Ant</a>'
        pag += f'<span class="btn btn-sm" style="background:{C1};color:white">{page_n}/{total_pages}</span>'
        if page_n < total_pages:
            pag += f'<a href="?q={q}&page={page_n+1}" class="btn btn-outline btn-sm">Sig →</a>'
        pag += '</div>'
    contenido = f"""
    {header_tramite(info['nombre'].upper(), icono="fa-solid fa-table")}
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
    if not request.session.get("admin"):
        return {"ok": False, "error": "no autorizado"}
    d = await request.json()
    tabla = d.get("tabla")
    pk_col = d.get("pk_col")
    pk_val = d.get("pk_val")
    col = d.get("col")
    val = d.get("val", "")
    if tabla not in TABLAS_DISPONIBLES or not col or not pk_val:
        return {"ok": False, "error": "datos inválidos"}
    try:
        # Si tocan timer_activo a mano, sincronizar la tarea local
        if tabla == "folios_registrados" and col == "timer_activo":
            if str(val).lower() in ("false", "0", "no", ""):
                await detener_timer_global(str(pk_val), "edicion_manual_tabla")
                return {"ok": True}
        supabase.table(tabla).update({col: val or None}).eq(pk_col, pk_val).execute()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.post("/panel/api/delete_row")
async def api_delete_row(request: Request):
    if not request.session.get("admin"):
        return {"ok": False, "error": "no autorizado"}
    d = await request.json()
    tabla = d.get("tabla")
    pk_col = d.get("pk_col")
    pk_val = d.get("pk_val")
    if tabla not in TABLAS_DISPONIBLES or not pk_val:
        return {"ok": False, "error": "datos inválidos"}
    try:
        if tabla == "folios_registrados":
            cancelar_timer_folio(str(pk_val).strip().upper())
        supabase.table(tabla).delete().eq(pk_col, pk_val).execute()
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}

# ===================== API TIMERS (por si quieres consumirla desde fuera) =====================

@app.get("/api/timer/{folio}")
async def api_estado_timer(folio: str):
    """Estado real del timer de un folio, leído de la BD."""
    folio = folio.strip().upper()
    row = await asyncio.to_thread(_leer_estado_folio, folio)
    if row in (None, "ERROR"):
        return {"ok": False, "folio": folio, "error": "no encontrado"}
    return {
        "ok": True,
        "folio": folio,
        "timer_activo": bool(row.get("timer_activo")),
        "estado": row.get("estado"),
        "estado_pago": row.get("estado_pago"),
        "en_memoria": folio in timers_activos,
    }

# ===================== HEALTH =====================

@app.get("/health")
async def health():
    timers_db = 0
    try:
        r = supabase.table("folios_registrados").select("folio").eq("timer_activo", True).eq("entidad", ENTIDAD).execute()
        timers_db = len(r.data or [])
    except Exception:
        pass
    return {"status": "healthy", "version": "1.1", "entidad": ENTIDAD,
            "timers_memoria": len(timers_activos),
            "timers_bd": timers_db,
            "siguiente_folio": f"{FOLIO_PREFIJO}{_folio_counter['siguiente']}"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
