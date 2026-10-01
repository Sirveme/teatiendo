"""Ficha que llena la IA: validación por tipo, extracción con mocks, reglas de sobrescritura y de etapas,
límite por contacto, fallos silenciosos y medición."""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app import crm, db, extraccion, ia
from conftest import ENTORNO_IA, TENANT, asistente_listo, evento_entrante, firmar, respuesta_openai

CAMPO = {"texto": {"tipo": "texto"}, "numero": {"tipo": "numero"}, "moneda": {"tipo": "moneda"},
         "opcion": {"tipo": "opcion", "opciones": ["Boleta", "Factura"]}, "documento": {"tipo": "documento"}}


def json_ia(campos: dict, etapa=None, envoltura="{}"):
    return respuesta_openai(envoltura.replace("{}", json.dumps({"campos": campos, "etapa": etapa})), prompt=600, completion=60)


def preparar(base, mensajes=("Hola, quiero la polera negra talla M",)):
    asyncio.run(crm.aplicar_preset(None, TENANT, "comercio_redes"))
    asistente_listo(base)

    async def flujo():
        cid = await db.upsert_contacto_entrante(None, TENANT, "51987654321", "Ana", datetime.now(timezone.utc))
        for texto in mensajes:
            await db.insertar_mensaje(None, TENANT, cid, wamid=None, direccion="in", tipo="text", texto=texto, estado=None)
        return cid
    return asyncio.run(flujo())


def extraer(cid, **kw):
    return asyncio.run(extraccion.extraer(None, TENANT, cid, **kw))


def etapa_de(base, cid):
    return next((e["clave"] for e in base.etapas if e["id"] == base.contactos[cid]["etapa_id"]), None)


# --- Validación por tipo -------------------------------------------------------------

def test_normalizar_valores_por_tipo():
    n = crm.normalizar_valor
    assert n(CAMPO["texto"], "  Polera negra M ") == "Polera negra M"
    assert n(CAMPO["texto"], "null") is None and n(CAMPO["texto"], "N/A") is None
    assert n(CAMPO["numero"], "2") == "2" and n(CAMPO["numero"], "dos") is None and n(CAMPO["numero"], 0) is None
    assert n(CAMPO["moneda"], "S/ 1,200.50") == "1200.50" and n(CAMPO["moneda"], "45,5") == "45.50"
    assert n(CAMPO["moneda"], 89) == "89.00"
    assert n(CAMPO["opcion"], "factura") == "Factura" and n(CAMPO["opcion"], "Nota de venta") is None
    assert n(CAMPO["documento"], "DNI 45678912") == "45678912" and n(CAMPO["documento"], "20123456789") == "20123456789"
    assert n(CAMPO["documento"], "1234567") is None
    assert n(CAMPO["texto"], True) is None and n(CAMPO["texto"], {"a": 1}) is None


def test_la_ia_no_pisa_campos_humanos():
    campos = [{"clave": "producto", "tipo": "texto"}, {"clave": "monto", "tipo": "moneda"}]
    ficha = {"producto": {"valor": "Polera roja", "fuente": "humano"}, "monto": {"valor": "50.00", "fuente": "ia"}}
    cambios = crm.cambios_ia_permitidos(ficha, {"producto": "Polera negra", "monto": "60", "desconocido": "x"}, campos)
    assert list(cambios) == ["monto"] and cambios["monto"]["valor"] == "60.00" and cambios["monto"]["fuente"] == "ia"
    assert crm.cambios_ia_permitidos(ficha, {"monto": "50"}, campos) == {}  # mismo valor: no se reescribe


def test_reglas_de_etapa_para_la_ia():
    etapas = [{"id": i + 1, **e, "orden": (i + 1) * 10, "oculta": False, "es_perdido": e.get("es_perdido", False)}
              for i, e in enumerate(__import__("app.presets", fromlist=["PRESETS"]).PRESETS["comercio_redes"]["etapas"])]
    permitido = crm.etapa_ia_permitida
    assert permitido(etapas, None, "consulta") and permitido(etapas, 1, "cotizado") and permitido(etapas, 2, "pedido_confirmado")
    assert not permitido(etapas, 2, "consulta")            # nunca retrocede
    assert not permitido(etapas, 2, "cotizado")            # ni se queda igual
    assert not permitido(etapas, 3, "pagado")              # Pagado solo lo marca una persona o PagoOK
    assert not permitido(etapas, 3, "comprobante_enviado")
    assert not permitido(etapas, 1, "perdido")             # Perdido solo una persona
    assert not permitido(etapas, 7, "cotizado")            # un contacto perdido no se reactiva
    assert not permitido(etapas, 4, "pedido_confirmado")   # ya pagado: no retrocede
    assert not permitido(etapas, 1, "inexistente")


# --- Extracción con mocks -------------------------------------------------------------

