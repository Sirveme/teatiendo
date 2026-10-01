"""Correcciones tras la primera prueba real: respuestas cortadas por límite de tokens, esfuerzo de razonamiento,
tokens de razonamiento, regla de urgencias y formato WhatsApp en el simulador."""
import asyncio
import json

import httpx
import pytest

from app import asistente, db, ia
from app.config import formato_whatsapp
from conftest import ENTORNO_IA, TENANT, asistente_listo, evento_entrante, firmar, respuesta_anthropic

SISTEMA = ia.Sistema("REGLAS", "Fecha")
HISTORIAL = [ia.Mensaje("user", "Hola")]


def anthropic_cortada(texto="Entiendo, espero que t", salida=400):
    return httpx.Response(200, json={"content": [{"type": "thinking", "thinking": ""}, {"type": "text", "text": texto}],
                                     "stop_reason": "max_tokens", "usage": {"input_tokens": 900, "output_tokens": salida}})


def openai_resp(texto, finish="stop", razonamiento=None):
    uso = {"prompt_tokens": 500, "completion_tokens": 120}
    if razonamiento is not None:
        uso["completion_tokens_details"] = {"reasoning_tokens": razonamiento}
    return httpx.Response(200, json={"choices": [{"message": {"content": texto}, "finish_reason": finish}], "usage": uso})


@pytest.fixture(autouse=True)
def limpiar_memoria_de_modelos():
    ia._sin_parametros_razonamiento.clear()
    yield
    ia._sin_parametros_razonamiento.clear()


def generar(nivel="estandar", **kw):
    return asyncio.run(ia.generar_respuesta(SISTEMA, HISTORIAL, nivel, **kw))


# --- Esfuerzo de razonamiento -----------------------------------------------------

def test_por_defecto_se_pide_el_menor_esfuerzo(proveedor):
    assert ia.CONFIG.niveles["estandar"].esfuerzo == "low" and ia.CONFIG.max_tokens == 1024
    proveedor.encolar(respuesta_anthropic("Hola"))
    generar("estandar")
    cuerpo = proveedor.cuerpo()
    assert cuerpo["output_config"] == {"effort": "low"} and "thinking" not in cuerpo and cuerpo["max_tokens"] == 1024
    proveedor.encolar(openai_resp("Hola"))
    generar("basico")
    assert proveedor.cuerpo()["reasoning_effort"] == "low"


def test_sin_razonamiento_y_omitir(proveedor, monkeypatch):
    monkeypatch.setattr(ia, "CONFIG", ia.cargar_config({**ENTORNO_IA, "IA_ESFUERZO_ESTANDAR": "sin_razonamiento",
                                                        "IA_ESFUERZO_BASICO": "omitir"}))
    proveedor.encolar(respuesta_anthropic("Hola"))
    generar("estandar")
    cuerpo = proveedor.cuerpo()
    assert cuerpo["thinking"] == {"type": "between_tools"} and "output_config" not in cuerpo
    proveedor.encolar(openai_resp("Hola"))
    generar("basico")
    assert "reasoning_effort" not in proveedor.cuerpo()


def test_esfuerzo_invalido_avisa_y_usa_low():
    cfg = ia.cargar_config({**ENTORNO_IA, "IA_ESFUERZO_PREMIUM": "turbo"})
    assert cfg.niveles["premium"].esfuerzo == "low" and any("IA_ESFUERZO_PREMIUM" in a for a in cfg.avisos)


def test_modelo_que_rechaza_el_esfuerzo_se_reintenta_sin_el_y_se_recuerda(proveedor):
    proveedor.encolar(httpx.Response(400, json={"type": "error", "error": {
        "type": "invalid_request_error", "message": "output_config.effort: This model does not support effort."}}))
    proveedor.encolar(respuesta_anthropic("Hola sin esfuerzo"))
    r = generar("estandar")
    assert r.ok and r.texto == "Hola sin esfuerzo"
    assert "output_config" in proveedor.cuerpo(0) and "output_config" not in proveedor.cuerpo(1)
    proveedor.encolar(respuesta_anthropic("Otra vez"))
    generar("estandar")
    assert "output_config" not in proveedor.cuerpo(2)  # ya no se vuelve a intentar con ese modelo


