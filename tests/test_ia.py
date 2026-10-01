"""Capa de proveedores: niveles, disponibilidad según llaves, adaptadores, errores y costo."""
import asyncio

import httpx

from app import ia
from conftest import ENTORNO_IA, respuesta_anthropic, respuesta_openai

SISTEMA = ia.Sistema(estable="REGLAS Y BASE DE CONOCIMIENTO", variable="Fecha y hora actuales en Lima: lunes")
HISTORIAL = [ia.Mensaje("user", "¿Atienden hoy?")]


def generar(nivel="estandar", cfg=None):
    return asyncio.run(ia.generar_respuesta(SISTEMA, HISTORIAL, nivel, cfg))


# --- Configuración -----------------------------------------------------------

def test_sin_ninguna_llave_la_ia_queda_no_disponible_y_no_falla():
    cfg = ia.cargar_config({})
    assert cfg.proveedores_disponibles == []
    assert all(not n.disponible for n in cfg.niveles.values())
    assert cfg.whatsapp_activo is False
    assert (cfg.historial_mensajes, cfg.max_tokens, cfg.timeout_s) == (12, 1024, 30.0)


def test_niveles_proveedor_modelo_y_llaves():
    cfg = ia.cargar_config(ENTORNO_IA)
    assert cfg.proveedores_disponibles == ["anthropic", "openai"]
    estandar, basico, premium = cfg.niveles["estandar"], cfg.niveles["basico"], cfg.niveles["premium"]
    assert (estandar.proveedor, estandar.modelo, estandar.disponible) == ("anthropic", "modelo-anthropic-prueba", True)
    assert (basico.proveedor, basico.disponible) == ("openai", True)
    assert premium.disponible is False and "XAI_API_KEY" in premium.motivo
    assert estandar.precio == ia.Precio(3, 15, 0.3, 3.75)


def test_formato_invalido_se_avisa_y_no_tumba_la_app():
    cfg = ia.cargar_config({"ANTHROPIC_API_KEY": "k", "IA_NIVEL_BASICO": "anthropic-sin-modelo",
                            "IA_NIVEL_ESTANDAR": "google:gemini", "IA_PRECIO_BASICO": "barato",
                            "IA_HISTORIAL_MENSAJES": "mil", "IA_WHATSAPP_ACTIVO": "true"})
    assert not cfg.niveles["basico"].disponible and "Formato inválido" in cfg.niveles["basico"].motivo
    assert not cfg.niveles["estandar"].disponible
    assert cfg.historial_mensajes == 12
    assert cfg.whatsapp_activo is True
    assert len(cfg.avisos) == 4


def test_modelo_con_dos_puntos_se_separa_en_el_primero():
    cfg = ia.cargar_config({"OPENAI_API_KEY": "k", "IA_NIVEL_BASICO": "openai:ft:mi-modelo:v2"})
    assert cfg.niveles["basico"].modelo == "ft:mi-modelo:v2"


def test_base_url_por_defecto_y_personalizada():
    assert ia.cargar_config({}).base_urls["xai"] == "https://api.x.ai/v1"
    assert ia.cargar_config({"OPENAI_BASE_URL": "https://proxy.local/v1/"}).base_urls["openai"] == "https://proxy.local/v1"


def test_costo_estimado():
    uso = ia.Uso(entrada=1_000_000, salida=100_000, cache_lectura=2_000_000, cache_escritura=0)
    assert ia.costo_estimado(ia.Precio(3, 15, 0.3, 3.75), uso) == 3 + 1.5 + 0.6
    assert ia.costo_estimado(ia.Precio(1, 2), ia.Uso(entrada=0, salida=0, cache_lectura=1_000_000)) == 1.0
    assert ia.costo_estimado(None, uso) is None


# --- Adaptadores -------------------------------------------------------------

