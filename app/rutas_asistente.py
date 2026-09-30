"""Pantalla Asistente: simulador (Probar), base de conocimiento, configuración y uso del mes."""
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from app import asistente, db, ia
from app.auth import requiere_login
from app.config import ZONA
from app.panel import _avisar, _pagina, _parcial, _pool_tenant

router = APIRouter(dependencies=[Depends(requiere_login)])

PESTANAS = ("probar", "conocimiento", "configuracion", "uso")
MAX_MENSAJE_SIMULADOR = 1000
TIPOS_SIMULADOS = {"text": None, "audio": "[Audio] (simulado)", "image": "[Imagen] (simulada)"}
LIMITES_CONFIG = {"nombre_negocio": 120, "nombre_asistente": 60, "horario_humano": 200,
                  "mensaje_derivacion": 600, "mensaje_no_texto": 600}
AYUDAS_SECCION = {
    "sobre_negocio": "Qué es tu negocio, a qué se dedica y qué lo diferencia.",
    "servicios_precios": "Cada servicio o producto con su precio. Si un precio varía, dilo así.",
    "horarios_contacto": "Días y horas de atención, dirección, referencias y medios de contacto.",
    "preguntas_frecuentes": "Las preguntas que más te hacen, con su respuesta.",
    "reglas": "Lo que el asistente NUNCA debe hacer (por ejemplo: dar diagnósticos o confirmar reservas).",
}


def _inicio_mes() -> datetime:
    return datetime.now(ZONA).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


async def _resumen_mes(pool, tenant_id: int) -> dict:
    filas = await db.resumen_uso(pool, tenant_id, _inicio_mes())
    return {
        "por_origen": {f["origen"]: f for f in filas},
        "consultas": sum(f["consultas"] for f in filas),
        "errores": sum(f["errores"] for f in filas),
        "costo": sum(f["costo"] for f in filas),
        "sin_precio": sum(f["sin_precio"] for f in filas),
        "tokens": sum(f["entrada"] + f["salida"] + f["cache"] for f in filas),
    }


async def _ctx_simulador(pool, tenant_id: int, respuesta=None, error_form: str | None = None) -> dict:
    contacto_id = await db.contacto_simulador(pool, tenant_id)
    mensajes = await db.listar_mensajes(pool, tenant_id, contacto_id, 100)
    cfg = await asistente.cargar_asistente(pool, tenant_id)
    return {
        "cfg": cfg,
        "sim_contacto": await db.obtener_contacto(pool, tenant_id, contacto_id),
        "sim_mensajes": mensajes,
        "usos": await db.uso_por_mensajes(pool, tenant_id, [m["id"] for m in mensajes if m["generado_por_ia"]]),
        "nivel_actual": ia.CONFIG.niveles[cfg["nivel"]],
        "respuesta": respuesta,
        "error_form": error_form,
    }


async def _pantalla(request: Request, pestana: str, cfg_formulario: dict | None = None, error: str | None = None):
    pool, tenant_id = _pool_tenant(request)
    cfg = await asistente.cargar_asistente(pool, tenant_id)
    if cfg_formulario:
        cfg = {**cfg, **cfg_formulario}
    ctx = {
        "pestana": pestana,
        "cfg": cfg,
        "error": error,
        "niveles": ia.CONFIG.niveles,
        "config_ia": ia.CONFIG,
        "secciones": asistente.SECCIONES,
        "ayudas": AYUDAS_SECCION,
        "ejemplo": asistente.EJEMPLO_CLINICA,
        "max_seccion": asistente.MAX_SECCION,
        "tokens_base": asistente.estimar_tokens(asistente.construir_sistema(cfg, datetime.now(ZONA)).estable),
        "vista_derivacion": asistente.mensaje_derivacion(cfg),
        "resumen": await _resumen_mes(pool, tenant_id),
        "nombre_mes": f"{asistente.MESES[_inicio_mes().month - 1]} {_inicio_mes().year}",
    }
    if pestana == "probar":
        ctx.update(await _ctx_simulador(pool, tenant_id))
        ctx["cfg"] = cfg
    elif pestana == "uso":
        ctx["ultimos"] = await db.ultimos_usos(pool, tenant_id)
    return _pagina(request, "asistente.html", "asistente", **ctx)


@router.get("/asistente")
async def pantalla(request: Request, pestana: str = "probar"):
    return await _pantalla(request, pestana if pestana in PESTANAS else "probar")


# --- Simulador ---------------------------------------------------------------

