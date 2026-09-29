"""Configuración: validación de variables de entorno, motor de vistas y formatos de fecha."""
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

REQUERIDAS = (
    "DATABASE_URL",
    "WA_ACCESS_TOKEN",
    "WA_PHONE_NUMBER_ID",
    "WA_WABA_ID",
    "WA_VERIFY_TOKEN",
    "META_APP_SECRET",
    "GRAPH_API_VERSION",
    "SESSION_SECRET",
    "ADMIN_EMAIL",
    "ADMIN_PASSWORD_HASH",
)


@dataclass(frozen=True)
class Config:
    database_url: str
    wa_access_token: str
    wa_phone_number_id: str
    wa_waba_id: str
    wa_verify_token: str
    meta_app_secret: str
    graph_api_version: str
    session_secret: str
    admin_email: str
    admin_password_hash: str
    en_railway: bool


def _abortar(lineas: list[str]) -> None:
    barra = "=" * 66
    mensaje = "\n".join(
        ["", barra, "  Te Atiendo no puede arrancar", barra, *lineas, "",
         "  Revisa la sección 'Variables de entorno' del LEEME.md.", barra, ""]
    )
    print(mensaje, file=sys.stderr, flush=True)
    raise SystemExit(1)


def cargar() -> Config:
    valores = {nombre: os.getenv(nombre, "").strip() for nombre in REQUERIDAS}

    faltan = [nombre for nombre, valor in valores.items() if not valor]
    if faltan:
        _abortar(["  Faltan estas variables de entorno obligatorias:", *[f"    - {n}" for n in faltan]])

    problemas = []
    if not re.fullmatch(r"v\d+\.\d+", valores["GRAPH_API_VERSION"]):
        problemas.append("    - GRAPH_API_VERSION debe tener el formato vNN.N (por ejemplo: v23.0).")
    if not valores["ADMIN_PASSWORD_HASH"].startswith("$argon2"):
        problemas.append("    - ADMIN_PASSWORD_HASH no es un hash Argon2. Genéralo con: python generar_hash.py")
    if len(valores["SESSION_SECRET"]) < 32:
        problemas.append("    - SESSION_SECRET debe tener al menos 32 caracteres.")
    if not valores["DATABASE_URL"].startswith(("postgres://", "postgresql://")):
        problemas.append("    - DATABASE_URL debe empezar con postgresql:// o postgres://")
    if problemas:
        _abortar(["  Hay variables con un valor inválido:", *problemas])

    en_railway = any(os.getenv(v) for v in ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "RAILWAY_PROJECT_ID"))

    return Config(
        database_url=valores["DATABASE_URL"],
        wa_access_token=valores["WA_ACCESS_TOKEN"],
        wa_phone_number_id=valores["WA_PHONE_NUMBER_ID"],
        wa_waba_id=valores["WA_WABA_ID"],
        wa_verify_token=valores["WA_VERIFY_TOKEN"],
        meta_app_secret=valores["META_APP_SECRET"],
        graph_api_version=valores["GRAPH_API_VERSION"],
        session_secret=valores["SESSION_SECRET"],
        admin_email=valores["ADMIN_EMAIL"],
        admin_password_hash=valores["ADMIN_PASSWORD_HASH"],
        en_railway=en_railway,
    )


# Se valida al importar: si falta algo, el proceso termina antes de aceptar tráfico.
CFG = cargar()

# ---------------------------------------------------------------------------
# Vistas (Jinja2) y formatos
# ---------------------------------------------------------------------------
ZONA = ZoneInfo("America/Lima")


def _local(dt: datetime | None) -> datetime | None:
    return dt.astimezone(ZONA) if dt else None


def hora_corta(dt: datetime | None) -> str:
    local = _local(dt)
    return local.strftime("%H:%M") if local else ""


def dia(dt: datetime | None) -> str:
    local = _local(dt)
    if not local:
        return ""
    diferencia = (datetime.now(ZONA).date() - local.date()).days
    if diferencia == 0:
        return "Hoy"
    if diferencia == 1:
        return "Ayer"
    return local.strftime("%d/%m/%Y")


def hora_lista(dt: datetime | None) -> str:
    """Hora para listas: 14:05 si es de hoy, 'Ayer' o la fecha si es anterior."""
    etiqueta = dia(dt)
    return hora_corta(dt) if etiqueta == "Hoy" else etiqueta


def fecha_hora(dt: datetime | None) -> str:
    local = _local(dt)
    return local.strftime("%d/%m/%Y %H:%M") if local else ""


VISTAS = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
VISTAS.env.filters.update(hora=hora_corta, dia=dia, hora_lista=hora_lista, fecha_hora=fecha_hora)
VISTAS.env.globals.update(
    ESTADOS_MENSAJE={
        "sent": ("✓", "Enviado"),
        "delivered": ("✓✓", "Entregado"),
        "read": ("✓✓", "Leído"),
        "failed": ("!", "Fallido"),
    },
    ESTADOS_PLANTILLA={
        "APPROVED": ("Aprobada", "ok"),
        "PENDING": ("En revisión", "pendiente"),
        "IN_APPEAL": ("En apelación", "pendiente"),
        "REJECTED": ("Rechazada", "error"),
        "PAUSED": ("Pausada", "pendiente"),
        "DISABLED": ("Deshabilitada", "error"),
        "PENDING_DELETION": ("Por eliminar", "error"),
    },
    CATEGORIAS={"UTILITY": "Utilidad", "MARKETING": "Marketing", "AUTHENTICATION": "Autenticación"},
)
