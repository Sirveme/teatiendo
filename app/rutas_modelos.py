"""Panel de modelos de IA (solo superadministrador): proveedores con llave cifrada, modelos con precios y ajustes,
niveles, botón «Probar», comparador de modelos, historial de precios y uso de las pruebas."""
import asyncio
import json
import re
from datetime import datetime

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse

from app import asistente, cifrado, config_ia, db, ia
from app.auth import requiere_superadmin
from app.config import ZONA
from app.panel import _avisar, _pagina, _parcial, _pool_tenant

router = APIRouter(prefix="/superadmin/ia", dependencies=[Depends(requiere_superadmin)])

PESTANAS = ("proveedores", "modelos", "niveles", "comparador", "precios", "uso")
RE_URL = re.compile(r"(https://[^\s/]+|http://(localhost|127\.0\.0\.1)(:\d+)?)(/[^\s]*)?")
MIN_COMPARAR, MAX_COMPARAR = 2, 3
MENSAJE_PRUEBA = "Hola, ¿en qué me puedes ayudar?"
SISTEMA_PRUEBA = ia.Sistema("Eres un asistente de prueba de la plataforma Te Atiendo. Responde en español, en una o dos frases.",
                            "Mensaje de prueba enviado desde el panel de modelos.")
MAX_PARAMETROS_EXTRA = 4000


def _usuario(request: Request) -> str:
    return request.session.get("admin") or "superadmin"


def _inicio_mes() -> datetime:
    return datetime.now(ZONA).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _volver(request: Request, pestana: str, texto: str):
    config_ia.invalidar()
    _avisar(request, "ok", texto)
    return RedirectResponse(f"/superadmin/ia?pestana={pestana}", status_code=303)


# --- Pantalla --------------------------------------------------------------------------

async def _pantalla(request: Request, pestana: str, error: str | None = None):
    pool, tenant_id = _pool_tenant(request)
    cfg_ia = await config_ia.obtener(pool, forzar=True)
    filas = {f["nivel"]: f for f in await config_ia.filas_niveles(pool)}
    asignados = await db.niveles_ia(pool)
    estado_niveles = []
    for clave in ia.NIVELES:
        efectivo = cfg_ia.niveles[clave]
        motivo_base = None
        if clave in filas and efectivo.origen != "base":
            motivo_base = config_ia.nivel_desde_fila(clave, filas[clave]).motivo
        estado_niveles.append({"clave": clave, "etiqueta": ia.ETIQUETAS_NIVEL[clave], "efectivo": efectivo,
                               "asignado_id": asignados.get(clave), "motivo_base": motivo_base})
    modelos = await db.listar_modelos_ia(pool)
    ctx = {
        "pestana": pestana, "error": error, "estado_niveles": estado_niveles,
        "proveedores": await db.listar_proveedores_ia(pool), "modelos": modelos,
        "modelos_activos": [m for m in modelos if m["activo"]],
        "esfuerzos": ia.ESFUERZOS, "tipos": ia.TIPOS_PROVEEDOR, "cifrado_motivo": cifrado.motivo_no_disponible(),
        "enmascarar": cifrado.enmascarar, "max_comparar": MAX_COMPARAR, "mensaje_prueba": MENSAJE_PRUEBA,
    }
    if pestana == "precios":
        ctx["historial"] = await db.historial_precios(pool)
    elif pestana == "uso":
        filas_uso = await db.uso_plataforma(pool, _inicio_mes())
        ctx["uso"] = filas_uso
        ctx["uso_total"] = sum(f["costo"] for f in filas_uso)
    elif pestana == "comparador":
        contacto_id = await db.contacto_simulador(pool, tenant_id)
        ctx["mensajes_simulador"] = len(await db.historial_para_ia(pool, tenant_id, contacto_id, cfg_ia.historial_mensajes))
    return _pagina(request, "modelos.html", "modelos", **ctx)


