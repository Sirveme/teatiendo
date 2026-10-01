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
URL_BASE_ANTHROPIC = "https://api.anthropic.com/v1"
TIPOS_PROVEEDOR = ("anthropic", "compatible_openai")
# Parámetros que parametros_extra nunca puede reemplazar.
PARAMETROS_PROTEGIDOS = {"model", "messages", "system", "stream", "max_tokens", "max_completion_tokens"}

# Esfuerzo de razonamiento por nivel (IA_ESFUERZO_<NIVEL>). Se envía tal cual como output_config.effort (Anthropic)
# o reasoning_effort (OpenAI/xAI). "sin_razonamiento": Anthropic thinking between_tools (solo Claude Sonnet 5.5),
# OpenAI/xAI reasoning_effort "none". "omitir": no se envía nada (el modelo usa su valor por defecto).
ESFUERZOS = ("low", "medium", "high", "xhigh", "max", "minimal", "none", "sin_razonamiento", "omitir")
ESFUERZO_DEFECTO = "low"
MULTIPLICADOR_REINTENTO = 4      # si la respuesta se corta por límite de tokens, se reintenta con 4x el límite
MAX_TOKENS_REINTENTO = 8000
PALABRAS_PARAMETRO_RAZONAMIENTO = ("effort", "thinking", "reasoning")
# Modelos que rechazaron los parámetros de razonamiento (p. ej. Claude Haiku 4.5 rechaza "effort"): no se reenvían.
_sin_parametros_razonamiento: set = set()

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
    esfuerzo: str = ESFUERZO_DEFECTO
    # Conexión y ajustes del modelo (vienen de la base de datos o, de respaldo, de las variables de entorno).
    tipo: str = "anthropic"                    # 'anthropic' | 'compatible_openai'
    url_base: str = URL_BASE_ANTHROPIC
    llave: str = field(default="", repr=False)
    campo_max_tokens: str = "max_tokens"       # OpenAI: max_completion_tokens; xAI y Anthropic: max_tokens
    max_tokens: int | None = None              # None = IA_MAX_TOKENS
    parametros_extra: dict = field(default_factory=dict)
    origen: str = "entorno"                    # 'entorno' | 'base'
    modelo_ref_id: int | None = None           # ia_modelos.id (solo origen 'base')
    precio_id: int | None = None               # ia_precios_historial.id vigente al resolver el nivel
    nombre_visible: str | None = None

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
    razonamiento: int | None = None  # tokens de razonamiento, solo si el proveedor los informa (ya incluidos en salida)

    def sumar(self, otro: "Uso") -> None:
        self.entrada += otro.entrada
        self.salida += otro.salida
        self.cache_lectura += otro.cache_lectura
        self.cache_escritura += otro.cache_escritura
        if otro.razonamiento is not None:
            self.razonamiento = (self.razonamiento or 0) + otro.razonamiento


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
    truncado: bool = False   # terminó por límite de tokens (aun tras el reintento): nunca se envía al cliente
    reintentos: int = 0      # reintentos por límite de tokens
    modelo_ref_id: int | None = None
    precio_id: int | None = None


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


