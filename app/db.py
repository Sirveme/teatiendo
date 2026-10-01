"""Acceso a PostgreSQL con asyncpg. Todas las consultas de datos filtran por tenant_id."""
import json
from contextlib import asynccontextmanager
from datetime import datetime

import asyncpg

CANAL_WHATSAPP = "whatsapp"
CANAL_WEB = "web"
WA_ID_SIMULADOR = "simulador"


async def _preparar_conexion(con: asyncpg.Connection) -> None:
    await con.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def crear_pool(url: str) -> asyncpg.Pool:
    # max_size 10: el candado del asistente retiene una conexión mientras la IA genera la respuesta.
    return await asyncpg.create_pool(url, min_size=1, max_size=10, command_timeout=15, init=_preparar_conexion)


# --- Tenants -----------------------------------------------------------------

async def asegurar_tenant(pool, waba_id: str, phone_number_id: str) -> int:
    """Crea el tenant inicial si no existe (las variables de entorno mandan sobre el waba_id)."""
    return await pool.fetchval(
        """
        INSERT INTO tenants (nombre, waba_id, phone_number_id, rubro)
        VALUES ('PERU SISTEMAS PRO E.I.R.L.', $1, $2, 'comercio_redes')
        ON CONFLICT (phone_number_id) DO UPDATE SET waba_id = EXCLUDED.waba_id
        RETURNING id
        """,
        waba_id, phone_number_id,
    )


async def obtener_tenant(pool, tenant_id: int):
    return await pool.fetchrow("SELECT id, nombre, waba_id, phone_number_id, rubro, modulos FROM tenants WHERE id = $1",
                               tenant_id)


async def tenant_por_phone(pool, phone_number_id: str | None) -> int | None:
    if not phone_number_id:
        return None
    return await pool.fetchval("SELECT id FROM tenants WHERE phone_number_id = $1", phone_number_id)


# --- Webhook -----------------------------------------------------------------

async def guardar_evento(pool, payload: dict) -> None:
    await pool.execute("INSERT INTO webhook_events (payload) VALUES ($1)", payload)


# --- Contactos ---------------------------------------------------------------

async def upsert_contacto_entrante(pool, tenant_id: int, wa_id: str, nombre: str | None, recibido_en: datetime,
                                   canal: str = CANAL_WHATSAPP, meta_user_id: str | None = None) -> int:
    return await pool.fetchval(
        """
        INSERT INTO contacts (tenant_id, canal, wa_id, nombre_perfil, ultimo_mensaje_entrante_en, meta_user_id)
        VALUES ($1, $5, $2, $3, $4, $6)
        ON CONFLICT (tenant_id, canal, wa_id) DO UPDATE SET
            nombre_perfil = COALESCE(EXCLUDED.nombre_perfil, contacts.nombre_perfil),
            ultimo_mensaje_entrante_en = GREATEST(contacts.ultimo_mensaje_entrante_en,
                                                  EXCLUDED.ultimo_mensaje_entrante_en),
            meta_user_id = COALESCE(EXCLUDED.meta_user_id, contacts.meta_user_id)
        RETURNING id
        """,
        tenant_id, wa_id, nombre, recibido_en, canal, meta_user_id,
    )


async def upsert_contacto(pool, tenant_id: int, wa_id: str, canal: str = CANAL_WHATSAPP) -> int:
    return await pool.fetchval(
        """
        INSERT INTO contacts (tenant_id, canal, wa_id) VALUES ($1, $3, $2)
        ON CONFLICT (tenant_id, canal, wa_id) DO UPDATE SET wa_id = EXCLUDED.wa_id
        RETURNING id
        """,
        tenant_id, wa_id, canal,
    )


async def listar_contactos(pool, tenant_id: int, *, q: str | None = None, filtro: str | None = None,
                           etapa_id: int | None = None, etiqueta: str | None = None, limite: int = 200):
    """Contactos de WhatsApp con su etapa efectiva (sin etapa = primera etapa visible), último mensaje y no leídos.
    filtro: None | 'no_leidas' | 'atencion'. q busca en nombre, teléfono y valores de la ficha."""
    return await pool.fetch(
        """
        SELECT c.id, c.wa_id, c.nombre_perfil, c.ultimo_mensaje_entrante_en, c.modo, c.derivado_en,
               c.ficha, c.etiquetas, c.etapa_desde, c.origen_anuncio, c.motivo_perdida,
               COALESCE(e.id, ep.id) AS etapa_id, COALESCE(e.nombre, ep.nombre) AS etapa_nombre,
               COALESCE(e.color, ep.color) AS etapa_color, COALESCE(e.es_perdido, false) AS etapa_perdida,
               m.texto AS ultimo_texto, m.direccion AS ultima_direccion, m.creado_en AS ultimo_en,
               (SELECT count(*) FROM messages mi
                WHERE mi.contact_id = c.id AND mi.direccion = 'in' AND mi.id > c.leido_hasta_id) AS no_leidos
        FROM contacts c
        LEFT JOIN etapas e ON e.id = c.etapa_id
        LEFT JOIN LATERAL (
            SELECT id, nombre, color FROM etapas
            WHERE tenant_id = c.tenant_id AND NOT es_perdido AND NOT oculta ORDER BY orden LIMIT 1
        ) ep ON c.etapa_id IS NULL
        LEFT JOIN LATERAL (
            SELECT id, texto, direccion, creado_en FROM messages
            WHERE contact_id = c.id ORDER BY id DESC LIMIT 1
        ) m ON TRUE
        WHERE c.tenant_id = $1 AND c.canal = 'whatsapp'
          AND ($2::text IS NULL
               OR strpos(lower(COALESCE(c.nombre_perfil, '')), lower($2)) > 0
               OR strpos(c.wa_id, $2) > 0
               OR EXISTS (SELECT 1 FROM jsonb_each(c.ficha) f WHERE strpos(lower(f.value ->> 'valor'), lower($2)) > 0))
          AND ($3::bigint IS NULL OR COALESCE(e.id, ep.id) = $3)
          AND ($4::text IS NULL OR $4 = ANY (c.etiquetas))
          AND ($5::text IS DISTINCT FROM 'no_leidas' OR EXISTS (
                SELECT 1 FROM messages mi WHERE mi.contact_id = c.id AND mi.direccion = 'in' AND mi.id > c.leido_hasta_id))
          AND ($5::text IS DISTINCT FROM 'atencion' OR (c.modo = 'humano' AND c.derivado_en IS NOT NULL))
        ORDER BY m.id DESC NULLS LAST, c.id DESC
        LIMIT $6
        """,
        tenant_id, q or None, etapa_id, etiqueta or None, filtro or None, limite,
    )


