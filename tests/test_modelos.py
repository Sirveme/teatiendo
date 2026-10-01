"""Panel de modelos de IA: cifrado de llaves, respaldo POR NIVEL en variables de entorno, costo con historial de
precios, acceso solo del superadministrador, «Probar» y comparador."""
import asyncio
import json

import httpx
import pytest
from cryptography.fernet import Fernet

from app import cifrado, config_ia, db, ia
from conftest import ENTORNO_IA, TENANT, asistente_listo, respuesta_anthropic, respuesta_openai

LLAVE_API = "sk-prueba-SECRETO-ab12"


@pytest.fixture
def maestra(monkeypatch):
    llave = Fernet.generate_key().decode()
    monkeypatch.setenv("IA_LLAVE_MAESTRA", llave)
    return llave


def crear(base, *, precio=("2", "10"), activo=True, proveedor_activo=True, llave=LLAVE_API, tipo="compatible_openai",
          extra=None, esfuerzo="medium"):
    async def flujo():
        pid = await db.crear_proveedor_ia(
            None, nombre=f"Proveedor {len(base.proveedores_ia) + 1}", tipo=tipo, url_base="https://api.ejemplo-ia.test/v1",
            campo_max_tokens="max_completion_tokens", activo=proveedor_activo,
            llave_cifrada=cifrado.cifrar(llave) if llave else None, llave_ultimos4=cifrado.ultimos4(llave) if llave else None)
        datos = {"proveedor_id": pid, "modelo_id": f"modelo-{len(base.modelos_ia) + 1}", "nombre_visible": "Modelo de prueba",
                 "precio_entrada": float(precio[0]) if precio else None, "precio_salida": float(precio[1]) if precio else None,
                 "precio_cache_lectura": None, "precio_cache_escritura": None, "max_tokens": 1500, "esfuerzo": esfuerzo,
                 "parametros_extra": extra or {}, "activo": activo, "notas": ""}
        return await db.crear_modelo_ia(None, datos, "superadmin")
    return asyncio.run(flujo())


def obtener():
    return asyncio.run(config_ia.obtener(None, forzar=True))


# --- Cifrado ----------------------------------------------------------------------------

def test_cifrado_ida_y_vuelta(maestra):
    token = cifrado.cifrar(LLAVE_API)
    assert token != LLAVE_API and LLAVE_API not in token
    assert cifrado.descifrar(token) == LLAVE_API
    assert cifrado.ultimos4(LLAVE_API) == "ab12" and cifrado.enmascarar("ab12") == "••••ab12"
    assert cifrado.enmascarar(None) == "Sin llave" and cifrado.motivo_no_disponible() is None


def test_sin_llave_maestra_o_incorrecta(monkeypatch, maestra):
    token = cifrado.cifrar(LLAVE_API)
    monkeypatch.setenv("IA_LLAVE_MAESTRA", Fernet.generate_key().decode())
    with pytest.raises(cifrado.ErrorCifrado, match="no es la misma"):
        cifrado.descifrar(token)
    monkeypatch.setenv("IA_LLAVE_MAESTRA", "corta")
    with pytest.raises(cifrado.ErrorCifrado, match="no es una llave válida"):
        cifrado.cifrar("x")
    monkeypatch.delenv("IA_LLAVE_MAESTRA")
    assert "IA_LLAVE_MAESTRA" in cifrado.motivo_no_disponible()


# --- Respaldo por nivel en variables de entorno ----------------------------------------------

def test_sin_configuracion_en_la_base_se_usan_las_variables(base, proveedor):
    cfg = obtener()
    assert all(n.origen == "entorno" for n in cfg.niveles.values())
    assert cfg.niveles["estandar"].modelo == "modelo-anthropic-prueba"


def test_respaldo_por_nivel(base, proveedor, maestra):
    mid = crear(base)
    base.niveles_map["basico"] = mid
    cfg = obtener()
    basico, estandar = cfg.niveles["basico"], cfg.niveles["estandar"]
    assert basico.origen == "base" and basico.modelo == "modelo-1" and basico.llave == LLAVE_API
    assert basico.url_base == "https://api.ejemplo-ia.test/v1" and basico.max_tokens == 1500 and basico.esfuerzo == "medium"
    assert basico.precio == ia.Precio(2.0, 10.0, None, None) and basico.precio_id == 1 and basico.modelo_ref_id == mid
    assert estandar.origen == "entorno"  # los niveles sin asignar siguen con las variables


