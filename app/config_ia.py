"""Configuración de IA efectiva: niveles definidos en el panel de modelos (base de datos) con respaldo POR NIVEL
en las variables de entorno. Un nivel usa la base si está asignado a un modelo activo, de un proveedor activo
y con llave que se puede descifrar; si no, usa IA_NIVEL_* como hasta ahora.
Se guarda en memoria unos segundos para no consultar la base en cada mensaje (con varios procesos, cada uno
ve los cambios a más tardar en CACHE_S segundos; el proceso que guarda los ve al instante)."""
import logging
import time
from dataclasses import replace

import asyncpg

from app import cifrado, db, ia

log = logging.getLogger("teatiendo.config_ia")

CACHE_S = 30
_cache: dict = {"momento": 0.0, "base": None, "config": None}


def invalidar() -> None:
    _cache["config"] = None


def _numero(valor) -> float | None:
    return float(valor) if valor is not None else None


def _precio(fila) -> ia.Precio | None:
    if fila["precio_entrada"] is None or fila["precio_salida"] is None:
        return None
    return ia.Precio(float(fila["precio_entrada"]), float(fila["precio_salida"]),
                     _numero(fila["precio_cache_lectura"]), _numero(fila["precio_cache_escritura"]))


def motivo_fila(fila) -> str | None:
    if not fila["modelo_activo"]:
        return "el modelo está inactivo"
    if not fila["proveedor_activo"]:
        return "el proveedor está inactivo"
    if not fila["llave_cifrada"]:
        return "el proveedor no tiene llave de API"
    return None


def nivel_desde_fila(clave: str, fila) -> ia.Nivel:
    """Nivel listo para llamar al modelo de la fila (o no disponible, con el motivo)."""
    motivo, llave = motivo_fila(fila), ""
    if motivo is None:
        try:
            llave = cifrado.descifrar(fila["llave_cifrada"])
        except cifrado.ErrorCifrado as e:
            motivo = str(e)
    return ia.Nivel(
        clave=clave, proveedor=fila["proveedor"], modelo=fila["modelo_id"], precio=_precio(fila),
        disponible=motivo is None, motivo=motivo, esfuerzo=fila["esfuerzo"], tipo=fila["tipo"],
        url_base=fila["url_base"], llave=llave, campo_max_tokens=fila["campo_max_tokens"],
        max_tokens=fila["max_tokens"], parametros_extra=dict(fila["parametros_extra"] or {}), origen="base",
        modelo_ref_id=fila["modelo_ref_id"], precio_id=fila["precio_id"], nombre_visible=fila["nombre_visible"],
    )


async def filas_niveles(pool) -> list:
    try:
        return list(await db.niveles_ia_configurados(pool))
    except (asyncpg.UndefinedTableError, asyncpg.UndefinedColumnError):
        return []  # migración 007 aún no ejecutada: solo variables de entorno


async def obtener(pool, forzar: bool = False) -> ia.ConfigIA:
    base = ia.CONFIG
    vigente = (_cache["config"] is not None and _cache["base"] is base
               and time.monotonic() - _cache["momento"] < CACHE_S)
    if vigente and not forzar:
        return _cache["config"]
    niveles = dict(base.niveles)
    for fila in await filas_niveles(pool):
        clave = fila["nivel"]
        candidato = nivel_desde_fila(clave, fila)
        if candidato.disponible or not base.niveles[clave].disponible:
            niveles[clave] = candidato  # la base manda; si ninguno sirve, se muestra el motivo de la base
        else:
            log.warning("Nivel %s: %s; se usa el respaldo de las variables de entorno", clave, candidato.motivo)
    config = replace(base, niveles=niveles)
    _cache.update(momento=time.monotonic(), base=base, config=config)
    return config


async def nivel_de_modelo(pool, modelo_id: int) -> ia.Nivel | None:
    """Para «Probar» y el comparador: un modelo de la base aunque no esté asignado a ningún nivel."""
    fila = await db.modelo_ia_completo(pool, modelo_id)
    return nivel_desde_fila("prueba", fila) if fila else None