async def obtener_contacto(pool, tenant_id: int, contacto_id: int):
    return await pool.fetchrow(
        """
        SELECT id, canal, wa_id, nombre_perfil, ultimo_mensaje_entrante_en, modo, derivado_en, aviso_no_texto_en,
               etapa_id, etapa_desde, motivo_perdida, ficha, etiquetas, origen_anuncio, origen_en, meta_user_id,
               leido_hasta_id, extraccion_en
        FROM contacts WHERE id = $1 AND tenant_id = $2
        """,
        contacto_id, tenant_id,
    )


async def contacto_simulador(pool, tenant_id: int) -> int:
    """Contacto del simulador web (canal 'web'): uno por tenant, nunca aparece en la bandeja."""
    return await pool.fetchval(
        """
        INSERT INTO contacts (tenant_id, canal, wa_id, nombre_perfil) VALUES ($1, 'web', $2, 'Simulador')
        ON CONFLICT (tenant_id, canal, wa_id) DO UPDATE SET wa_id = EXCLUDED.wa_id
        RETURNING id
        """,
        tenant_id, WA_ID_SIMULADOR,
    )


async def cambiar_modo(pool, tenant_id: int, contacto_id: int, modo: str, derivado: bool = False) -> None:
    """'humano' + derivado=True: lo derivó el asistente (Requiere atención). 'ia': reinicia el estado."""
    await pool.execute(
        """
        UPDATE contacts SET
            modo = $3::text,
            derivado_en = CASE WHEN $3::text = 'ia' THEN NULL
                               WHEN $4 THEN now()
                               ELSE derivado_en END,
            aviso_no_texto_en = CASE WHEN $3::text = 'ia' THEN NULL ELSE aviso_no_texto_en END
        WHERE id = $2 AND tenant_id = $1
        """,
        tenant_id, contacto_id, modo, derivado,
    )


async def tomar_control_humano(pool, tenant_id: int, contacto_id: int) -> None:
    """Una persona del equipo respondió a mano: el asistente deja de responder a este contacto."""
    await pool.execute("UPDATE contacts SET modo = 'humano' WHERE id = $2 AND tenant_id = $1 AND modo = 'ia'",
                       tenant_id, contacto_id)


async def marcar_aviso_no_texto(pool, tenant_id: int, contacto_id: int) -> None:
    await pool.execute("UPDATE contacts SET aviso_no_texto_en = now() WHERE id = $2 AND tenant_id = $1",
                       tenant_id, contacto_id)


@asynccontextmanager
async def candado_contacto(pool, contacto_id: int):
    """Candado de PostgreSQL por contacto (pg_advisory_xact_lock): serializa las respuestas del asistente
    a un mismo contacto aunque la app corra en varios procesos. Se libera al terminar la transacción."""
    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended('teatiendo:asistente:' || $1::bigint::text, 0))",
                contacto_id)
            yield


# --- Mensajes ----------------------------------------------------------------

async def listar_mensajes(pool, tenant_id: int, contacto_id: int, limite: int = 200):
    return await pool.fetch(
        """
        SELECT * FROM (
            SELECT id, direccion, tipo, texto, estado, error_json, creado_en, generado_por_ia
            FROM messages WHERE tenant_id = $1 AND contact_id = $2
            ORDER BY id DESC LIMIT $3
        ) t ORDER BY id  -- orden de llegada: la hora de Meta (en segundos) y la del servidor pueden cruzarse
        """,
        tenant_id, contacto_id, limite,
    )


async def historial_para_ia(pool, tenant_id: int, contacto_id: int, limite: int):
    """Últimos N mensajes, SOLO dirección y texto (nada de teléfonos ni identificadores).
    Se ordena por id (orden de llegada), no por creado_en: los entrantes llevan la hora de Meta, con precisión
    de segundos, y una respuesta nuestra puede quedar con hora posterior a un mensaje que llegó después."""
    return await pool.fetch(
        """
        SELECT direccion, texto FROM (
            SELECT direccion, texto, id FROM messages
            WHERE tenant_id = $1 AND contact_id = $2
            ORDER BY id DESC LIMIT $3
        ) t ORDER BY id
        """,
        tenant_id, contacto_id, limite,
    )


async def ultimo_entrante_id(pool, contacto_id: int) -> int | None:
    return await pool.fetchval("SELECT max(id) FROM messages WHERE contact_id = $1 AND direccion = 'in'", contacto_id)


