"""Flujo por WhatsApp con mocks (webhook → asistente → Meta), derivación en la bandeja y pantalla Asistente."""
import asyncio

import pytest

from app import asistente, db, ia
from conftest import ENTORNO_IA, TENANT, asistente_listo, evento_entrante, firmar, respuesta_anthropic


@pytest.fixture
def whatsapp_encendido(proveedor, monkeypatch):
    monkeypatch.setattr(ia, "CONFIG", ia.cargar_config({**ENTORNO_IA, "IA_WHATSAPP_ACTIVO": "true"}))
    return proveedor


def recibir(cliente, cuerpo: bytes):
    r = cliente.post("/webhook", content=cuerpo, headers={"X-Hub-Signature-256": firmar(cuerpo)})
    assert r.status_code == 200
    return r


def contacto_whatsapp(base):
    return next(c for c in base.contactos.values() if c["canal"] == "whatsapp")


# --- WhatsApp ----------------------------------------------------------------

def test_apagado_por_defecto_no_llama_a_la_ia(cliente, base, proveedor, envios):
    assert ia.CONFIG.whatsapp_activo is False
    asistente_listo(base)
    recibir(cliente, evento_entrante("wamid.1"))
    assert len(base.mensajes) == 1 and proveedor.solicitudes == [] and envios == []


def test_encendido_responde_y_envia_por_whatsapp(cliente, base, whatsapp_encendido, envios):
    asistente_listo(base)
    whatsapp_encendido.encolar(respuesta_anthropic("¡Hola! Atendemos de lunes a viernes de 8:00 a 20:00."))
    recibir(cliente, evento_entrante("wamid.1", texto="¿Qué horario tienen?"))
    c = contacto_whatsapp(base)
    assert envios == [("51987654321", "¡Hola! Atendemos de lunes a viernes de 8:00 a 20:00.")]
    salida = base.mensajes[-1]
    assert (salida["direccion"], salida["wamid"], salida["estado"], salida["generado_por_ia"]) == ("out", "wamid.IA1", "sent", True)
    assert base.usos[-1]["origen"] == "whatsapp" and base.usos[-1]["message_id"] == salida["id"]
    assert base.candados == [c["id"]]  # pasó por el candado de PostgreSQL del contacto


def test_asistente_inactivo_no_responde(cliente, base, whatsapp_encendido, envios):
    asistente_listo(base, activo=False)
    recibir(cliente, evento_entrante("wamid.1"))
    assert whatsapp_encendido.solicitudes == [] and envios == []


def test_contacto_en_modo_humano_no_recibe_respuesta(cliente, base, whatsapp_encendido, envios):
    asistente_listo(base)
    recibir(cliente, evento_entrante("wamid.1"))
    contacto_whatsapp(base)["modo"] = "humano"
    recibir(cliente, evento_entrante("wamid.2", texto="¿Siguen ahí?"))
    assert len(whatsapp_encendido.solicitudes) == 1 and len(envios) == 1


def test_evento_duplicado_genera_una_sola_respuesta(cliente, base, whatsapp_encendido, envios):
    asistente_listo(base)
    cuerpo = evento_entrante("wamid.1")
    recibir(cliente, cuerpo)
    recibir(cliente, cuerpo)
    assert len(whatsapp_encendido.solicitudes) == 1 and len(envios) == 1


def test_si_llego_un_mensaje_mas_nuevo_la_tarea_antigua_no_responde(base, whatsapp_encendido, envios):
    asistente_listo(base)

    async def flujo():
        cid = await db.upsert_contacto(None, TENANT, "51987654321")
        viejo = await db.insertar_mensaje(None, TENANT, cid, wamid="w1", direccion="in", tipo="text", texto="Hola", estado=None)
        await db.insertar_mensaje(None, TENANT, cid, wamid="w2", direccion="in", tipo="text", texto="¿Precio?", estado=None)
        return await asistente.al_recibir_whatsapp(None, TENANT, cid, viejo, "text")
    assert asyncio.run(flujo()) is None
    assert whatsapp_encendido.solicitudes == [] and envios == []


def test_derivar_por_whatsapp_marca_requiere_atencion_y_se_puede_devolver(cliente, base, whatsapp_encendido, envios):
    asistente_listo(base)
    whatsapp_encendido.encolar(respuesta_anthropic("Te comunico con alguien. [DERIVAR]"))
    recibir(cliente, evento_entrante("wamid.1", texto="Quiero hablar con una persona"))
    c = contacto_whatsapp(base)
    assert envios == [("51987654321", "Te paso con una persona; te responderá de lunes a viernes de 9:00 a 18:00.")]
    assert c["modo"] == "humano" and c["derivado_en"] is not None

    assert "Requiere atención" in cliente.get("/bandeja").text
    pagina = cliente.get(f"/bandeja/{c['id']}").text
    assert "Requiere atención." in pagina and "Devolver al asistente" in pagina

    r = cliente.post(f"/bandeja/{c['id']}/devolver-ia")
    assert "El asistente vuelve a responder" in r.text
    assert c["modo"] == "ia" and c["derivado_en"] is None