@router.get("")
async def pantalla(request: Request, pestana: str = "niveles"):
    try:
        return await _pantalla(request, pestana if pestana in PESTANAS else "niveles")
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
        return _pagina(request, "modelos.html", "modelos", pestana="sin_migracion", error=(
            "Falta la migración 007 (panel de modelos). Ejecuta schema.sql en PGAdmin. Mientras tanto, la IA "
            "sigue funcionando con las variables de entorno."))


# --- Proveedores ------------------------------------------------------------------------

def _leer_proveedor(form) -> tuple[dict, str | None]:
    datos = {
        "nombre": str(form.get("nombre") or "").strip()[:60],
        "tipo": str(form.get("tipo") or ""),
        "url_base": str(form.get("url_base") or "").strip().rstrip("/"),
        "campo_max_tokens": str(form.get("campo_max_tokens") or "max_tokens"),
        "activo": form.get("activo") == "1",
    }
    if not datos["nombre"]:
        return datos, "El proveedor necesita un nombre."
    if datos["tipo"] not in ia.TIPOS_PROVEEDOR:
        return datos, "Elige el tipo: Anthropic o compatible con OpenAI."
    if not RE_URL.fullmatch(datos["url_base"]):
        return datos, "La URL base debe empezar con https:// (por ejemplo, https://api.openai.com/v1)."
    if datos["campo_max_tokens"] not in ("max_tokens", "max_completion_tokens"):
        return datos, "Campo de límite de tokens no válido."
    return datos, None


def _cifrar_llave(llave: str) -> tuple[str | None, str | None, str | None]:
    """(llave_cifrada, últimos 4, error)."""
    try:
        return cifrado.cifrar(llave), cifrado.ultimos4(llave), None
    except cifrado.ErrorCifrado as e:
        return None, None, f"No se pudo guardar la llave: {e}"


@router.post("/proveedores")
async def crear_proveedor(request: Request):
    pool, _ = _pool_tenant(request)
    form = await request.form()
    datos, error = _leer_proveedor(form)
    llave = str(form.get("llave") or "").strip()
    cifrada = ultimos = None
    if not error and llave:
        cifrada, ultimos, error = _cifrar_llave(llave)
    if error:
        return await _pantalla(request, "proveedores", error)
    try:
        await db.crear_proveedor_ia(pool, **datos, llave_cifrada=cifrada, llave_ultimos4=ultimos)
    except asyncpg.UniqueViolationError:
        return await _pantalla(request, "proveedores", f"Ya existe un proveedor llamado «{datos['nombre']}».")
    return _volver(request, "proveedores", f"Proveedor «{datos['nombre']}» creado.")


@router.post("/proveedores/{proveedor_id}")
async def actualizar_proveedor(request: Request, proveedor_id: int):
    pool, _ = _pool_tenant(request)
    form = await request.form()
    datos, error = _leer_proveedor(form)
    llave = str(form.get("llave") or "").strip()
    quitar = form.get("quitar_llave") == "1"
    cifrada = ultimos = None
    if not error and llave:
        cifrada, ultimos, error = _cifrar_llave(llave)
    if error:
        return await _pantalla(request, "proveedores", error)
    try:
        await db.actualizar_proveedor_ia(pool, proveedor_id, **datos, cambiar_llave=bool(llave) or quitar,
                                         llave_cifrada=cifrada, llave_ultimos4=ultimos)
    except asyncpg.UniqueViolationError:
        return await _pantalla(request, "proveedores", f"Ya existe un proveedor llamado «{datos['nombre']}».")
    return _volver(request, "proveedores", f"Proveedor «{datos['nombre']}» guardado.")


# --- Modelos ----------------------------------------------------------------------------

