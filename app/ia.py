"""Capa de proveedores de IA: una sola función, generar_respuesta(), con adaptadores para Anthropic y
para APIs compatibles con OpenAI (OpenAI y xAI). Niveles, llaves y precios vienen de variables de entorno;
no hay nombres de modelo en el código. Todo es opcional: sin llaves la app arranca y la IA figura como no disponible.
"""
import logging
import os
import time
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("teatiendo.ia")

NIVELES = ("basico", "estandar", "premium")
ETIQUETAS_NIVEL = {"basico": "Básico", "estandar": "Estándar", "premium": "Premium"}
PROVEEDORES = ("anthropic", "openai", "xai")
NOMBRES_PROVEEDOR = {"anthropic": "Anthropic", "openai": "OpenAI", "xai": "xAI"}
URL_ANTHROPIC = "https://api.anthropic.com/v1/messages"
VERSION_ANTHROPIC = "2023-06-01"
BASE_URL_DEFECTO = {"openai": "https://api.openai.com/v1", "xai": "https://api.x.ai/v1"}

# Transporte HTTP inyectable (las pruebas usan httpx.MockTransport). None = red real.
_transporte: httpx.AsyncBaseTransport | None = None


@dataclass(frozen=True)
class Precio:
    """USD por millón de tokens. Si falta el precio de caché se usa el de entrada."""
    entrada: float
    salida: float
    cache_lectura: float | None = None
    cache_escritura: float | None = None


@dataclass(frozen=True)
class Nivel:
    clave: str
    proveedor: str | None
    modelo: str | None
    precio: Precio | None
    disponible: bool
    motivo: str | None = None  # por qué no está disponible

    @property
    def etiqueta(self) -> str:
        return ETIQUETAS_NIVEL[self.clave]


@dataclass(frozen=True)
class ConfigIA:
    niveles: dict
    llaves: dict
    base_urls: dict
    historial_mensajes: int
    max_tokens: int
    timeout_s: float
    whatsapp_activo: bool
    extraccion_intervalo_s: int = 120
    avisos: tuple = ()

    @property
    def proveedores_disponibles(self) -> list[str]:
        return [p for p in PROVEEDORES if p in self.llaves]


@dataclass(frozen=True)
class Sistema:
    """estable: reglas + base de conocimiento (se cachea). variable: fecha y hora (fuera del caché)."""
    estable: str
    variable: str


@dataclass(frozen=True)
class Mensaje:
    rol: str  # 'user' | 'assistant'
    texto: str


@dataclass
class Uso:
    """entrada = tokens de entrada NO cacheados; la caché se cuenta aparte (igual en todos los proveedores)."""
    entrada: int = 0
    salida: int = 0
    cache_lectura: int = 0
    cache_escritura: int = 0


@dataclass
class Resultado:
    ok: bool
    texto: str
    nivel: str
    proveedor: str | None
    modelo: str | None
    uso: Uso = field(default_factory=Uso)
    costo: float | None = None
    duracion_ms: int = 0
    error: str | None = None


# --- Configuración -----------------------------------------------------------

def _entero(env, nombre: str, defecto: int, minimo: int, maximo: int, avisos: list) -> int:
    valor = env.get(nombre, "").strip()
    if not valor:
        return defecto
    try:
        numero = int(valor)
        if not minimo <= numero <= maximo:
            raise ValueError
        return numero
    except ValueError:
        avisos.append(f"{nombre} debe ser un número entre {minimo} y {maximo}; se usa {defecto}.")
        return defecto


def _precio(texto: str, nombre: str, avisos: list) -> Precio | None:
    if not texto:
        return None
    try:
        numeros = [float(p.strip()) for p in texto.split(",")]
    except ValueError:
        numeros = []
    if len(numeros) not in (2, 3, 4) or any(n < 0 for n in numeros):
        avisos.append(f"{nombre} debe tener el formato entrada,salida[,cache_lectura,cache_escritura] en USD por millón.")
        return None
    return Precio(*numeros)


