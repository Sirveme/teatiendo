"""CRM: panel de detalles de la conversación, pipeline, contactos (con exportación CSV auditada) y ajustes."""
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app import crm, db
from app.auth import requiere_login
from app.config import ZONA
from app.panel import _avisar, _pagina, _parcial, _pool_tenant
from app.presets import ETIQUETAS_MODULO, ETIQUETAS_TIPO, MODULOS, PRESETS, TIPOS_CAMPO

router = APIRouter(dependencies=[Depends(requiere_login)])

LIMITE_PIPELINE = 500
LIMITE_TABLA = 1000
LIMITE_CSV = 10000
PESTANAS_AJUSTES = ("rubro", "embudo", "campos", "modulos")
RE_COLOR = re.compile(r"#[0-9a-fA-F]{6}")


def _usuario(request: Request) -> str:
    return request.session.get("admin") or "equipo"


def _entero(valor) -> int | None:
    try:
        return int(valor) if valor not in (None, "") else None
    except (TypeError, ValueError):
        return None


async def _contacto_whatsapp(pool, tenant_id: int, contacto_id: int):
    contacto = await db.obtener_contacto(pool, tenant_id, contacto_id)
    if not contacto or contacto["canal"] != db.CANAL_WHATSAPP:
        raise HTTPException(status_code=404, detail="Contacto no encontrado")
    return contacto


def _filtros(request: Request) -> dict:
    q = request.query_params
    return {"q": (q.get("q") or "").strip()[:100], "etapa": _entero(q.get("etapa")),
            "etiqueta": (q.get("etiqueta") or "").strip()[:30]}


# --- Panel de detalles ----------------------------------------------------------

async def _detalles(request: Request, contacto_id: int, aviso: str | None = None):
    pool, tenant_id = _pool_tenant(request)
    await _contacto_whatsapp(pool, tenant_id, contacto_id)
    return _parcial(request, "_detalles.html", **await crm.contexto_detalles(pool, tenant_id, contacto_id, aviso))


@router.get("/parcial/detalles/{contacto_id}")
async def detalles(request: Request, contacto_id: int):
    return await _detalles(request, contacto_id)


async def _mover(request: Request, contacto_id: int, form) -> str | None:
    pool, tenant_id = _pool_tenant(request)
    await _contacto_whatsapp(pool, tenant_id, contacto_id)
    etapas = await db.listar_etapas(pool, tenant_id, incluir_ocultas=True)
    etapa = next((e for e in etapas if e["id"] == _entero(form.get("etapa_id"))), None)
    motivo = str(form.get("motivo") or "").strip()[:60]
    detalle = str(form.get("motivo_detalle") or "").strip()[:200]
    if motivo and detalle:
        motivo = f"{motivo}: {detalle}"
    error = crm.validar_movimiento_humano(etapa, motivo)
    if not error:
        await db.mover_etapa(pool, tenant_id, contacto_id, etapa["id"], motivo=motivo or None, autor=_usuario(request))
    return error


@router.post("/contactos/{contacto_id}/etapa")
async def cambiar_etapa(request: Request, contacto_id: int):
    form = await request.form()
    error = await _mover(request, contacto_id, form)
    if form.get("vista") == "pipeline":
        return await _pipeline(request, error)
    return await _detalles(request, contacto_id, error or "Etapa actualizada.")


@router.post("/contactos/{contacto_id}/ficha")
async def guardar_ficha(request: Request, contacto_id: int):
    """Guarda SOLO los campos que la persona cambió; esos quedan con fuente 'humano' y la IA ya no los toca."""
    pool, tenant_id = _pool_tenant(request)
    contacto = await _contacto_whatsapp(pool, tenant_id, contacto_id)
    form = await request.form()
    ficha = contacto["ficha"] or {}
    invalidos, cambios = [], []
    for campo in await db.listar_campos(pool, tenant_id):
        if campo["clave"] not in form:
            continue
        texto = str(form.get(campo["clave"]) or "").strip()
        valor = crm.normalizar_valor(campo, texto) if texto else None
        if texto and valor is None:
            invalidos.append(campo["etiqueta"])
        elif valor != (ficha.get(campo["clave"]) or {}).get("valor"):
            cambios.append((campo["clave"], valor))
    if invalidos:  # no se guarda nada hasta corregir
        return await _detalles(request, contacto_id,
                               "No se guardó: revisa " + ", ".join(invalidos) + " (valor no válido para el tipo de campo).")
    for clave, valor in cambios:
        await db.editar_campo_humano(pool, tenant_id, contacto_id, clave, valor)
    n = len(cambios)
    return await _detalles(request, contacto_id, f"Ficha guardada ({n} cambio{'s' if n != 1 else ''})." if n else "Sin cambios.")