def test_extraccion_completa_la_ficha_y_avanza_etapa(base, proveedor):
    cid = preparar(base, ["Hola, quiero la polera negra talla M, 2 unidades", "Con boleta, mi DNI es 45678912. Delivery."])
    proveedor.encolar(json_ia({"producto": "Polera negra talla M", "cantidad": 2, "tipo_comprobante": "boleta",
                               "documento": "45678912", "entrega": "Delivery", "direccion": None, "monto": None,
                               "medio_pago": None}, etapa="pedido_confirmado"))
    r = extraer(cid, respetar_limite=False)
    assert r.estado == "ok" and r.etapa == "Pedido confirmado"
    ficha = base.contactos[cid]["ficha"]
    assert {k: v["valor"] for k, v in ficha.items()} == {"producto": "Polera negra talla M", "cantidad": "2",
                                                         "tipo_comprobante": "Boleta", "documento": "45678912",
                                                         "entrega": "Delivery"}
    assert all(v["fuente"] == "ia" for v in ficha.values())
    assert etapa_de(base, cid) == "pedido_confirmado"
    assert base.cambios_etapa[-1]["fuente"] == "ia"
    uso = base.usos[-1]
    assert uso["origen"] == "extraccion" and uso["nivel"] == "basico" and uso["ok"] and uso["message_id"] is None
    cuerpo = proveedor.cuerpo()
    assert cuerpo["response_format"] == {"type": "json_object"} and cuerpo["max_completion_tokens"] == extraccion.MAX_TOKENS
    sistema, usuario = cuerpo["messages"]
    assert "tipo_comprobante (opción: Boleta, Factura)" in sistema["content"]
    assert "- pagado" not in sistema["content"] and "- perdido" not in sistema["content"]  # etapas no permitidas
    assert "Cliente: Con boleta, mi DNI es 45678912. Delivery." in usuario["content"]
    for dato in ("51987654321", "Ana"):
        assert dato not in json.dumps(cuerpo, ensure_ascii=False)


def test_la_ia_no_marca_pagado_aunque_el_cliente_diga_que_pago(base, proveedor):
    cid = preparar(base, ["Ya te yapeé los S/ 89, te mando la captura"])
    proveedor.encolar(json_ia({"medio_pago": "Yape", "monto": "89"}, etapa="pagado"))
    r = extraer(cid, respetar_limite=False)
    assert r.estado == "ok" and r.etapa is None and base.contactos[cid]["etapa_id"] is None
    assert base.contactos[cid]["ficha"]["medio_pago"]["valor"] == "Yape"


def test_json_dentro_de_texto_o_bloque_de_codigo(base, proveedor):
    cid = preparar(base)
    proveedor.encolar(json_ia({"producto": "Polera negra"}, envoltura="Aquí está:\n```json\n{}\n```"))
    assert extraer(cid, respetar_limite=False).estado == "ok"
    assert base.contactos[cid]["ficha"]["producto"]["valor"] == "Polera negra"


def test_json_invalido_o_valores_de_tipo_incorrecto(base, proveedor):
    cid = preparar(base)
    proveedor.encolar(respuesta_openai("No puedo ayudarte con eso"))
    r = extraer(cid, respetar_limite=False)
    assert r.estado == "error" and base.contactos[cid]["ficha"] == {}
    proveedor.encolar(json_ia({"cantidad": "muchas", "tipo_comprobante": "ticket", "documento": "123", "monto": "gratis"}))
    assert extraer(cid, respetar_limite=False).estado == "ok" and base.contactos[cid]["ficha"] == {}


def test_nunca_sobrescribe_lo_que_edito_una_persona(base, proveedor):
    cid = preparar(base)
    asyncio.run(db.editar_campo_humano(None, TENANT, cid, "producto", "Casaca azul"))
    proveedor.encolar(json_ia({"producto": "Polera negra", "cantidad": "1"}))
    r = extraer(cid, respetar_limite=False)
    ficha = base.contactos[cid]["ficha"]
    assert ficha["producto"] == {**ficha["producto"], "valor": "Casaca azul", "fuente": "humano"}
    assert ficha["cantidad"]["valor"] == "1" and r.campos == ["Cantidad"]


def test_la_etapa_solo_avanza_y_un_perdido_no_se_reactiva(base, proveedor):
    cid = preparar(base)
    proveedor.encolar(json_ia({}, etapa="cotizado"))
    extraer(cid, respetar_limite=False)
    proveedor.encolar(json_ia({}, etapa="consulta"))
    extraer(cid, respetar_limite=False)
    assert etapa_de(base, cid) == "cotizado"
    perdido = next(e for e in base.etapas if e["clave"] == "perdido")
    asyncio.run(db.mover_etapa(None, TENANT, cid, perdido["id"], motivo="Precio", autor="ana"))
    proveedor.encolar(json_ia({}, etapa="pedido_confirmado"))
    extraer(cid, respetar_limite=False)
    assert etapa_de(base, cid) == "perdido" and base.contactos[cid]["motivo_perdida"] == "Precio"


