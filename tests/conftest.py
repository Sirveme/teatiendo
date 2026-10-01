"""Fixtures comunes: variables de entorno de prueba, base de datos en memoria, proveedor de IA simulado
(httpx.MockTransport) y cliente del panel con sesión iniciada. No requieren PostgreSQL ni llaves reales."""
import hashlib
import hmac
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from argon2 import PasswordHasher  # noqa: E402

CLAVE_ADMIN = "claveDePrueba123"
ENTORNO = {
    "DATABASE_URL": "postgresql://u:p@localhost/prueba", "WA_ACCESS_TOKEN": "token-prueba",
    "WA_PHONE_NUMBER_ID": "111", "WA_WABA_ID": "222", "WA_VERIFY_TOKEN": "verificar",
    "META_APP_SECRET": "secreto-app", "GRAPH_API_VERSION": "v23.0", "SESSION_SECRET": "s" * 40,
    "ADMIN_EMAIL": "admin@prueba.pe", "ADMIN_PASSWORD_HASH": PasswordHasher().hash(CLAVE_ADMIN),
}
os.environ.update(ENTORNO)
for variable in list(os.environ):  # la IA no debe tomar llaves reales del equipo donde se corren las pruebas
    if variable.startswith("IA_") or variable.endswith("_API_KEY") or variable.endswith("_BASE_URL"):
        del os.environ[variable]

import asyncpg  # noqa: E402
import httpx  # noqa: E402
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db, ia, meta  # noqa: E402
from app.main import app  # noqa: E402

TENANT = 1


