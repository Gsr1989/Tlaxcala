# ============================================================
# consulta_publica_tlax.py
# Consulta pública de folio (QR) - Tlaxcala SMyT
#
# Página = cajón de resultado + el portal oficial de la SMyT
# (https://telecomsmytlax.wixsite.com/my-site) incrustado completo
# con iframe, igual que ya lo manejas. Así se ve TAL CUAL el sitio:
# menús, carruseles, banners, footer, todo.
#
# USO en main (2 líneas):
#   from consulta_publica_tlax import crear_router_consulta_tlax
#   app.include_router(crear_router_consulta_tlax(supabase, TZ))
# ============================================================

import html as html_lib
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

PORTAL_URL = "https://telecomsmytlax.wixsite.com/my-site"

LOGO_ESTADO = "https://static.wixstatic.com/media/068a6d_fe7b18f545504876a08fa882ca52ee05~mv2.png"
LOGO_SMYT = "https://static.wixstatic.com/media/068a6d_4ee71e07fac841d4ae1e300f3aad10cc~mv2.png"

# Paleta del propio sistema Tlaxcala (C1, C3, ACCENT de tu main)
CSS = """
:root{ --morado:#422b7c; --morado2:#341f63; --dorado:#e6d194; --magenta:#a11a5c; --gris:#777; }
*{ box-sizing:border-box; }
html,body{ margin:0; padding:0; background:#f4f4f4; font-family:'Roboto',Arial,sans-serif; color:#444; }

.tl-top{ background:#fff; box-shadow:0 2px 10px rgba(0,0,0,.08); border-bottom:4px solid var(--morado); }
.tl-top-in{ max-width:1100px; margin:auto; padding:14px 20px; display:flex; align-items:center;
  justify-content:center; gap:28px; flex-wrap:wrap; }
.tl-top img{ height:64px; width:auto; object-fit:contain; max-width:100%; }

.tl-wrap{ max-width:900px; margin:28px auto 10px; padding:0 18px; }
.tl-head{ text-align:center; margin-bottom:22px; }
.tl-head small{ display:block; color:var(--magenta); font-size:.85rem; font-weight:700; letter-spacing:3px;
  text-transform:uppercase; margin-bottom:6px; }
.tl-head h1{ margin:0; color:var(--morado); font-size:2rem; font-weight:400; line-height:1.2; }
.tl-head p{ margin:8px 0 0; color:var(--gris); font-size:1rem; }

.tl-card{ background:#fff; border:1px solid var(--dorado); border-radius:20px; overflow:hidden;
  box-shadow:0 8px 28px rgba(0,0,0,.10); }

.tl-status{ display:flex; align-items:center; gap:18px; padding:22px 28px; }
.tl-status.ok{ background:#e6f4e8; color:#155724; }
.tl-status.venc{ background:#fff7dc; color:#856404; }
.tl-status.err{ background:#f8d7da; color:#721c24; }
.tl-ico{ width:54px; height:54px; flex:0 0 54px; border-radius:50%; display:flex; align-items:center;
  justify-content:center; font-size:1.8rem; font-weight:700; color:#fff; line-height:1; }
.tl-status.ok .tl-ico{ background:#38934d; }
.tl-status.venc .tl-ico{ background:#d59e16; }
.tl-status.err .tl-ico{ background:#b72f3c; }
.tl-status h2{ margin:0 0 3px; font-size:1.2rem; font-weight:700; letter-spacing:.5px; color:inherit; }
.tl-status p{ margin:0; font-size:.98rem; line-height:1.4; color:inherit; }

.tl-body{ padding:26px 28px 30px; }

.tl-folio{ text-align:center; padding-bottom:22px; }
.tl-folio span{ display:block; color:#999; font-size:.8rem; font-weight:700; letter-spacing:3px; margin-bottom:6px; }
.tl-folio strong{ display:block; color:var(--morado); font-size:2.2rem; font-weight:700; letter-spacing:1px;
  overflow-wrap:anywhere; }
.tl-folio i{ display:block; width:70px; height:4px; background:var(--dorado); border-radius:4px; margin:14px auto 0; }

.tl-sub{ color:var(--magenta); font-size:1.1rem; font-weight:700; margin:0 0 14px; padding-bottom:8px;
  border-bottom:1px solid #eee; }

.tl-grid{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
.tl-item{ background:#f8f9fa; border-radius:12px; border-left:5px solid var(--dorado); padding:13px 16px; min-width:0; }
.tl-item.wide{ grid-column:span 2; }
.tl-item span{ display:block; color:#999; font-size:.72rem; font-weight:700; letter-spacing:1px;
  text-transform:uppercase; margin-bottom:5px; }
/* Todos los valores iguales: misma fuente, tamaño y peso */
.tl-item strong{ display:block; color:#333; font-size:1rem; font-weight:500; line-height:1.4; overflow-wrap:anywhere; }

.tl-vig{ margin-top:22px; background:#f8f9fa; border-radius:12px; border-left:5px solid var(--dorado); padding:16px 18px; }
.tl-bar{ height:12px; background:#e6e0f2; border-radius:20px; overflow:hidden; }
.tl-bar b{ display:block; height:100%; border-radius:20px; }
.tl-bar b.ok{ background:linear-gradient(90deg,var(--morado),#38934d); }
.tl-bar b.venc{ background:linear-gradient(90deg,var(--morado),#d59e16); }
.tl-vig-txt{ margin-top:10px; text-align:center; color:var(--gris); font-size:.92rem; }

.tl-note{ margin-top:20px; background:#f8f9fa; border-radius:12px; border-left:5px solid var(--morado);
  padding:13px 16px; color:var(--gris); font-size:.88rem; line-height:1.55; }
.tl-note b{ color:var(--morado); font-weight:700; }

.tl-empty{ padding:10px 28px 34px; text-align:center; color:var(--gris); font-size:1rem; line-height:1.6; }

.tl-portal{ max-width:1200px; margin:30px auto 0; padding:0 0 0; }
.tl-portal-bar{ display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap;
  background:var(--morado); color:#fff; padding:12px 20px; }
.tl-portal-bar strong{ font-size:.95rem; font-weight:500; }
.tl-portal-bar a{ color:#fff; font-size:.85rem; text-decoration:underline; }
.tl-portal iframe{ display:block; width:100%; height:100vh; min-height:640px; border:none; background:#fff; }

@media (max-width:640px){
  .tl-top img{ height:46px; }
  .tl-top-in{ gap:14px; }
  .tl-wrap{ margin-top:20px; padding:0 12px; }
  .tl-head h1{ font-size:1.55rem; }
  .tl-status{ padding:16px 16px; gap:14px; }
  .tl-ico{ width:44px; height:44px; flex-basis:44px; font-size:1.4rem; }
  .tl-status h2{ font-size:1.02rem; }
  .tl-status p{ font-size:.88rem; }
  .tl-body{ padding:20px 14px 24px; }
  .tl-folio strong{ font-size:1.6rem; }
  .tl-grid{ grid-template-columns:1fr; }
  .tl-item.wide{ grid-column:span 1; }
}
"""