def cargar_config(env=None) -> ConfigIA:
    env = os.environ if env is None else env
    avisos: list[str] = []
    llaves = {p: env.get(f"{p.upper()}_API_KEY", "").strip() for p in PROVEEDORES}
    llaves = {p: v for p, v in llaves.items() if v}
    base_urls = {p: (env.get(f"{p.upper()}_BASE_URL", "").strip() or url).rstrip("/")
                 for p, url in BASE_URL_DEFECTO.items()}

    niveles = {}
    for clave in NIVELES:
        variable = f"IA_NIVEL_{clave.upper()}"
        precio = _precio(env.get(f"IA_PRECIO_{clave.upper()}", "").strip(), f"IA_PRECIO_{clave.upper()}", avisos)
        valor = env.get(variable, "").strip()
        if not valor:
            niveles[clave] = Nivel(clave, None, None, precio, False, f"No configurado ({variable})")
            continue
        proveedor, separador, modelo = valor.partition(":")
        proveedor, modelo = proveedor.strip().lower(), modelo.strip()
        if not separador or not modelo or proveedor not in PROVEEDORES:
            avisos.append(f"{variable} debe tener el formato proveedor:modelo (proveedor: anthropic, openai o xai).")
            niveles[clave] = Nivel(clave, None, None, precio, False, f"Formato inválido en {variable}")
        elif proveedor not in llaves:
            niveles[clave] = Nivel(clave, proveedor, modelo, precio, False, f"Falta {proveedor.upper()}_API_KEY")
        else:
            niveles[clave] = Nivel(clave, proveedor, modelo, precio, True)

    config = ConfigIA(
        niveles=niveles,
        llaves=llaves,
        base_urls=base_urls,
        historial_mensajes=_entero(env, "IA_HISTORIAL_MENSAJES", 12, 1, 50, avisos),
        max_tokens=_entero(env, "IA_MAX_TOKENS", 400, 50, 4000, avisos),
        timeout_s=float(_entero(env, "IA_TIMEOUT_S", 30, 5, 120, avisos)),
        whatsapp_activo=env.get("IA_WHATSAPP_ACTIVO", "").strip().lower() in ("1", "true", "si", "sí", "yes", "on"),
        extraccion_intervalo_s=_entero(env, "IA_EXTRACCION_INTERVALO_S", 120, 0, 3600, avisos),
        avisos=tuple(avisos),
    )
    for aviso in avisos:
        log.warning("Configuración de IA: %s", aviso)
    return config


CONFIG = cargar_config()


def costo_estimado(precio: Precio | None, uso: Uso) -> float | None:
    if precio is None:
        return None
    lectura = precio.cache_lectura if precio.cache_lectura is not None else precio.entrada
    escritura = precio.cache_escritura if precio.cache_escritura is not None else precio.entrada
    total = (uso.entrada * precio.entrada + uso.salida * precio.salida
             + uso.cache_lectura * lectura + uso.cache_escritura * escritura) / 1_000_000
    return round(total, 6)


# --- Errores legibles --------------------------------------------------------

def _error_http(proveedor: str, modelo: str, estado: int, cuerpo) -> str:
    nombre = NOMBRES_PROVEEDOR[proveedor]
    detalle = ""
    if isinstance(cuerpo, dict):
        error = cuerpo.get("error")
        detalle = (error.get("message") if isinstance(error, dict) else error) or cuerpo.get("message") or ""
    detalle = f" ({str(detalle)[:200]})" if detalle else ""
    if estado in (401, 403):
        return f"La llave de API de {nombre} no es válida o no tiene permisos.{detalle}"
    if estado == 404:
        return f"El modelo «{modelo}» no existe en {nombre} o tu cuenta no tiene acceso.{detalle}"
    if estado == 429:
        return f"{nombre} rechazó la solicitud por límite de uso o saldo insuficiente.{detalle}"
    if estado >= 500:
        return f"El servicio de {nombre} no está disponible en este momento (HTTP {estado}). Inténtalo en unos minutos."
    return f"{nombre} rechazó la solicitud (HTTP {estado}).{detalle}"


# --- Adaptadores -------------------------------------------------------------