@pytest.mark.parametrize("cambio,motivo", [
    ({"activo": False}, "el modelo está inactivo"),
    ({"proveedor_activo": False}, "el proveedor está inactivo"),
    ({"llave": None}, "no tiene llave"),
])
def test_modelo_no_utilizable_usa_el_respaldo_del_entorno(base, proveedor, maestra, cambio, motivo):
    base.niveles_map["estandar"] = crear(base, **cambio)
    efectivo = obtener().niveles["estandar"]
    assert efectivo.origen == "entorno" and efectivo.disponible
    assert motivo in config_ia.nivel_desde_fila("estandar", base._fila_modelo(base.niveles_map["estandar"])).motivo


def test_si_ni_la_base_ni_el_entorno_sirven_se_muestra_el_motivo(base, proveedor, maestra, monkeypatch):
    base.niveles_map["premium"] = crear(base, activo=False)  # premium tampoco tiene llave en el entorno
    efectivo = obtener().niveles["premium"]
    assert not efectivo.disponible and efectivo.motivo == "el modelo está inactivo"


def test_llave_maestra_cambiada_cae_al_respaldo(base, proveedor, maestra, monkeypatch):
    base.niveles_map["estandar"] = crear(base)
    monkeypatch.setenv("IA_LLAVE_MAESTRA", Fernet.generate_key().decode())
    assert obtener().niveles["estandar"].origen == "entorno"


def test_cache_y_su_invalidacion(base, proveedor, maestra):
    assert asyncio.run(config_ia.obtener(None)).niveles["basico"].origen == "entorno"
    base.niveles_map["basico"] = crear(base)
    assert asyncio.run(config_ia.obtener(None)).niveles["basico"].origen == "entorno"  # aún en caché
    config_ia.invalidar()
    assert asyncio.run(config_ia.obtener(None)).niveles["basico"].origen == "base"


def test_la_llamada_usa_la_conexion_de_la_base(base, proveedor, maestra):
    base.niveles_map["estandar"] = crear(base, extra={"temperature": 0.2, "model": "intento-de-pisar"})
    asistente_listo(base)
    proveedor.encolar(respuesta_openai("Hola desde la base", prompt=1000, completion=100))

    async def flujo():
        cid = await db.contacto_simulador(None, TENANT)
        await db.insertar_mensaje(None, TENANT, cid, wamid=None, direccion="in", tipo="text", texto="Hola", estado=None, canal="web")
        from app import asistente
        return await asistente.atender(None, TENANT, cid, tipo="text", origen="simulador", canal="web")
    r = asyncio.run(flujo())
    assert r.accion == "responder" and r.texto == "Hola desde la base"
    solicitud = proveedor.solicitudes[-1]
    cuerpo = json.loads(solicitud.content)
    assert str(solicitud.url) == "https://api.ejemplo-ia.test/v1/chat/completions"
    assert solicitud.headers["authorization"] == f"Bearer {LLAVE_API}"
    assert cuerpo["model"] == "modelo-1" and cuerpo["max_completion_tokens"] == 1500
    assert cuerpo["reasoning_effort"] == "medium" and cuerpo["temperature"] == 0.2  # extra sí; "model" protegido
    uso = base.usos[-1]
    assert uso["modelo_ref_id"] == 1 and uso["precio_id"] == 1
    assert uso["costo_estimado"] == round((1000 * 2 + 100 * 10) / 1_000_000, 6)


# --- Costo con historial de precios --------------------------------------------------------------

def test_costo_con_el_precio_vigente_en_su_momento(cliente, base, proveedor, maestra):
    mid = crear(base, precio=("2", "10"))
    base.niveles_map["estandar"] = mid
    asistente_listo(base)
    proveedor.encolar(respuesta_openai("Antes del cambio", prompt=1000, completion=100))
    cliente.post("/asistente/probar", data={"texto": "Hola"})
    antes = dict(next(u for u in base.usos if u["origen"] == "simulador"))
    assert antes["costo_estimado"] == 0.003 and antes["precio_id"] == 1

    datos = {**base.modelos_ia[mid], "precio_entrada": 4.0, "precio_salida": 20.0}
    datos.pop("id")
    assert asyncio.run(db.actualizar_modelo_ia(None, mid, datos, "superadmin")) is True
    assert len(base.precios_hist) == 2
    assert asyncio.run(db.actualizar_modelo_ia(None, mid, datos, "superadmin")) is False  # sin cambio de precio
    assert len(base.precios_hist) == 2

    config_ia.invalidar()
    proveedor.encolar(respuesta_openai("Después del cambio", prompt=1000, completion=100))
    cliente.post("/asistente/probar", data={"texto": "Otra"})
    despues = [u for u in base.usos if u["origen"] == "simulador"][-1]
    assert despues["costo_estimado"] == 0.006 and despues["precio_id"] == 2
    primero = next(u for u in base.usos if u["origen"] == "simulador")
    assert primero["costo_estimado"] == antes["costo_estimado"] and primero["precio_id"] == 1  # no cambia