class BaseEnMemoria:
    """Implementa las funciones de app/db.py que usan el asistente, el webhook y el panel."""

    def __init__(self):
        self.contactos: dict[int, dict] = {}
        self.mensajes: list[dict] = []
        self.asistente: dict | None = None
        self.usos: list[dict] = []
        self.eventos: list = []
        self.candados: list[int] = []
        self.etapas: list[dict] = []
        self.campos: list[dict] = []
        self.notas: list[dict] = []
        self.cambios_etapa: list[dict] = []
        self.exportaciones: list[dict] = []
        self.proveedores_ia: dict[int, dict] = {}
        self.modelos_ia: dict[int, dict] = {}
        self.niveles_map: dict[str, int | None] = {}
        self.precios_hist: list[dict] = []
        self.tenant = {"id": TENANT, "nombre": "PERU SISTEMAS PRO E.I.R.L.", "waba_id": "222", "phone_number_id": "111",
                       "rubro": "comercio_redes", "modulos": {}}

    # tenants / webhook
    async def obtener_tenant(self, pool, tenant_id):
        return dict(self.tenant)

    async def tenant_por_phone(self, pool, phone):
        return TENANT if phone == "111" else None

    async def guardar_evento(self, pool, payload):
        self.eventos.append(payload)

    # contactos
    def _nuevo_contacto(self, canal, wa_id, nombre=None):
        cid = len(self.contactos) + 1
        self.contactos[cid] = {"id": cid, "canal": canal, "wa_id": wa_id, "nombre_perfil": nombre,
                               "ultimo_mensaje_entrante_en": None, "modo": "ia", "derivado_en": None,
                               "aviso_no_texto_en": None, "etapa_id": None, "etapa_desde": None, "motivo_perdida": None,
                               "ficha": {}, "etiquetas": [], "origen_anuncio": None, "origen_en": None,
                               "meta_user_id": None, "leido_hasta_id": 0, "extraccion_en": None,
                               "extraccion_pendiente": False}
        return cid

    def _buscar(self, canal, wa_id):
        return next((c["id"] for c in self.contactos.values() if c["canal"] == canal and c["wa_id"] == wa_id), None)

    async def upsert_contacto_entrante(self, pool, tenant_id, wa_id, nombre, recibido_en, canal="whatsapp",
                                       meta_user_id=None):
        cid = self._buscar(canal, wa_id) or self._nuevo_contacto(canal, wa_id, nombre)
        c = self.contactos[cid]
        c["meta_user_id"] = meta_user_id or c["meta_user_id"]
        c["nombre_perfil"] = nombre or c["nombre_perfil"]
        c["ultimo_mensaje_entrante_en"] = max(filter(None, [c["ultimo_mensaje_entrante_en"], recibido_en]))
        return cid

    async def upsert_contacto(self, pool, tenant_id, wa_id, canal="whatsapp"):
        return self._buscar(canal, wa_id) or self._nuevo_contacto(canal, wa_id)

    async def contacto_simulador(self, pool, tenant_id):
        return self._buscar("web", "simulador") or self._nuevo_contacto("web", "simulador", "Simulador")

    async def obtener_contacto(self, pool, tenant_id, contacto_id):
        c = self.contactos.get(contacto_id)
        return dict(c) if c else None

    async def listar_contactos(self, pool, tenant_id, *, q=None, filtro=None, etapa_id=None, etiqueta=None, limite=200):
        primera = next((e for e in sorted(self.etapas, key=lambda e: e["orden"]) if not e["es_perdido"] and not e["oculta"]), None)
        filas = []
        for c in self.contactos.values():
            if c["canal"] != "whatsapp":
                continue
            ms = [m for m in self.mensajes if m["contact_id"] == c["id"]]
            u = ms[-1] if ms else {}
            etapa = next((e for e in self.etapas if e["id"] == c["etapa_id"]), None) or primera
            no_leidos = sum(1 for m in ms if m["direccion"] == "in" and m["id"] > c["leido_hasta_id"])
            valores = " ".join(str((v or {}).get("valor") or "") for v in c["ficha"].values()).lower()
            if q and not (q.lower() in (c["nombre_perfil"] or "").lower() or q in c["wa_id"] or q.lower() in valores):
                continue
            if etapa_id and (not etapa or etapa["id"] != etapa_id):
                continue
            if etiqueta and etiqueta not in c["etiquetas"]:
                continue
            if filtro == "no_leidas" and not no_leidos:
                continue
            if filtro == "atencion" and not (c["modo"] == "humano" and c["derivado_en"]):
                continue
            filas.append({**c, "ultimo_texto": u.get("texto"), "ultima_direccion": u.get("direccion"),
                          "ultimo_en": u.get("creado_en"), "no_leidos": no_leidos,
                          "etapa_id": etapa["id"] if etapa else None, "etapa_nombre": etapa["nombre"] if etapa else None,
                          "etapa_color": etapa["color"] if etapa else None,
                          "etapa_perdida": bool(etapa and etapa["es_perdido"])})
        return filas[:limite]

    async def cambiar_modo(self, pool, tenant_id, contacto_id, modo, derivado=False):
        c = self.contactos[contacto_id]
        c["modo"] = modo
        if modo == "ia":
            c["derivado_en"] = c["aviso_no_texto_en"] = None
        elif derivado:
            c["derivado_en"] = datetime.now(timezone.utc)

    async def tomar_control_humano(self, pool, tenant_id, contacto_id):
        if self.contactos[contacto_id]["modo"] == "ia":
            self.contactos[contacto_id]["modo"] = "humano"

    async def marcar_aviso_no_texto(self, pool, tenant_id, contacto_id):
        self.contactos[contacto_id]["aviso_no_texto_en"] = datetime.now(timezone.utc)

    def candado_contacto(self, pool, contacto_id):
        base = self

        @asynccontextmanager
        async def candado():
            base.candados.append(contacto_id)
            yield
        return candado()

    # mensajes
    async def insertar_mensaje(self, pool, tenant_id, contacto_id, *, wamid, direccion, tipo, texto, estado,
                               error_json=None, creado_en=None, canal="whatsapp", generado_por_ia=False):
        if wamid and any(m["wamid"] == wamid for m in self.mensajes):
            return None
        m = {"id": len(self.mensajes) + 1, "contact_id": contacto_id, "canal": canal, "wamid": wamid,
             "direccion": direccion, "tipo": tipo, "texto": texto, "estado": estado, "error_json": error_json,
             "creado_en": creado_en or datetime.now(timezone.utc), "generado_por_ia": generado_por_ia}
        self.mensajes.append(m)
        return m["id"]

    def mensajes_de(self, contacto_id):
        return [m for m in self.mensajes if m["contact_id"] == contacto_id]

    async def listar_mensajes(self, pool, tenant_id, contacto_id, limite=200):
        return self.mensajes_de(contacto_id)[-limite:]

    async def historial_para_ia(self, pool, tenant_id, contacto_id, limite):
        return [{"direccion": m["direccion"], "texto": m["texto"]} for m in self.mensajes_de(contacto_id)[-limite:]]

    async def ultimo_entrante_id(self, pool, contacto_id):
        return max((m["id"] for m in self.mensajes_de(contacto_id) if m["direccion"] == "in"), default=None)

    async def borrar_mensajes_contacto(self, pool, tenant_id, contacto_id):
        self.mensajes = [m for m in self.mensajes if m["contact_id"] != contacto_id]

    async def actualizar_estado(self, pool, tenant_id, wamid, estado, errores=None):
        for m in self.mensajes:
            if m["wamid"] == wamid:
                m["estado"] = estado

    async def listar_plantillas(self, pool, tenant_id, solo_aprobadas=False):
        return []

    # asistente
    async def obtener_asistente(self, pool, tenant_id):
        return dict(self.asistente) if self.asistente else None

    async def guardar_config_asistente(self, pool, tenant_id, datos):
        self.asistente = {**(self.asistente or {c: "" for c in db.CAMPOS_CONOCIMIENTO}), **datos}

    async def guardar_conocimiento(self, pool, tenant_id, datos):
        base = self.asistente or {"activo": False, "nombre_negocio": "", "nombre_asistente": "", "trato": "tu",
                                  "nivel": "estandar", "horario_humano": "", "mensaje_derivacion": "",
                                  "mensaje_no_texto": "", "extraccion_activa": False}
        self.asistente = {**base, **datos}

    # uso de IA
    async def registrar_uso_ia(self, pool, tenant_id, *, origen, resultado, message_id):
        u = resultado.uso
        self.usos.append({"id": len(self.usos) + 1, "message_id": message_id, "origen": origen,
                          "nivel": resultado.nivel, "proveedor": resultado.proveedor, "modelo": resultado.modelo,
                          "tokens_entrada": u.entrada, "tokens_salida": u.salida,
                          "tokens_cache_lectura": u.cache_lectura, "tokens_cache_escritura": u.cache_escritura,
                          "costo_estimado": resultado.costo, "duracion_ms": resultado.duracion_ms,
                          "ok": resultado.ok, "error": resultado.error, "creado_en": datetime.now(timezone.utc),
                          "tokens_razonamiento": u.razonamiento, "truncado": resultado.truncado,
                          "reintentos": resultado.reintentos, "modelo_ref_id": resultado.modelo_ref_id,
                          "precio_id": resultado.precio_id})
        return len(self.usos)

    async def uso_por_mensajes(self, pool, tenant_id, ids):
        return {u["message_id"]: u for u in self.usos if u["message_id"] in ids}

    async def resumen_uso(self, pool, tenant_id, desde):
        filas = {}
        for u in self.usos:
            if u["origen"] in ("prueba", "comparador"):
                continue
            f = filas.setdefault(u["origen"], {"origen": u["origen"], "consultas": 0, "errores": 0, "entrada": 0,
                                               "salida": 0, "cache": 0, "costo": 0.0, "sin_precio": 0})
            f["consultas"] += 1
            f["errores"] += 0 if u["ok"] else 1
            f["entrada"] += u["tokens_entrada"]
            f["salida"] += u["tokens_salida"]
            f["cache"] += u["tokens_cache_lectura"] + u["tokens_cache_escritura"]
            f["costo"] += u["costo_estimado"] or 0
            f["sin_precio"] += 1 if u["ok"] and u["costo_estimado"] is None else 0
        return list(filas.values())

    async def ultimos_usos(self, pool, tenant_id, limite=15):
        return [{**u, "cache": u["tokens_cache_lectura"] + u["tokens_cache_escritura"]}
                for u in self.usos[::-1] if u["origen"] not in ("prueba", "comparador")][:limite]

    # --- CRM ------------------------------------------------------------------
    async def listar_etapas(self, pool, tenant_id, incluir_ocultas=False):
        return sorted([dict(e) for e in self.etapas if incluir_ocultas or not e["oculta"]], key=lambda e: (e["orden"], e["id"]))

    async def listar_campos(self, pool, tenant_id, solo_activos=True):
        return sorted([dict(c) for c in self.campos if not solo_activos or c["activo"]], key=lambda c: (c["orden"], c["id"]))

    async def insertar_etapas_faltantes(self, pool, tenant_id, etapas):
        base, n = max((e["orden"] for e in self.etapas), default=0), 0
        for i, e in enumerate(etapas, start=1):
            if any(x["clave"] == e["clave"] for x in self.etapas):
                continue
            self.etapas.append({"id": len(self.etapas) + 1, "clave": e["clave"], "nombre": e["nombre"], "color": e["color"],
                                "orden": base + i * 10, "descripcion_ia": e.get("descripcion_ia", ""),
                                "avanzable_por_ia": bool(e.get("avanzable_por_ia")), "es_perdido": bool(e.get("es_perdido")),
                                "oculta": False})
            n += 1
        return n

    async def insertar_campos_faltantes(self, pool, tenant_id, campos):
        base, n = max((c["orden"] for c in self.campos), default=0), 0
        for i, c in enumerate(campos, start=1):
            if any(x["clave"] == c["clave"] for x in self.campos):
                continue
            self.campos.append({"id": len(self.campos) + 1, "clave": c["clave"], "etiqueta": c["etiqueta"], "tipo": c["tipo"],
                                "opciones": list(c.get("opciones", [])), "descripcion_ia": c.get("descripcion_ia", ""),
                                "orden": base + i * 10, "activo": True})
            n += 1
        return n

    async def actualizar_rubro(self, pool, tenant_id, rubro, modulos):
        self.tenant.update(rubro=rubro, modulos=dict(modulos))

    async def actualizar_modulos(self, pool, tenant_id, modulos):
        self.tenant["modulos"] = dict(modulos)

    async def actualizar_etapa(self, pool, tenant_id, etapa_id, *, nombre, color, orden, oculta):
        next(e for e in self.etapas if e["id"] == etapa_id).update(nombre=nombre, color=color, orden=orden, oculta=oculta)

    async def actualizar_campo(self, pool, tenant_id, campo_id, *, etiqueta, descripcion_ia, opciones, activo):
        next(c for c in self.campos if c["id"] == campo_id).update(etiqueta=etiqueta, descripcion_ia=descripcion_ia,
                                                                   opciones=opciones, activo=activo)

    async def agregar_campo(self, pool, tenant_id, *, clave, etiqueta, tipo, opciones, descripcion_ia):
        return bool(await self.insertar_campos_faltantes(pool, tenant_id, [
            {"clave": clave, "etiqueta": etiqueta, "tipo": tipo, "opciones": opciones, "descripcion_ia": descripcion_ia}]))

    async def aplicar_ficha_ia(self, pool, tenant_id, contacto_id, cambios):
        ficha = self.contactos[contacto_id]["ficha"]
        escritas = sorted(k for k in cambios if (ficha.get(k) or {}).get("fuente") != "humano")
        for k in escritas:
            ficha[k] = cambios[k]
        return escritas

    async def avanzar_etapa_ia(self, pool, tenant_id, contacto_id, clave):
        c = self.contactos[contacto_id]
        nueva = next((e for e in self.etapas if e["clave"] == clave), None)
        actual = next((e for e in self.etapas if e["id"] == c["etapa_id"]), None)
        if (not nueva or not nueva["avanzable_por_ia"] or nueva["es_perdido"] or nueva["oculta"]
                or (actual and actual["es_perdido"]) or nueva["orden"] <= (actual["orden"] if actual else -10**9)):
            return None
        anterior = c["etapa_id"]
        c.update(etapa_id=nueva["id"], etapa_desde=datetime.now(timezone.utc))
        self.cambios_etapa.append({"contact_id": contacto_id, "anterior": anterior, "nueva": nueva["id"], "fuente": "ia"})
        return anterior, nueva["id"]

    async def mover_etapa(self, pool, tenant_id, contacto_id, etapa_id, *, motivo, autor):
        c = self.contactos[contacto_id]
        destino = next((e for e in self.etapas if e["id"] == etapa_id), None)
        if not destino:
            return None
        anterior = c["etapa_id"]
        if anterior != etapa_id:
            c["etapa_desde"] = datetime.now(timezone.utc)
            self.cambios_etapa.append({"contact_id": contacto_id, "anterior": anterior, "nueva": etapa_id,
                                       "fuente": "humano", "autor": autor, "motivo": motivo})
        c.update(etapa_id=etapa_id, motivo_perdida=motivo if destino["es_perdido"] else None)
        return anterior, etapa_id

    async def editar_campo_humano(self, pool, tenant_id, contacto_id, clave, valor):
        self.contactos[contacto_id]["ficha"][clave] = {"valor": valor, "fuente": "humano",
                                                       "actualizado_en": datetime.now(timezone.utc).isoformat()}

    async def liberar_campo(self, pool, tenant_id, contacto_id, clave):
        self.contactos[contacto_id]["ficha"].pop(clave, None)

    async def agregar_etiqueta(self, pool, tenant_id, contacto_id, etiqueta):
        if etiqueta not in self.contactos[contacto_id]["etiquetas"]:
            self.contactos[contacto_id]["etiquetas"].append(etiqueta)

    async def quitar_etiqueta(self, pool, tenant_id, contacto_id, etiqueta):
        self.contactos[contacto_id]["etiquetas"] = [t for t in self.contactos[contacto_id]["etiquetas"] if t != etiqueta]

    async def listar_etiquetas_tenant(self, pool, tenant_id):
        return sorted({t for c in self.contactos.values() if c["canal"] == "whatsapp" for t in c["etiquetas"]})

    async def listar_notas(self, pool, tenant_id, contacto_id):
        return [n for n in self.notas[::-1] if n["contact_id"] == contacto_id]

    async def agregar_nota(self, pool, tenant_id, contacto_id, autor, texto):
        self.notas.append({"id": len(self.notas) + 1, "contact_id": contacto_id, "autor": autor, "texto": texto,
                           "creado_en": datetime.now(timezone.utc)})

    async def guardar_origen_anuncio(self, pool, tenant_id, contacto_id, origen):
        c = self.contactos[contacto_id]
        if c["origen_anuncio"] is not None:
            return False
        c.update(origen_anuncio=origen, origen_en=datetime.now(timezone.utc))
        return True

    async def marcar_leido(self, pool, tenant_id, contacto_id):
        ultimo = await self.ultimo_entrante_id(pool, contacto_id) or 0
        c = self.contactos[contacto_id]
        c["leido_hasta_id"] = max(c["leido_hasta_id"], ultimo)

    async def reiniciar_crm_contacto(self, pool, tenant_id, contacto_id):
        self.contactos[contacto_id].update(ficha={}, etapa_id=None, etapa_desde=None, motivo_perdida=None, etiquetas=[],
                                           extraccion_en=None, extraccion_pendiente=False)
        self.notas = [n for n in self.notas if n["contact_id"] != contacto_id]

    async def reclamar_extraccion(self, pool, contacto_id, intervalo_s):
        c = self.contactos[contacto_id]
        ahora = datetime.now(timezone.utc)
        if c["extraccion_en"] and (ahora - c["extraccion_en"]).total_seconds() < intervalo_s:
            return False
        c.update(extraccion_en=ahora, extraccion_pendiente=False)
        return True

    async def marcar_extraccion_pendiente(self, pool, contacto_id):
        self.contactos[contacto_id]["extraccion_pendiente"] = True

    async def reclamar_extracciones_pendientes(self, pool, intervalo_s, limite=10):
        ahora, filas = datetime.now(timezone.utc), []
        for c in self.contactos.values():
            if c["extraccion_pendiente"] and (not c["extraccion_en"] or (ahora - c["extraccion_en"]).total_seconds() >= intervalo_s):
                c.update(extraccion_pendiente=False, extraccion_en=ahora)
                filas.append({"id": c["id"], "tenant_id": TENANT})
        return filas[:limite]

    async def registrar_exportacion(self, pool, tenant_id, usuario, filtros, filas):
        self.exportaciones.append({"usuario": usuario, "filtros": filtros, "filas": filas})
    # --- Panel de modelos -------------------------------------------------------
    def _fila_modelo(self, modelo_id):
        m = self.modelos_ia.get(modelo_id)
        if not m:
            return None
        p = self.proveedores_ia[m["proveedor_id"]]
        h = max((x for x in self.precios_hist if x["modelo_id"] == modelo_id), key=lambda x: x["id"], default=None)
        return {"modelo_ref_id": m["id"], "modelo_id": m["modelo_id"], "nombre_visible": m["nombre_visible"],
                "max_tokens": m["max_tokens"], "esfuerzo": m["esfuerzo"], "parametros_extra": m["parametros_extra"],
                "modelo_activo": m["activo"], "proveedor_id": p["id"], "proveedor": p["nombre"], "tipo": p["tipo"],
                "url_base": p["url_base"], "campo_max_tokens": p["campo_max_tokens"], "llave_cifrada": p["llave_cifrada"],
                "proveedor_activo": p["activo"], "precio_id": h["id"] if h else None,
                **{c: (h[c] if h else None) for c in db.CAMPOS_PRECIO}}

    async def niveles_ia_configurados(self, pool):
        return [{"nivel": n, **self._fila_modelo(mid)} for n, mid in self.niveles_map.items()
                if mid and self._fila_modelo(mid)]

    async def modelo_ia_completo(self, pool, modelo_id):
        return self._fila_modelo(modelo_id)

    async def listar_proveedores_ia(self, pool):
        return [{**p, "modelos": sum(1 for m in self.modelos_ia.values() if m["proveedor_id"] == p["id"])}
                for p in sorted(self.proveedores_ia.values(), key=lambda p: p["nombre"])]

    async def crear_proveedor_ia(self, pool, *, nombre, tipo, url_base, campo_max_tokens, activo, llave_cifrada,
                                 llave_ultimos4):
        if any(p["nombre"] == nombre for p in self.proveedores_ia.values()):
            raise asyncpg.UniqueViolationError("duplicado")
        pid = len(self.proveedores_ia) + 1
        self.proveedores_ia[pid] = {"id": pid, "nombre": nombre, "tipo": tipo, "url_base": url_base,
                                    "campo_max_tokens": campo_max_tokens, "activo": activo, "llave_cifrada": llave_cifrada,
                                    "llave_ultimos4": llave_ultimos4, "actualizado_en": datetime.now(timezone.utc)}
        return pid

    async def actualizar_proveedor_ia(self, pool, proveedor_id, *, nombre, tipo, url_base, campo_max_tokens, activo,
                                      cambiar_llave, llave_cifrada=None, llave_ultimos4=None):
        p = self.proveedores_ia[proveedor_id]
        p.update(nombre=nombre, tipo=tipo, url_base=url_base, campo_max_tokens=campo_max_tokens, activo=activo)
        if cambiar_llave:
            p.update(llave_cifrada=llave_cifrada, llave_ultimos4=llave_ultimos4)

    async def listar_modelos_ia(self, pool):
        return [{**m, "proveedor": self.proveedores_ia[m["proveedor_id"]]["nombre"],
                 "proveedor_activo": self.proveedores_ia[m["proveedor_id"]]["activo"],
                 "con_llave": bool(self.proveedores_ia[m["proveedor_id"]]["llave_cifrada"])}
                for m in self.modelos_ia.values()]

    async def obtener_modelo_ia(self, pool, modelo_id):
        return self.modelos_ia.get(modelo_id)

    def _historial(self, modelo_id, datos, usuario):
        self.precios_hist.append({"id": len(self.precios_hist) + 1, "modelo_id": modelo_id,
                                  "vigente_desde": datetime.now(timezone.utc), "registrado_por": usuario,
                                  **{c: datos[c] for c in db.CAMPOS_PRECIO}})

    async def crear_modelo_ia(self, pool, datos, usuario):
        if any(m["proveedor_id"] == datos["proveedor_id"] and m["modelo_id"] == datos["modelo_id"]
               for m in self.modelos_ia.values()):
            raise asyncpg.UniqueViolationError("duplicado")
        mid = len(self.modelos_ia) + 1
        self.modelos_ia[mid] = {"id": mid, **datos}
        self._historial(mid, datos, usuario)
        return mid

    async def actualizar_modelo_ia(self, pool, modelo_id, datos, usuario):
        actual = self.modelos_ia[modelo_id]
        cambio = any(actual[c] != datos[c] for c in db.CAMPOS_PRECIO)
        actual.update(datos)
        if cambio:
            self._historial(modelo_id, datos, usuario)
        return cambio

    async def niveles_ia(self, pool):
        return dict(self.niveles_map)

    async def asignar_nivel_ia(self, pool, nivel, modelo_id, usuario):
        self.niveles_map[nivel] = modelo_id

    async def historial_precios(self, pool, limite=200):
        filas = []
        for h in self.precios_hist[::-1][:limite]:
            m = self.modelos_ia[h["modelo_id"]]
            filas.append({**h, "nombre_visible": m["nombre_visible"], "id_api": m["modelo_id"],
                          "proveedor": self.proveedores_ia[m["proveedor_id"]]["nombre"]})
        return filas

    async def uso_plataforma(self, pool, desde):
        filas = {}
        for u in self.usos:
            if u["origen"] not in ("prueba", "comparador"):
                continue
            f = filas.setdefault(u["origen"], {"origen": u["origen"], "consultas": 0, "errores": 0, "entrada": 0,
                                               "salida": 0, "costo": 0.0})
            f["consultas"] += 1
            f["errores"] += 0 if u["ok"] else 1
            f["entrada"] += u["tokens_entrada"] + u["tokens_cache_lectura"] + u["tokens_cache_escritura"]
            f["salida"] += u["tokens_salida"]
            f["costo"] += u["costo_estimado"] or 0
        return list(filas.values())


