"""Acceso a PostgreSQL con asyncpg. Todas las consultas de datos filtran por tenant_id."""
import json
from datetime import datetime

import asyncpg


async def _preparar_conexion(con: asyncpg.Connection) -> None:
    await con.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def crear_pool(url: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(url, min_size=1, max_size=5, command_timeout=15, init=_preparar_conexion)


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

async def upsert_contacto_entrante(pool, tenant_id: int, wa_id: str, nombre: str | None, recibido_en: datetime) -> int:
    return await pool.fetchval(
        """
        INSERT INTO contacts (tenant_id, wa_id, nombre_perfil, ultimo_mensaje_entrante_en)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (tenant_id, wa_id) DO UPDATE SET
            nombre_perfil = COALESCE(EXCLUDED.nombre_perfil, contacts.nombre_perfil),
            ultimo_mensaje_entrante_en = GREATEST(contacts.ultimo_mensaje_entrante_en,
                                                  EXCLUDED.ultimo_mensaje_entrante_en)
        RETURNING id
        """,
        tenant_id, wa_id, nombre, recibido_en,
    )


async def upsert_contacto(pool, tenant_id: int, wa_id: str) -> int:
    return await pool.fetchval(
        """
        INSERT INTO contacts (tenant_id, wa_id) VALUES ($1, $2)
        ON CONFLICT (tenant_id, wa_id) DO UPDATE SET wa_id = EXCLUDED.wa_id
        RETURNING id
        """,
        tenant_id, wa_id,
    )


async def listar_contactos(pool, tenant_id: int):
    return await pool.fetch(
        """
        SELECT c.id, c.wa_id, c.nombre_perfil, c.ultimo_mensaje_entrante_en,
               m.texto AS ultimo_texto, m.direccion AS ultima_direccion, m.creado_en AS ultimo_en
        FROM contacts c
        LEFT JOIN LATERAL (
            SELECT texto, direccion, creado_en FROM messages
            WHERE contact_id = c.id ORDER BY creado_en DESC, id DESC LIMIT 1
        ) m ON TRUE
        WHERE c.tenant_id = $1
        ORDER BY m.creado_en DESC NULLS LAST, c.id DESC
        LIMIT 200
        """,
        tenant_id,
    )


async def obtener_contacto(pool, tenant_id: int, contacto_id: int):
    return await pool.fetchrow(
        "SELECT id, wa_id, nombre_perfil, ultimo_mensaje_entrante_en FROM contacts WHERE id = $1 AND tenant_id = $2",
        contacto_id, tenant_id,
    )


# --- Mensajes ----------------------------------------------------------------

async def listar_mensajes(pool, tenant_id: int, contacto_id: int, limite: int = 200):
    return await pool.fetch(
        """
        SELECT * FROM (
            SELECT id, direccion, tipo, texto, estado, error_json, creado_en
            FROM messages WHERE tenant_id = $1 AND contact_id = $2
            ORDER BY creado_en DESC, id DESC LIMIT $3
        ) t ORDER BY creado_en, id
        """,
        tenant_id, contacto_id, limite,
    )


async def insertar_mensaje(pool, tenant_id: int, contacto_id: int, *, wamid: str | None, direccion: str,
                           tipo: str, texto: str | None, estado: str | None,
                           error_json=None, creado_en: datetime | None = None) -> int | None:
    """Inserta un mensaje. Si el wamid ya existe no duplica y devuelve None (idempotencia)."""
    return await pool.fetchval(
        """
        INSERT INTO messages (tenant_id, contact_id, wamid, direccion, tipo, texto, estado, error_json, creado_en)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, COALESCE($9, now()))
        ON CONFLICT (wamid) DO NOTHING
        RETURNING id
        """,
        tenant_id, contacto_id, wamid, direccion, tipo, texto, estado, error_json, creado_en,
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
        SELECT id, nombre, idioma, categoria, cuerpo, estado_meta, meta_template_id, motivo_rechazo, creado_en
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
                           estado_meta: str, meta_template_id: str | None, motivo_rechazo: str | None) -> None:
    await pool.execute(
        """
        INSERT INTO templates (tenant_id, nombre, idioma, categoria, cuerpo, estado_meta, meta_template_id, motivo_rechazo)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (tenant_id, nombre, idioma) DO UPDATE SET
            categoria        = EXCLUDED.categoria,
            cuerpo           = COALESCE(NULLIF(EXCLUDED.cuerpo, ''), templates.cuerpo),
            estado_meta      = EXCLUDED.estado_meta,
            meta_template_id = COALESCE(EXCLUDED.meta_template_id, templates.meta_template_id),
            motivo_rechazo   = EXCLUDED.motivo_rechazo
        """,
        tenant_id, nombre, idioma, categoria, cuerpo, estado_meta, meta_template_id, motivo_rechazo,
    )
