"""CRM en el panel: origen del anuncio (referral), identificador de Meta, panel de detalles, pipeline,
filtros y no leídos de la bandeja, y exportación CSV auditada."""
import asyncio
import csv
import io
import json

from app import crm
from conftest import TENANT, evento_entrante, firmar


def recibir(cliente, cuerpo: bytes):
    assert cliente.post("/webhook", content=cuerpo, headers={"X-Hub-Signature-256": firmar(cuerpo)}).status_code == 200


def con_referral(wamid, referral, **kw):
    evento = json.loads(evento_entrante(wamid, **kw))
    evento["entry"][0]["changes"][0]["value"]["messages"][0]["referral"] = referral
    return json.dumps(evento).encode()


def con_identidad(wamid, user_id, sin_from=False, **kw):
    evento = json.loads(evento_entrante(wamid, **kw))
    valor = evento["entry"][0]["changes"][0]["value"]
    valor["contacts"][0]["user_id"] = user_id
    valor["messages"][0]["from_user_id"] = user_id
    if sin_from:
        del valor["messages"][0]["from"]
        del valor["contacts"][0]["wa_id"]
    return json.dumps(evento).encode()


def contacto(base, wa_id="51987654321"):
    return next(c for c in base.contactos.values() if c["wa_id"] == wa_id)


REFERRAL = {"source_url": "https://fb.me/anuncio1", "source_id": "120211", "source_type": "ad",
            "headline": "Poleras de algodón", "body": "Envíos a todo Lima", "media_type": "image",
            "image_url": "https://scontent.example/img.jpg", "ctwa_clid": "ARAkLk", "desconocido": "no se guarda"}


# --- Referral e identidad -------------------------------------------------------------

def test_referral_guarda_solo_el_primer_origen(cliente, base):
    recibir(cliente, con_referral("wamid.1", REFERRAL))
    recibir(cliente, con_referral("wamid.2", {**REFERRAL, "headline": "Otro anuncio", "source_id": "999"}))
    origen = contacto(base)["origen_anuncio"]
    assert origen["headline"] == "Poleras de algodón" and origen["source_id"] == "120211"
    assert origen["ctwa_clid"] == "ARAkLk" and "desconocido" not in origen
    pagina = cliente.get(f"/bandeja/{contacto(base)['id']}").text
    assert "Origen del anuncio" in pagina and "Poleras de algodón" in pagina and 'href="https://fb.me/anuncio1"' in pagina


def test_mensaje_sin_referral_no_crea_origen(cliente, base):
    recibir(cliente, evento_entrante("wamid.1"))
    assert contacto(base)["origen_anuncio"] is None
    assert "Origen del anuncio" not in cliente.get(f"/bandeja/{contacto(base)['id']}").text


def test_identificador_de_meta_opcional(cliente, base):
    recibir(cliente, evento_entrante("wamid.1"))
    assert contacto(base)["meta_user_id"] is None                  # sin el dato todo funciona con wa_id
    recibir(cliente, con_identidad("wamid.2", "PE.13491208655302741918"))
    assert contacto(base)["meta_user_id"] == "PE.13491208655302741918"
    recibir(cliente, evento_entrante("wamid.3"))
    assert contacto(base)["meta_user_id"] == "PE.13491208655302741918"   # no se borra si no viene


def test_mensaje_sin_telefono_se_omite_sin_romper(cliente, base):
    recibir(cliente, con_identidad("wamid.1", "PE.999", sin_from=True))
    assert base.contactos == {} and len(base.eventos) == 1   # queda en el log crudo


# --- Bandeja: filtros, búsqueda y no leídos ---------------------------------------------

