"""Presets por rubro: estructura, reglas de etapas avanzables por la IA y aplicación idempotente."""
import asyncio

import pytest

from app import crm, db
from app.presets import MODULOS, PRESETS, TIPOS_CAMPO
from conftest import TENANT


@pytest.mark.parametrize("rubro", list(PRESETS))
def test_estructura_valida(rubro):
    p = PRESETS[rubro]
    claves = [e["clave"] for e in p["etapas"]]
    assert len(claves) == len(set(claves))
    perdidas = [e for e in p["etapas"] if e.get("es_perdido")]
    assert len(perdidas) == 1 and perdidas[0]["avanzable_por_ia"] is False  # "Perdido" nunca lo marca la IA
    assert all(e["color"].startswith("#") and len(e["color"]) == 7 for e in p["etapas"])
    assert all("avanzable_por_ia" in e for e in p["etapas"])
    campos = [c["clave"] for c in p["campos"]]
    assert len(campos) == len(set(campos)) and campos
    for c in p["campos"]:
        assert c["tipo"] in TIPOS_CAMPO and c["descripcion_ia"]
        if c["tipo"] == "opcion":
            assert len(c["opciones"]) >= 2
    assert set(p["modulos"]) == set(MODULOS)
    assert p["motivos_perdida"]


def test_comercio_redes_quien_avanza_cada_etapa():
    etapas = {e["clave"]: e for e in PRESETS["comercio_redes"]["etapas"]}
    assert list(etapas) == ["consulta", "cotizado", "pedido_confirmado", "pagado", "comprobante_enviado", "entregado", "perdido"]
    assert [c for c, e in etapas.items() if e["avanzable_por_ia"]] == ["consulta", "cotizado", "pedido_confirmado"]
    for clave in ("pagado", "comprobante_enviado", "entregado", "perdido"):
        assert etapas[clave]["avanzable_por_ia"] is False
    campos = {c["clave"]: c for c in PRESETS["comercio_redes"]["campos"]}
    assert set(campos) == {"producto", "cantidad", "tipo_comprobante", "documento", "entrega", "direccion", "monto", "medio_pago"}
    assert campos["tipo_comprobante"]["opciones"] == ["Boleta", "Factura"]
    assert campos["documento"]["tipo"] == "documento" and campos["monto"]["tipo"] == "moneda"
    assert PRESETS["comercio_redes"]["modulos"] == {"pedidos": True, "pagos": True, "comprobantes": True, "agenda": False}


def test_clinica_atendido_solo_persona():
    etapas = {e["clave"]: e for e in PRESETS["clinica"]["etapas"]}
    assert etapas["atendido"]["avanzable_por_ia"] is False
    assert etapas["cita_confirmada"]["avanzable_por_ia"] is False


def test_aplicar_preset_es_idempotente_y_respeta_lo_renombrado(base):
    async def flujo():
        primera = await crm.aplicar_preset(None, TENANT, "comercio_redes")
        consulta = next(e for e in await db.listar_etapas(None, TENANT) if e["clave"] == "consulta")
        await db.actualizar_etapa(None, TENANT, consulta["id"], nombre="Primer contacto", color="#123456",
                                  orden=consulta["orden"], oculta=False)
        segunda = await crm.aplicar_preset(None, TENANT, "comercio_redes")
        return primera, segunda
    primera, segunda = asyncio.run(flujo())
    assert primera == {"etapas": 7, "campos": 8} and segunda == {"etapas": 0, "campos": 0}
    assert len(base.etapas) == 7 and len(base.campos) == 8
    assert next(e for e in base.etapas if e["clave"] == "consulta")["nombre"] == "Primer contacto"
    assert base.tenant["rubro"] == "comercio_redes" and base.tenant["modulos"]["pagos"] is True


def test_cambiar_de_rubro_solo_agrega(base):
    asyncio.run(crm.aplicar_preset(None, TENANT, "comercio_redes"))
    nuevos = asyncio.run(crm.aplicar_preset(None, TENANT, "clinica"))
    assert nuevos["etapas"] == 3  # cita_solicitada, cita_confirmada, atendido (consulta y perdido ya existían)
    assert len(base.etapas) == 10 and base.tenant["rubro"] == "clinica"


def test_asegurar_preset_al_arrancar_usa_el_rubro_del_tenant(base):
    asyncio.run(crm.asegurar_preset(None, TENANT))
    assert [e["clave"] for e in base.etapas][:2] == ["consulta", "cotizado"]
    asyncio.run(crm.asegurar_preset(None, TENANT))  # la segunda vez no hace nada
    assert len(base.etapas) == 7


def test_ajustes_renombrar_etapa_y_agregar_campo(cliente, base):
    asyncio.run(crm.aplicar_preset(None, TENANT, "comercio_redes"))
    datos = {}
    for e in base.etapas:
        datos.update({f"nombre_{e['id']}": e["nombre"], f"color_{e['id']}": e["color"], f"orden_{e['id']}": e["orden"]})
    datos["nombre_1"] = "Primer contacto"
    datos["oculta_7"] = "1"  # intento de ocultar "Perdido": se ignora
    assert cliente.post("/ajustes/etapas", data=datos, follow_redirects=False).status_code == 303
    assert base.etapas[0]["nombre"] == "Primer contacto" and base.etapas[6]["oculta"] is False

    campos = {}
    for c in base.campos:
        campos.update({f"etiqueta_{c['id']}": c["etiqueta"], f"descripcion_{c['id']}": c["descripcion_ia"], f"activo_{c['id']}": "1"})
        if c["tipo"] == "opcion":
            campos[f"opciones_{c['id']}"] = ", ".join(c["opciones"])
    r = cliente.post("/ajustes/campos", data={**campos, "nueva_etiqueta": "Talla", "nuevo_tipo": "opcion",
                                              "nuevas_opciones": "S, M, L", "nueva_descripcion": "Talla pedida"},
                     follow_redirects=False)
    assert r.status_code == 303
    talla = next(c for c in base.campos if c["clave"] == "talla")
    assert talla["opciones"] == ["S", "M", "L"] and talla["tipo"] == "opcion"
    r = cliente.post("/ajustes/campos", data={**campos, "nueva_etiqueta": "Color", "nuevo_tipo": "opcion", "nuevas_opciones": "Rojo"})
    assert "al menos dos opciones" in r.text
    for p in ("rubro", "embudo", "campos", "modulos"):
        assert cliente.get(f"/ajustes?pestana={p}").status_code == 200
