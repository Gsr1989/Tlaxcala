import asyncio
import os
from datetime import datetime, timedelta
from typing import Optional
from dotenv import load_dotenv

# FastAPI
from fastapi import FastAPI, Request, HTTPException, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials

# Supabase
import supabase
from supabase import create_client, Client

# PDF & QR
import fitz
import qrcode
from io import BytesIO
import base64

# Aiogram (Telegram)
from aiogram import Bot, Dispatcher, types
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Utils
import secrets
import logging
from decimal import Decimal

# Load environment
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
BOT_TOKEN = os.getenv("BOT_TOKEN")
BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")

ADMIN_USER = os.getenv("ADMIN_USER", "Serg890105tm3")
ADMIN_PASS = os.getenv("ADMIN_PASS", "Serg890105tm3")

ENTIDAD = "tlaxcala"
FOLIO_PREFIJO = "ZX"
FOLIO_INICIO = 53314
BUCKET_NAME = "permisos-tlaxcala"
PLANTILLA_PDF = "TLAXCALA2026(1).pdf"

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================================
# FASTAPI SETUP
# ============================================================================

app = FastAPI(title="GOB TLAX - Permisos Provisionales")

# Middleware
app.add_middleware(SessionMiddleware, secret_key=secrets.token_urlsafe(32))

# Static files
app.mount("/assets", StaticFiles(directory="assets"), name="assets")

# Templates
templates = Jinja2Templates(directory="templates")

# Supabase
supabase_client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Security
security = HTTPBasic()

# ============================================================================
# AIOGRAM BOT SETUP
# ============================================================================

bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

class VehicleForm(StatesGroup):
    marca = State()
    linea = State()
    anio = State()
    serie = State()
    motor = State()
    color = State()
    nombre = State()
    cve_vehicular = State()

# ============================================================================
# FUNCIONES AUXILIARES
# ============================================================================

def generar_folio() -> str:
    """Genera próximo folio disponible"""
    try:
        result = supabase_client.table("folio_watermark").select("*").eq("prefijo", FOLIO_PREFIJO).execute()
        if result.data:
            ultimo = result.data[0]["ultimo_asignado"]
            nuevo = ultimo + 1
        else:
            nuevo = FOLIO_INICIO
        
        supabase_client.table("folio_watermark").upsert({
            "prefijo": FOLIO_PREFIJO,
            "ultimo_asignado": nuevo
        }).execute()
        
        return f"{FOLIO_PREFIJO}{nuevo:05d}"
    except Exception as e:
        logger.error(f"Error generando folio: {e}")
        return f"{FOLIO_PREFIJO}{FOLIO_INICIO:05d}"

def generar_qr(data: str) -> str:
    """Genera QR y retorna base64"""
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    buffer.seek(0)
    
    return base64.b64encode(buffer.getvalue()).decode()

def generar_pdf(folio: str, datos: dict) -> bytes:
    """Genera PDF con datos del folio"""
    try:
        # Cargar plantilla
        pdf_doc = fitz.open(PLANTILLA_PDF)
        page = pdf_doc[0]
        
        # Coordenadas (pts 792x612)
        coords = {
            "folio": (460, 270),
            "fecha_exp": (52, 205),
            "fecha_ven": (52, 239),
            "nombre": (52, 298),
            "serie": (53, 369),
            "serie2": (53, 403),
            "modelo": (137, 403),
            "color": (188, 403),
            "motor": (53, 437),
            "marca": (138, 437),
            "linea": (138, 449),
            "cve": (204, 437),
        }
        
        # Insertar texto
        page.insert_text(coords["folio"], folio, fontsize=35, color=(0, 0, 0))
        page.insert_text(coords["fecha_exp"], datos.get("fecha_expedicion", ""), fontsize=9)
        page.insert_text(coords["fecha_ven"], datos.get("fecha_vencimiento", ""), fontsize=9)
        page.insert_text(coords["nombre"], datos.get("nombre", "").upper(), fontsize=9)
        page.insert_text(coords["serie"], datos.get("numero_serie", "").upper(), fontsize=9)
        page.insert_text(coords["serie2"], datos.get("numero_serie", "").upper(), fontsize=9)
        page.insert_text(coords["modelo"], str(datos.get("anio", "")).upper(), fontsize=9)
        page.insert_text(coords["color"], datos.get("color", "").upper(), fontsize=9)
        page.insert_text(coords["motor"], datos.get("numero_motor", "").upper(), fontsize=9)
        page.insert_text(coords["marca"], datos.get("marca", "").upper(), fontsize=9)
        page.insert_text(coords["linea"], datos.get("linea", "").upper(), fontsize=9)
        page.insert_text(coords["cve"], datos.get("cve_vehicular", "").upper(), fontsize=9)
        
        # QR codes
        qr_url = f"{BASE_URL}/consulta/{folio}"
        qr_data = generar_qr(qr_url)
        qr_data_texto = generar_qr(str(datos))
        
        # Guardar PDF en Supabase
        pdf_bytes = pdf_doc.write()
        pdf_doc.close()
        
        return pdf_bytes
    except Exception as e:
        logger.error(f"Error generando PDF: {e}")
        return b""