def test_bandeja_tres_columnas_no_leidos_y_filtros(cliente, base):
    asyncio.run(crm.aplicar_preset(None, TENANT, "comercio_redes"))
    recibir(cliente, evento_entrante("wamid.1", wa_id="51911111111", nombre="Ana"))
    recibir(cliente, evento_entrante("wamid.2", wa_id="51922222222", nombre="Beto", texto="Hola"))
    ana, beto = contacto(base, "51911111111"), contacto(base, "51922222222")
    lista = cliente.get("/parcial/conversaciones?filtro=no_leidas").text
    assert "Ana" in lista and "Beto" in lista and 'class="contador-no-leidos"' in lista

    pagina = cliente.get(f"/bandeja/{ana['id']}").text      # abrirla la marca como leída
    assert 'class="panel-detalles"' in pagina and 'id="detalles"' in pagina and 'id="filtros-bandeja"' in pagina
    lista = cliente.get("/parcial/conversaciones?filtro=no_leidas").text
    assert "Ana" not in lista and "Beto" in lista

    beto["modo"], beto["derivado_en"] = "humano", ana["ultimo_mensaje_entrante_en"]
    assert "Ana" not in cliente.get("/parcial/conversaciones?filtro=atencion").text
    asyncio.run(crm.db.editar_campo_humano(None, TENANT, ana["id"], "producto", "Polera negra"))
    busqueda = cliente.get("/parcial/conversaciones?q=polera").text
    assert "Ana" in busqueda and "Beto" not in busqueda
    cotizado = next(e for e in base.etapas if e["clave"] == "cotizado")
    ana["etapa_id"] = cotizado["id"]
    por_etapa = cliente.get(f"/parcial/conversaciones?etapa={cotizado['id']}").text
    assert "Ana" in por_etapa and "Beto" not in por_etapa
    assert "Ninguna conversación coincide" in cliente.get("/parcial/conversaciones?q=zzz").text


# --- Panel de detalles -------------------------------------------------------------------

def test_panel_de_detalles_ficha_etapa_modo_etiquetas_y_notas(cliente, base):
    asyncio.run(crm.aplicar_preset(None, TENANT, "comercio_redes"))
    recibir(cliente, evento_entrante("wamid.1"))
    c = contacto(base)
    c["ficha"]["producto"] = {"valor": "Polera negra", "fuente": "ia"}
    url = f"/contactos/{c['id']}"

    # Guardar ficha: solo lo que cambió queda como "humano"
    r = cliente.post(f"{url}/ficha", data={"producto": "Polera negra", "monto": "S/ 89", "documento": "123"})
    assert "No se guardó: revisa DNI o RUC del comprador" in r.text and "monto" not in c["ficha"]
    r = cliente.post(f"{url}/ficha", data={"producto": "Polera negra", "monto": "S/ 89", "tipo_comprobante": "boleta"})
    assert "Ficha guardada (2 cambios)" in r.text
    assert c["ficha"]["producto"]["fuente"] == "ia"
    assert c["ficha"]["monto"] == {**c["ficha"]["monto"], "valor": "89.00", "fuente": "humano"}
    assert c["ficha"]["tipo_comprobante"]["valor"] == "Boleta"
    cliente.post(f"{url}/campo/liberar", data={"clave": "monto"})
    assert "monto" not in c["ficha"]

    # Etapa: una persona puede retroceder; "Perdido" exige motivo
    pagado = next(e for e in base.etapas if e["clave"] == "pagado")
    perdido = next(e for e in base.etapas if e["clave"] == "perdido")
    consulta = next(e for e in base.etapas if e["clave"] == "consulta")
    cliente.post(f"{url}/etapa", data={"etapa_id": pagado["id"]})
    cliente.post(f"{url}/etapa", data={"etapa_id": consulta["id"]})
    assert c["etapa_id"] == consulta["id"]
    r = cliente.post(f"{url}/etapa", data={"etapa_id": perdido["id"]})
    assert "Indica el motivo" in r.text and c["etapa_id"] == consulta["id"]
    cliente.post(f"{url}/etapa", data={"etapa_id": perdido["id"], "motivo": "Otro", "motivo_detalle": "Se mudó"})
    assert c["etapa_id"] == perdido["id"] and c["motivo_perdida"] == "Otro: Se mudó"
    assert [x["fuente"] for x in base.cambios_etapa] == ["humano", "humano", "humano"]

    # Interruptor de IA por conversación (contacts.modo)
    cliente.post(f"{url}/modo", data={"modo": "humano"})
    assert c["modo"] == "humano"
    r = cliente.post(f"{url}/modo", data={"modo": "ia"})
    assert c["modo"] == "ia" and 'aria-checked="true"' in r.text

    # Etiquetas y notas
    cliente.post(f"{url}/etiquetas", data={"etiqueta": "  Cliente VIP "})
    cliente.post(f"{url}/etiquetas", data={"etiqueta": "cliente vip"})
    assert c["etiquetas"] == ["cliente vip"]
    cliente.post(f"{url}/etiquetas/quitar", data={"etiqueta": "cliente vip"})
    assert c["etiquetas"] == []
    r = cliente.post(f"{url}/notas", data={"texto": "Prefiere entrega en la tarde"})
    assert "Prefiere entrega en la tarde" in r.text and base.notas[-1]["autor"] == "admin@prueba.pe"