def _esc(valor):
    return html_lib.escape(str(valor if valor not in (None, "") else "—"))


def _fecha(valor):
    if not valor:
        return None
    texto = str(valor).strip()
    try:
        return datetime.fromisoformat(texto.replace("Z", "+00:00")).date()
    except Exception:
        try:
            return datetime.strptime(texto[:10], "%Y-%m-%d").date()
        except Exception:
            return None


def _pagina(titulo: str, contenido: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{html_lib.escape(titulo)} — SMyT Tlaxcala</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>

<header class="tl-top">
  <div class="tl-top-in">
    <img src="{LOGO_ESTADO}" alt="Gobierno del Estado de Tlaxcala">
    <img src="{LOGO_SMYT}" alt="Secretaría de Movilidad y Transporte">
  </div>
</header>

<main class="tl-wrap">
{contenido}
</main>

<section class="tl-portal">
  <div class="tl-portal-bar">
    <strong>Portal oficial — Secretaría de Movilidad y Transporte de Tlaxcala</strong>
    <a href="{PORTAL_URL}" target="_blank" rel="noopener">Abrir en pantalla completa</a>
  </div>
  <iframe src="{PORTAL_URL}" title="Portal SMyT Tlaxcala" loading="lazy">
    Tu navegador no soporta iframes.
  </iframe>
</section>

</body>
</html>"""


def _cabecera() -> str:
    return """
<div class="tl-head">
  <small>Verificación oficial</small>
  <h1>Consulta de permiso</h1>
  <p>Secretaría de Movilidad y Transporte — Tlaxcala</p>
</div>
"""


def crear_router_consulta_tlax(supabase, timezone: str) -> APIRouter:

    router = APIRouter()

    @router.get("/consulta/{folio}", response_class=HTMLResponse)
    async def consulta_publica_tlax(folio: str):

        folio = folio.strip().upper()

        try:

            res = (
                supabase
                .table("folios_registrados")
                .select("*")
                .eq("folio", folio)
                .limit(1)
                .execute()
            )

            hoy = datetime.now(ZoneInfo(timezone)).date()

            # ---------------- NO ENCONTRADO ----------------
            if not res.data:

                contenido = f"""
{_cabecera()}
<div class="tl-card">
  <div class="tl-status err">
    <div class="tl-ico">×</div>
    <div>
      <h2>FOLIO NO ENCONTRADO</h2>
      <p>No existe un permiso registrado con este folio.</p>
    </div>
  </div>
  <div class="tl-body">
    <div class="tl-folio">
      <span>FOLIO CONSULTADO</span>
      <strong>{_esc(folio)}</strong>
      <i></i>
    </div>
    <div class="tl-empty">
      Verifica que el folio esté capturado correctamente. Si el problema
      continúa, comunícate con la Secretaría de Movilidad y Transporte.
    </div>
  </div>
</div>
"""
                return HTMLResponse(
                    _pagina(f"Folio {folio}", contenido)
                )

            # ---------------- ENCONTRADO ----------------
            r = res.data[0]

            fe = _fecha(r.get("fecha_expedicion"))
            fv = _fecha(r.get("fecha_vencimiento"))

            vigente = fv is not None and hoy <= fv

            if vigente:
                clase = "ok"
                icono = "✓"
                titulo = "PERMISO VIGENTE"
                sub = "El permiso se encuentra dentro de su periodo de vigencia."
            else:
                clase = "venc"
                icono = "!"
                titulo = "PERMISO VENCIDO"
                sub = "El periodo de vigencia de este permiso ha concluido."

            fe_txt = fe.strftime("%d/%m/%Y") if fe else "—"
            fv_txt = fv.strftime("%d/%m/%Y") if fv else "—"

            pct = 0
            texto_vig = ""

            if fe and fv:

                total = max(1, (fv - fe).days + 1)

                if hoy < fe:
                    faltan = (fe - hoy).days
                    texto_vig = (
                        f"La vigencia inicia en {faltan} "
                        f"día{'s' if faltan != 1 else ''} "
                        f"(duración total: {total} días)."
                    )
                elif hoy > fv:
                    pct = 100
                    pasados = (hoy - fv).days
                    texto_vig = (
                        f"Venció hace {pasados} "
                        f"día{'s' if pasados != 1 else ''} "
                        f"(duración total: {total} días)."
                    )
                else:
                    transcurridos = (hoy - fe).days + 1
                    pct = round(transcurridos / total * 100)
                    restantes = (fv - hoy).days + 1
                    texto_vig = (
                        f"Día {transcurridos} de {total} · "
                        f"quedan {restantes} "
                        f"día{'s' if restantes != 1 else ''} de vigencia."
                    )

                pct = max(0, min(100, pct))

            vig_txt = (
                f'<div class="tl-vig-txt">{_esc(texto_vig)}</div>'
                if texto_vig else ""
            )

            contenido = f"""
{_cabecera()}
<div class="tl-card">

  <div class="tl-status {clase}">
    <div class="tl-ico">{icono}</div>
    <div>
      <h2>{titulo}</h2>
      <p>{sub}</p>
    </div>
  </div>

  <div class="tl-body">

    <div class="tl-folio">
      <span>FOLIO</span>
      <strong>{_esc(folio)}</strong>
      <i></i>
    </div>

    <h3 class="tl-sub">Información del permiso</h3>

    <div class="tl-grid">
      <div class="tl-item wide"><span>Propietario</span><strong>{_esc(r.get("nombre"))}</strong></div>
      <div class="tl-item"><span>Marca</span><strong>{_esc(r.get("marca"))}</strong></div>
      <div class="tl-item"><span>Línea</span><strong>{_esc(r.get("linea"))}</strong></div>
      <div class="tl-item"><span>Modelo (año)</span><strong>{_esc(r.get("anio"))}</strong></div>
      <div class="tl-item"><span>Color</span><strong>{_esc(r.get("color"))}</strong></div>
      <div class="tl-item"><span>Número de serie / NIV</span><strong>{_esc(r.get("numero_serie"))}</strong></div>
      <div class="tl-item"><span>Número de motor</span><strong>{_esc(r.get("numero_motor"))}</strong></div>
      <div class="tl-item"><span>Clave vehicular</span><strong>{_esc(r.get("cve_vehicular"))}</strong></div>
      <div class="tl-item"><span>Fecha de expedición</span><strong>{fe_txt}</strong></div>
      <div class="tl-item"><span>Fecha de vencimiento</span><strong>{fv_txt}</strong></div>
    </div>

    <div class="tl-vig">
      <div class="tl-bar"><b class="{clase}" style="width:{pct}%"></b></div>
      {vig_txt}
    </div>

    <div class="tl-note">
      <b>Consulta verificada.</b>
      La información mostrada corresponde al registro asociado al folio
      consultado al día {hoy.strftime("%d/%m/%Y")}.
    </div>

  </div>
</div>
"""
            return HTMLResponse(
                _pagina(f"Folio {folio}", contenido)
            )

        except Exception as e:

            print(f"[CONSULTA TLAX] Error consultando {folio}: {e!r}")

            contenido = f"""
{_cabecera()}
<div class="tl-card">
  <div class="tl-status err">
    <div class="tl-ico">!</div>
    <div>
      <h2>NO FUE POSIBLE REALIZAR LA CONSULTA</h2>
      <p>Inténtalo nuevamente en unos minutos.</p>
    </div>
  </div>
  <div class="tl-empty" style="padding-top:20px">
    Si el problema continúa, comunícate con la Secretaría de Movilidad y Transporte.
  </div>
</div>
"""
            return HTMLResponse(
                _pagina("Error de consulta", contenido),
                status_code=500
            )

    return router