@pytest.fixture
def base(monkeypatch):
    from app import config_ia
    config_ia.invalidar()
    b = BaseEnMemoria()
    for nombre in dir(b):
        if not nombre.startswith("_") and callable(getattr(b, nombre)) and hasattr(db, nombre):
            monkeypatch.setattr(db, nombre, getattr(b, nombre))
    return b


# --- Proveedor de IA simulado ------------------------------------------------

ENTORNO_IA = {
    "ANTHROPIC_API_KEY": "sk-ant-prueba",
    "OPENAI_API_KEY": "sk-openai-prueba",
    "IA_NIVEL_BASICO": "openai:modelo-openai-prueba",
    "IA_NIVEL_ESTANDAR": "anthropic:modelo-anthropic-prueba",
    "IA_PRECIO_ESTANDAR": "3,15,0.3,3.75",
    "IA_NIVEL_PREMIUM": "xai:modelo-xai-prueba",  # sin XAI_API_KEY → no disponible
}


class ProveedorSimulado:
    """Registra cada solicitud y responde con lo que se encole (por defecto, un texto de Anthropic)."""

    def __init__(self):
        self.solicitudes: list[httpx.Request] = []
        self.respuestas: list = []

    def cuerpo(self, i=-1) -> dict:
        return json.loads(self.solicitudes[i].content)

    def encolar(self, respuesta):
        self.respuestas.append(respuesta)

    def __call__(self, request: httpx.Request):
        self.solicitudes.append(request)
        if self.respuestas:
            r = self.respuestas.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return respuesta_anthropic("Hola, ¿en qué te ayudo?")