async def borrar_mensajes_contacto(pool, tenant_id: int, contacto_id: int) -> None:
    await pool.execute("DELETE FROM messages WHERE tenant_id = $1 AND contact_id = $2", tenant_id, contacto_id)


async def insertar_mensaje(pool, tenant_id: int, contacto_id: int, *, wamid: str | None, direccion: str,
                           tipo: str, texto: str | None, estado: str | None,
                           error_json=None, creado_en: datetime | None = None,
                           canal: str = CANAL_WHATSAPP, generado_por_ia: bool = False) -> int | None:
    """Inserta un mensaje. Si el wamid ya existe no duplica y devuelve None (idempotencia)."""
    return await pool.fetchval(
        """
        INSERT INTO messages (tenant_id, contact_id, canal, wamid, direccion, tipo, texto, estado, error_json,
                              creado_en, generado_por_ia)
        VALUES ($1, $2, $10, $3, $4, $5, $6, $7, $8, COALESCE($9, now()), $11)
        ON CONFLICT (wamid) DO NOTHING
        RETURNING id
        """,
        tenant_id, contacto_id, wamid, direccion, tipo, texto, estado, error_json, creado_en, canal, generado_por_ia,
    )


async def actualizar_estado(pool, tenant_id: int, wamid: str, estado: str, errores=None) -> None:
    """Actualiza el estado por wamid sin retroceder (sent < delivered < read). 'failed' siempre se aplica."""
    await pool.execute(
        """
        UPDATE messages SET estado = $3::text, error_json = COALESCE($4::jsonb, error_json)
        WHERE tenant_id = $1 AND wamid = $2 AND direccion = 'out'
          AND (
                $3::text = 'failed'
             OR (COALESCE(estado, '') <> 'failed'
                 AND array_position(ARRAY['sent','delivered','read'], $3::text)
                     > COALESCE(array_position(ARRAY['sent','delivered','read'], estado), 0))
          )
        """,
        tenant_id, wamid, estado, errores,
    )


# --- Plantillas --------------------------------------------------------------

async def listar_plantillas(pool, tenant_id: int, solo_aprobadas: bool = False):
    sql = """
        SELECT id, nombre, idioma, categoria, categoria_original, cuerpo, estado_meta, meta_template_id,
               motivo_rechazo, creado_en
        FROM templates WHERE tenant_id = $1
    """
    if solo_aprobadas:
        sql += " AND estado_meta = 'APPROVED'"
    sql += " ORDER BY creado_en DESC, id DESC"
    return await pool.fetch(sql, tenant_id)


async def obtener_plantilla(pool, tenant_id: int, plantilla_id: int):
    return await pool.fetchrow(
        "SELECT id, nombre, idioma, categoria, cuerpo, estado_meta FROM templates WHERE id = $1 AND tenant_id = $2",
        plantilla_id, tenant_id,
    )


async def upsert_plantilla(pool, tenant_id: int, *, nombre: str, idioma: str, categoria: str, cuerpo: str,
                           estado_meta: str, meta_template_id: str | None, motivo_rechazo: str | None,
                           categoria_original: str | None = None) -> None:
    """categoria es la actual según Meta. categoria_original se fija la primera vez y no se sobrescribe:
    al crear desde la app es la categoría enviada; al sincronizar una plantilla nueva, la primera vista."""
    await pool.execute(
        """
        INSERT INTO templates (tenant_id, nombre, idioma, categoria, categoria_original, cuerpo, estado_meta,
                               meta_template_id, motivo_rechazo)
        VALUES ($1, $2, $3, $4, COALESCE($9, $4), $5, $6, $7, $8)
        ON CONFLICT (tenant_id, nombre, idioma) DO UPDATE SET
            categoria          = COALESCE(NULLIF(EXCLUDED.categoria, ''), templates.categoria),
            categoria_original = COALESCE(templates.categoria_original, EXCLUDED.categoria_original),
            cuerpo             = COALESCE(NULLIF(EXCLUDED.cuerpo, ''), templates.cuerpo),
            estado_meta        = EXCLUDED.estado_meta,
            meta_template_id   = COALESCE(EXCLUDED.meta_template_id, templates.meta_template_id),
            motivo_rechazo     = EXCLUDED.motivo_rechazo
        """,
        tenant_id, nombre, idioma, categoria, cuerpo, estado_meta, meta_template_id, motivo_rechazo,
        categoria_original,
    )


# --- Asistente ---------------------------------------------------------------

CAMPOS_CONFIG_ASISTENTE = ("activo", "nombre_negocio", "nombre_asistente", "trato", "nivel", "horario_humano",
                           "mensaje_derivacion", "mensaje_no_texto", "extraccion_activa")
CAMPOS_CONOCIMIENTO = ("sobre_negocio", "servicios_precios", "horarios_contacto", "preguntas_frecuentes", "reglas")


async def obtener_asistente(pool, tenant_id: int):
    return await pool.fetchrow(
        f"SELECT {', '.join(CAMPOS_CONFIG_ASISTENTE + CAMPOS_CONOCIMIENTO)}, actualizado_en "
        "FROM asistentes WHERE tenant_id = $1",
        tenant_id,
    )


async def _guardar_campos_asistente(pool, tenant_id: int, campos: tuple, datos: dict) -> None:
    columnas = ", ".join(campos)
    valores = ", ".join(f"${i}" for i in range(2, len(campos) + 2))
    actualizar = ", ".join(f"{c} = EXCLUDED.{c}" for c in campos)
    await pool.execute(
        f"""
        INSERT INTO asistentes (tenant_id, {columnas}) VALUES ($1, {valores})
        ON CONFLICT (tenant_id) DO UPDATE SET {actualizar}, actualizado_en = now()
        """,
        tenant_id, *[datos[c] for c in campos],
    )


