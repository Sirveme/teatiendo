"""Panel de administración: bandeja, conversación, nuevo mensaje y plantillas."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import db, meta
from app.auth import requiere_login
from app.config import VISTAS, hora_corta

router = APIRouter(dependencies=[Depends(requiere_login)])

VENTANA_24H = timedelta(hours=24)
CATEGORIAS_VALIDAS = ("UTILITY", "MARKETING")
ESTADOS_EN_REVISION = ("PENDING", "IN_APPEAL")


# --- Utilidades --------------------------------------------------------------

def _pagina(request: Request, plantilla: str, seccion: str, **ctx):
    """Página completa: consume el aviso flash de la sesión."""
    ctx.update(usuario=request.session.get("admin"), seccion=seccion, aviso=request.session.pop("aviso", None))
    return VISTAS.TemplateResponse(request, plantilla, ctx)


def _parcial(request: Request, plantilla: str, **ctx):
    """Fragmento HTMX: no toca el aviso flash (el refresco periódico no debe consumirlo)."""
    return VISTAS.TemplateResponse(request, plantilla, ctx)


def _avisar(request: Request, tipo: str, texto: str) -> None:
    request.session["aviso"] = {"tipo": tipo, "texto": texto}


def _pool_tenant(request: Request):
    return request.app.state.pool, request.app.state.tenant_id


def _ventana(contacto) -> dict:
    ultimo = contacto["ultimo_mensaje_entrante_en"]
    if not ultimo:
        return {"abierta": False, "texto": "Sin mensajes del cliente"}
    restante = ultimo + VENTANA_24H - datetime.now(timezone.utc)
    if restante.total_seconds() <= 0:
        return {"abierta": False, "texto": "Ventana de 24 h cerrada"}
    horas, minutos = divmod(int(restante.total_seconds()) // 60, 60)
    return {"abierta": True, "texto": f"Ventana abierta · quedan {horas} h {minutos} min"}


async def _plantillas_aprobadas(pool, tenant_id: int) -> list[dict]:
    return [
        {**dict(p), "vars": meta.contar_variables(p["cuerpo"])}
        for p in await db.listar_plantillas(pool, tenant_id, solo_aprobadas=True)
    ]


async def _contacto_o_404(pool, tenant_id: int, contacto_id: int):
    contacto = await db.obtener_contacto(pool, tenant_id, contacto_id)
    if not contacto:
        raise HTTPException(status_code=404, detail="Conversación no encontrada")
    return contacto


async def _ctx_conversacion(request: Request, contacto_id: int, aviso_conv: str | None = None) -> dict:
    pool, tenant_id = _pool_tenant(request)
    contacto = await _contacto_o_404(pool, tenant_id, contacto_id)
    mensajes = [
        {**dict(m), "error_texto": meta.error_legible(m["error_json"])}
        for m in await db.listar_mensajes(pool, tenant_id, contacto_id)
    ]
    return {
        "contacto": contacto,
        "mensajes": mensajes,
        "ventana": _ventana(contacto),
        "plantillas": await _plantillas_aprobadas(pool, tenant_id),
        "aviso_conv": aviso_conv,
    }


async def _plantilla_y_valores(pool, tenant_id: int, form) -> tuple:
    """Valida la plantilla elegida (debe estar APROBADA) y los valores de sus variables."""
    try:
        plantilla_id = int(form.get("plantilla_id") or 0)
    except ValueError:
        plantilla_id = 0
    plantilla = await db.obtener_plantilla(pool, tenant_id, plantilla_id) if plantilla_id else None
    if not plantilla or plantilla["estado_meta"] != "APPROVED":
        return None, [], "Elige una plantilla aprobada."
    total = meta.contar_variables(plantilla["cuerpo"])
    valores = [str(form.get(f"var_{i}", "")).strip() for i in range(1, total + 1)]
    if any(not v for v in valores):
        return plantilla, valores, "Completa el valor de todas las variables de la plantilla."
    return plantilla, valores, None


async def _enviar_y_registrar_plantilla(pool, tenant_id: int, destino: str, plantilla, valores: list[str]):
    """Envía la plantilla, registra el mensaje (enviado o fallido) y devuelve (ok, datos, contacto_id)."""
    tenant = await db.obtener_tenant(pool, tenant_id)
    ok, datos = await meta.enviar_plantilla(tenant["phone_number_id"], destino, plantilla["nombre"],
                                            plantilla["idioma"], valores)
    wa_id = (meta.wa_id_de(datos) if ok else None) or destino
    contacto_id = await db.upsert_contacto(pool, tenant_id, wa_id)
    await db.insertar_mensaje(
        pool, tenant_id, contacto_id,
        wamid=meta.wamid_de(datos) if ok else None, direccion="out", tipo="template",
        texto=meta.renderizar(plantilla["cuerpo"], valores),
        estado="sent" if ok else "failed", error_json=None if ok else datos,
    )
    return ok, datos, contacto_id


# --- Bandeja y conversación --------------------------------------------------

@router.get("/")
async def inicio():
    return RedirectResponse("/bandeja", status_code=303)


@router.get("/bandeja")
async def bandeja(request: Request):
    pool, tenant_id = _pool_tenant(request)
    return _pagina(request, "bandeja.html", "bandeja",
                   contactos=await db.listar_contactos(pool, tenant_id), contacto=None)


@router.get("/bandeja/{contacto_id}")
async def conversacion(request: Request, contacto_id: int):
    pool, tenant_id = _pool_tenant(request)
    ctx = await _ctx_conversacion(request, contacto_id)
    return _pagina(request, "bandeja.html", "bandeja", contactos=await db.listar_contactos(pool, tenant_id), **ctx)


@router.get("/parcial/conversacion/{contacto_id}")
async def conversacion_parcial(request: Request, contacto_id: int):
    return _parcial(request, "_conversacion.html", **await _ctx_conversacion(request, contacto_id))


@router.post("/bandeja/{contacto_id}/responder")
async def responder(request: Request, contacto_id: int, texto: str = Form("")):
    pool, tenant_id = _pool_tenant(request)
    contacto = await _contacto_o_404(pool, tenant_id, contacto_id)
    texto = texto.strip()
    aviso = None

    if not texto:
        aviso = "Escribe un mensaje antes de enviar."
    elif len(texto) > 4096:
        aviso = "El mensaje supera los 4096 caracteres que permite WhatsApp."
    elif not _ventana(contacto)["abierta"]:
        # Regla de 24 horas aplicada en el servidor, no solo en la interfaz.
        aviso = ("No se envió: pasaron más de 24 horas desde el último mensaje del cliente. "
                 "Usa una plantilla aprobada.")
    else:
        tenant = await db.obtener_tenant(pool, tenant_id)
        ok, datos = await meta.enviar_texto(tenant["phone_number_id"], contacto["wa_id"], texto)
        await db.insertar_mensaje(
            pool, tenant_id, contacto_id,
            wamid=meta.wamid_de(datos) if ok else None, direccion="out", tipo="text", texto=texto,
            estado="sent" if ok else "failed", error_json=None if ok else datos,
        )
    return _parcial(request, "_conversacion.html", **await _ctx_conversacion(request, contacto_id, aviso))


@router.post("/bandeja/{contacto_id}/plantilla")
async def responder_con_plantilla(request: Request, contacto_id: int):
    pool, tenant_id = _pool_tenant(request)
    contacto = await _contacto_o_404(pool, tenant_id, contacto_id)
    plantilla, valores, aviso = await _plantilla_y_valores(pool, tenant_id, await request.form())
    if not aviso:
        await _enviar_y_registrar_plantilla(pool, tenant_id, contacto["wa_id"], plantilla, valores)
    return _parcial(request, "_conversacion.html", **await _ctx_conversacion(request, contacto_id, aviso))


# --- Nuevo mensaje -----------------------------------------------------------

@router.get("/nuevo")
async def nuevo(request: Request):
    pool, tenant_id = _pool_tenant(request)
    return _pagina(request, "nuevo.html", "nuevo", plantillas=await _plantillas_aprobadas(pool, tenant_id),
                   numero="", plantilla_id=None, valores=[], error=None)


@router.post("/nuevo")
async def nuevo_enviar(request: Request):
    pool, tenant_id = _pool_tenant(request)
    form = await request.form()
    numero_txt = str(form.get("numero", "")).strip()
    numero = meta.normalizar_numero(numero_txt)
    plantilla, valores, error = await _plantilla_y_valores(pool, tenant_id, form)
    if not numero:
        error = "Número inválido. Escríbelo con código de país, por ejemplo: 51987654321."

    if not error:
        ok, datos, contacto_id = await _enviar_y_registrar_plantilla(pool, tenant_id, numero, plantilla, valores)
        if ok:
            _avisar(request, "ok", f"Plantilla «{plantilla['nombre']}» enviada a +{numero}.")
            return RedirectResponse(f"/bandeja/{contacto_id}", status_code=303)
        error = "Meta rechazó el envío. " + meta.error_legible(datos)

    return _pagina(request, "nuevo.html", "nuevo", plantillas=await _plantillas_aprobadas(pool, tenant_id),
                   numero=numero_txt, plantilla_id=plantilla["id"] if plantilla else None,
                   valores=valores, error=error)


# --- Plantillas --------------------------------------------------------------

async def _ctx_lista(request: Request, sync_info: str | None = None, sync_error: str | None = None) -> dict:
    pool, tenant_id = _pool_tenant(request)
    lista = await db.listar_plantillas(pool, tenant_id)
    return {
        "lista": lista,
        "hay_en_revision": any(p["estado_meta"] in ESTADOS_EN_REVISION for p in lista),
        "sync_info": sync_info,
        "sync_error": sync_error,
    }


@router.get("/plantillas")
async def plantillas(request: Request):
    return _pagina(request, "plantillas.html", "plantillas", **await _ctx_lista(request),
                   form={"categoria": "UTILITY", "ejemplos": []}, error=None)


@router.post("/plantillas")
async def crear_plantilla(request: Request):
    pool, tenant_id = _pool_tenant(request)
    form = await request.form()
    nombre = str(form.get("nombre", "")).strip()
    categoria = str(form.get("categoria", "")).strip().upper()
    cuerpo = str(form.get("cuerpo", "")).strip()
    ejemplos = [str(form.get(f"ejemplo_{i}", "")).strip() for i in range(1, meta.contar_variables(cuerpo) + 1)]

    error = (
        meta.validar_nombre(nombre)
        or (None if categoria in CATEGORIAS_VALIDAS else "Elige la categoría: Utilidad o Marketing.")
        or meta.validar_cuerpo(cuerpo)
        or ("Escribe un ejemplo para cada variable: Meta lo exige para revisar la plantilla."
            if any(not e for e in ejemplos) else None)
    )
    if not error:
        tenant = await db.obtener_tenant(pool, tenant_id)
        ok, datos = await meta.crear_plantilla(tenant["waba_id"], nombre, categoria, cuerpo, ejemplos)
        if ok:
            await db.upsert_plantilla(
                pool, tenant_id, nombre=nombre, idioma=meta.IDIOMA, categoria=datos.get("category", categoria),
                cuerpo=cuerpo, estado_meta=datos.get("status", "PENDING"),
                meta_template_id=datos.get("id"), motivo_rechazo=None,
            )
            _avisar(request, "ok", f"Plantilla «{nombre}» enviada a revisión de Meta. "
                                   f"Estado actual: {datos.get('status', 'PENDING')}.")
            return RedirectResponse("/plantillas", status_code=303)
        error = "Meta no aceptó la plantilla. " + meta.error_legible(datos)

    return _pagina(request, "plantillas.html", "plantillas", **await _ctx_lista(request),
                   form={"nombre": nombre, "categoria": categoria, "cuerpo": cuerpo, "ejemplos": ejemplos},
                   error=error)


@router.post("/plantillas/sincronizar")
async def sincronizar_plantillas(request: Request):
    pool, tenant_id = _pool_tenant(request)
    tenant = await db.obtener_tenant(pool, tenant_id)
    ok, datos = await meta.listar_plantillas(tenant["waba_id"])
    if not ok:
        return _parcial(request, "_plantillas_lista.html",
                        **await _ctx_lista(request, sync_error="No se pudo sincronizar. " + meta.error_legible(datos)))

    for t in datos:
        motivo = t.get("rejected_reason")
        await db.upsert_plantilla(
            pool, tenant_id, nombre=t.get("name", ""), idioma=t.get("language", meta.IDIOMA),
            categoria=t.get("category", ""), cuerpo=meta.cuerpo_de(t), estado_meta=t.get("status", "PENDING"),
            meta_template_id=t.get("id"), motivo_rechazo=None if motivo in (None, "", "NONE") else motivo,
        )
    info = f"Sincronizado con Meta a las {hora_corta(datetime.now(timezone.utc))} · {len(datos)} plantilla(s)"
    return _parcial(request, "_plantillas_lista.html", **await _ctx_lista(request, sync_info=info))
