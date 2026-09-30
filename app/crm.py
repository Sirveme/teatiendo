"""Motor del CRM (uno solo para todos los rubros): aplica presets, valida los valores de la ficha, decide qué
puede escribir la IA y qué etapas puede avanzar, arma el panel de detalles y genera el CSV."""
import csv
import io
import re
import unicodedata
from datetime import datetime, timezone

from app import db
from app.presets import MODULOS, PRESETS

FUENTE_IA, FUENTE_HUMANO = "ia", "humano"
MAX_TEXTO = 300
MAX_ETIQUETA, MAX_ETIQUETAS = 30, 20
MAX_NOTA = 2000
VALORES_VACIOS = {"", "null", "none", "n/a", "na", "-", "ninguno", "no indica", "desconocido"}


# --- Presets -------------------------------------------------------------------

async def aplicar_preset(pool, tenant_id: int, rubro: str) -> dict:
    """Agrega las etapas y campos del preset que falten (nunca borra ni pisa lo que el tenant renombró)
    y fija el rubro y sus módulos."""
    preset = PRESETS[rubro]
    etapas = await db.insertar_etapas_faltantes(pool, tenant_id, preset["etapas"])
    campos = await db.insertar_campos_faltantes(pool, tenant_id, preset["campos"])
    await db.actualizar_rubro(pool, tenant_id, rubro, dict(preset["modulos"]))
    return {"etapas": etapas, "campos": campos}


async def asegurar_preset(pool, tenant_id: int) -> None:
    """Al arrancar: si el tenant aún no tiene embudo, copia el de su rubro."""
    if await db.listar_etapas(pool, tenant_id, incluir_ocultas=True):
        return
    tenant = await db.obtener_tenant(pool, tenant_id)
    await aplicar_preset(pool, tenant_id, tenant["rubro"] if tenant["rubro"] in PRESETS else "generico")


def motivos_perdida(rubro: str) -> list[str]:
    return PRESETS.get(rubro, PRESETS["generico"])["motivos_perdida"]


def modulos_de(tenant) -> dict:
    guardados = tenant["modulos"] or {}
    return {m: bool(guardados.get(m)) for m in MODULOS}


# --- Valores de la ficha -------------------------------------------------------------

