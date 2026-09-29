"""Cliente de la WhatsApp Cloud API (Graph API de Meta) y utilidades de plantillas."""
import logging
import re

import httpx

from app.config import CFG

log = logging.getLogger("teatiendo.meta")

IDIOMA = "es"
RE_VARIABLE = re.compile(r"\{\{\s*(\d+)\s*\}\}")
RE_NOMBRE = re.compile(r"[a-z0-9_]{1,512}")

_cliente: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _cliente
    if _cliente is None:
        _cliente = httpx.AsyncClient(
            base_url=f"https://graph.facebook.com/{CFG.graph_api_version}/",
            headers={"Authorization": f"Bearer {CFG.wa_access_token}"},
            timeout=httpx.Timeout(20.0),
        )
    return _cliente


async def cerrar() -> None:
    global _cliente
    if _cliente is not None:
        await _cliente.aclose()
        _cliente = None


async def _llamar(metodo: str, ruta: str, **kwargs) -> tuple[bool, dict]:
    """Devuelve (ok, datos). Nunca lanza excepción: los errores vuelven con el formato de Meta."""
    try:
        r = await _http().request(metodo, ruta, **kwargs)
    except httpx.HTTPError as e:
        log.warning("Sin conexión con Meta (%s %s): %s", metodo, ruta, e)
        return False, {"error": {"message": f"No se pudo conectar con Meta ({e.__class__.__name__}).", "code": "red"}}
    try:
        datos = r.json()
    except ValueError:
        datos = {"error": {"message": f"Respuesta no válida de Meta (HTTP {r.status_code}).", "code": r.status_code}}
    ok = r.is_success and "error" not in datos
    if not ok:
        log.warning("Meta respondió error en %s %s: %s", metodo, ruta, datos)
    return ok, datos


# --- Mensajes ----------------------------------------------------------------

async def enviar_texto(phone_number_id: str, destino: str, texto: str) -> tuple[bool, dict]:
    return await _llamar("POST", f"{phone_number_id}/messages", json={
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": destino,
        "type": "text",
        "text": {"preview_url": False, "body": texto},
    })


async def enviar_plantilla(phone_number_id: str, destino: str, nombre: str, idioma: str,
                           valores: list[str]) -> tuple[bool, dict]:
    plantilla: dict = {"name": nombre, "language": {"code": idioma}}
    if valores:
        plantilla["components"] = [
            {"type": "body", "parameters": [{"type": "text", "text": v} for v in valores]}
        ]
    return await _llamar("POST", f"{phone_number_id}/messages", json={
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": destino,
        "type": "template",
        "template": plantilla,
    })


def wamid_de(datos: dict) -> str | None:
    mensajes = datos.get("messages") or [{}]
    return mensajes[0].get("id")


def wa_id_de(datos: dict) -> str | None:
    contactos = datos.get("contacts") or [{}]
    return contactos[0].get("wa_id")


# --- Plantillas --------------------------------------------------------------

async def crear_plantilla(waba_id: str, nombre: str, categoria: str, cuerpo: str,
                          ejemplos: list[str]) -> tuple[bool, dict]:
    componente: dict = {"type": "BODY", "text": cuerpo}
    if ejemplos:
        componente["example"] = {"body_text": [ejemplos]}
    return await _llamar("POST", f"{waba_id}/message_templates", json={
        "name": nombre,
        "language": IDIOMA,
        "category": categoria,
        "components": [componente],
    })


async def listar_plantillas(waba_id: str) -> tuple[bool, list | dict]:
    """Descarga todas las plantillas de la WABA siguiendo la paginación."""
    plantillas: list = []
    url: str | None = f"{waba_id}/message_templates"
    params: dict | None = {"fields": "id,name,language,category,status,rejected_reason,components", "limit": 100}
    for _ in range(20):
        ok, datos = await _llamar("GET", url, params=params)
        if not ok:
            return False, datos
        plantillas.extend(datos.get("data", []))
        url = (datos.get("paging") or {}).get("next")
        params = None  # la URL "next" ya trae los parámetros
        if not url:
            break
    return True, plantillas


def cuerpo_de(plantilla_meta: dict) -> str:
    for componente in plantilla_meta.get("components", []):
        if componente.get("type") == "BODY":
            return componente.get("text", "")
    return ""


def contar_variables(cuerpo: str | None) -> int:
    numeros = [int(n) for n in RE_VARIABLE.findall(cuerpo or "")]
    return max(numeros, default=0)