def _leer_modelo(form, proveedores_ids: set) -> tuple[dict, str | None]:
    datos = {
        "modelo_id": str(form.get("modelo_id") or "").strip()[:120],
        "nombre_visible": str(form.get("nombre_visible") or "").strip()[:80],
        "esfuerzo": str(form.get("esfuerzo") or ia.ESFUERZO_DEFECTO),
        "activo": form.get("activo") == "1",
        "notas": str(form.get("notas") or "").strip()[:500],
    }
    try:
        datos["proveedor_id"] = int(form.get("proveedor_id") or 0)
    except ValueError:
        datos["proveedor_id"] = 0
    try:
        datos["max_tokens"] = int(form.get("max_tokens") or 1024)
    except ValueError:
        datos["max_tokens"] = -1
    for campo in db.CAMPOS_PRECIO:
        texto = str(form.get(campo) or "").strip().replace(",", ".")
        try:
            datos[campo] = float(texto) if texto else None
        except ValueError:
            return datos, "Los precios deben ser números (USD por millón de tokens)."
        if datos[campo] is not None and not 0 <= datos[campo] <= 10000:
            return datos, "Los precios deben estar entre 0 y 10 000 USD por millón."
    texto_extra = str(form.get("parametros_extra") or "").strip() or "{}"
    try:
        extra = json.loads(texto_extra)
    except ValueError:
        return datos, "Los parámetros extra deben ser un JSON válido, por ejemplo {\"temperature\": 0.3}."
    datos["parametros_extra"] = extra
    if datos["proveedor_id"] not in proveedores_ids:
        return datos, "Elige un proveedor."
    if not datos["modelo_id"] or " " in datos["modelo_id"]:
        return datos, "Escribe el id exacto del modelo en la API del proveedor (sin espacios)."
    if not datos["nombre_visible"]:
        return datos, "Escribe el nombre visible del modelo."
    if (datos["precio_entrada"] is None) != (datos["precio_salida"] is None):
        return datos, "Para estimar costos indica el precio de entrada y el de salida (o deja ambos vacíos)."
    if not 100 <= datos["max_tokens"] <= 64000:
        return datos, "El máximo de tokens debe estar entre 100 y 64 000."
    if datos["esfuerzo"] not in ia.ESFUERZOS:
        return datos, "Esfuerzo de razonamiento no válido."
    if not isinstance(extra, dict):
        return datos, "Los parámetros extra deben ser un objeto JSON ({...})."
    protegidos = sorted(set(extra) & ia.PARAMETROS_PROTEGIDOS)
    if protegidos:
        return datos, f"Los parámetros extra no pueden incluir: {', '.join(protegidos)}."
    if len(texto_extra) > MAX_PARAMETROS_EXTRA:
        return datos, "Los parámetros extra son demasiado largos."
    return datos, None


async def _ids_proveedores(pool) -> set:
    return {p["id"] for p in await db.listar_proveedores_ia(pool)}


@router.post("/modelos")
async def crear_modelo(request: Request):
    pool, _ = _pool_tenant(request)
    datos, error = _leer_modelo(await request.form(), await _ids_proveedores(pool))
    if error:
        return await _pantalla(request, "modelos", error)
    try:
        await db.crear_modelo_ia(pool, datos, _usuario(request))
    except asyncpg.UniqueViolationError:
        return await _pantalla(request, "modelos", f"Ese proveedor ya tiene el modelo «{datos['modelo_id']}».")
    return _volver(request, "modelos", f"Modelo «{datos['nombre_visible']}» creado.")


@router.post("/modelos/{modelo_id}")
async def actualizar_modelo(request: Request, modelo_id: int):
    pool, _ = _pool_tenant(request)
    datos, error = _leer_modelo(await request.form(), await _ids_proveedores(pool))
    if error:
        return await _pantalla(request, "modelos", error)
    try:
        cambio_precio = await db.actualizar_modelo_ia(pool, modelo_id, datos, _usuario(request))
    except asyncpg.UniqueViolationError:
        return await _pantalla(request, "modelos", f"Ese proveedor ya tiene el modelo «{datos['modelo_id']}».")
    extra = " El precio nuevo rige desde ahora; los costos ya registrados no cambian." if cambio_precio else ""
    return _volver(request, "modelos", f"Modelo «{datos['nombre_visible']}» guardado.{extra}")