def test_detalles_de_un_contacto_del_simulador_da_404(cliente, base):
    sim = asyncio.run(crm.db.contacto_simulador(None, TENANT))
    assert cliente.get(f"/parcial/detalles/{sim}").status_code == 404


# --- Pipeline -----------------------------------------------------------------------------

def test_pipeline_columnas_tarjetas_y_mover(cliente, base):
    asyncio.run(crm.aplicar_preset(None, TENANT, "comercio_redes"))
    recibir(cliente, evento_entrante("wamid.1", nombre="Ana"))
    c = contacto(base)
    c["ficha"] = {"producto": {"valor": "Polera negra M", "fuente": "ia"}, "monto": {"valor": "89.00", "fuente": "ia"}}
    pagina = cliente.get("/pipeline").text
    assert pagina.count('class="columna"') == 7 and "Polera negra M" in pagina and "S/ 89.00" in pagina
    assert 'draggable="true"' in pagina and "Mover a…" in pagina and 'id="dialogo-perdido"' in pagina
    cotizado = next(e for e in base.etapas if e["clave"] == "cotizado")
    perdido = next(e for e in base.etapas if e["clave"] == "perdido")
    r = cliente.post(f"/contactos/{c['id']}/etapa", data={"etapa_id": cotizado["id"], "vista": "pipeline"})
    assert 'id="tablero"' in r.text and c["etapa_id"] == cotizado["id"]
    r = cliente.post(f"/contactos/{c['id']}/etapa", data={"etapa_id": perdido["id"], "vista": "pipeline"})
    assert "Indica el motivo" in r.text and c["etapa_id"] == cotizado["id"]


# --- Contactos y CSV --------------------------------------------------------------------

def test_contactos_y_csv_con_filtros_bom_separador_proteccion_y_auditoria(cliente, base):
    asyncio.run(crm.aplicar_preset(None, TENANT, "comercio_redes"))
    recibir(cliente, evento_entrante("wamid.1", wa_id="51911111111", nombre="=HYPERLINK(\"http://x\")"))
    recibir(cliente, evento_entrante("wamid.2", wa_id="51922222222", nombre="Beto"))
    a, b = contacto(base, "51911111111"), contacto(base, "51922222222")
    a["etiquetas"] = ["vip"]
    a["ficha"] = {"producto": {"valor": "Polera; negra", "fuente": "ia"}, "monto": {"valor": "89.00", "fuente": "humano"}}

    pagina = cliente.get("/contactos?etiqueta=vip").text
    assert "Beto" not in pagina and "Polera; negra" in pagina and "/contactos.csv?etiqueta=vip" in pagina

    r = cliente.get("/contactos.csv?etiqueta=vip")
    assert r.headers["content-type"].startswith("text/csv") and "attachment" in r.headers["content-disposition"]
    texto = r.content.decode("utf-8")
    assert texto.startswith("﻿")
    filas = list(csv.reader(io.StringIO(texto.lstrip("﻿")), delimiter=";"))
    assert filas[0][:5] == ["Nombre", "Teléfono", "Etapa", "Motivo de pérdida", "Etiquetas"]
    assert "Producto y variante" in filas[0] and "Monto" in filas[0]
    assert len(filas) == 2
    fila = dict(zip(filas[0], filas[1]))
    assert fila["Nombre"].startswith("'=")                    # protegido contra fórmulas
    assert fila["Producto y variante"] == "Polera; negra"    # el ; dentro del valor va entre comillas
    assert fila["Teléfono"] == "51911111111" and fila["Etiquetas"] == "vip" and fila["Monto"] == "89.00"
    assert base.exportaciones == [{"usuario": "admin@prueba.pe", "filtros": {"etiqueta": "vip"}, "filas": 1}]

    cliente.get("/contactos.csv")
    assert base.exportaciones[-1]["filtros"] == {} and base.exportaciones[-1]["filas"] == 2


def test_celda_segura():
    assert crm._celda("=1+1") == "'=1+1" and crm._celda("+51") == "'+51" and crm._celda("-5") == "'-5"
    assert crm._celda("@SUM") == "'@SUM" and crm._celda("Hola") == "Hola" and crm._celda(None) == ""