# --- Panel: acceso, llaves, validaciones -------------------------------------------------------

def test_solo_el_superadministrador(cliente, base, monkeypatch):
    assert cliente.get("/superadmin/ia").status_code == 200       # ADMIN_EMAIL es el respaldo
    assert 'href="/superadmin/ia"' in cliente.get("/bandeja").text
    monkeypatch.setenv("SUPERADMIN_EMAILS", "otra@prueba.pe, jefa@prueba.pe")
    assert cliente.get("/superadmin/ia").status_code == 403
    assert cliente.post("/superadmin/ia/niveles", data={}).status_code == 403
    assert 'href="/superadmin/ia"' not in cliente.get("/bandeja").text
    monkeypatch.setenv("SUPERADMIN_EMAILS", "otra@prueba.pe, ADMIN@prueba.pe")
    assert cliente.get("/superadmin/ia").status_code == 200


def test_llave_cifrada_y_nunca_en_el_html(cliente, base, maestra):
    datos = {"nombre": "OpenAI", "tipo": "compatible_openai", "url_base": "https://api.openai.com/v1/",
             "campo_max_tokens": "max_completion_tokens", "activo": "1", "llave": LLAVE_API}
    assert cliente.post("/superadmin/ia/proveedores", data=datos, follow_redirects=False).status_code == 303
    p = base.proveedores_ia[1]
    assert p["url_base"] == "https://api.openai.com/v1" and p["llave_ultimos4"] == "ab12"
    assert p["llave_cifrada"] != LLAVE_API and cifrado.descifrar(p["llave_cifrada"]) == LLAVE_API
    pagina = cliente.get("/superadmin/ia?pestana=proveedores").text
    assert "••••ab12" in pagina and "SECRETO" not in pagina and p["llave_cifrada"] not in pagina
    # guardar sin llave conserva la anterior; quitar la borra
    cliente.post("/superadmin/ia/proveedores/1", data={**datos, "llave": ""})
    assert cifrado.descifrar(base.proveedores_ia[1]["llave_cifrada"]) == LLAVE_API
    cliente.post("/superadmin/ia/proveedores/1", data={**datos, "llave": "", "quitar_llave": "1"})
    assert base.proveedores_ia[1]["llave_cifrada"] is None
    r = cliente.post("/superadmin/ia/proveedores", data={**datos})
    assert "Ya existe un proveedor" in r.text


def test_sin_llave_maestra_no_se_guardan_llaves(cliente, base, monkeypatch):
    monkeypatch.delenv("IA_LLAVE_MAESTRA", raising=False)
    datos = {"nombre": "OpenAI", "tipo": "compatible_openai", "url_base": "https://api.openai.com/v1",
             "campo_max_tokens": "max_completion_tokens", "activo": "1", "llave": LLAVE_API}
    r = cliente.post("/superadmin/ia/proveedores", data=datos)
    assert "No se pudo guardar la llave" in r.text and base.proveedores_ia == {}
    assert "No se pueden guardar llaves" in cliente.get("/superadmin/ia?pestana=proveedores").text


def test_validaciones_de_modelo(cliente, base, maestra):
    crear(base)
    datos = {"proveedor_id": "1", "modelo_id": "otro-modelo", "nombre_visible": "Otro", "precio_entrada": "1",
             "precio_salida": "5", "max_tokens": "1024", "esfuerzo": "low", "activo": "1", "parametros_extra": ""}
    assert "no pueden incluir: messages, model" in cliente.post(
        "/superadmin/ia/modelos", data={**datos, "parametros_extra": '{"model": "x", "messages": []}'}).text
    assert "JSON válido" in cliente.post("/superadmin/ia/modelos", data={**datos, "parametros_extra": "{temperature"}).text
    assert "entrada y el de salida" in cliente.post("/superadmin/ia/modelos", data={**datos, "precio_salida": ""}).text
    assert "sin espacios" in cliente.post("/superadmin/ia/modelos", data={**datos, "modelo_id": "mi modelo"}).text
    r = cliente.post("/superadmin/ia/modelos", data={**datos, "parametros_extra": '{"temperature": 0.3}'}, follow_redirects=False)
    assert r.status_code == 303 and base.modelos_ia[2]["parametros_extra"] == {"temperature": 0.3}
    assert len(base.precios_hist) == 2
    assert "Ese proveedor ya tiene" in cliente.post("/superadmin/ia/modelos", data=datos).text