async def guardar_config_asistente(pool, tenant_id: int, datos: dict) -> None:
    await _guardar_campos_asistente(pool, tenant_id, CAMPOS_CONFIG_ASISTENTE, datos)


async def guardar_conocimiento(pool, tenant_id: int, datos: dict) -> None:
    await _guardar_campos_asistente(pool, tenant_id, CAMPOS_CONOCIMIENTO, datos)


# --- Uso de IA ---------------------------------------------------------------

async def registrar_uso_ia(pool, tenant_id: int, *, origen: str, resultado, message_id: int | None) -> int:
    """resultado: ia.Resultado. Se registra también cuando falla (ok = false, sin tokens)."""
    uso = resultado.uso
    return await pool.fetchval(
        """
        INSERT INTO ia_uso (tenant_id, message_id, origen, nivel, proveedor, modelo, tokens_entrada, tokens_salida,
                            tokens_cache_lectura, tokens_cache_escritura, costo_estimado, duracion_ms, ok, error,
                            tokens_razonamiento, truncado, reintentos, modelo_ref_id, precio_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19)
        RETURNING id
        """,
        tenant_id, message_id, origen, resultado.nivel, resultado.proveedor, resultado.modelo, uso.entrada,
        uso.salida, uso.cache_lectura, uso.cache_escritura, resultado.costo, resultado.duracion_ms, resultado.ok,
        resultado.error, uso.razonamiento, resultado.truncado, resultado.reintentos, resultado.modelo_ref_id,
        resultado.precio_id,
    )


async def uso_por_mensajes(pool, tenant_id: int, message_ids: list[int]) -> dict:
    if not message_ids:
        return {}
    filas = await pool.fetch(
        """
        SELECT message_id, proveedor, modelo, nivel, tokens_entrada, tokens_salida, tokens_cache_lectura,
               tokens_cache_escritura, costo_estimado, duracion_ms, tokens_razonamiento, reintentos
        FROM ia_uso WHERE tenant_id = $1 AND message_id = ANY($2::bigint[])
        """,
        tenant_id, message_ids,
    )
    return {f["message_id"]: f for f in filas}


async def resumen_uso(pool, tenant_id: int, desde: datetime):
    return await pool.fetch(
        """
        SELECT origen, count(*) AS consultas, count(*) FILTER (WHERE NOT ok) AS errores,
               COALESCE(sum(tokens_entrada), 0) AS entrada, COALESCE(sum(tokens_salida), 0) AS salida,
               COALESCE(sum(tokens_cache_lectura + tokens_cache_escritura), 0) AS cache,
               COALESCE(sum(costo_estimado), 0) AS costo,
               count(*) FILTER (WHERE ok AND costo_estimado IS NULL) AS sin_precio
        FROM ia_uso WHERE tenant_id = $1 AND creado_en >= $2 AND origen NOT IN ('prueba', 'comparador')
        GROUP BY origen ORDER BY origen
        """,
        tenant_id, desde,
    )


async def ultimos_usos(pool, tenant_id: int, limite: int = 15):
    return await pool.fetch(
        """
        SELECT creado_en, origen, nivel, proveedor, modelo, tokens_entrada, tokens_salida,
               tokens_cache_lectura + tokens_cache_escritura AS cache, costo_estimado, duracion_ms, ok, error
        FROM ia_uso WHERE tenant_id = $1 AND origen NOT IN ('prueba', 'comparador')
        ORDER BY creado_en DESC, id DESC LIMIT $2
        """,
        tenant_id, limite,
    )


# --- CRM: embudo y campos de la ficha ------------------------------------------

async def listar_etapas(pool, tenant_id: int, incluir_ocultas: bool = False):
    return await pool.fetch(
        """
        SELECT id, clave, nombre, color, orden, descripcion_ia, avanzable_por_ia, es_perdido, oculta
        FROM etapas WHERE tenant_id = $1 AND ($2 OR NOT oculta) ORDER BY orden, id
        """,
        tenant_id, incluir_ocultas,
    )


async def listar_campos(pool, tenant_id: int, solo_activos: bool = True):
    return await pool.fetch(
        """
        SELECT id, clave, etiqueta, tipo, opciones, descripcion_ia, orden, activo
        FROM campos_ficha WHERE tenant_id = $1 AND ($2 = false OR activo) ORDER BY orden, id
        """,
        tenant_id, solo_activos,
    )


async def insertar_etapas_faltantes(pool, tenant_id: int, etapas: list[dict]) -> int:
    """Agrega las etapas del preset que el tenant aún no tiene (por clave). Nunca modifica ni borra las existentes."""
    insertadas = 0
    async with pool.acquire() as con:
        async with con.transaction():
            base = await con.fetchval("SELECT COALESCE(max(orden), 0) FROM etapas WHERE tenant_id = $1", tenant_id)
            for i, e in enumerate(etapas, start=1):
                estado = await con.execute(
                    """
                    INSERT INTO etapas (tenant_id, clave, nombre, color, orden, descripcion_ia, avanzable_por_ia, es_perdido)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8) ON CONFLICT (tenant_id, clave) DO NOTHING
                    """,
                    tenant_id, e["clave"], e["nombre"], e["color"], base + i * 10, e.get("descripcion_ia", ""),
                    bool(e.get("avanzable_por_ia")), bool(e.get("es_perdido")),
                )
                insertadas += estado.endswith(" 1")
    return insertadas