def test_otro_400_no_dispara_el_reintento_sin_esfuerzo(proveedor):
    proveedor.encolar(httpx.Response(400, json={"error": {"message": "messages: field required"}}))
    r = generar("estandar")
    assert not r.ok and len(proveedor.solicitudes) == 1


# --- Respuestas cortadas ---------------------------------------------------------------

def test_respuesta_cortada_se_reintenta_con_mas_margen(proveedor):
    proveedor.encolar(anthropic_cortada())
    proveedor.encolar(respuesta_anthropic("Entiendo, espero que te mejores pronto.", entrada=900, salida=60))
    r = generar("estandar")
    assert r.ok and r.texto == "Entiendo, espero que te mejores pronto." and r.reintentos == 1 and not r.truncado
    assert proveedor.cuerpo(0)["max_tokens"] == 1024 and proveedor.cuerpo(1)["max_tokens"] == 4096
    assert (r.uso.entrada, r.uso.salida) == (1800, 460)        # se suman ambos intentos
    assert r.costo == round((1800 * 3 + 460 * 15) / 1_000_000, 6)


def test_si_tambien_se_corta_al_reintentar_nunca_se_usa(proveedor):
    proveedor.encolar(anthropic_cortada())
    proveedor.encolar(anthropic_cortada("Entiendo, espero que te mejo", 4096))
    r = generar("estandar")
    assert not r.ok and r.truncado and r.texto == "" and "límite de tokens" in r.error and "4096" in r.error


def test_openai_finish_reason_length_y_tokens_de_razonamiento(proveedor):
    proveedor.encolar(openai_resp("Hola, te cuen", finish="length", razonamiento=1024))
    proveedor.encolar(openai_resp("Hola, te cuento que sí.", razonamiento=300))
    r = generar("basico")
    assert r.ok and r.reintentos == 1 and r.uso.razonamiento == 1324
    assert proveedor.cuerpo(1)["max_completion_tokens"] == 4096


def test_rechazo_del_proveedor_no_se_envia(proveedor):
    proveedor.encolar(httpx.Response(200, json={"content": [], "stop_reason": "refusal", "usage": {}}))
    r = generar("estandar")
    assert not r.ok and "filtro de seguridad" in r.error


def test_cortada_en_whatsapp_nunca_se_envia_y_deriva(cliente, base, proveedor, envios, monkeypatch):
    monkeypatch.setattr(ia, "CONFIG", ia.cargar_config({**ENTORNO_IA, "IA_WHATSAPP_ACTIVO": "true"}))
    asistente_listo(base)
    proveedor.encolar(anthropic_cortada())
    proveedor.encolar(anthropic_cortada("Entiendo, espero", 4096))
    cuerpo = evento_entrante("wamid.1", texto="Me siento mal")
    cliente.post("/webhook", content=cuerpo, headers={"X-Hub-Signature-256": firmar(cuerpo)})
    assert len(envios) == 1 and envios[0][1].startswith("Te paso con una persona")
    assert "espero" not in envios[0][1]
    uso = base.usos[-1]
    assert uso["truncado"] is True and uso["reintentos"] == 1 and uso["ok"] is False


def test_cortada_en_el_simulador_muestra_aviso_claro(cliente, base, proveedor):
    asistente_listo(base)
    proveedor.encolar(anthropic_cortada())
    proveedor.encolar(anthropic_cortada("Entiendo", 4096))
    r = cliente.post("/asistente/probar", data={"texto": "Hola"})
    assert "Respuesta cortada por el límite de tokens." in r.text and "no se enviaría" in r.text


def test_reintento_exitoso_se_muestra_en_las_metricas(cliente, base, proveedor):
    asistente_listo(base)
    proveedor.encolar(anthropic_cortada())
    proveedor.encolar(respuesta_anthropic("Respuesta completa."))
    r = cliente.post("/asistente/probar", data={"texto": "Hola"})
    assert "Respuesta completa." in r.text and "se reintentó por límite de tokens" in r.text


