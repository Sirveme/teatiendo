"""Cifrado de las llaves de API de los proveedores de IA (Fernet: AES-128-CBC + HMAC-SHA256).
La llave maestra vive en la variable de entorno IA_LLAVE_MAESTRA (se genera con generar_hash.py).
Las llaves descifradas nunca se muestran ni se envían al navegador: solo sus últimos 4 caracteres."""
import os

from cryptography.fernet import Fernet, InvalidToken

VARIABLE = "IA_LLAVE_MAESTRA"


class ErrorCifrado(Exception):
    """Falta la llave maestra, es inválida o no corresponde a la llave con la que se cifró."""


def _fernet() -> Fernet:
    llave = os.getenv(VARIABLE, "").strip()
    if not llave:
        raise ErrorCifrado(f"Falta la variable {VARIABLE}: genérala con «python generar_hash.py».")
    try:
        return Fernet(llave.encode())
    except (ValueError, TypeError) as e:
        raise ErrorCifrado(f"{VARIABLE} no es una llave válida (debe ser una llave Fernet de 44 caracteres).") from e


def motivo_no_disponible() -> str | None:
    try:
        _fernet()
        return None
    except ErrorCifrado as e:
        return str(e)


def cifrar(texto: str) -> str:
    return _fernet().encrypt(texto.encode()).decode()


def descifrar(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise ErrorCifrado(f"No se pudo descifrar la llave: {VARIABLE} no es la misma con la que se guardó.") from e


def ultimos4(texto: str) -> str:
    return (texto or "")[-4:]


def enmascarar(ultimos: str | None) -> str:
    return f"••••{ultimos}" if ultimos else "Sin llave"
