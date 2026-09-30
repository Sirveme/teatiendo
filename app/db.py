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
        INSERT INTO tenants (nombre, waba_id, phone_number_id)
        VALUES ('PERU SISTEMAS PRO E.I.R.L.', $1, $2)
        ON CONFLICT (phone_number_id) DO UPDATE SET waba_id = EXCLUDED.waba_id
        RETURNING id
        """,
        waba_id, phone_number_id,
    )


async def obtener_tenant(pool, tenant_id: int):
    return await pool.fetchrow("SELECT id, nombre, waba_id, phone_number_id FROM tenants WHERE id = $1", tenant_id)


async def tenant_por_phone(pool, phone_number_id: str | None) -> int | None:
    if not phone_number_id:
        return None
    return await pool.fetchval("SELECT id FROM tenants WHERE phone_number_id = $1", phone_number_id)


# --- Webhook -----------------------------------------------------------------

async def guardar_evento(pool, payload: dict) -> None:
    await pool.execute("INSERT INTO webhook_events (payload) VALUES ($1)", payload)


# --- Contactos ---------------------------------------------------------------

async def upsert_contacto_entrante(pool, tenant_id: int, wa_id: str, nombre: str | None, recibido_en: datetime,
                                   canal: str = CANAL_WHATSAPP) -> int:
    return await pool.fetchval(
        """
        INSERT INTO contacts (tenant_id, canal, wa_id, nombre_perfil, ultimo_mensaje_entrante_en)
        VALUES ($1, $5, $2, $3, $4)
        ON CONFLICT (tenant_id, canal, wa_id) DO UPDATE SET
            nombre_perfil = COALESCE(EXCLUDED.nombre_perfil, contacts.nombre_perfil),
            ultimo_mensaje_entrante_en = GREATEST(contacts.ultimo_mensaje_entrante_en,
                                                  EXCLUDED.ultimo_mensaje_entrante_en)
        RETURNING id
        """,
        tenant_id, wa_id, nombre, recibido_en, canal,
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


async def listar_contactos(pool, tenant_id: int):
    return await pool.fetch(
        """
        SELECT c.id, c.wa_id, c.nombre_perfil, c.ultimo_mensaje_entrante_en, c.modo, c.derivado_en,
               m.texto AS ultimo_texto, m.direccion AS ultima_direccion, m.creado_en AS ultimo_en
        FROM contacts c
        LEFT JOIN LATERAL (
            SELECT texto, direccion, creado_en FROM messages
            WHERE contact_id = c.id ORDER BY id DESC LIMIT 1
        ) m ON TRUE
        WHERE c.tenant_id = $1 AND c.canal = 'whatsapp'
        ORDER BY m.creado_en DESC NULLS LAST, c.id DESC
        LIMIT 200
        """,
        tenant_id,
    )


async def obtener_contacto(pool, tenant_id: int, contacto_id: int):
    return await pool.fetchrow(
        """
        SELECT id, canal, wa_id, nombre_perfil, ultimo_mensaje_entrante_en, modo, derivado_en, aviso_no_texto_en
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
                           "mensaje_derivacion", "mensaje_no_texto")
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
                            tokens_cache_lectura, tokens_cache_escritura, costo_estimado, duracion_ms, ok, error)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
        RETURNING id
        """,
        tenant_id, message_id, origen, resultado.nivel, resultado.proveedor, resultado.modelo, uso.entrada,
        uso.salida, uso.cache_lectura, uso.cache_escritura, resultado.costo, resultado.duracion_ms, resultado.ok,
        resultado.error,
    )


async def uso_por_mensajes(pool, tenant_id: int, message_ids: list[int]) -> dict:
    if not message_ids:
        return {}
    filas = await pool.fetch(
        """
        SELECT message_id, proveedor, modelo, nivel, tokens_entrada, tokens_salida, tokens_cache_lectura,
               tokens_cache_escritura, costo_estimado, duracion_ms
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
        FROM ia_uso WHERE tenant_id = $1 AND creado_en >= $2
        GROUP BY origen ORDER BY origen
        """,
        tenant_id, desde,
    )


async def ultimos_usos(pool, tenant_id: int, limite: int = 15):
    return await pool.fetch(
        """
        SELECT creado_en, origen, nivel, proveedor, modelo, tokens_entrada, tokens_salida,
               tokens_cache_lectura + tokens_cache_escritura AS cache, costo_estimado, duracion_ms, ok, error
        FROM ia_uso WHERE tenant_id = $1 ORDER BY creado_en DESC, id DESC LIMIT $2
        """,
        tenant_id, limite,
    )