# --- Urgencias --------------------------------------------------------------------------

def test_prompt_incluye_regla_de_urgencias():
    from datetime import datetime
    s = asistente.construir_sistema({**asistente.DEFECTOS, "nombre_negocio": "X"}, datetime(2026, 9, 28, 10))
    for frase in ("URGENCIAS", "acuda a emergencia", "106 SAMU", "Nunca minimices", "indicaciones médicas",
                  "dirección o el teléfono del negocio", "[URGENCIA] y [DERIVAR]"):
        assert frase in s.estable, frase


def test_urgencia_envia_la_indicacion_de_emergencia_y_deriva(base, proveedor):
    asistente_listo(base, horarios_contacto="Av. Ejemplo 123. Teléfono +51 900 000 000.")
    proveedor.encolar(respuesta_anthropic(
        "Por favor acude de inmediato a emergencia o llama al 106 (SAMU). Nuestra dirección: Av. Ejemplo 123, "
        "teléfono +51 900 000 000. [URGENCIA] [DERIVAR]"))

    async def flujo():
        cid = await db.contacto_simulador(None, TENANT)
        await db.insertar_mensaje(None, TENANT, cid, wamid=None, direccion="in", tipo="text",
                                  texto="Me duele mucho el pecho y no puedo respirar", estado=None, canal="web")
        return cid, await asistente.atender(None, TENANT, cid, tipo="text", origen="simulador", canal="web")
    cid, r = asyncio.run(flujo())
    assert r.accion == "derivar" and r.motivo.startswith("Urgencia")
    assert r.texto.startswith("Por favor acude de inmediato a emergencia o llama al 106 (SAMU)")
    assert "Av. Ejemplo 123" in r.texto and r.texto.endswith("de lunes a viernes de 9:00 a 18:00.")
    assert "[URGENCIA]" not in r.texto and "[DERIVAR]" not in r.texto
    assert base.contactos[cid]["modo"] == "humano" and base.contactos[cid]["derivado_en"] is not None
    assert len([m for m in base.mensajes_de(cid) if m["direccion"] == "out"]) == 1  # un solo mensaje


# --- Formato WhatsApp ---------------------------------------------------------------------

def test_formato_whatsapp():
    assert str(formato_whatsapp("La *polera negra* cuesta _S/ 45_")) == "La <strong>polera negra</strong> cuesta <em>S/ 45</em>"
    assert str(formato_whatsapp("~antes~ y ```codigo```")) == "<s>antes</s> y <code>codigo</code>"
    assert str(formato_whatsapp("<script>alert(1)</script> *hola*")) == "&lt;script&gt;alert(1)&lt;/script&gt; <strong>hola</strong>"
    assert str(formato_whatsapp("2*3*4 y nombre_de_archivo_x")) == "2*3*4 y nombre_de_archivo_x"  # no rompe palabras
    assert str(formato_whatsapp("* no es negrita *")) == "* no es negrita *"
    assert str(formato_whatsapp(None)) == ""


def test_bandeja_muestra_formato_whatsapp_y_escapa_html(cliente, base):
    cuerpo = evento_entrante("wamid.f1", texto="Quiero la *polera negra* _talla M_ <b>ya</b>")
    cliente.post("/webhook", content=cuerpo, headers={"X-Hub-Signature-256": firmar(cuerpo)})
    cid = next(c for c in base.contactos.values() if c["canal"] == "whatsapp")["id"]
    pagina = cliente.get(f"/bandeja/{cid}").text
    assert "Quiero la <strong>polera negra</strong> <em>talla M</em> &lt;b&gt;ya&lt;/b&gt;" in pagina


def test_simulador_muestra_negrita_y_cursiva(cliente, base, proveedor):
    asistente_listo(base)
    proveedor.encolar(respuesta_anthropic("La *consulta general* cuesta _S/ 60_."))
    r = cliente.post("/asistente/probar", data={"texto": "¿Precio?"})
    assert "La <strong>consulta general</strong> cuesta <em>S/ 60</em>." in r.text