def _conexion_entorno(proveedor: str, llave: str, base_urls: dict) -> dict:
    if proveedor == "anthropic":
        return {"tipo": "anthropic", "url_base": URL_BASE_ANTHROPIC, "llave": llave, "campo_max_tokens": "max_tokens"}
    return {"tipo": "compatible_openai", "url_base": base_urls[proveedor], "llave": llave,
            "campo_max_tokens": "max_completion_tokens" if proveedor == "openai" else "max_tokens"}


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
        esfuerzo = env.get(f"IA_ESFUERZO_{clave.upper()}", "").strip().lower() or ESFUERZO_DEFECTO
        if esfuerzo not in ESFUERZOS:
            avisos.append(f"IA_ESFUERZO_{clave.upper()} debe ser uno de: {', '.join(ESFUERZOS)}; se usa {ESFUERZO_DEFECTO}.")
            esfuerzo = ESFUERZO_DEFECTO
        valor = env.get(variable, "").strip()
        if not valor:
            niveles[clave] = Nivel(clave, None, None, precio, False, f"No configurado ({variable})", esfuerzo)
            continue
        proveedor, separador, modelo = valor.partition(":")
        proveedor, modelo = proveedor.strip().lower(), modelo.strip()
        if not separador or not modelo or proveedor not in PROVEEDORES:
            avisos.append(f"{variable} debe tener el formato proveedor:modelo (proveedor: anthropic, openai o xai).")
            niveles[clave] = Nivel(clave, None, None, precio, False, f"Formato inválido en {variable}", esfuerzo)
        elif proveedor not in llaves:
            niveles[clave] = Nivel(clave, proveedor, modelo, precio, False, f"Falta {proveedor.upper()}_API_KEY", esfuerzo)
        else:
            niveles[clave] = Nivel(clave, proveedor, modelo, precio, True, None, esfuerzo, **_conexion_entorno(
                proveedor, llaves[proveedor], base_urls))

    config = ConfigIA(
        niveles=niveles,
        llaves=llaves,
        base_urls=base_urls,
        historial_mensajes=_entero(env, "IA_HISTORIAL_MENSAJES", 12, 1, 50, avisos),
        # El razonamiento cuenta dentro de max_tokens aunque no se devuelva: 1024 deja margen sobre ~120 palabras.
        max_tokens=_entero(env, "IA_MAX_TOKENS", 1024, 100, 8000, avisos),
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

class _ErrorProveedor(Exception):
    def __init__(self, mensaje: str, por_parametros: bool = False):
        super().__init__(mensaje)
        self.por_parametros = por_parametros  # 400 causado por los parámetros de razonamiento


def _detalle(cuerpo) -> str:
    if not isinstance(cuerpo, dict):
        return ""
    error = cuerpo.get("error")
    return str((error.get("message") if isinstance(error, dict) else error) or cuerpo.get("message") or "")


def _nombre(proveedor: str | None) -> str:
    return NOMBRES_PROVEEDOR.get(proveedor or "", proveedor or "el proveedor")


def _error_http(proveedor: str, modelo: str, estado: int, cuerpo) -> _ErrorProveedor:
    nombre = _nombre(proveedor)
    texto = _detalle(cuerpo)
    detalle = f" ({texto[:200]})" if texto else ""
    if estado in (401, 403):
        return _ErrorProveedor(f"La llave de API de {nombre} no es válida o no tiene permisos.{detalle}")
    if estado == 404:
        return _ErrorProveedor(f"El modelo «{modelo}» no existe en {nombre} o tu cuenta no tiene acceso.{detalle}")
    if estado == 429:
        return _ErrorProveedor(f"{nombre} rechazó la solicitud por límite de uso o saldo insuficiente.{detalle}")
    if estado >= 500:
        return _ErrorProveedor(f"El servicio de {nombre} no está disponible en este momento (HTTP {estado}). Inténtalo en unos minutos.")
    por_parametros = estado == 400 and any(p in texto.lower() for p in PALABRAS_PARAMETRO_RAZONAMIENTO)
    return _ErrorProveedor(f"{nombre} rechazó la solicitud (HTTP {estado}).{detalle}", por_parametros)


# --- Adaptadores -------------------------------------------------------------
# Cada adaptador devuelve (texto, uso, fin) con fin: "ok" | "truncado" (límite de tokens) | "rechazo" (filtro).

async def _anthropic(cliente: httpx.AsyncClient, cfg: ConfigIA, nivel: Nivel, sistema: Sistema,
                     historial: list[Mensaje], max_tokens: int, json_modo: bool, razonamiento: bool):
    carga = {
        "model": nivel.modelo,
        "max_tokens": max_tokens,  # Anthropic no tiene modo JSON: se pide en el prompt y se lee de forma tolerante
        "system": [
            {"type": "text", "text": sistema.estable, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": sistema.variable},
        ],
        "messages": [{"role": m.rol, "content": m.texto} for m in historial],
    }
    if razonamiento and nivel.esfuerzo == "sin_razonamiento":
        carga["thinking"] = {"type": "between_tools"}        # nivel más bajo; solo Claude Sonnet 5.5 lo acepta
    elif razonamiento and nivel.esfuerzo != "omitir":
        carga["output_config"] = {"effort": nivel.esfuerzo}  # p. ej. "low": razonamiento corto o ninguno
    _agregar_extra(carga, nivel)
    respuesta = await cliente.post(f"{nivel.url_base.rstrip('/')}/messages", headers={
        "x-api-key": nivel.llave,
        "anthropic-version": VERSION_ANTHROPIC,
        "content-type": "application/json",
    }, json=carga)
    datos = _json(respuesta)
    if not respuesta.is_success:
        raise _error_http(nivel.proveedor, nivel.modelo, respuesta.status_code, datos)
    # Se leen solo los bloques de texto (los de razonamiento vienen aparte y no se muestran).
    texto = "".join(b.get("text", "") for b in datos.get("content", []) if b.get("type") == "text")
    u = datos.get("usage") or {}
    motivo = datos.get("stop_reason")
    fin = ("truncado" if motivo in ("max_tokens", "model_context_window_exceeded")
           else "rechazo" if motivo == "refusal" else "ok")
    # Anthropic no informa por separado los tokens de razonamiento: van dentro de output_tokens.
    return texto, Uso(
        entrada=int(u.get("input_tokens") or 0),
        salida=int(u.get("output_tokens") or 0),
        cache_lectura=int(u.get("cache_read_input_tokens") or 0),
        cache_escritura=int(u.get("cache_creation_input_tokens") or 0),
    ), fin


async def _compatible_openai(cliente: httpx.AsyncClient, cfg: ConfigIA, nivel: Nivel, sistema: Sistema,
                             historial: list[Mensaje], max_tokens: int, json_modo: bool, razonamiento: bool):
    # El bloque estable va primero para que la caché automática por prefijo de estos proveedores lo aproveche.
    carga = {
        "model": nivel.modelo,
        "messages": [{"role": "system", "content": f"{sistema.estable}\n\n{sistema.variable}"}]
                    + [{"role": m.rol, "content": m.texto} for m in historial],
    }
    # Los modelos recientes de OpenAI exigen max_completion_tokens; xAI acepta max_tokens (campo del proveedor).
    carga[nivel.campo_max_tokens] = max_tokens
    if razonamiento and nivel.esfuerzo != "omitir":
        carga["reasoning_effort"] = "none" if nivel.esfuerzo == "sin_razonamiento" else nivel.esfuerzo
    if json_modo:
        carga["response_format"] = {"type": "json_object"}
    _agregar_extra(carga, nivel)
    respuesta = await cliente.post(f"{nivel.url_base.rstrip('/')}/chat/completions",
                                   headers={"Authorization": f"Bearer {nivel.llave}"}, json=carga)
    datos = _json(respuesta)
    if not respuesta.is_success:
        raise _error_http(nivel.proveedor, nivel.modelo, respuesta.status_code, datos)
    opciones = datos.get("choices") or [{}]
    texto = ((opciones[0].get("message") or {}).get("content")) or ""
    motivo = opciones[0].get("finish_reason")
    fin = "truncado" if motivo == "length" else "rechazo" if motivo == "content_filter" else "ok"
    u = datos.get("usage") or {}
    cacheados = int(((u.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    razon = (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
    return texto, Uso(
        entrada=max(int(u.get("prompt_tokens") or 0) - cacheados, 0),
        salida=int(u.get("completion_tokens") or 0),
        cache_lectura=cacheados,
        razonamiento=int(razon) if razon is not None else None,
    ), fin


def _agregar_extra(carga: dict, nivel: Nivel) -> None:
    """parametros_extra del modelo (JSON del panel), sin reemplazar nunca los parámetros protegidos."""
    for clave, valor in (nivel.parametros_extra or {}).items():
        if clave not in PARAMETROS_PROTEGIDOS:
            carga[clave] = valor


def _json(respuesta: httpx.Response):
    try:
        return respuesta.json()
    except ValueError:
        return {}


ADAPTADORES = {"anthropic": _anthropic, "compatible_openai": _compatible_openai}


async def generar_respuesta(sistema: Sistema, historial: list[Mensaje], nivel: "str | Nivel",
                            cfg: ConfigIA | None = None, *, json_modo: bool = False,
                            max_tokens: int | None = None) -> Resultado:
    """Genera una respuesta. Nunca lanza excepción: los fallos vuelven en Resultado.error, en español.
    json_modo: pide JSON al proveedor cuando lo soporta (OpenAI y xAI)."""
    cfg = cfg or CONFIG
    if isinstance(nivel, Nivel):
        datos_nivel, nivel = nivel, nivel.clave
    else:
        datos_nivel = cfg.niveles.get(nivel)
    if datos_nivel is None:
        return Resultado(False, "", nivel, None, None, error=f"Nivel de IA desconocido: {nivel}.")
    if not datos_nivel.disponible:
        return Resultado(False, "", nivel, datos_nivel.proveedor, datos_nivel.modelo,
                         error=f"El nivel {datos_nivel.etiqueta} no está disponible: {datos_nivel.motivo}.")
    if not historial or historial[-1].rol != "user":
        return Resultado(False, "", nivel, datos_nivel.proveedor, datos_nivel.modelo,
                         error="No hay un mensaje del cliente para responder.")

    nombre = _nombre(datos_nivel.proveedor)
    adaptador = ADAPTADORES[datos_nivel.tipo]
    clave_modelo = (datos_nivel.proveedor, datos_nivel.modelo)
    inicio = time.monotonic()
    resultado = Resultado(False, "", nivel, datos_nivel.proveedor, datos_nivel.modelo,
                          modelo_ref_id=datos_nivel.modelo_ref_id, precio_id=datos_nivel.precio_id)
    limite = max_tokens or datos_nivel.max_tokens or cfg.max_tokens
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(cfg.timeout_s), transport=_transporte) as cliente:
            async def llamar(tokens: int):
                usar_razonamiento = clave_modelo not in _sin_parametros_razonamiento
                try:
                    return await adaptador(cliente, cfg, datos_nivel, sistema, historial, tokens, json_modo,
                                           usar_razonamiento)
                except _ErrorProveedor as e:
                    if not (e.por_parametros and usar_razonamiento):
                        raise
                    # El modelo no acepta el parámetro de razonamiento: se recuerda y se repite sin él.
                    log.info("IA %s/%s no acepta parámetros de razonamiento; se envía sin ellos", *clave_modelo)
                    _sin_parametros_razonamiento.add(clave_modelo)
                    return await adaptador(cliente, cfg, datos_nivel, sistema, historial, tokens, json_modo, False)

            texto, uso, fin = await llamar(limite)
            resultado.uso = uso
            if fin == "truncado":  # nunca se usa una respuesta cortada: un reintento con más margen
                resultado.reintentos = 1
                limite = min(limite * MULTIPLICADOR_REINTENTO, max(MAX_TOKENS_REINTENTO, limite * 2))
                texto, uso, fin = await llamar(limite)
                resultado.uso.sumar(uso)
        resultado.costo = costo_estimado(datos_nivel.precio, resultado.uso)
        if fin == "truncado":
            resultado.truncado = True
            resultado.error = (f"La respuesta se cortó por el límite de tokens, incluso al reintentar con {limite} "
                               "tokens. No se envía una respuesta incompleta.")
        elif fin == "rechazo":
            resultado.error = f"{nombre} no generó la respuesta (filtro de seguridad del proveedor)."
        else:
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