# --- Niveles ----------------------------------------------------------------------------

@router.post("/niveles")
async def guardar_niveles(request: Request):
    pool, _ = _pool_tenant(request)
    form = await request.form()
    ids = {m["id"] for m in await db.listar_modelos_ia(pool)}
    for clave in ia.NIVELES:
        texto = str(form.get(clave) or "")
        modelo_id = int(texto) if texto.isdigit() and int(texto) in ids else None
        await db.asignar_nivel_ia(pool, clave, modelo_id, _usuario(request))
    return _volver(request, "niveles", "Niveles guardados. Los que quedaron sin modelo usan las variables de entorno.")


# --- Probar y comparar --------------------------------------------------------------------

@router.post("/modelos/{modelo_id}/probar")
async def probar_modelo(request: Request, modelo_id: int):
    pool, tenant_id = _pool_tenant(request)
    nivel = await config_ia.nivel_de_modelo(pool, modelo_id)
    if nivel is None:
        raise HTTPException(status_code=404, detail="Modelo no encontrado")
    mensaje = str((await request.form()).get("mensaje") or "").strip()[:1000] or MENSAJE_PRUEBA
    if not nivel.disponible:
        return _parcial(request, "_prueba_modelo.html", nivel=nivel, resultado=None, error=nivel.motivo)
    resultado = await ia.generar_respuesta(SISTEMA_PRUEBA, [ia.Mensaje("user", mensaje)], nivel)
    await db.registrar_uso_ia(pool, tenant_id, origen="prueba", resultado=resultado, message_id=None)
    return _parcial(request, "_prueba_modelo.html", nivel=nivel, resultado=resultado, error=None)


@router.post("/comparar")
async def comparar(request: Request):
    pool, tenant_id = _pool_tenant(request)
    form = await request.form()
    ids = list(dict.fromkeys(int(i) for i in form.getlist("modelo_ids") if str(i).isdigit()))
    if not MIN_COMPARAR <= len(ids) <= MAX_COMPARAR:
        return _parcial(request, "_comparador.html", resultados=[],
                        error=f"Elige entre {MIN_COMPARAR} y {MAX_COMPARAR} modelos.")
    cfg_ia = await config_ia.obtener(pool)
    contacto_id = await db.contacto_simulador(pool, tenant_id)
    filas = [dict(f) for f in await db.historial_para_ia(pool, tenant_id, contacto_id, cfg_ia.historial_mensajes)]
    mensaje = str(form.get("mensaje") or "").strip()[:1000]
    if mensaje:
        filas.append({"direccion": "in", "texto": mensaje})
    else:
        # Se compara qué respondería cada modelo al último mensaje del cliente: se quitan las respuestas que ya
        # tuvo en el simulador.
        while filas and filas[-1]["direccion"] == "out":
            filas.pop()
    historial = asistente.preparar_historial(filas)
    if not historial:
        return _parcial(request, "_comparador.html", resultados=[],
                        error="No hay un mensaje del cliente: escribe algo en el simulador o en el campo de mensaje.")
    sistema = asistente.construir_sistema(await asistente.cargar_asistente(pool, tenant_id), datetime.now(ZONA))
    niveles = [await config_ia.nivel_de_modelo(pool, i) for i in ids]

    async def correr(nivel):
        if nivel is None or not nivel.disponible:
            return None
        return await ia.generar_respuesta(sistema, historial, nivel)

    respuestas = await asyncio.gather(*(correr(n) for n in niveles))  # en paralelo
    resultados = []
    for nivel, resultado in zip(niveles, respuestas):
        if resultado is not None:
            await db.registrar_uso_ia(pool, tenant_id, origen="comparador", resultado=resultado, message_id=None)
        resultados.append({"nivel": nivel, "resultado": resultado,
                           "error": None if resultado else (nivel.motivo if nivel else "Modelo no encontrado.")})
    return _parcial(request, "_comparador.html", resultados=resultados, error=None,
                    mensajes=len(historial), ultimo=historial[-1].texto)