@router.post("/contactos/{contacto_id}/campo/liberar")
async def liberar_campo(request: Request, contacto_id: int):
    pool, tenant_id = _pool_tenant(request)
    await _contacto_whatsapp(pool, tenant_id, contacto_id)
    clave = str((await request.form()).get("clave") or "")
    await db.liberar_campo(pool, tenant_id, contacto_id, clave)
    return await _detalles(request, contacto_id, "La IA puede volver a completar ese campo.")


@router.post("/contactos/{contacto_id}/modo")
async def cambiar_modo(request: Request, contacto_id: int):
    pool, tenant_id = _pool_tenant(request)
    await _contacto_whatsapp(pool, tenant_id, contacto_id)
    modo = str((await request.form()).get("modo") or "")
    if modo not in ("ia", "humano"):
        return await _detalles(request, contacto_id, "Modo no válido.")
    await db.cambiar_modo(pool, tenant_id, contacto_id, modo)
    return await _detalles(request, contacto_id, "El asistente vuelve a responder a este contacto." if modo == "ia"
                           else "Desde ahora atiende una persona; el asistente no responde a este contacto.")


@router.post("/contactos/{contacto_id}/etiquetas")
async def agregar_etiqueta(request: Request, contacto_id: int):
    pool, tenant_id = _pool_tenant(request)
    contacto = await _contacto_whatsapp(pool, tenant_id, contacto_id)
    etiqueta = crm.normalizar_etiqueta(str((await request.form()).get("etiqueta") or ""))
    if not etiqueta:
        return await _detalles(request, contacto_id, "Escribe una etiqueta.")
    if len(contacto["etiquetas"] or []) >= crm.MAX_ETIQUETAS:
        return await _detalles(request, contacto_id, f"Máximo {crm.MAX_ETIQUETAS} etiquetas por contacto.")
    await db.agregar_etiqueta(pool, tenant_id, contacto_id, etiqueta)
    return await _detalles(request, contacto_id)


@router.post("/contactos/{contacto_id}/etiquetas/quitar")
async def quitar_etiqueta(request: Request, contacto_id: int):
    pool, tenant_id = _pool_tenant(request)
    await _contacto_whatsapp(pool, tenant_id, contacto_id)
    await db.quitar_etiqueta(pool, tenant_id, contacto_id, str((await request.form()).get("etiqueta") or ""))
    return await _detalles(request, contacto_id)


@router.post("/contactos/{contacto_id}/notas")
async def agregar_nota(request: Request, contacto_id: int):
    pool, tenant_id = _pool_tenant(request)
    await _contacto_whatsapp(pool, tenant_id, contacto_id)
    texto = str((await request.form()).get("texto") or "").strip()
    if not texto:
        return await _detalles(request, contacto_id, "Escribe la nota.")
    await db.agregar_nota(pool, tenant_id, contacto_id, _usuario(request), texto[:crm.MAX_NOTA])
    return await _detalles(request, contacto_id, "Nota guardada.")


# --- Pipeline ---------------------------------------------------------------------

async def _pipeline(request: Request, error: str | None = None):
    pool, tenant_id = _pool_tenant(request)
    tenant = await db.obtener_tenant(pool, tenant_id)
    etapas = await db.listar_etapas(pool, tenant_id)
    campos = await db.listar_campos(pool, tenant_id)
    campo_producto = next((c for c in campos if c["tipo"] == "texto"), None)
    campo_monto = next((c for c in campos if c["tipo"] == "moneda"), None)
    contactos = await db.listar_contactos(pool, tenant_id, limite=LIMITE_PIPELINE)
    columnas = []
    for etapa in etapas:
        tarjetas = []
        for c in (c for c in contactos if c["etapa_id"] == etapa["id"]):
            ficha = c["ficha"] or {}
            tarjetas.append({
                "id": c["id"], "nombre": c["nombre_perfil"] or f"+{c['wa_id']}",
                "producto": (ficha.get(campo_producto["clave"]) or {}).get("valor") if campo_producto else None,
                "monto": (ficha.get(campo_monto["clave"]) or {}).get("valor") if campo_monto else None,
                "tiempo": crm.tiempo_desde(c["etapa_desde"]), "atencion": c["modo"] == "humano" and c["derivado_en"],
                "no_leidos": c["no_leidos"],
            })
        total = sum(float(t["monto"]) for t in tarjetas if t["monto"])
        columnas.append({"etapa": etapa, "tarjetas": tarjetas, "total": total})
    return _pagina(request, "pipeline.html", "pipeline", columnas=columnas, etapas=etapas, error=error,
                   motivos=crm.motivos_perdida(tenant["rubro"]), limite=LIMITE_PIPELINE,
                   recortado=len(contactos) >= LIMITE_PIPELINE)