def renderizar(cuerpo: str, valores: list[str]) -> str:
    def reemplazo(m: re.Match) -> str:
        i = int(m.group(1)) - 1
        return valores[i] if 0 <= i < len(valores) else m.group(0)
    return RE_VARIABLE.sub(reemplazo, cuerpo)


def validar_nombre(nombre: str) -> str | None:
    if not RE_NOMBRE.fullmatch(nombre):
        return "El nombre solo puede tener letras minúsculas, números y guion bajo (ej.: confirmacion_pedido)."
    return None


def validar_cuerpo(cuerpo: str) -> str | None:
    if not cuerpo:
        return "El cuerpo de la plantilla no puede estar vacío."
    if len(cuerpo) > 1024:
        return "El cuerpo no puede superar los 1024 caracteres."
    numeros = sorted({int(n) for n in RE_VARIABLE.findall(cuerpo)})
    if numeros and numeros != list(range(1, len(numeros) + 1)):
        return "Las variables deben ser consecutivas y empezar en {{1}} (por ejemplo: {{1}}, {{2}}, {{3}})."
    if re.match(r"^\s*\{\{", cuerpo) or re.search(r"\}\}\s*$", cuerpo):
        return "Meta no permite que el cuerpo empiece ni termine con una variable. Agrega texto antes o después."
    return None


def normalizar_numero(texto: str) -> str | None:
    """Deja solo dígitos. Nueve dígitos que empiezan con 9 se asumen celulares de Perú (+51)."""
    digitos = re.sub(r"\D", "", texto or "")
    if len(digitos) == 9 and digitos.startswith("9"):
        digitos = "51" + digitos
    return digitos if 8 <= len(digitos) <= 15 else None


# --- Errores legibles --------------------------------------------------------

SUGERENCIAS = {
    10: "La app no tiene el permiso necesario. Revisa los permisos del token.",
    100: "Algún parámetro de la solicitud no es válido.",
    190: "El token de acceso expiró o no es válido. Actualiza WA_ACCESS_TOKEN.",
    200: "La app no tiene el permiso necesario. Revisa los permisos del token.",
    368: "La cuenta está restringida temporalmente por incumplir políticas.",
    131009: "Uno de los valores enviados no es válido.",
    131026: "No se pudo entregar: el número no tiene WhatsApp o no aceptó las condiciones vigentes.",
    131030: "El número no está en la lista de destinatarios permitidos (la app está en modo desarrollo).",
    131031: "La cuenta de WhatsApp Business está bloqueada.",
    131042: "Hay un problema con el método de pago de la cuenta de WhatsApp Business.",
    131047: "Pasaron más de 24 horas desde el último mensaje del cliente. Envía una plantilla aprobada.",
    131049: "Meta no entregó el mensaje para cuidar la experiencia del usuario (límite de marketing).",
    131056: "Demasiados mensajes seguidos a este número. Espera unos minutos.",
    132000: "La cantidad de variables no coincide con la plantilla.",
    132001: "La plantilla no existe en ese idioma o todavía no está aprobada.",
    132005: "El texto de la plantilla con los valores reemplazados es demasiado largo.",
    132012: "El formato de los valores no coincide con lo que espera la plantilla.",
}


def error_legible(error) -> str:
    """Convierte un error de Meta (respuesta de envío o 'errors' de un status) en texto para el usuario."""
    if not error:
        return ""
    items = error if isinstance(error, list) else [error.get("error", error) if isinstance(error, dict) else error]
    partes = []
    for e in items:
        if not isinstance(e, dict):
            partes.append(str(e))
            continue
        codigo = e.get("code")
        titulo = e.get("error_user_title")
        mensaje = e.get("error_user_msg") or e.get("message") or e.get("title") or "Error desconocido"
        detalle = (e.get("error_data") or {}).get("details") if isinstance(e.get("error_data"), dict) else None

        texto = f"Error {codigo}: " if codigo not in (None, "red") else ""
        texto += f"{titulo}. {mensaje}" if titulo and titulo not in mensaje else mensaje
        if detalle and detalle not in texto:
            texto += f" ({detalle})"
        sugerencia = SUGERENCIAS.get(codigo) if isinstance(codigo, int) else None
        if sugerencia:
            texto += f" — {sugerencia}"
        partes.append(texto)
    return " | ".join(partes)