def _sin_tildes(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn").lower().strip()


def _numero(texto: str) -> float | None:
    limpio = re.sub(r"(?i)s/\.?|soles?|pen|\s", "", texto)
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", limpio):
        limpio = limpio.replace(",", "")          # 1,200.50
    else:
        limpio = limpio.replace(",", ".")         # 12,5
    try:
        numero = float(limpio)
    except ValueError:
        return None
    return numero if 0 < numero < 1_000_000_000 else None


def normalizar_valor(campo, valor) -> str | None:
    """Valida un valor según el tipo del campo. Devuelve el texto normalizado o None si no es válido."""
    if valor is None or isinstance(valor, (bool, dict, list)):
        return None
    texto = str(valor).strip()
    if _sin_tildes(texto) in VALORES_VACIOS:
        return None
    tipo = campo["tipo"]
    if tipo == "texto":
        return texto[:MAX_TEXTO]
    if tipo in ("numero", "moneda"):
        numero = _numero(texto)
        if numero is None:
            return None
        if tipo == "moneda":
            return f"{numero:.2f}"
        return str(int(numero)) if numero == int(numero) else f"{numero:g}"
    if tipo == "opcion":
        opciones = list(campo["opciones"] or [])
        return next((o for o in opciones if _sin_tildes(o) == _sin_tildes(texto)), None)
    if tipo == "documento":
        digitos = re.sub(r"\D", "", texto)
        return digitos if len(digitos) in (8, 11) else None
    return None


def cambios_ia_permitidos(ficha_actual: dict, propuestos: dict, campos) -> dict:
    """Qué puede escribir la IA: solo campos activos, con valor válido, distinto del actual y NUNCA uno que
    haya editado una persona. (La misma regla se vuelve a aplicar dentro del UPDATE, de forma atómica.)"""
    por_clave = {c["clave"]: c for c in campos}
    ahora = datetime.now(timezone.utc).isoformat()
    cambios = {}
    for clave, valor in (propuestos or {}).items():
        campo = por_clave.get(clave)
        if campo is None:
            continue
        actual = (ficha_actual or {}).get(clave) or {}
        if actual.get("fuente") == FUENTE_HUMANO:
            continue
        normalizado = normalizar_valor(campo, valor)
        if normalizado is None or actual.get("valor") == normalizado:
            continue
        cambios[clave] = {"valor": normalizado, "fuente": FUENTE_IA, "actualizado_en": ahora}
    return cambios


def etapa_efectiva(etapas, etapa_id):
    """Etapa del contacto; si no tiene, la primera etapa visible que no sea 'Perdido'."""
    if etapa_id is not None:
        encontrada = next((e for e in etapas if e["id"] == etapa_id), None)
        if encontrada:
            return encontrada
    return next((e for e in etapas if not e["es_perdido"] and not e["oculta"]), None)


def etapa_ia_permitida(etapas, etapa_actual_id, clave_nueva) -> bool:
    """La IA solo avanza: destino avanzable_por_ia, nunca 'Perdido', orden mayor y el contacto no está perdido."""
    nueva = next((e for e in etapas if e["clave"] == clave_nueva), None)
    if not nueva or not nueva["avanzable_por_ia"] or nueva["es_perdido"] or nueva["oculta"]:
        return False
    actual = next((e for e in etapas if e["id"] == etapa_actual_id), None) if etapa_actual_id else None
    if actual and actual["es_perdido"]:
        return False
    return nueva["orden"] > (actual["orden"] if actual else float("-inf"))


def validar_movimiento_humano(etapa, motivo: str | None) -> str | None:
    if etapa is None:
        return "Esa etapa no existe."
    if etapa["es_perdido"] and not (motivo or "").strip():
        return "Indica el motivo para marcar la venta como perdida."
    return None


def normalizar_etiqueta(texto: str) -> str | None:
    etiqueta = re.sub(r"\s+", " ", (texto or "").strip().lower())
    return etiqueta[:MAX_ETIQUETA] or None


def clave_desde_etiqueta(etiqueta: str) -> str:
    clave = re.sub(r"[^a-z0-9]+", "_", _sin_tildes(etiqueta)).strip("_")[:40]
    return clave if clave and clave[0].isalpha() else f"campo_{clave}".strip("_")


def tiempo_desde(fecha: datetime | None) -> str:
    if not fecha:
        return ""
    minutos = int((datetime.now(timezone.utc) - fecha).total_seconds() // 60)
    if minutos < 60:
        return f"{max(minutos, 0)} min"
    if minutos < 60 * 24:
        return f"{minutos // 60} h"
    return f"{minutos // (60 * 24)} d"


# --- Panel de detalles -----------------------------------------------------------

async def contexto_detalles(pool, tenant_id: int, contacto_id: int, aviso: str | None = None) -> dict | None:
    contacto = await db.obtener_contacto(pool, tenant_id, contacto_id)
    if not contacto:
        return None
    tenant = await db.obtener_tenant(pool, tenant_id)
    todas = await db.listar_etapas(pool, tenant_id, incluir_ocultas=True)
    actual = etapa_efectiva(todas, contacto["etapa_id"])
    visibles = [e for e in todas if not e["oculta"] or (actual and e["id"] == actual["id"])]
    ficha = contacto["ficha"] or {}
    campos = [{**dict(c), "valor": (ficha.get(c["clave"]) or {}).get("valor"),
               "fuente": (ficha.get(c["clave"]) or {}).get("fuente")}
              for c in await db.listar_campos(pool, tenant_id)]
    asistente = await db.obtener_asistente(pool, tenant_id)
    return {
        "contacto": contacto,
        "etapas": visibles,
        "etapa_actual": actual,
        "tiempo_etapa": tiempo_desde(contacto["etapa_desde"]),
        "campos": campos,
        "notas": await db.listar_notas(pool, tenant_id, contacto_id),
        "motivos": motivos_perdida(tenant["rubro"]),
        "etiquetas_tenant": await db.listar_etiquetas_tenant(pool, tenant_id),
        "asistente_activo": bool(asistente and asistente["activo"]),
        "aviso_detalles": aviso,
    }


# --- Exportación CSV -----------------------------------------------------------------

def _celda(valor) -> str:
    """Texto seguro para Excel: una celda que empieza con = + - @ se exporta con apóstrofo (evita fórmulas)."""
    texto = "" if valor is None else str(valor)
    return "'" + texto if texto[:1] in ("=", "+", "-", "@", "\t", "\r") else texto


def generar_csv(contactos, campos) -> str:
    """CSV con separador ';' y BOM UTF-8 (Excel en español lo abre bien con doble clic)."""
    salida = io.StringIO()
    escritor = csv.writer(salida, delimiter=";", lineterminator="\r\n")
    escritor.writerow(["Nombre", "Teléfono", "Etapa", "Motivo de pérdida", "Etiquetas",
                       *[c["etiqueta"] for c in campos], "Origen del anuncio", "Último mensaje"])
    for c in contactos:
        ficha = c["ficha"] or {}
        origen = c["origen_anuncio"] or {}
        escritor.writerow([_celda(v) for v in [
            c["nombre_perfil"] or "", c["wa_id"], c["etapa_nombre"] or "", c["motivo_perdida"] or "",
            ", ".join(c["etiquetas"] or []),
            *[(ficha.get(campo["clave"]) or {}).get("valor") or "" for campo in campos],
            origen.get("headline") or origen.get("source_url") or "",
            c["ultimo_en"].astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if c["ultimo_en"] else "",
        ]])
    return "﻿" + salida.getvalue()