@router.get("/pipeline")
async def pipeline(request: Request):
    return await _pipeline(request)


# --- Contactos y CSV --------------------------------------------------------------

@router.get("/contactos")
async def contactos(request: Request):
    pool, tenant_id = _pool_tenant(request)
    f = _filtros(request)
    filas = await db.listar_contactos(pool, tenant_id, q=f["q"], etapa_id=f["etapa"], etiqueta=f["etiqueta"],
                                      limite=LIMITE_TABLA)
    campos = await db.listar_campos(pool, tenant_id)
    # En la tabla, el primer campo de cada tipo útil (p. ej. producto, monto, tipo de comprobante); el CSV lleva todos.
    visibles = []
    for tipo in ("texto", "moneda", "opcion"):
        primero = next((c for c in campos if c["tipo"] == tipo), None)
        if primero:
            visibles.append(primero)
    return _pagina(request, "contactos.html", "contactos", contactos=filas, campos=visibles, filtros=f,
                   etapas=await db.listar_etapas(pool, tenant_id),
                   etiquetas=await db.listar_etiquetas_tenant(pool, tenant_id),
                   consulta=str(request.url.query), recortado=len(filas) >= LIMITE_TABLA, limite=LIMITE_TABLA)


@router.get("/contactos.csv")
async def exportar_csv(request: Request):
    pool, tenant_id = _pool_tenant(request)
    f = _filtros(request)
    filas = await db.listar_contactos(pool, tenant_id, q=f["q"], etapa_id=f["etapa"], etiqueta=f["etiqueta"],
                                      limite=LIMITE_CSV)
    contenido = crm.generar_csv(filas, await db.listar_campos(pool, tenant_id))
    await db.registrar_exportacion(pool, tenant_id, _usuario(request),
                                   {k: v for k, v in f.items() if v not in (None, "")}, len(filas))
    nombre = f"contactos-{datetime.now(ZONA):%Y%m%d-%H%M}.csv"
    return Response(contenido.encode("utf-8"), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{nombre}"'})


# --- Ajustes ----------------------------------------------------------------------

async def _ajustes(request: Request, pestana: str, error: str | None = None):
    pool, tenant_id = _pool_tenant(request)
    tenant = await db.obtener_tenant(pool, tenant_id)
    return _pagina(request, "ajustes.html", "ajustes", pestana=pestana, error=error, tenant=tenant,
                   presets=PRESETS, etapas=await db.listar_etapas(pool, tenant_id, incluir_ocultas=True),
                   campos=await db.listar_campos(pool, tenant_id, solo_activos=False),
                   modulos=crm.modulos_de(tenant), etiquetas_modulo=ETIQUETAS_MODULO,
                   tipos=TIPOS_CAMPO, etiquetas_tipo=ETIQUETAS_TIPO)


@router.get("/ajustes")
async def ajustes(request: Request, pestana: str = "rubro"):
    return await _ajustes(request, pestana if pestana in PESTANAS_AJUSTES else "rubro")


@router.post("/ajustes/rubro")
async def aplicar_rubro(request: Request):
    pool, tenant_id = _pool_tenant(request)
    rubro = str((await request.form()).get("rubro") or "")
    if rubro not in PRESETS:
        return await _ajustes(request, "rubro", "Elige un rubro válido.")
    nuevos = await crm.aplicar_preset(pool, tenant_id, rubro)
    _avisar(request, "ok", f"Rubro «{PRESETS[rubro]['nombre']}» aplicado: {nuevos['etapas']} etapa(s) y "
                           f"{nuevos['campos']} campo(s) nuevos. No se borró nada de lo que ya tenías.")
    return RedirectResponse("/ajustes?pestana=rubro", status_code=303)


@router.post("/ajustes/etapas")
async def guardar_etapas(request: Request):
    pool, tenant_id = _pool_tenant(request)
    form = await request.form()
    etapas = await db.listar_etapas(pool, tenant_id, incluir_ocultas=True)
    cambios, errores = [], []
    for e in etapas:
        nombre = str(form.get(f"nombre_{e['id']}", e["nombre"])).strip()[:40]
        color = str(form.get(f"color_{e['id']}", e["color"])).strip()
        orden = _entero(form.get(f"orden_{e['id']}"))
        oculta = form.get(f"oculta_{e['id']}") == "1" and not e["es_perdido"]  # "Perdido" siempre visible
        if not nombre:
            errores.append("Cada etapa necesita un nombre.")
        elif not RE_COLOR.fullmatch(color):
            errores.append(f"Color no válido en «{nombre}».")
        cambios.append((e["id"], nombre, color, orden if orden is not None else e["orden"], oculta))
    if errores:
        return await _ajustes(request, "embudo", " ".join(dict.fromkeys(errores)))
    for etapa_id, nombre, color, orden, oculta in cambios:
        await db.actualizar_etapa(pool, tenant_id, etapa_id, nombre=nombre, color=color, orden=orden, oculta=oculta)
    _avisar(request, "ok", "Embudo actualizado.")
    return RedirectResponse("/ajustes?pestana=embudo", status_code=303)


def _opciones(texto: str) -> list[str]:
    return list(dict.fromkeys(o.strip()[:40] for o in (texto or "").split(",") if o.strip()))[:20]


@router.post("/ajustes/campos")
async def guardar_campos(request: Request):
    pool, tenant_id = _pool_tenant(request)
    form = await request.form()
    campos = await db.listar_campos(pool, tenant_id, solo_activos=False)
    for c in campos:
        etiqueta = str(form.get(f"etiqueta_{c['id']}", c["etiqueta"])).strip()[:60] or c["etiqueta"]
        opciones = _opciones(str(form.get(f"opciones_{c['id']}", ""))) if c["tipo"] == "opcion" else []
        if c["tipo"] == "opcion" and len(opciones) < 2:
            return await _ajustes(request, "campos", f"«{etiqueta}» necesita al menos dos opciones separadas por comas.")
        await db.actualizar_campo(pool, tenant_id, c["id"], etiqueta=etiqueta,
                                  descripcion_ia=str(form.get(f"descripcion_{c['id']}", "")).strip()[:300],
                                  opciones=opciones, activo=form.get(f"activo_{c['id']}") == "1")

    nueva = str(form.get("nueva_etiqueta") or "").strip()[:60]
    if nueva:
        tipo = str(form.get("nuevo_tipo") or "texto")
        opciones = _opciones(str(form.get("nuevas_opciones") or ""))
        if tipo not in TIPOS_CAMPO:
            return await _ajustes(request, "campos", "Tipo de campo no válido.")
        if tipo == "opcion" and len(opciones) < 2:
            return await _ajustes(request, "campos", "Un campo de opciones necesita al menos dos opciones separadas por comas.")
        clave = crm.clave_desde_etiqueta(nueva)
        agregado = await db.agregar_campo(pool, tenant_id, clave=clave, etiqueta=nueva, tipo=tipo,
                                          opciones=opciones if tipo == "opcion" else [],
                                          descripcion_ia=str(form.get("nueva_descripcion") or "").strip()[:300])
        if not agregado:
            return await _ajustes(request, "campos", f"Ya existe un campo con la clave «{clave}».")
    _avisar(request, "ok", "Campos de la ficha guardados.")
    return RedirectResponse("/ajustes?pestana=campos", status_code=303)


@router.post("/ajustes/modulos")
async def guardar_modulos(request: Request):
    pool, tenant_id = _pool_tenant(request)
    form = await request.form()
    await db.actualizar_modulos(pool, tenant_id, {m: form.get(m) == "1" for m in MODULOS})
    _avisar(request, "ok", "Módulos guardados.")
    return RedirectResponse("/ajustes?pestana=modulos", status_code=303)