@router.post("/asistente/probar")
async def probar(request: Request):
    pool, tenant_id = _pool_tenant(request)
    form = await request.form()
    tipo = str(form.get("tipo") or "text")
    tipo = tipo if tipo in TIPOS_SIMULADOS else "text"
    texto = str(form.get("texto", "")).strip() if tipo == "text" else TIPOS_SIMULADOS[tipo]
    if not texto:
        return _parcial(request, "_simulador.html", **await _ctx_simulador(pool, tenant_id, error_form="Escribe un mensaje."))
    if len(texto) > MAX_MENSAJE_SIMULADOR:
        return _parcial(request, "_simulador.html", **await _ctx_simulador(
            pool, tenant_id, error_form=f"El mensaje no puede superar los {MAX_MENSAJE_SIMULADOR} caracteres."))

    contacto_id = await db.contacto_simulador(pool, tenant_id)
    await db.insertar_mensaje(pool, tenant_id, contacto_id, wamid=None, direccion="in", tipo=tipo, texto=texto,
                              estado=None, canal=db.CANAL_WEB)
    respuesta = await asistente.atender(pool, tenant_id, contacto_id, tipo=tipo, origen="simulador", canal=db.CANAL_WEB)
    return _parcial(request, "_simulador.html", **await _ctx_simulador(pool, tenant_id, respuesta))


@router.post("/asistente/probar/reiniciar")
async def reiniciar(request: Request):
    pool, tenant_id = _pool_tenant(request)
    contacto_id = await db.contacto_simulador(pool, tenant_id)
    await db.borrar_mensajes_contacto(pool, tenant_id, contacto_id)  # el uso medido se conserva
    await db.cambiar_modo(pool, tenant_id, contacto_id, "ia")
    return _parcial(request, "_simulador.html", **await _ctx_simulador(pool, tenant_id))


@router.post("/asistente/probar/devolver")
async def devolver_simulador(request: Request):
    pool, tenant_id = _pool_tenant(request)
    await db.cambiar_modo(pool, tenant_id, await db.contacto_simulador(pool, tenant_id), "ia")
    return _parcial(request, "_simulador.html", **await _ctx_simulador(pool, tenant_id))


# --- Base de conocimiento y configuración ------------------------------------

@router.post("/asistente/conocimiento")
async def guardar_conocimiento(request: Request):
    pool, tenant_id = _pool_tenant(request)
    form = await request.form()
    datos = {clave: str(form.get(clave, "")).strip() for clave in db.CAMPOS_CONOCIMIENTO}
    largas = [titulo for clave, titulo in asistente.SECCIONES if len(datos[clave]) > asistente.MAX_SECCION]
    if largas:
        return await _pantalla(request, "conocimiento", datos,
                               f"Estas secciones superan los {asistente.MAX_SECCION} caracteres: {', '.join(largas)}.")
    await db.guardar_conocimiento(pool, tenant_id, datos)
    _avisar(request, "ok", "Base de conocimiento guardada. Pruébala en la pestaña «Probar».")
    return RedirectResponse("/asistente?pestana=conocimiento", status_code=303)


@router.post("/asistente/configuracion")
async def guardar_configuracion(request: Request):
    pool, tenant_id = _pool_tenant(request)
    actual = await asistente.cargar_asistente(pool, tenant_id)
    form = await request.form()
    datos = {clave: str(form.get(clave, "")).strip() for clave in LIMITES_CONFIG}
    datos["activo"] = form.get("activo") == "1"
    datos["trato"] = form.get("trato") if form.get("trato") in ("tu", "usted") else actual["trato"]
    # Un nivel no disponible se muestra deshabilitado y no se envía: se conserva el guardado.
    datos["nivel"] = form.get("nivel") if form.get("nivel") in ia.NIVELES else actual["nivel"]

    error = None
    largos = [clave for clave, limite in LIMITES_CONFIG.items() if len(datos[clave]) > limite]
    nivel = ia.CONFIG.niveles[datos["nivel"]]
    if largos:
        error = "Hay campos demasiado largos: " + ", ".join(largos) + "."
    elif datos["activo"] and not nivel.disponible:
        error = f"No puedes activar el asistente: el nivel {nivel.etiqueta} no está disponible ({nivel.motivo})."
    elif datos["activo"] and not any((actual[clave] or "").strip() for clave in db.CAMPOS_CONOCIMIENTO):
        error = "Carga la base de conocimiento antes de activar el asistente."
    if error:
        return await _pantalla(request, "configuracion", datos, error)

    await db.guardar_config_asistente(pool, tenant_id, datos)
    _avisar(request, "ok", "Configuración del asistente guardada.")
    return RedirectResponse("/asistente?pestana=configuracion", status_code=303)