def guardar_folio(folio: str, datos: dict, usuario_id: str = None) -> bool:
    """Guarda folio en Supabase"""
    try:
        fecha_exp = datetime.now().strftime("%d/%m/%Y")
        fecha_ven = (datetime.now() + timedelta(days=30)).strftime("%d/%m/%Y")
        
        folio_data = {
            "folio": folio,
            "marca": datos.get("marca"),
            "linea": datos.get("linea"),
            "anio": datos.get("anio"),
            "numero_serie": datos.get("serie"),
            "numero_motor": datos.get("motor"),
            "color": datos.get("color"),
            "nombre": datos.get("nombre"),
            "cve_vehicular": datos.get("cve_vehicular"),
            "fecha_expedicion": fecha_exp,
            "fecha_vencimiento": fecha_ven,
            "entidad": ENTIDAD,
            "estado": "vigente",
            "estado_pago": "pendiente",
            "creado_por": usuario_id or "bot",
            "user_id": usuario_id,
        }
        
        supabase_client.table("folios_registrados").insert(folio_data).execute()
        return True
    except Exception as e:
        logger.error(f"Error guardando folio: {e}")
        return False

# ============================================================================
# RUTAS PÚBLICAS
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Root redirect"""
    return RedirectResponse(url="/portal")

@app.get("/portal", response_class=HTMLResponse)
async def portal(request: Request):
    """Portal público"""
    return HTMLResponse("""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>GOB TLAX - Permisos Provisionales</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; }}
            .hero {{ padding: 60px 20px; text-align: center; color: white; }}
            .card {{ border: none; border-radius: 12px; box-shadow: 0 8px 24px rgba(0,0,0,0.1); }}
        </style>
    </head>
    <body>
        <div class="hero">
            <h1 class="mb-4">Permiso Provisional de Circulación</h1>
            <p class="lead mb-4">Secretaría de Movilidad y Transporte - Tlaxcala</p>
            <a href="/consulta_folio" class="btn btn-light btn-lg">Consultar Folio</a>
        </div>
    </body>
    </html>
    """)

@app.get("/consulta_folio", response_class=HTMLResponse)
async def consulta_folio_page(request: Request):
    """Página de consulta de folio"""
    return HTMLResponse("""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Consulta de Folio</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {{ background: #f8f9fa; padding: 40px 20px; }}
            .search-container {{ max-width: 500px; margin: 0 auto; }}
            .card {{ border: none; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }}
        </style>
    </head>
    <body>
        <div class="search-container">
            <div class="card p-4">
                <h3 class="mb-4">Consultar Folio</h3>
                <form action="/consulta" method="get">
                    <div class="mb-3">
                        <label for="folio" class="form-label">Ingrese Folio</label>
                        <input type="text" class="form-control" id="folio" name="folio" placeholder="Ej: ZX53314" required>
                    </div>
                    <button type="submit" class="btn btn-primary w-100">Consultar</button>
                </form>
            </div>
        </div>
    </body>
    </html>
    """)

@app.get("/consulta", response_class=HTMLResponse)
async def consulta(request: Request, folio: str):
    """Consulta de folio - redirige a la ruta con folio"""
    return RedirectResponse(url=f"/consulta/{folio}")

@app.get("/consulta/{folio}", response_class=HTMLResponse)
async def consulta_folio_resultado(folio: str, request: Request):
    """Resultado de consulta de folio con template"""
    try:
        # Buscar folio en BD
        resultado = supabase_client.table("folios_registrados").select("*").eq("folio", folio).execute()
        
        if not resultado.data:
            estado = "no-encontrado"
            datos = None
            status_badge = "NO ENCONTRADO"
            status_class = "no-encontrado"
        else:
            datos = resultado.data[0]
            
            # Validar si está vencido
            fecha_venc_str = datos.get("fecha_vencimiento")
            try:
                fecha_venc = datetime.strptime(fecha_venc_str, "%d/%m/%Y")
                estado = "vencido" if datetime.now() > fecha_venc else "vigente"
            except:
                estado = "vigente"
            
            status_badge = estado.upper()
            status_class = estado
        
        return templates.TemplateResponse("consulta_folio.html", {
            "request": request,
            "folio": folio,
            "estado": estado,
            "status_badge": status_badge,
            "status_class": status_class,
            "datos": datos or {}
        })
    except Exception as e:
        logger.error(f"Error en consulta: {e}")
        return HTMLResponse(f"<h1>Error: {str(e)}</h1>", status_code=500)

# ============================================================================
# PANEL ADMIN
# ============================================================================

@app.get("/panel/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Página de login"""
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Admin - Login</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {{ background: #f5f5f5; display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
            .login-card {{ width: 100%; max-width: 400px; }}
        </style>
    </head>
    <body>
        <div class="login-card">
            <div class="card shadow-lg">
                <div class="card-body p-5">
                    <h3 class="text-center mb-4">Panel Admin</h3>
                    <form method="post" action="/panel/login">
                        <div class="mb-3">
                            <input type="text" name="username" class="form-control" placeholder="Usuario" required>
                        </div>
                        <div class="mb-3">
                            <input type="password" name="password" class="form-control" placeholder="Contraseña" required>
                        </div>
                        <button type="submit" class="btn btn-primary w-100">Ingresar</button>
                    </form>
                </div>
            </div>
        </div>
    </body>
    </html>
    """)

@app.post("/panel/login", response_class=HTMLResponse)
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    """Validar login"""
    if username == ADMIN_USER and password == ADMIN_PASS:
        request.session["admin"] = True
        return RedirectResponse(url="/panel/admin", status_code=302)
    return HTMLResponse("<h1>Credenciales inválidas</h1>")

@app.get("/panel/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    """Panel admin - requiere login"""
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login")
    
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Admin Panel</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body>
        <nav class="navbar navbar-dark bg-dark">
            <div class="container-fluid">
                <span class="navbar-brand mb-0 h1">GOB TLAX Admin</span>
                <a href="/panel/logout" class="btn btn-outline-light">Logout</a>
            </div>
        </nav>
        <div class="container mt-5">
            <h2>Bienvenido al Panel Admin</h2>
            <ul class="list-group mt-4">
                <li class="list-group-item"><a href="/panel/folios">Ver Folios</a></li>
                <li class="list-group-item"><a href="/panel/crear_usuario">Crear Usuario</a></li>
            </ul>
        </div>
    </body>
    </html>
    """)

@app.get("/panel/logout")
async def logout(request: Request):
    """Logout"""
    request.session.clear()
    return RedirectResponse(url="/portal")

# ============================================================================
# TELEGRAM BOT
# ============================================================================

@app.post("/webhook")
async def webhook(request: Request):
    """Webhook para Telegram"""
    try:
        update = await request.json()
        await dp.feed_update(bot, types.Update(**update))
        return {"ok": True}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"ok": False}

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    """Comando /start"""
    await message.answer(
        "¡Bienvenido a GOB TLAX Permisos!\n\n"
        "Usa /tlaxcala para solicitar un permiso provisional"
    )

@dp.message(Command("tlaxcala"))
async def cmd_tlaxcala(message: types.Message, state: FSMContext):
    """Inicia formulario"""
    await state.set_state(VehicleForm.marca)
    await message.answer("Ingrese la MARCA del vehículo:")

@dp.message(VehicleForm.marca)
async def process_marca(message: types.Message, state: FSMContext):
    await state.update_data(marca=message.text)
    await state.set_state(VehicleForm.linea)
    await message.answer("Ingrese la LÍNEA:")

@dp.message(VehicleForm.linea)
async def process_linea(message: types.Message, state: FSMContext):
    await state.update_data(linea=message.text)
    await state.set_state(VehicleForm.anio)
    await message.answer("Ingrese el AÑO:")

@dp.message(VehicleForm.anio)
async def process_anio(message: types.Message, state: FSMContext):
    await state.update_data(anio=message.text)
    await state.set_state(VehicleForm.serie)
    await message.answer("Ingrese la SERIE:")

@dp.message(VehicleForm.serie)
async def process_serie(message: types.Message, state: FSMContext):
    await state.update_data(serie=message.text)
    await state.set_state(VehicleForm.motor)
    await message.answer("Ingrese el MOTOR:")

@dp.message(VehicleForm.motor)
async def process_motor(message: types.Message, state: FSMContext):
    await state.update_data(motor=message.text)
    await state.set_state(VehicleForm.color)
    await message.answer("Ingrese el COLOR:")

@dp.message(VehicleForm.color)
async def process_color(message: types.Message, state: FSMContext):
    await state.update_data(color=message.text)
    await state.set_state(VehicleForm.nombre)
    await message.answer("Ingrese NOMBRE del propietario:")

@dp.message(VehicleForm.nombre)
async def process_nombre(message: types.Message, state: FSMContext):
    await state.update_data(nombre=message.text)
    await state.set_state(VehicleForm.cve_vehicular)
    await message.answer("Ingrese CLAVE VEHICULAR:")

@dp.message(VehicleForm.cve_vehicular)
async def process_cve(message: types.Message, state: FSMContext):
    data = await state.get_data()
    data["cve_vehicular"] = message.text
    
    # Generar folio
    folio = generar_folio()
    guardar_folio(folio, data, str(message.from_user.id))
    
    await message.answer(
        f"✅ Folio generado: <b>{folio}</b>\n\n"
        f"Consulta tu permiso en: {BASE_URL}/consulta/{folio}",
        parse_mode="HTML"
    )
    await state.clear()

# ============================================================================
# HEALTH CHECK
# ============================================================================

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}

# ============================================================================
# STARTUP
# ============================================================================

@app.on_event("startup")
async def startup():
    """Startup event"""
    logger.info("🚀 Sistema iniciado")
    # Aquí puedes configurar el webhook de Telegram si es necesario
    # await bot.set_webhook_url(f"{BASE_URL}/webhook")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