async def insertar_campos_faltantes(pool, tenant_id: int, campos: list[dict]) -> int:
    insertados = 0
    async with pool.acquire() as con:
        async with con.transaction():
            base = await con.fetchval("SELECT COALESCE(max(orden), 0) FROM campos_ficha WHERE tenant_id = $1", tenant_id)
            for i, c in enumerate(campos, start=1):
                estado = await con.execute(
                    """
                    INSERT INTO campos_ficha (tenant_id, clave, etiqueta, tipo, opciones, descripcion_ia, orden)
                    VALUES ($1, $2, $3, $4, $5, $6, $7) ON CONFLICT (tenant_id, clave) DO NOTHING
                    """,
                    tenant_id, c["clave"], c["etiqueta"], c["tipo"], c.get("opciones", []), c.get("descripcion_ia", ""),
                    base + i * 10,
                )
                insertados += estado.endswith(" 1")
    return insertados


async def actualizar_rubro(pool, tenant_id: int, rubro: str, modulos: dict) -> None:
    await pool.execute("UPDATE tenants SET rubro = $2, modulos = $3 WHERE id = $1", tenant_id, rubro, modulos)


async def actualizar_modulos(pool, tenant_id: int, modulos: dict) -> None:
    await pool.execute("UPDATE tenants SET modulos = $2 WHERE id = $1", tenant_id, modulos)


async def actualizar_etapa(pool, tenant_id: int, etapa_id: int, *, nombre: str, color: str, orden: int, oculta: bool) -> None:
    await pool.execute(
        "UPDATE etapas SET nombre = $3, color = $4, orden = $5, oculta = $6 WHERE id = $2 AND tenant_id = $1",
        tenant_id, etapa_id, nombre, color, orden, oculta,
    )


async def actualizar_campo(pool, tenant_id: int, campo_id: int, *, etiqueta: str, descripcion_ia: str,
                           opciones: list, activo: bool) -> None:
    await pool.execute(
        """
        UPDATE campos_ficha SET etiqueta = $3, descripcion_ia = $4, opciones = $5, activo = $6
        WHERE id = $2 AND tenant_id = $1
        """,
        tenant_id, campo_id, etiqueta, descripcion_ia, opciones, activo,
    )


async def agregar_campo(pool, tenant_id: int, *, clave: str, etiqueta: str, tipo: str, opciones: list,
                        descripcion_ia: str) -> bool:
    return bool(await insertar_campos_faltantes(pool, tenant_id, [{
        "clave": clave, "etiqueta": etiqueta, "tipo": tipo, "opciones": opciones, "descripcion_ia": descripcion_ia}]))


# --- CRM: ficha, etapa, etiquetas y notas del contacto ---------------------------

async def aplicar_ficha_ia(pool, tenant_id: int, contacto_id: int, cambios: dict) -> list[str]:
    """Escribe los campos propuestos por la IA SALVO los que editó una persona (fuente 'humano').
    La regla se aplica dentro del UPDATE, con el contacto bloqueado. Devuelve las claves escritas."""
    if not cambios:
        return []
    fila = await pool.fetchval(
        """
        WITH c AS (SELECT id, ficha FROM contacts WHERE id = $2 AND tenant_id = $1 FOR UPDATE),
        permitidos AS (
            SELECT e.key, e.value FROM c, jsonb_each($3::jsonb) AS e
            WHERE COALESCE(c.ficha -> e.key ->> 'fuente', '') <> 'humano'
        ),
        upd AS (
            UPDATE contacts SET ficha = contacts.ficha || COALESCE((SELECT jsonb_object_agg(key, value) FROM permitidos), '{}'::jsonb)
            FROM c WHERE contacts.id = c.id RETURNING contacts.id
        )
        SELECT COALESCE(array_agg(key ORDER BY key), '{}') FROM permitidos
        """,
        tenant_id, contacto_id, cambios,
    )
    return list(fila or [])


async def avanzar_etapa_ia(pool, tenant_id: int, contacto_id: int, clave: str):
    """La IA solo AVANZA: etapa destino avanzable_por_ia, nunca 'Perdido', de orden mayor a la actual y
    solo si el contacto no está en 'Perdido'. Devuelve (anterior_id, nueva_id) o None."""
    async with pool.acquire() as con:
        async with con.transaction():
            fila = await con.fetchrow(
                """
                WITH nueva AS (
                    SELECT id, orden FROM etapas
                    WHERE tenant_id = $1 AND clave = $3 AND avanzable_por_ia AND NOT es_perdido AND NOT oculta
                ),
                act AS (
                    SELECT c.id, c.etapa_id, e.orden AS orden_actual, COALESCE(e.es_perdido, false) AS perdido
                    FROM contacts c LEFT JOIN etapas e ON e.id = c.etapa_id
                    WHERE c.id = $2 AND c.tenant_id = $1 FOR UPDATE OF c
                )
                UPDATE contacts SET etapa_id = nueva.id, etapa_desde = now()
                FROM nueva, act
                WHERE contacts.id = act.id AND NOT act.perdido AND nueva.orden > COALESCE(act.orden_actual, -2147483648)
                RETURNING act.etapa_id AS anterior, nueva.id AS nueva
                """,
                tenant_id, contacto_id, clave,
            )
            if fila:
                await con.execute(
                    """INSERT INTO cambios_etapa (tenant_id, contact_id, etapa_anterior_id, etapa_nueva_id, fuente, autor)
                       VALUES ($1, $2, $3, $4, 'ia', 'Asistente')""",
                    tenant_id, contacto_id, fila["anterior"], fila["nueva"],
                )
                return fila["anterior"], fila["nueva"]
    return None