def respuesta_anthropic(texto, entrada=900, salida=40, cache_lectura=0, cache_escritura=0, estado=200):
    return httpx.Response(estado, json={
        "content": [{"type": "text", "text": texto}],
        "usage": {"input_tokens": entrada, "output_tokens": salida,
                  "cache_read_input_tokens": cache_lectura, "cache_creation_input_tokens": cache_escritura},
    })


def respuesta_openai(texto, prompt=1000, completion=50, cacheados=0):
    return httpx.Response(200, json={
        "choices": [{"message": {"role": "assistant", "content": texto}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion,
                  "prompt_tokens_details": {"cached_tokens": cacheados}},
    })


@pytest.fixture
def proveedor(monkeypatch):
    p = ProveedorSimulado()
    monkeypatch.setattr(ia, "_transporte", httpx.MockTransport(p))
    monkeypatch.setattr(ia, "CONFIG", ia.cargar_config(ENTORNO_IA))
    return p


@pytest.fixture
def envios(monkeypatch):
    """Sustituye meta.enviar_texto: registra (destino, texto) y simula un envío exitoso."""
    registro = []

    async def enviar_texto(phone_number_id, destino, texto):
        registro.append((destino, texto))
        return True, {"messages": [{"id": f"wamid.IA{len(registro)}"}], "contacts": [{"wa_id": destino}]}

    monkeypatch.setattr(meta, "enviar_texto", enviar_texto)
    return registro


# --- Cliente del panel -------------------------------------------------------

@pytest.fixture
def cliente(base):
    app.state.pool = object()
    app.state.tenant_id = TENANT
    c = TestClient(app, base_url="http://localhost")
    r = c.post("/login", data={"email": ENTORNO["ADMIN_EMAIL"], "password": CLAVE_ADMIN}, follow_redirects=False)
    assert r.status_code == 303
    return c


def firmar(cuerpo: bytes) -> str:
    return "sha256=" + hmac.new(ENTORNO["META_APP_SECRET"].encode(), cuerpo, hashlib.sha256).hexdigest()


def evento_entrante(wamid: str, wa_id: str = "51987654321", tipo: str = "text", texto: str = "Hola",
                    nombre: str = "María Pérez") -> bytes:
    mensaje = {"from": wa_id, "id": wamid, "timestamp": str(int(datetime.now(timezone.utc).timestamp())), "type": tipo}
    mensaje[tipo] = {"body": texto} if tipo == "text" else {"id": "media-1"}
    return json.dumps({"object": "whatsapp_business_account", "entry": [{"id": "222", "changes": [{
        "field": "messages", "value": {
            "messaging_product": "whatsapp", "metadata": {"phone_number_id": "111"},
            "contacts": [{"wa_id": wa_id, "profile": {"name": nombre}}], "messages": [mensaje]}}]}]}).encode()


def asistente_listo(base, **cambios):
    """Asistente activo con una base de conocimiento mínima."""
    base.asistente = {
        "activo": True, "extraccion_activa": False, "nombre_negocio": "Clínica Demo Amazonía", "nombre_asistente": "Sofía", "trato": "tu",
        "nivel": "estandar", "horario_humano": "de lunes a viernes de 9:00 a 18:00",
        "mensaje_derivacion": "Te paso con una persona; te responderá {horario_humano}.",
        "mensaje_no_texto": "Por ahora solo leo texto. ¿Puedes escribir tu consulta?",
        "sobre_negocio": "Clínica ficticia.", "servicios_precios": "Consulta general: S/ 60.",
        "horarios_contacto": "Lunes a viernes de 8:00 a 20:00.", "preguntas_frecuentes": "", "reglas": "No dar diagnósticos.",
        **cambios,
    }