def test_asignar_niveles_desde_el_panel(cliente, base, proveedor, maestra):
    mid = crear(base)
    cliente.post("/superadmin/ia/niveles", data={"basico": str(mid), "estandar": "", "premium": "999"})
    assert base.niveles_map == {"basico": mid, "estandar": None, "premium": None}
    pagina = cliente.get("/superadmin/ia?pestana=niveles").text
    assert "Base de datos" in pagina and "Variables de entorno" in pagina
    assert asyncio.run(config_ia.obtener(None)).niveles["basico"].origen == "base"  # se invalidó la caché


def test_probar_modelo_y_uso_fuera_del_resumen_del_negocio(cliente, base, proveedor, maestra):
    mid = crear(base)
    proveedor.encolar(respuesta_openai("Soy un *modelo* de prueba.", prompt=200, completion=20))
    r = cliente.post(f"/superadmin/ia/modelos/{mid}/probar", data={"mensaje": "¿Quién eres?"})
    assert "Soy un <strong>modelo</strong> de prueba." in r.text and "US$ 0.00060" in r.text
    assert json.loads(proveedor.solicitudes[-1].content)["messages"][-1]["content"] == "¿Quién eres?"
    assert base.usos[-1]["origen"] == "prueba"
    assert "Consultas" in cliente.get("/asistente?pestana=uso").text
    assert asyncio.run(db.resumen_uso(None, TENANT, None)) == []          # no cuenta para el negocio
    assert "Probar modelo" in cliente.get("/superadmin/ia?pestana=uso").text
    inactivo = crear(base, llave=None)
    assert "no tiene llave" in cliente.post(f"/superadmin/ia/modelos/{inactivo}/probar", data={}).text


def test_comparador_en_paralelo_con_la_conversacion_del_simulador(cliente, base, proveedor, maestra):
    asistente_listo(base)
    a, b = crear(base), crear(base, tipo="anthropic", precio=("3", "15"))
    proveedor.encolar(respuesta_anthropic("Respuesta del simulador"))
    cliente.post("/asistente/probar", data={"texto": "¿Cuánto cuesta la consulta?"})
    proveedor.solicitudes.clear()
    assert "Elige entre 2 y 3 modelos" in cliente.post("/superadmin/ia/comparar", data={"modelo_ids": [str(a)]}).text

    def responder(request: httpx.Request):
        proveedor.solicitudes.append(request)
        if request.url.path.endswith("/chat/completions"):
            return respuesta_openai("Cuesta S/ 60 (modelo A).", prompt=900, completion=30)
        return respuesta_anthropic("La consulta cuesta S/ 60 (modelo B).", entrada=900, salida=30)
    ia._transporte = httpx.MockTransport(responder)
    r = cliente.post("/superadmin/ia/comparar", data={"modelo_ids": [str(a), str(b)], "mensaje": "¿Y el sábado?"})
    assert "Cuesta S/ 60 (modelo A)." in r.text and "La consulta cuesta S/ 60 (modelo B)." in r.text
    assert len(proveedor.solicitudes) == 2
    for s in proveedor.solicitudes:
        contenido = json.dumps(json.loads(s.content), ensure_ascii=False)
        assert "¿Cuánto cuesta la consulta?" in contenido and "¿Y el sábado?" in contenido  # simulador + mensaje
        assert "Clínica Demo Amazonía" in contenido                                      # prompt del asistente
    assert [u["origen"] for u in base.usos[-2:]] == ["comparador", "comparador"]
    # Sin mensaje extra: se compara la respuesta al último mensaje del cliente del simulador
    proveedor.solicitudes.clear()
    r = cliente.post("/superadmin/ia/comparar", data={"modelo_ids": [str(a), str(b)]})
    assert "Cuesta S/ 60 (modelo A)." in r.text and len(proveedor.solicitudes) == 2
    for s in proveedor.solicitudes:
        mensajes = json.loads(s.content)["messages"]
        assert mensajes[-1]["role"] == "user" and mensajes[-1]["content"] == "¿Cuánto cuesta la consulta?"
    assert len([m for m in base.mensajes if m["direccion"] == "out"]) == 1  # no guarda mensajes nuevos