def test_imagen_por_whatsapp_deriva_sin_llamar_a_la_ia(cliente, base, whatsapp_encendido, envios):
    asistente_listo(base)
    recibir(cliente, evento_entrante("wamid.1", tipo="image"))
    assert whatsapp_encendido.solicitudes == []
    assert len(envios) == 1 and contacto_whatsapp(base)["modo"] == "humano"


def test_audio_por_whatsapp_primero_pide_texto(cliente, base, whatsapp_encendido, envios):
    asistente_listo(base)
    recibir(cliente, evento_entrante("wamid.1", tipo="audio"))
    assert envios[-1][1] == "Por ahora solo leo texto. ¿Puedes escribir tu consulta?"
    assert contacto_whatsapp(base)["modo"] == "ia"
    recibir(cliente, evento_entrante("wamid.2", tipo="audio"))
    assert contacto_whatsapp(base)["modo"] == "humano" and len(envios) == 2


def test_si_la_ia_falla_en_whatsapp_el_cliente_pasa_a_una_persona(cliente, base, whatsapp_encendido, envios):
    asistente_listo(base, nivel="premium")  # sin XAI_API_KEY
    recibir(cliente, evento_entrante("wamid.1"))
    assert envios[-1][1].startswith("Te paso con una persona")
    assert contacto_whatsapp(base)["modo"] == "humano"
    assert base.usos[-1]["ok"] is False and base.usos[-1]["message_id"] == base.mensajes[-1]["id"]


def test_respuesta_manual_del_equipo_pasa_el_contacto_a_humano(cliente, base, proveedor, envios):
    asistente_listo(base)
    recibir(cliente, evento_entrante("wamid.1"))
    c = contacto_whatsapp(base)
    assert c["modo"] == "ia"
    cliente.post(f"/bandeja/{c['id']}/responder", data={"texto": "Hola, soy Ana del equipo"})
    assert envios == [("51987654321", "Hola, soy Ana del equipo")]
    assert c["modo"] == "humano" and c["derivado_en"] is None
    assert "Atendida por tu equipo" in cliente.get(f"/bandeja/{c['id']}").text


# --- Panel -------------------------------------------------------------------

def test_el_simulador_no_aparece_en_la_bandeja(cliente, base, proveedor):
    asistente_listo(base)
    cliente.post("/asistente/probar", data={"texto": "Hola"})
    sim = next(c for c in base.contactos.values() if c["canal"] == "web")
    assert "Simulador" not in cliente.get("/bandeja").text
    assert cliente.get(f"/bandeja/{sim['id']}").status_code == 404


def test_pantalla_asistente_y_simulador(cliente, base, proveedor):
    asistente_listo(base)
    for pestana in ("probar", "conocimiento", "configuracion", "uso"):
        assert cliente.get(f"/asistente?pestana={pestana}").status_code == 200
    proveedor.encolar(respuesta_anthropic("La consulta cuesta S/ 60.", entrada=1000, salida=20))
    r = cliente.post("/asistente/probar", data={"texto": "¿Precio?", "tipo": "text"})
    assert "La consulta cuesta S/ 60." in r.text
    assert "modelo-anthropic-prueba · 1000 entrada · 20 salida · US$ 0.00330 · " in " ".join(r.text.split())

    r = cliente.post("/asistente/probar", data={"tipo": "audio"})
    assert "Por ahora solo leo texto" in r.text and "Mensaje automático, sin llamada a la IA" in r.text

    uso = cliente.get("/asistente?pestana=uso").text
    assert "Simulador" in uso and "US$ 0.0033" in uso

    r = cliente.post("/asistente/probar/reiniciar")
    assert "Escribe como si fueras un cliente" in r.text
    assert len(base.usos) == 1  # el uso medido se conserva


def test_configuracion_valida_activacion(cliente, base, proveedor):
    datos = {"nombre_negocio": "Mi negocio", "nombre_asistente": "Sofía", "trato": "usted", "nivel": "estandar",
             "horario_humano": "de 9 a 18", "mensaje_derivacion": "", "mensaje_no_texto": "", "activo": "1"}
    r = cliente.post("/asistente/configuracion", data=datos)
    assert "Carga la base de conocimiento antes de activar" in r.text

    cliente.post("/asistente/conocimiento", data={"sobre_negocio": "Vendemos pan.", "reglas": "No fiar."})
    r = cliente.post("/asistente/configuracion", data={**datos, "nivel": "premium"})
    assert "el nivel Premium no está disponible" in r.text

    r = cliente.post("/asistente/configuracion", data=datos, follow_redirects=False)
    assert r.status_code == 303
    assert base.asistente["activo"] is True and base.asistente["trato"] == "usted"
    assert base.asistente["sobre_negocio"] == "Vendemos pan."
    assert "Configuración del asistente guardada" in cliente.get("/asistente?pestana=configuracion").text


def test_cargar_ejemplo_clinica_esta_marcado_como_ficticio(cliente, base, proveedor):
    pagina = cliente.get("/asistente?pestana=conocimiento").text
    assert "Cargar ejemplo: clínica" in pagina
    for clave, texto in asistente.EJEMPLO_CLINICA.items():
        assert texto.startswith("EJEMPLO FICTICIO"), clave
    assert "Clínica Demo Amazonía" in asistente.EJEMPLO_CLINICA["sobre_negocio"]