async def mover_etapa(pool, tenant_id: int, contacto_id: int, etapa_id: int, *, motivo: str | None, autor: str):
    """Movimiento hecho por una persona: cualquier dirección. 'Perdido' guarda el motivo (validado antes)."""
    async with pool.acquire() as con:
        async with con.transaction():
            anterior = await con.fetchrow("SELECT etapa_id FROM contacts WHERE id = $2 AND tenant_id = $1 FOR UPDATE",
                                          tenant_id, contacto_id)
            destino = await con.fetchrow("SELECT id, es_perdido FROM etapas WHERE id = $2 AND tenant_id = $1",
                                         tenant_id, etapa_id)
            if not anterior or not destino:
                return None
            await con.execute(
                """
                UPDATE contacts SET etapa_id = $3,
                    etapa_desde = CASE WHEN etapa_id IS DISTINCT FROM $3 THEN now() ELSE etapa_desde END,
                    motivo_perdida = CASE WHEN $4 THEN $5 ELSE NULL END
                WHERE id = $2 AND tenant_id = $1
                """,
                tenant_id, contacto_id, etapa_id, destino["es_perdido"], motivo,
            )
            if anterior["etapa_id"] != etapa_id:
                await con.execute(
                    """INSERT INTO cambios_etapa (tenant_id, contact_id, etapa_anterior_id, etapa_nueva_id, fuente, autor, motivo)
                       VALUES ($1, $2, $3, $4, 'humano', $5, $6)""",
                    tenant_id, contacto_id, anterior["etapa_id"], etapa_id, autor, motivo if destino["es_perdido"] else None,
                )
            return anterior["etapa_id"], etapa_id


async def editar_campo_humano(pool, tenant_id: int, contacto_id: int, clave: str, valor: str | None) -> None:
    """Una persona fija el valor (también vacío): desde ahora la IA no lo sobrescribe."""
    await pool.execute(
        """
        UPDATE contacts SET ficha = jsonb_set(ficha, ARRAY[$3::text],
            jsonb_build_object('valor', $4::text, 'fuente', 'humano', 'actualizado_en', now()), true)
        WHERE id = $2 AND tenant_id = $1
        """,
        tenant_id, contacto_id, clave, valor,
    )


async def liberar_campo(pool, tenant_id: int, contacto_id: int, clave: str) -> None:
    """Quita el valor del campo y vuelve a permitir que la IA lo complete."""
    await pool.execute("UPDATE contacts SET ficha = ficha - $3::text WHERE id = $2 AND tenant_id = $1",
                       tenant_id, contacto_id, clave)


async def agregar_etiqueta(pool, tenant_id: int, contacto_id: int, etiqueta: str) -> None:
    await pool.execute(
        """UPDATE contacts SET etiquetas = array_append(etiquetas, $3::text)
           WHERE id = $2 AND tenant_id = $1 AND NOT ($3::text = ANY (etiquetas))""",
        tenant_id, contacto_id, etiqueta,
    )


async def quitar_etiqueta(pool, tenant_id: int, contacto_id: int, etiqueta: str) -> None:
    await pool.execute("UPDATE contacts SET etiquetas = array_remove(etiquetas, $3::text) WHERE id = $2 AND tenant_id = $1",
                       tenant_id, contacto_id, etiqueta)


async def listar_etiquetas_tenant(pool, tenant_id: int) -> list[str]:
    filas = await pool.fetch(
        "SELECT DISTINCT unnest(etiquetas) AS etiqueta FROM contacts WHERE tenant_id = $1 AND canal = 'whatsapp' ORDER BY 1",
        tenant_id)
    return [f["etiqueta"] for f in filas]


async def listar_notas(pool, tenant_id: int, contacto_id: int):
    return await pool.fetch(
        "SELECT id, autor, texto, creado_en FROM notas WHERE tenant_id = $1 AND contact_id = $2 ORDER BY creado_en DESC, id DESC",
        tenant_id, contacto_id)


async def agregar_nota(pool, tenant_id: int, contacto_id: int, autor: str, texto: str) -> None:
    await pool.execute("INSERT INTO notas (tenant_id, contact_id, autor, texto) VALUES ($1, $2, $3, $4)",
                       tenant_id, contacto_id, autor, texto)


async def guardar_origen_anuncio(pool, tenant_id: int, contacto_id: int, origen: dict) -> bool:
    """Guarda SOLO el primer origen del contacto."""
    estado = await pool.execute(
        "UPDATE contacts SET origen_anuncio = $3, origen_en = now() WHERE id = $2 AND tenant_id = $1 AND origen_anuncio IS NULL",
        tenant_id, contacto_id, origen)
    return estado.endswith(" 1")


async def marcar_leido(pool, tenant_id: int, contacto_id: int) -> None:
    await pool.execute(
        """UPDATE contacts SET leido_hasta_id = GREATEST(leido_hasta_id, COALESCE(
               (SELECT max(id) FROM messages WHERE contact_id = $2 AND direccion = 'in'), 0))
           WHERE id = $2 AND tenant_id = $1""",
        tenant_id, contacto_id)


