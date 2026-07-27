import asyncio
import os
from datetime import datetime, timedelta
from typing import Optional, List
from dotenv import load_dotenv
import json
import secrets
import logging
from decimal import Decimal

# FastAPI
from fastapi import FastAPI, Request, HTTPException, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials

# Supabase
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

# ============================================================================
# CONFIGURACIÓN
# ============================================================================

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

app = FastAPI(title="GOB TLAX - Permisos Provisionales v3.0")

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
        pdf_doc = fitz.open(PLANTILLA_PDF)
        page = pdf_doc[0]
        
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
        
        qr_url = f"{BASE_URL}/consulta/{folio}"
        generar_qr(qr_url)
        generar_qr(str(datos))
        
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

def obtener_estadisticas() -> dict:
    """Obtiene estadísticas del sistema"""
    try:
        total = supabase_client.table("folios_registrados").select("count", count="exact").execute()
        vigentes = supabase_client.table("folios_registrados").select("*").eq("estado", "vigente").execute()
        vencidos = supabase_client.table("folios_registrados").select("*").eq("estado", "vencido").execute()
        
        return {
            "total_folios": total.count or 0,
            "folios_vigentes": len(vigentes.data or []),
            "folios_vencidos": len(vencidos.data or []),
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Error obteniendo estadísticas: {e}")
        return {}

def obtener_folios_paginado(pagina: int = 1, por_pagina: int = 20) -> dict:
    """Obtiene folios con paginación"""
    try:
        inicio = (pagina - 1) * por_pagina
        resultado = supabase_client.table("folios_registrados").select("*").range(inicio, inicio + por_pagina - 1).execute()
        
        total = supabase_client.table("folios_registrados").select("count", count="exact").execute()
        
        return {
            "folios": resultado.data or [],
            "total": total.count or 0,
            "pagina": pagina,
            "total_paginas": (total.count or 0 + por_pagina - 1) // por_pagina
        }
    except Exception as e:
        logger.error(f"Error obteniendo folios: {e}")
        return {}

# ============================================================================
# RUTAS PÚBLICAS
# ============================================================================

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return RedirectResponse(url="/portal")

@app.get("/portal", response_class=HTMLResponse)
async def portal(request: Request):
    stats = obtener_estadisticas()
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html lang="es">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>GOB TLAX - Permisos Provisionales</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
        <style>
            body {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; font-family: 'Roboto', sans-serif; }}
            .hero {{ padding: 80px 20px; text-align: center; color: white; }}
            .hero h1 {{ font-size: 3rem; font-weight: 700; margin-bottom: 20px; text-shadow: 0 4px 8px rgba(0,0,0,0.2); }}
            .hero p {{ font-size: 1.3rem; opacity: 0.95; margin-bottom: 40px; }}
            .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 20px; margin-top: 60px; }}
            .stat-card {{ background: white; padding: 30px; border-radius: 12px; box-shadow: 0 8px 24px rgba(0,0,0,0.15); text-align: center; }}
            .stat-card h3 {{ color: #667eea; font-weight: 700; margin: 0; }}
            .stat-card p {{ color: #999; margin-top: 10px; }}
            .btn-primary {{ background: white; color: #667eea; border: none; font-weight: 600; padding: 12px 40px; }}
            .btn-primary:hover {{ transform: translateY(-2px); box-shadow: 0 8px 16px rgba(0,0,0,0.2); }}
        </style>
    </head>
    <body>
        <div class="hero">
            <h1><i class="bi bi-car-front-fill"></i> Permiso Provisional de Circulación</h1>
            <p>Secretaría de Movilidad y Transporte - Tlaxcala</p>
            <div style="margin: 30px 0;">
                <a href="/consulta_folio" class="btn btn-primary btn-lg">🔍 Consultar Folio</a>
            </div>
            <div class="stats" style="max-width: 800px; margin: 0 auto;">
                <div class="stat-card">
                    <h3>{stats.get('total_folios', 0)}</h3>
                    <p>Folios Emitidos</p>
                </div>
                <div class="stat-card">
                    <h3>{stats.get('folios_vigentes', 0)}</h3>
                    <p>Vigentes</p>
                </div>
                <div class="stat-card">
                    <h3>{stats.get('folios_vencidos', 0)}</h3>
                    <p>Vencidos</p>
                </div>
            </div>
        </div>
    </body>
    </html>
    """)

@app.get("/consulta_folio", response_class=HTMLResponse)
async def consulta_folio_page(request: Request):
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
    return RedirectResponse(url=f"/consulta/{folio}")

@app.get("/consulta/{folio}", response_class=HTMLResponse)
async def consulta_folio_resultado(folio: str, request: Request):
    """Resultado de consulta con template"""
    try:
        resultado = supabase_client.table("folios_registrados").select("*").eq("folio", folio).execute()
        
        if not resultado.data:
            estado = "no-encontrado"
            datos = None
            status_badge = "NO ENCONTRADO"
            status_class = "no-encontrado"
        else:
            datos = resultado.data[0]
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
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Admin - Login</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); display: flex; align-items: center; justify-content: center; min-height: 100vh; }}
            .login-card {{ width: 100%; max-width: 400px; }}
            .card {{ border: none; border-radius: 12px; box-shadow: 0 8px 24px rgba(0,0,0,0.15); }}
        </style>
    </head>
    <body>
        <div class="login-card">
            <div class="card">
                <div class="card-body p-5">
                    <h3 class="text-center mb-4">Panel Administración</h3>
                    <form method="post" action="/panel/login">
                        <div class="mb-3">
                            <label class="form-label">Usuario</label>
                            <input type="text" name="username" class="form-control" placeholder="Usuario" required>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">Contraseña</label>
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
    if username == ADMIN_USER and password == ADMIN_PASS:
        request.session["admin"] = True
        return RedirectResponse(url="/panel/admin", status_code=302)
    return HTMLResponse("<h1>❌ Credenciales inválidas</h1>")

@app.get("/panel/admin", response_class=HTMLResponse)
async def admin_panel(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login")
    
    stats = obtener_estadisticas()
    
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Admin Panel</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
        <style>
            .sidebar {{ background: #422b7c; color: white; min-height: 100vh; }}
            .card {{ border: none; box-shadow: 0 4px 12px rgba(0,0,0,0.08); }}
            .stat {{ padding: 20px; background: white; border-radius: 8px; text-align: center; }}
            .stat-number {{ font-size: 2rem; font-weight: 700; color: #667eea; }}
        </style>
    </head>
    <body>
        <nav class="navbar navbar-dark bg-dark mb-4">
            <div class="container-fluid">
                <span class="navbar-brand mb-0 h5"><i class="bi bi-shield-lock"></i> GOB TLAX Admin v3.0</span>
                <a href="/panel/logout" class="btn btn-outline-light">Logout</a>
            </div>
        </nav>
        <div class="container-fluid">
            <div class="row mb-4">
                <div class="col-md-3">
                    <div class="stat">
                        <div class="stat-number">{stats.get('total_folios', 0)}</div>
                        <div>Total Folios</div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="stat">
                        <div class="stat-number" style="color: #16a34a;">{stats.get('folios_vigentes', 0)}</div>
                        <div>Vigentes</div>
                    </div>
                </div>
                <div class="col-md-3">
                    <div class="stat">
                        <div class="stat-number" style="color: #dc2626;">{stats.get('folios_vencidos', 0)}</div>
                        <div>Vencidos</div>
                    </div>
                </div>
            </div>
            
            <div class="row">
                <div class="col-12">
                    <div class="card p-4">
                        <h4>Opciones Disponibles</h4>
                        <ul class="list-group mt-3">
                            <li class="list-group-item"><a href="/panel/folios">📋 Ver todos los folios</a></li>
                            <li class="list-group-item"><a href="/panel/crear_usuario">👤 Crear usuario</a></li>
                            <li class="list-group-item"><a href="/panel/registro_admin">📝 Registro manual</a></li>
                            <li class="list-group-item"><a href="/panel/tablas">🗄️ Editor de tablas</a></li>
                        </ul>
                    </div>
                </div>
            </div>
        </div>
    </body>
    </html>
    """)

@app.get("/panel/folios", response_class=HTMLResponse)
async def panel_folios(request: Request, pagina: int = 1):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login")
    
    datos_pag = obtener_folios_paginado(pagina, 20)
    folios = datos_pag.get("folios", [])
    total_paginas = datos_pag.get("total_paginas", 0)
    
    filas_html = ""
    for f in folios:
        fecha_exp = f.get("fecha_expedicion", "N/A")
        nombre = f.get("nombre", "N/A")
        marca = f.get("marca", "N/A")
        estado = f.get("estado", "N/A")
        color_estado = "success" if estado == "vigente" else "danger"
        
        filas_html += f"""
        <tr>
            <td><strong>{f.get('folio')}</strong></td>
            <td>{nombre}</td>
            <td>{marca} {f.get('linea', '')}</td>
            <td>{fecha_exp}</td>
            <td><span class="badge bg-{color_estado}">{estado.upper()}</span></td>
            <td>
                <a href="/panel/validar/{f.get('folio')}" class="btn btn-sm btn-info">Validar</a>
                <a href="/panel/pdf/{f.get('folio')}" class="btn btn-sm btn-secondary">PDF</a>
            </td>
        </tr>
        """
    
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Folios - Admin</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body>
        <nav class="navbar navbar-dark bg-dark mb-4">
            <div class="container-fluid">
                <a href="/panel/admin" class="btn btn-outline-light">← Volver</a>
                <span class="navbar-brand mb-0">Folios Registrados</span>
            </div>
        </nav>
        <div class="container-fluid">
            <table class="table table-hover">
                <thead class="table-dark">
                    <tr>
                        <th>Folio</th>
                        <th>Propietario</th>
                        <th>Vehículo</th>
                        <th>Expedición</th>
                        <th>Estado</th>
                        <th>Acciones</th>
                    </tr>
                </thead>
                <tbody>
                    {filas_html}
                </tbody>
            </table>
            <nav>
                <ul class="pagination">
    """)

@app.get("/panel/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/portal")

@app.get("/panel/crear_usuario", response_class=HTMLResponse)
async def crear_usuario_page(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login")
    
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Crear Usuario</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body>
        <nav class="navbar navbar-dark bg-dark mb-4">
            <div class="container-fluid">
                <a href="/panel/admin" class="btn btn-outline-light">← Volver</a>
                <span class="navbar-brand mb-0">Crear Nuevo Usuario</span>
            </div>
        </nav>
        <div class="container" style="max-width: 500px;">
            <div class="card">
                <div class="card-body">
                    <form method="post" action="/panel/crear_usuario">
                        <div class="mb-3">
                            <label class="form-label">Usuario</label>
                            <input type="text" name="username" class="form-control" required>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">Contraseña</label>
                            <input type="password" name="password" class="form-control" required>
                        </div>
                        <div class="mb-3">
                            <label class="form-label">Folios Asignados</label>
                            <input type="number" name="folios_asignados" class="form-control" value="10" required>
                        </div>
                        <button type="submit" class="btn btn-primary w-100">Crear Usuario</button>
                    </form>
                </div>
            </div>
        </div>
    </body>
    </html>
    """)

@app.post("/panel/crear_usuario", response_class=HTMLResponse)
async def crear_usuario_post(request: Request, username: str = Form(...), password: str = Form(...), folios_asignados: int = Form(...)):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login")
    
    try:
        supabase_client.table("verificacion_tlaxcala").insert({
            "username": username,
            "password": password,
            "folios_asignac": folios_asignados,
            "folios_usados": 0
        }).execute()
        
        return HTMLResponse(f"<h2>✅ Usuario '{username}' creado correctamente</h2><a href='/panel/admin'>Volver al panel</a>")
    except Exception as e:
        return HTMLResponse(f"<h2>❌ Error: {str(e)}</h2>")

@app.get("/panel/validar/{folio}", response_class=HTMLResponse)
async def validar_folio(folio: str, request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login")
    
    try:
        resultado = supabase_client.table("folios_registrados").select("*").eq("folio", folio).execute()
        if resultado.data:
            datos = resultado.data[0]
            supabase_client.table("folios_registrados").update({"estado_pago": "validado"}).eq("folio", folio).execute()
            return HTMLResponse(f"<h2>✅ Folio {folio} validado correctamente</h2><a href='/panel/folios'>Ver folios</a>")
    except Exception as e:
        return HTMLResponse(f"<h2>❌ Error: {str(e)}</h2>")

@app.get("/panel/pdf/{folio}")
async def descargar_pdf(folio: str, request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login")
    
    try:
        resultado = supabase_client.table("folios_registrados").select("*").eq("folio", folio).execute()
        if resultado.data:
            pdf_bytes = generar_pdf(folio, resultado.data[0])
            return FileResponse(BytesIO(pdf_bytes), media_type="application/pdf", filename=f"{folio}.pdf")
    except Exception as e:
        logger.error(f"Error descargando PDF: {e}")
    
    return HTMLResponse("<h1>Error descargando PDF</h1>")

@app.get("/panel/tablas", response_class=HTMLResponse)
async def panel_tablas(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse(url="/panel/login")
    
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="UTF-8">
        <title>Editor de Tablas</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    </head>
    <body>
        <nav class="navbar navbar-dark bg-dark mb-4">
            <div class="container-fluid">
                <a href="/panel/admin" class="btn btn-outline-light">← Volver</a>
                <span class="navbar-brand mb-0">Editor de Tablas</span>
            </div>
        </nav>
        <div class="container">
            <div class="row">
                <div class="col-md-3">
                    <a href="/panel/tabla/folios_registrados" class="btn btn-primary w-100 mb-2">Folios Registrados</a>
                </div>
                <div class="col-md-3">
                    <a href="/panel/tabla/verificacion_tlaxcala" class="btn btn-primary w-100 mb-2">Usuarios</a>
                </div>
                <div class="col-md-3">
                    <a href="/panel/tabla/folio_watermark" class="btn btn-primary w-100 mb-2">Watermark</a>
                </div>
            </div>
        </div>
    </body>
    </html>
    """)

# ============================================================================
# TELEGRAM BOT
# ============================================================================

@app.post("/webhook")
async def webhook(request: Request):
    try:
        update = await request.json()
        await dp.feed_update(bot, types.Update(**update))
        return {"ok": True}
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return {"ok": False}

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "¡Bienvenido a GOB TLAX Permisos!\n\n"
        "Opciones disponibles:\n"
        "/tlaxcala - Solicitar permiso provisional\n"
        "/folios - Ver tus folios activos\n"
        "/ayuda - Información adicional"
    )

@dp.message(Command("tlaxcala"))
async def cmd_tlaxcala(message: types.Message, state: FSMContext):
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
    
    folio = generar_folio()
    guardar_folio(folio, data, str(message.from_user.id))
    
    await message.answer(
        f"✅ <b>Folio generado: {folio}</b>\n\n"
        f"Consulta tu permiso en:\n{BASE_URL}/consulta/{folio}\n\n"
        f"Válido por 30 días.",
        parse_mode="HTML"
    )
    await state.clear()

# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.get("/api/estadisticas")
async def api_estadisticas():
    return obtener_estadisticas()

@app.get("/api/folios")
async def api_folios(pagina: int = 1):
    return obtener_folios_paginado(pagina)

@app.get("/api/folio/{folio}")
async def api_folio(folio: str):
    try:
        resultado = supabase_client.table("folios_registrados").select("*").eq("folio", folio).execute()
        if resultado.data:
            return resultado.data[0]
        return {"error": "Folio no encontrado"}
    except Exception as e:
        return {"error": str(e)}

# ============================================================================
# HEALTH CHECK
# ============================================================================

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "3.0",
        "timestamp": datetime.now().isoformat(),
        "sistema": ENTIDAD
    }

# ============================================================================
# STARTUP
# ============================================================================

@app.on_event("startup")
async def startup():
    logger.info("🚀 GOB TLAX Sistema v3.0 iniciado")
    logger.info(f"Base URL: {BASE_URL}")
    logger.info(f"Entidad: {ENTIDAD}")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