def test_anthropic_envia_sistema_en_dos_bloques_y_cachea_solo_el_estable(proveedor):
    proveedor.encolar(respuesta_anthropic("Sí, hoy atendemos.", entrada=120, salida=12, cache_lectura=1500))
    r = generar("estandar")
    assert r.ok and r.texto == "Sí, hoy atendemos."
    solicitud = proveedor.solicitudes[0]
    assert str(solicitud.url) == ia.URL_ANTHROPIC
    assert solicitud.headers["x-api-key"] == "sk-ant-prueba"
    assert solicitud.headers["anthropic-version"] == ia.VERSION_ANTHROPIC
    cuerpo = proveedor.cuerpo()
    assert cuerpo["model"] == "modelo-anthropic-prueba" and cuerpo["max_tokens"] == 1024
    estable, variable = cuerpo["system"]
    assert estable == {"type": "text", "text": SISTEMA.estable, "cache_control": {"type": "ephemeral"}}
    assert variable == {"type": "text", "text": SISTEMA.variable}  # la fecha queda fuera del caché
    assert cuerpo["messages"] == [{"role": "user", "content": "¿Atienden hoy?"}]
    assert (r.uso.entrada, r.uso.salida, r.uso.cache_lectura) == (120, 12, 1500)
    assert r.costo == round((120 * 3 + 12 * 15 + 1500 * 0.3) / 1_000_000, 6)
    assert r.proveedor == "anthropic" and r.modelo == "modelo-anthropic-prueba"


def test_openai_usa_chat_completions_y_descuenta_la_cache(proveedor):
    proveedor.encolar(respuesta_openai("Hola", prompt=1000, completion=20, cacheados=800))
    r = generar("basico")
    assert r.ok
    solicitud = proveedor.solicitudes[0]
    assert str(solicitud.url) == "https://api.openai.com/v1/chat/completions"
    assert solicitud.headers["authorization"] == "Bearer sk-openai-prueba"
    cuerpo = proveedor.cuerpo()
    assert cuerpo["max_completion_tokens"] == 1024 and "max_tokens" not in cuerpo
    assert cuerpo["messages"][0]["role"] == "system"
    assert cuerpo["messages"][0]["content"].startswith(SISTEMA.estable)  # prefijo estable → caché por prefijo
    assert cuerpo["messages"][0]["content"].endswith(SISTEMA.variable)
    assert (r.uso.entrada, r.uso.cache_lectura, r.uso.salida) == (200, 800, 20)
    assert r.costo is None  # el nivel básico no tiene precio configurado


def test_xai_usa_su_base_url_y_max_tokens(proveedor, monkeypatch):
    monkeypatch.setattr(ia, "CONFIG", ia.cargar_config({**ENTORNO_IA, "XAI_API_KEY": "xai-prueba"}))
    proveedor.encolar(respuesta_openai("Hola"))
    assert generar("premium").ok
    assert str(proveedor.solicitudes[0].url) == "https://api.x.ai/v1/chat/completions"
    assert proveedor.cuerpo()["max_tokens"] == 1024


def test_errores_legibles(proveedor):
    proveedor.encolar(httpx.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}}))
    r = generar()
    assert not r.ok and "llave de API de Anthropic no es válida" in r.error and "invalid x-api-key" in r.error

    proveedor.encolar(httpx.Response(429, json={"error": {"message": "rate limit"}}))
    assert "límite de uso o saldo" in generar().error

    proveedor.encolar(httpx.Response(404, json={"error": {"message": "model not found"}}))
    assert "modelo «modelo-anthropic-prueba» no existe" in generar().error

    proveedor.encolar(httpx.Response(529, json={"error": {"message": "overloaded"}}))
    assert "no está disponible en este momento" in generar().error

    proveedor.encolar(httpx.ReadTimeout("lento"))
    assert "no respondió en 30 segundos" in generar().error

    proveedor.encolar(httpx.ConnectError("sin red"))
    assert "No se pudo conectar con Anthropic" in generar().error

    proveedor.encolar(respuesta_anthropic("   "))
    assert "respuesta vacía" in generar().error


def test_nivel_no_disponible_no_llama_al_proveedor(proveedor):
    r = generar("premium")
    assert not r.ok and "no está disponible" in r.error and "XAI_API_KEY" in r.error
    assert proveedor.solicitudes == []


def test_sin_mensaje_del_cliente_no_llama_al_proveedor(proveedor):
    r = asyncio.run(ia.generar_respuesta(SISTEMA, [], "estandar"))
    assert not r.ok and proveedor.solicitudes == []