async def reiniciar_crm_contacto(pool, tenant_id: int, contacto_id: int) -> None:
    async with pool.acquire() as con:
        async with con.transaction():
            await con.execute(
                """UPDATE contacts SET ficha = '{}'::jsonb, etapa_id = NULL, etapa_desde = NULL, motivo_perdida = NULL,
                          etiquetas = '{}', extraccion_en = NULL, extraccion_pendiente = false
                   WHERE id = $2 AND tenant_id = $1""", tenant_id, contacto_id)
            await con.execute("DELETE FROM notas WHERE tenant_id = $1 AND contact_id = $2", tenant_id, contacto_id)
            await con.execute("DELETE FROM cambios_etapa WHERE tenant_id = $1 AND contact_id = $2", tenant_id, contacto_id)


# --- CRM: límite de extracción por contacto ---------------------------------------

async def reclamar_extraccion(pool, contacto_id: int, intervalo_s: int) -> bool:
    """True si pasaron al menos intervalo_s desde la última extracción (y la marca como hecha ahora).
    Atómico: con varios procesos, solo uno la obtiene."""
    return bool(await pool.fetchval(
        """UPDATE contacts SET extraccion_en = now(), extraccion_pendiente = false
           WHERE id = $1 AND (extraccion_en IS NULL OR extraccion_en <= now() - make_interval(secs => $2))
           RETURNING id""",
        contacto_id, intervalo_s))


async def marcar_extraccion_pendiente(pool, contacto_id: int) -> None:
    await pool.execute("UPDATE contacts SET extraccion_pendiente = true WHERE id = $1", contacto_id)


async def reclamar_extracciones_pendientes(pool, intervalo_s: int, limite: int = 10):
    """Contactos con extracción postergada cuyo intervalo ya venció (FOR UPDATE SKIP LOCKED: sin duplicados)."""
    return await pool.fetch(
        """UPDATE contacts SET extraccion_pendiente = false, extraccion_en = now()
           WHERE id IN (
               SELECT id FROM contacts
               WHERE extraccion_pendiente AND (extraccion_en IS NULL OR extraccion_en <= now() - make_interval(secs => $1))
               ORDER BY extraccion_en NULLS FIRST LIMIT $2 FOR UPDATE SKIP LOCKED)
           RETURNING id, tenant_id""",
        intervalo_s, limite)


async def registrar_exportacion(pool, tenant_id: int, usuario: str, filtros: dict, filas: int) -> None:
    await pool.execute("INSERT INTO exportaciones (tenant_id, usuario, filtros, filas) VALUES ($1, $2, $3, $4)",
                       tenant_id, usuario, filtros, filas)


# --- Panel de modelos (superadministrador) ------------------------------------------

_SELECT_MODELO_COMPLETO = """
    SELECT m.id AS modelo_ref_id, m.modelo_id, m.nombre_visible, m.max_tokens, m.esfuerzo, m.parametros_extra,
           m.activo AS modelo_activo, p.id AS proveedor_id, p.nombre AS proveedor, p.tipo, p.url_base,
           p.campo_max_tokens, p.llave_cifrada, p.activo AS proveedor_activo,
           h.id AS precio_id, h.precio_entrada, h.precio_salida, h.precio_cache_lectura, h.precio_cache_escritura
    FROM ia_modelos m
    JOIN ia_proveedores p ON p.id = m.proveedor_id
    LEFT JOIN LATERAL (
        SELECT id, precio_entrada, precio_salida, precio_cache_lectura, precio_cache_escritura
        FROM ia_precios_historial WHERE modelo_id = m.id AND vigente_desde <= now()
        ORDER BY vigente_desde DESC, id DESC LIMIT 1
    ) h ON TRUE
"""


async def niveles_ia_configurados(pool):
    """Niveles asignados en la base, con todo lo necesario para llamar al modelo y el precio vigente."""
    return await pool.fetch(
        "SELECT n.nivel, x.* FROM ia_niveles n JOIN (" + _SELECT_MODELO_COMPLETO + ") x ON x.modelo_ref_id = n.modelo_id")


async def modelo_ia_completo(pool, modelo_id: int):
    return await pool.fetchrow(_SELECT_MODELO_COMPLETO + " WHERE m.id = $1", modelo_id)


async def listar_proveedores_ia(pool):
    return await pool.fetch(
        """SELECT p.id, p.nombre, p.tipo, p.url_base, p.campo_max_tokens, p.llave_ultimos4, p.activo, p.actualizado_en,
                  (SELECT count(*) FROM ia_modelos m WHERE m.proveedor_id = p.id) AS modelos
           FROM ia_proveedores p ORDER BY p.nombre""")