def test_si_la_ia_falla_la_ficha_queda_igual_y_se_mide(base, proveedor):
    import httpx
    cid = preparar(base)
    proveedor.encolar(httpx.Response(429, json={"error": {"message": "rate limit"}}))
    r = extraer(cid, respetar_limite=False)
    assert r.estado == "error" and base.contactos[cid]["ficha"] == {} and base.contactos[cid]["etapa_id"] is None
    assert base.usos[-1]["origen"] == "extraccion" and base.usos[-1]["ok"] is False


def test_sin_nivel_basico_no_llama_ni_falla(base, proveedor, monkeypatch):
    cid = preparar(base)
    monkeypatch.setattr(ia, "CONFIG", ia.cargar_config({"ANTHROPIC_API_KEY": "k", "IA_NIVEL_ESTANDAR": "anthropic:m"}))
    r = extraer(cid, respetar_limite=False)
    assert r.estado == "sin_nivel" and proveedor.solicitudes == [] and base.usos == []


# --- Límite por contacto (2 minutos, configurable) ---------------------------------------

def test_limite_una_extraccion_cada_dos_minutos_y_la_pendiente_incluye_lo_nuevo(base, proveedor):
    cid = preparar(base)
    assert ia.CONFIG.extraccion_intervalo_s == 120
    proveedor.encolar(json_ia({"producto": "Polera negra"}))
    assert extraer(cid).estado == "ok"
    asyncio.run(db.insertar_mensaje(None, TENANT, cid, wamid=None, direccion="in", tipo="text", texto="Mejor talla L", estado=None))
    assert extraer(cid).estado == "limitada" and base.contactos[cid]["extraccion_pendiente"] is True
    assert len(proveedor.solicitudes) == 1
    assert asyncio.run(extraccion.procesar_pendientes(None)) == 0          # aún no vence el intervalo
    base.contactos[cid]["extraccion_en"] -= timedelta(seconds=121)
    base.asistente["extraccion_activa"] = True
    proveedor.encolar(json_ia({"producto": "Polera negra talla L"}))
    assert asyncio.run(extraccion.procesar_pendientes(None)) == 1
    assert "Mejor talla L" in proveedor.cuerpo()["messages"][1]["content"]   # la siguiente incluye lo nuevo
    assert base.contactos[cid]["ficha"]["producto"]["valor"] == "Polera negra talla L"
    assert base.contactos[cid]["extraccion_pendiente"] is False


def test_intervalo_configurable(monkeypatch):
    assert ia.cargar_config({"IA_EXTRACCION_INTERVALO_S": "300"}).extraccion_intervalo_s == 300


# --- WhatsApp y simulador -------------------------------------------------------------------

def test_whatsapp_sin_interruptor_no_extrae(cliente, base, proveedor, envios):
    asyncio.run(crm.aplicar_preset(None, TENANT, "comercio_redes"))
    asistente_listo(base, activo=False)
    cuerpo = evento_entrante("wamid.1", texto="Quiero la polera negra")
    cliente.post("/webhook", content=cuerpo, headers={"X-Hub-Signature-256": firmar(cuerpo)})
    assert proveedor.solicitudes == [] and base.usos == []


def test_whatsapp_con_interruptor_extrae_sin_responder_al_cliente(cliente, base, proveedor, envios):
    asyncio.run(crm.aplicar_preset(None, TENANT, "comercio_redes"))
    asistente_listo(base, activo=False, extraccion_activa=True)
    proveedor.encolar(json_ia({"producto": "Polera negra"}, etapa="consulta"))
    cuerpo = evento_entrante("wamid.1", texto="Quiero la polera negra")
    cliente.post("/webhook", content=cuerpo, headers={"X-Hub-Signature-256": firmar(cuerpo)})
    c = next(c for c in base.contactos.values() if c["canal"] == "whatsapp")
    assert c["ficha"]["producto"]["valor"] == "Polera negra" and envios == []
    assert base.usos[-1]["origen"] == "extraccion"


def test_simulador_extrae_siempre_y_muestra_la_ficha(cliente, base, proveedor, monkeypatch):
    from conftest import respuesta_anthropic
    asyncio.run(crm.aplicar_preset(None, TENANT, "comercio_redes"))
    asistente_listo(base)  # extraccion_activa = False: en el simulador igual corre
    proveedor.encolar(respuesta_anthropic("¡Hola! La polera negra cuesta S/ 45."))
    proveedor.encolar(json_ia({"producto": "Polera negra", "monto": "45"}, etapa="cotizado"))
    r = cliente.post("/asistente/probar", data={"texto": "¿Cuánto cuesta la polera negra?"})
    t = " ".join(r.text.split())
    assert "Ficha actualizada por la IA: Producto y variante, Monto" in t and "etapa → <strong>Cotizado</strong>" in t
    assert 'id="ficha-simulador"' in r.text and "S/ 45.00" in r.text
    r = cliente.post("/asistente/probar/reiniciar")
    sim = next(c for c in base.contactos.values() if c["canal"] == "web")
    assert sim["ficha"] == {} and sim["etapa_id"] is None
