"""Inicio de sesión del administrador (Argon2) y protección de rutas."""
import logging
import time
from collections import defaultdict, deque

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app.config import CFG, VISTAS, es_superadmin

log = logging.getLogger("teatiendo.auth")
router = APIRouter()

_hasher = PasswordHasher()
_fallidos: dict[str, deque] = defaultdict(deque)
MAX_INTENTOS = 5
VENTANA_BLOQUEO_S = 600


class NoAutenticado(Exception):
    """Se lanza cuando una ruta protegida recibe una petición sin sesión."""


def requiere_login(request: Request) -> str:
    admin = request.session.get("admin")
    if not admin:
        raise NoAutenticado()
    return admin


def requiere_superadmin(request: Request) -> str:
    """Panel de modelos de IA: solo SUPERADMIN_EMAILS (o ADMIN_EMAIL si no está definida)."""
    admin = requiere_login(request)
    if not es_superadmin(admin):
        raise HTTPException(status_code=403, detail="Solo el superadministrador puede ver esta sección.")
    return admin


def _ip(request: Request) -> str:
    return request.client.host if request.client else "desconocida"


def _bloqueado(ip: str) -> bool:
    intentos = _fallidos[ip]
    ahora = time.monotonic()
    while intentos and ahora - intentos[0] > VENTANA_BLOQUEO_S:
        intentos.popleft()
    return len(intentos) >= MAX_INTENTOS


def _vista_login(request: Request, error: str | None = None, email: str = "", status_code: int = 200):
    return VISTAS.TemplateResponse(request, "login.html", {"error": error, "email": email}, status_code=status_code)


@router.get("/login")
async def ver_login(request: Request):
    if request.session.get("admin"):
        return RedirectResponse("/bandeja", status_code=303)
    return _vista_login(request)


@router.post("/login")
async def iniciar_sesion(request: Request, email: str = Form(""), password: str = Form("")):
    ip = _ip(request)
    if _bloqueado(ip):
        return _vista_login(request, "Demasiados intentos fallidos. Espera 10 minutos e inténtalo de nuevo.",
                            email, status_code=429)

    email_ok = email.strip().casefold() == CFG.admin_email.casefold()
    try:
        clave_ok = _hasher.verify(CFG.admin_password_hash, password)
    except VerificationError:
        clave_ok = False
    except InvalidHashError:
        log.error("ADMIN_PASSWORD_HASH no es un hash Argon2 válido. Regenéralo con generar_hash.py")
        clave_ok = False

    if not (email_ok and clave_ok):
        _fallidos[ip].append(time.monotonic())
        log.warning("Inicio de sesión fallido desde %s", ip)
        return _vista_login(request, "Correo o contraseña incorrectos.", email, status_code=401)

    _fallidos.pop(ip, None)
    request.session.clear()
    request.session["admin"] = CFG.admin_email
    return RedirectResponse("/bandeja", status_code=303)


@router.post("/logout")
async def cerrar_sesion(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