async def crear_proveedor_ia(pool, *, nombre, tipo, url_base, campo_max_tokens, activo, llave_cifrada, llave_ultimos4) -> int:
    return await pool.fetchval(
        """INSERT INTO ia_proveedores (nombre, tipo, url_base, campo_max_tokens, activo, llave_cifrada, llave_ultimos4)
           VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
        nombre, tipo, url_base, campo_max_tokens, activo, llave_cifrada, llave_ultimos4)


async def actualizar_proveedor_ia(pool, proveedor_id: int, *, nombre, tipo, url_base, campo_max_tokens, activo,
                                  cambiar_llave: bool, llave_cifrada=None, llave_ultimos4=None) -> None:
    """cambiar_llave=False conserva la llave guardada (el formulario nunca la recibe de vuelta)."""
    await pool.execute(
        """UPDATE ia_proveedores SET nombre = $2, tipo = $3, url_base = $4, campo_max_tokens = $5, activo = $6,
                  llave_cifrada = CASE WHEN $7 THEN $8 ELSE llave_cifrada END,
                  llave_ultimos4 = CASE WHEN $7 THEN $9 ELSE llave_ultimos4 END,
                  actualizado_en = now()
           WHERE id = $1""",
        proveedor_id, nombre, tipo, url_base, campo_max_tokens, activo, cambiar_llave, llave_cifrada, llave_ultimos4)


async def listar_modelos_ia(pool):
    return await pool.fetch(
        """SELECT m.*, p.nombre AS proveedor, p.activo AS proveedor_activo, (p.llave_cifrada IS NOT NULL) AS con_llave
           FROM ia_modelos m JOIN ia_proveedores p ON p.id = m.proveedor_id
           ORDER BY m.activo DESC, p.nombre, m.nombre_visible""")


async def obtener_modelo_ia(pool, modelo_id: int):
    return await pool.fetchrow("SELECT * FROM ia_modelos WHERE id = $1", modelo_id)


CAMPOS_PRECIO = ("precio_entrada", "precio_salida", "precio_cache_lectura", "precio_cache_escritura")


async def crear_modelo_ia(pool, datos: dict, usuario: str) -> int:
    async with pool.acquire() as con:
        async with con.transaction():
            modelo_id = await con.fetchval(
                """INSERT INTO ia_modelos (proveedor_id, modelo_id, nombre_visible, precio_entrada, precio_salida,
                       precio_cache_lectura, precio_cache_escritura, max_tokens, esfuerzo, parametros_extra, activo, notas)
                   VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12) RETURNING id""",
                datos["proveedor_id"], datos["modelo_id"], datos["nombre_visible"], *[datos[c] for c in CAMPOS_PRECIO],
                datos["max_tokens"], datos["esfuerzo"], datos["parametros_extra"], datos["activo"], datos["notas"])
            await con.execute(
                """INSERT INTO ia_precios_historial (modelo_id, precio_entrada, precio_salida, precio_cache_lectura,
                       precio_cache_escritura, registrado_por) VALUES ($1, $2, $3, $4, $5, $6)""",
                modelo_id, *[datos[c] for c in CAMPOS_PRECIO], usuario)
            return modelo_id


async def actualizar_modelo_ia(pool, modelo_id: int, datos: dict, usuario: str) -> bool:
    """Devuelve True si cambió algún precio (entonces se agrega una fila al historial: rige desde ahora)."""
    async with pool.acquire() as con:
        async with con.transaction():
            actual = await con.fetchrow("SELECT * FROM ia_modelos WHERE id = $1 FOR UPDATE", modelo_id)
            if not actual:
                return False
            await con.execute(
                """UPDATE ia_modelos SET proveedor_id = $2, modelo_id = $3, nombre_visible = $4, precio_entrada = $5,
                       precio_salida = $6, precio_cache_lectura = $7, precio_cache_escritura = $8, max_tokens = $9,
                       esfuerzo = $10, parametros_extra = $11, activo = $12, notas = $13, actualizado_en = now()
                   WHERE id = $1""",
                modelo_id, datos["proveedor_id"], datos["modelo_id"], datos["nombre_visible"],
                *[datos[c] for c in CAMPOS_PRECIO], datos["max_tokens"], datos["esfuerzo"], datos["parametros_extra"],
                datos["activo"], datos["notas"])
            cambio = any((actual[c] is None) != (datos[c] is None) or
                         (actual[c] is not None and float(actual[c]) != float(datos[c])) for c in CAMPOS_PRECIO)
            if cambio:
                await con.execute(
                    """INSERT INTO ia_precios_historial (modelo_id, precio_entrada, precio_salida, precio_cache_lectura,
                           precio_cache_escritura, registrado_por) VALUES ($1, $2, $3, $4, $5, $6)""",
                    modelo_id, *[datos[c] for c in CAMPOS_PRECIO], usuario)
            return cambio


async def niveles_ia(pool) -> dict:
    return {f["nivel"]: f["modelo_id"] for f in await pool.fetch("SELECT nivel, modelo_id FROM ia_niveles")}


async def asignar_nivel_ia(pool, nivel: str, modelo_id: int | None, usuario: str) -> None:
    await pool.execute(
        """INSERT INTO ia_niveles (nivel, modelo_id, actualizado_por) VALUES ($1, $2, $3)
           ON CONFLICT (nivel) DO UPDATE SET modelo_id = EXCLUDED.modelo_id, actualizado_por = EXCLUDED.actualizado_por,
               actualizado_en = now()""",
        nivel, modelo_id, usuario)


async def historial_precios(pool, limite: int = 200):
    return await pool.fetch(
        """SELECT h.*, m.nombre_visible, m.modelo_id AS id_api, p.nombre AS proveedor
           FROM ia_precios_historial h JOIN ia_modelos m ON m.id = h.modelo_id JOIN ia_proveedores p ON p.id = m.proveedor_id
           ORDER BY h.vigente_desde DESC, h.id DESC LIMIT $1""", limite)


async def uso_plataforma(pool, desde: datetime):
    """Pruebas y comparador del superadministrador (no cuentan en el resumen de ningún negocio)."""
    return await pool.fetch(
        """SELECT origen, count(*) AS consultas, count(*) FILTER (WHERE NOT ok) AS errores,
                  COALESCE(sum(tokens_entrada + tokens_cache_lectura + tokens_cache_escritura), 0) AS entrada,
                  COALESCE(sum(tokens_salida), 0) AS salida, COALESCE(sum(costo_estimado), 0) AS costo
           FROM ia_uso WHERE origen IN ('prueba', 'comparador') AND creado_en >= $1
           GROUP BY origen ORDER BY origen""", desde)
