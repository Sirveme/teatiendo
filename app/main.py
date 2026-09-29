"""Te Atiendo · punto de entrada de la aplicación FastAPI."""
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import asyncpg
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import CFG  # valida las variables de entorno al importar
from app import auth, db, meta, panel, webhook

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("teatiendo")

HOSTS_LOCALES = {"localhost", "127.0.0.1", "::1"}


class SesionAdaptable:
    """Cookie de sesión firmada. En Railway siempre es https_only; en localhost se relaja
    para poder probar el login por http://localhost sin certificado."""

    def __init__(self, app, secreto: str, forzar_https: bool):
        opciones = dict(secret_key=secreto, session_cookie="teatiendo_sesion", max_age=12 * 3600, same_site="lax")
        self.segura = SessionMiddleware(app, https_only=True, **opciones)
        self.local = SessionMiddleware(app, https_only=False, **opciones)
        self.forzar_https = forzar_https

    @staticmethod
    def _host(scope) -> str:
        host = dict(scope.get("headers") or []).get(b"host", b"").decode("latin-1").lower()
        if host.startswith("["):  # IPv6: [::1]:8000
            return host[1:host.find("]")]
        return host.rsplit(":", 1)[0]

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and not self.forzar_https and self._host(scope) in HOSTS_LOCALES:
            await self.local(scope, receive, send)
        else:
            await self.segura(scope, receive, send)


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        pool = await db.crear_pool(CFG.database_url)
    except Exception as e:
        log.critical("No se pudo conectar a PostgreSQL. Revisa DATABASE_URL. Detalle: %s", e)
        raise RuntimeError("Sin conexión a la base de datos") from e
    try:
        tenant_id = await db.asegurar_tenant(pool, CFG.wa_waba_id, CFG.wa_phone_number_id)
    except asyncpg.UndefinedTableError as e:
        await pool.close()
        log.critical("Las tablas no existen. Ejecuta schema.sql en la base de datos (PGAdmin) y vuelve a desplegar.")
        raise RuntimeError("Falta ejecutar schema.sql") from e

    app.state.pool = pool
    app.state.tenant_id = tenant_id
    log.info("Te Atiendo listo · tenant %s · número %s · Graph %s · cookie https_only=%s",
             tenant_id, CFG.wa_phone_number_id, CFG.graph_api_version,
             "siempre" if CFG.en_railway else "salvo localhost")
    yield
    await meta.cerrar()
    await pool.close()


app = FastAPI(title="Te Atiendo", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SesionAdaptable, secreto=CFG.session_secret, forzar_https=CFG.en_railway)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

app.include_router(webhook.router)
app.include_router(auth.router)
app.include_router(panel.router)


@app.exception_handler(auth.NoAutenticado)
async def _redirigir_a_login(request: Request, exc: auth.NoAutenticado):
    if request.headers.get("hx-request"):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


@app.get("/salud", include_in_schema=False)
async def salud():
    return JSONResponse({"ok": True})