async def _anthropic(cliente: httpx.AsyncClient, cfg: ConfigIA, nivel: Nivel, sistema: Sistema,
                     historial: list[Mensaje], max_tokens: int, json_modo: bool) -> tuple[str, Uso]:
    respuesta = await cliente.post(URL_ANTHROPIC, headers={
        "x-api-key": cfg.llaves["anthropic"],
        "anthropic-version": VERSION_ANTHROPIC,
        "content-type": "application/json",
    }, json={
        "model": nivel.modelo,
        "max_tokens": max_tokens,  # Anthropic no tiene modo JSON: se pide en el prompt y se lee de forma tolerante
        "system": [
            {"type": "text", "text": sistema.estable, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": sistema.variable},
        ],
        "messages": [{"role": m.rol, "content": m.texto} for m in historial],
    })
    datos = _json(respuesta)
    if not respuesta.is_success:
        raise _ErrorProveedor(_error_http("anthropic", nivel.modelo, respuesta.status_code, datos))
    texto = "".join(b.get("text", "") for b in datos.get("content", []) if b.get("type") == "text")
    u = datos.get("usage") or {}
    return texto, Uso(
        entrada=int(u.get("input_tokens") or 0),
        salida=int(u.get("output_tokens") or 0),
        cache_lectura=int(u.get("cache_read_input_tokens") or 0),
        cache_escritura=int(u.get("cache_creation_input_tokens") or 0),
    )


async def _compatible_openai(cliente: httpx.AsyncClient, cfg: ConfigIA, nivel: Nivel, sistema: Sistema,
                             historial: list[Mensaje], max_tokens: int, json_modo: bool) -> tuple[str, Uso]:
    proveedor = nivel.proveedor
    # El bloque estable va primero para que la caché automática por prefijo de estos proveedores lo aproveche.
    carga = {
        "model": nivel.modelo,
        "messages": [{"role": "system", "content": f"{sistema.estable}\n\n{sistema.variable}"}]
                    + [{"role": m.rol, "content": m.texto} for m in historial],
    }
    # Los modelos recientes de OpenAI exigen max_completion_tokens; xAI acepta max_tokens.
    carga["max_completion_tokens" if proveedor == "openai" else "max_tokens"] = max_tokens
    if json_modo:
        carga["response_format"] = {"type": "json_object"}
    respuesta = await cliente.post(f"{cfg.base_urls[proveedor]}/chat/completions",
                                   headers={"Authorization": f"Bearer {cfg.llaves[proveedor]}"}, json=carga)
    datos = _json(respuesta)
    if not respuesta.is_success:
        raise _ErrorProveedor(_error_http(proveedor, nivel.modelo, respuesta.status_code, datos))
    opciones = datos.get("choices") or [{}]
    texto = ((opciones[0].get("message") or {}).get("content")) or ""
    u = datos.get("usage") or {}
    cacheados = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    return texto, Uso(
        entrada=max(int(u.get("prompt_tokens") or 0) - cacheados, 0),
        salida=int(u.get("completion_tokens") or 0),
        cache_lectura=cacheados,
    )


class _ErrorProveedor(Exception):
    pass


def _json(respuesta: httpx.Response):
    try:
        return respuesta.json()
    except ValueError:
        return {}


ADAPTADORES = {"anthropic": _anthropic, "openai": _compatible_openai, "xai": _compatible_openai}


async def generar_respuesta(sistema: Sistema, historial: list[Mensaje], nivel: str,
                            cfg: ConfigIA | None = None, *, json_modo: bool = False,
                            max_tokens: int | None = None) -> Resultado:
    """Genera una respuesta. Nunca lanza excepción: los fallos vuelven en Resultado.error, en español.
    json_modo: pide JSON al proveedor cuando lo soporta (OpenAI y xAI)."""
    cfg = cfg or CONFIG
    datos_nivel: Nivel | None = cfg.niveles.get(nivel)
    if datos_nivel is None:
        return Resultado(False, "", nivel, None, None, error=f"Nivel de IA desconocido: {nivel}.")
    if not datos_nivel.disponible:
        return Resultado(False, "", nivel, datos_nivel.proveedor, datos_nivel.modelo,
                         error=f"El nivel {datos_nivel.etiqueta} no está disponible: {datos_nivel.motivo}.")
    if not historial or historial[-1].rol != "user":
        return Resultado(False, "", nivel, datos_nivel.proveedor, datos_nivel.modelo,
                         error="No hay un mensaje del cliente para responder.")

    nombre = NOMBRES_PROVEEDOR[datos_nivel.proveedor]
    inicio = time.monotonic()
    resultado = Resultado(False, "", nivel, datos_nivel.proveedor, datos_nivel.modelo)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(cfg.timeout_s), transport=_transporte) as cliente:
            texto, uso = await ADAPTADORES[datos_nivel.proveedor](cliente, cfg, datos_nivel, sistema, historial,
                                                                  max_tokens or cfg.max_tokens, json_modo)
        resultado.uso = uso
        resultado.costo = costo_estimado(datos_nivel.precio, uso)
        resultado.texto = texto.strip()
        resultado.ok = bool(resultado.texto)
        if not resultado.ok:
            resultado.error = f"{nombre} devolvió una respuesta vacía."
    except _ErrorProveedor as e:
        resultado.error = str(e)
    except httpx.TimeoutException:
        resultado.error = f"{nombre} no respondió en {int(cfg.timeout_s)} segundos."
    except httpx.HTTPError as e:
        resultado.error = f"No se pudo conectar con {nombre} ({e.__class__.__name__})."
    resultado.duracion_ms = int((time.monotonic() - inicio) * 1000)
    if resultado.error:
        log.warning("IA %s/%s: %s", datos_nivel.proveedor, datos_nivel.modelo, resultado.error)
    return resultado
