"""Webhook de WhatsApp: verificación de Meta, validación de firma y procesamiento de eventos."""
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import PlainTextResponse

from app import asistente, db, extraccion
from app.config import CFG

log = logging.getLogger("teatiendo.webhook")
router = APIRouter()

ETIQUETAS_MEDIA = {
    "image": "Imagen", "audio": "Audio", "video": "Video", "document": "Documento",
    "sticker": "Sticker", "location": "Ubicación", "contacts": "Contacto",
}


@router.get("/webhook")
async def verificar(request: Request):
    q = request.query_params
    token = q.get("hub.verify_token", "")
    if q.get("hub.mode") == "subscribe" and hmac.compare_digest(token.encode(), CFG.wa_verify_token.encode()):
        log.info("Webhook verificado por Meta")
        return PlainTextResponse(q.get("hub.challenge", ""))
    log.warning("Verificación de webhook rechazada (token incorrecto)")
    return PlainTextResponse("Forbidden", status_code=403)


def _firma_valida(cuerpo: bytes, firma: str) -> bool:
    esperada = "sha256=" + hmac.new(CFG.meta_app_secret.encode(), cuerpo, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperada, firma or "")


@router.post("/webhook")
async def recibir(request: Request, tareas: BackgroundTasks):
    cuerpo = await request.body()  # cuerpo CRUDO: la firma se calcula sobre los bytes exactos
    if not _firma_valida(cuerpo, request.headers.get("x-hub-signature-256", "")):
        log.warning("Webhook con firma inválida desde %s", request.client.host if request.client else "?")
        return PlainTextResponse("Forbidden", status_code=403)

    try:
        payload = json.loads(cuerpo)
    except ValueError:
        return PlainTextResponse("JSON inválido", status_code=400)

    pool = request.app.state.pool
    await db.guardar_evento(pool, payload)
    tareas.add_task(procesar, pool, payload)
    return PlainTextResponse("EVENT_RECEIVED")


def _extraer_contenido(msg: dict) -> tuple[str, str]:
    tipo = msg.get("type", "desconocido")
    if tipo == "text":
        return tipo, (msg.get("text") or {}).get("body", "")
    if tipo == "button":
        return tipo, (msg.get("button") or {}).get("text", "")
    if tipo == "interactive":
        i = msg.get("interactive") or {}
        respuesta = i.get("button_reply") or i.get("list_reply") or {}
        return tipo, respuesta.get("title", "[Respuesta interactiva]")
    if tipo == "reaction":
        emoji = (msg.get("reaction") or {}).get("emoji")
        return tipo, f"[Reacción {emoji}]" if emoji else "[Reacción eliminada]"
    if tipo in ETIQUETAS_MEDIA:
        datos = msg.get(tipo)
        leyenda = datos.get("caption") if isinstance(datos, dict) else None
        return tipo, f"[{ETIQUETAS_MEDIA[tipo]}]" + (f" {leyenda}" if leyenda else "")
    return tipo, "[Mensaje no compatible]"


async def procesar(pool, payload: dict) -> None:
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                if change.get("field") != "messages":
                    continue
                valor = change.get("value") or {}
                phone_number_id = (valor.get("metadata") or {}).get("phone_number_id")
                tenant_id = await db.tenant_por_phone(pool, phone_number_id)
                if not tenant_id:
                    log.warning("Evento para un phone_number_id desconocido: %s", phone_number_id)
                    continue

                nombres = {
                    c.get("wa_id"): (c.get("profile") or {}).get("name")
                    for c in valor.get("contacts", [])
                }
                ids_meta = {c.get("wa_id"): c.get("user_id") for c in valor.get("contacts", []) if c.get("user_id")}
                for msg in valor.get("messages", []):
                    await _procesar_mensaje(pool, tenant_id, msg, nombres, ids_meta)
                for status in valor.get("statuses", []):
                    await _procesar_status(pool, tenant_id, status)
                for error in valor.get("errors", []):
                    log.warning("Error reportado por Meta en el webhook: %s", error)
    except Exception:
        log.exception("Error procesando un evento del webhook (el payload quedó guardado en webhook_events)")


CAMPOS_REFERRAL = ("source_id", "source_type", "source_url", "headline", "body", "media_type", "image_url",
                   "video_url", "thumbnail_url", "ctwa_clid")


def origen_de_referral(msg: dict) -> dict | None:
    """Datos del anuncio con clic a WhatsApp ("referral"), solo las claves conocidas y con texto."""
    referral = msg.get("referral")
    if not isinstance(referral, dict):
        return None
    datos = {k: str(referral[k])[:1000] for k in CAMPOS_REFERRAL if referral.get(k)}
    return datos or None


async def _procesar_mensaje(pool, tenant_id: int, msg: dict, nombres: dict, ids_meta: dict | None = None) -> None:
    wa_id = msg.get("from")
    wamid = msg.get("id")
    if not wamid:
        return
    if not wa_id:
        # Usuarios con nombre de usuario pueden llegar sin teléfono (solo from_user_id). Aún no se soporta enviarles.
        log.warning("Mensaje %s sin número de teléfono (from_user_id=%s): se omite", wamid, msg.get("from_user_id"))
        return
    meta_user_id = msg.get("from_user_id") or (ids_meta or {}).get(wa_id)
    marca = int(msg.get("timestamp") or 0)
    recibido_en = datetime.fromtimestamp(marca, tz=timezone.utc) if marca else datetime.now(timezone.utc)
    contacto_id = await db.upsert_contacto_entrante(pool, tenant_id, wa_id, nombres.get(wa_id), recibido_en,
                                                    canal=db.CANAL_WHATSAPP, meta_user_id=meta_user_id)
    tipo, texto = _extraer_contenido(msg)
    nuevo = await db.insertar_mensaje(
        pool, tenant_id, contacto_id, wamid=wamid, direccion="in", tipo=tipo,
        texto=texto, estado=None, creado_en=recibido_en, canal=db.CANAL_WHATSAPP,
    )
    if nuevo is None:
        log.info("Mensaje %s ya registrado; se ignora el duplicado", wamid)
        return
    origen = origen_de_referral(msg)
    if origen:
        await db.guardar_origen_anuncio(pool, tenant_id, contacto_id, origen)  # solo el primero
    try:
        await asistente.al_recibir_whatsapp(pool, tenant_id, contacto_id, nuevo, tipo)
    except Exception:
        log.exception("El asistente falló al procesar el mensaje %s (el mensaje quedó guardado)", wamid)
    try:
        await extraccion.al_recibir_whatsapp(pool, tenant_id, contacto_id, nuevo)
    except Exception:
        log.exception("La extracción de ficha falló para el mensaje %s (la ficha quedó como estaba)", wamid)


async def _procesar_status(pool, tenant_id: int, status: dict) -> None:
    wamid = status.get("id")
    estado = status.get("status")
    if not wamid or estado not in ("sent", "delivered", "read", "failed"):
        return
    await db.actualizar_estado(pool, tenant_id, wamid, estado, status.get("errors"))
