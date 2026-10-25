# field_crypto.py
# Cifrado de campos sensibles en la DB de configuración (visioncore.db).
# Usa NaCl SecretBox (mismo algoritmo que mediamtx_crypto.py).
# La clave vive en Windows Credential Manager con nombre "field_key".
#
# Qué cifra:
#   - camera_config.data.password
#   - camera_config.data.streams[].url  (contiene user:pass@ en la URL RTSP)
#   - camera_config.data.selected_main_profile.url
#   - camera_config.data.selected_sub_profile.url
#   - ⭐ SOPORTE MULTI-LENTE (2025):
#       - camera_config.data.lentes[].main_stream.url
#       - camera_config.data.lentes[].sub_stream.url
#       - camera_config.data.lentes[].main_profile.url
#       - camera_config.data.lentes[].sub_profile.url
#       - camera_config.data.lentes[].streams[].url
#
# Qué NO cifra:
#   - IP, camera_name, camera_index, device_info, personas, zonas, etc.
#   Esos quedan en texto plano porque no son secretos.

import os
import base64
import string
import secrets
import keyring
from nacl.secret import SecretBox
from nacl.exceptions import CryptoError

SERVICE_NAME = "VisionCore"
KEY_NAME = "field_key"

# Prefijo para detectar si un campo ya está cifrado
# (NaCl SecretBox con nonce + MAC da base64 que empieza de forma variable,
#  así que usamos un prefijo fijo al guardar para poder detectar el estado)
PREFIX = "enc::v1::"


# ============================================================
# CLAVE
# ============================================================
def generar_clave_campos() -> bytes:
    """Genera una clave textual de 32 caracteres y la guarda."""
    caracteres = string.ascii_letters + string.digits + "-_"
    clave = ''.join(secrets.choice(caracteres) for _ in range(32))
    keyring.set_password(SERVICE_NAME, KEY_NAME, clave)
    print(f"[FieldCrypto] Clave generada: {clave[:10]}...")
    return clave.encode('utf-8')


def obtener_clave_campos() -> bytes:
    """Lee la clave del Credential Manager. Si no existe, la crea."""
    clave = keyring.get_password(SERVICE_NAME, KEY_NAME)
    if not clave:
        print("[FieldCrypto] No hay clave, generando nueva...")
        return generar_clave_campos()

    clave_bytes = clave.encode('utf-8')
    if len(clave_bytes) != 32:
        print(f"[FieldCrypto] Clave inválida ({len(clave_bytes)} bytes), regenerando...")
        return generar_clave_campos()
    return clave_bytes


# ============================================================
# CIFRADO / DESCIFRADO DE STRINGS
# ============================================================
def cifrar_campo(texto: str) -> str:
    """
    Cifra un string. Devuelve el texto cifrado con el prefijo 'enc::v1::'.
    Si el texto ya está cifrado, lo devuelve tal cual.
    """
    if not texto:
        return texto
    if es_campo_cifrado(texto):
        return texto  # ya está cifrado, no re-cifrar

    try:
        clave = obtener_clave_campos()
        box = SecretBox(clave)
        cifrado = box.encrypt(texto.encode('utf-8'))
        b64 = base64.b64encode(cifrado).decode('utf-8')
        return f"{PREFIX}{b64}"
    except Exception as e:
        print(f"[FieldCrypto] Error cifrando: {e}")
        return texto


def descifrar_campo(texto_cifrado: str) -> str:
    """
    Descifra un string cifrado con 'enc::v1::'. Si no tiene el prefijo,
    lo devuelve tal cual (compatibilidad con datos en claro).
    """
    if not texto_cifrado:
        return texto_cifrado
    if not es_campo_cifrado(texto_cifrado):
        return texto_cifrado  # no está cifrado, devolver tal cual

    try:
        b64 = texto_cifrado[len(PREFIX):]
        clave = obtener_clave_campos()
        box = SecretBox(clave)
        cifrado_bytes = base64.b64decode(b64)
        descifrado = box.decrypt(cifrado_bytes)
        return descifrado.decode('utf-8')
    except CryptoError:
        print(f"[FieldCrypto] ⚠️ No se pudo descifrar (clave incorrecta o dato corrupto)")
        return texto_cifrado  # devolver el cifrado para no romper
    except Exception as e:
        print(f"[FieldCrypto] Error descifrando: {e}")
        return texto_cifrado


def es_campo_cifrado(texto: str) -> bool:
    """Devuelve True si el string ya está cifrado (tiene el prefijo)."""
    return isinstance(texto, str) and texto.startswith(PREFIX)


# ============================================================
# CIFRADO DE CAMPOS SENSIBLES EN UN OBJETO CÁMARA
# ============================================================
def _procesar_campo(d: dict, key: str, fn):
    """Aplica fn al campo si existe y es string no vacío."""
    if isinstance(d, dict) and key in d and isinstance(d[key], str) and d[key]:
        d[key] = fn(d[key])


def _procesar_urls_en_lista(lst: list, fn):
    """Aplica fn a .url de cada item de una lista de dicts."""
    if not isinstance(lst, list):
        return
    for item in lst:
        if isinstance(item, dict) and 'url' in item and isinstance(item['url'], str) and item['url']:
            item['url'] = fn(item['url'])


def _procesar_perfil(prof, fn):
    """Aplica fn a .url de un perfil (main o sub)."""
    if isinstance(prof, dict) and 'url' in prof and isinstance(prof['url'], str) and prof['url']:
        prof['url'] = fn(prof['url'])


def _procesar_lentes(lentes: list, fn):
    """
    ⭐ SOPORTE MULTI-LENTE:
    Aplica fn a todas las URLs dentro de cada lente:
      - main_stream.url
      - sub_stream.url
      - main_profile.url
      - sub_profile.url
      - streams[].url
    """
    if not isinstance(lentes, list):
        return
    for lente in lentes:
        if not isinstance(lente, dict):
            continue
        
        # main_stream.url
        _procesar_perfil(lente.get('main_stream'), fn)
        
        # sub_stream.url
        _procesar_perfil(lente.get('sub_stream'), fn)
        
        # main_profile.url
        _procesar_perfil(lente.get('main_profile'), fn)
        
        # sub_profile.url
        _procesar_perfil(lente.get('sub_profile'), fn)
        
        # streams[].url
        _procesar_urls_en_lista(lente.get('streams', []), fn)


def cifrar_campos_sensibles(camera_dict: dict) -> dict:
    """
    Cifra los campos sensibles de un objeto cámara:
      - password
      - streams[].url
      - selected_main_profile.url
      - selected_sub_profile.url
      - ⭐ lentes[].main_stream.url
      - ⭐ lentes[].sub_stream.url
      - ⭐ lentes[].main_profile.url
      - ⭐ lentes[].sub_profile.url
      - ⭐ lentes[].streams[].url

    Modifica el dict IN-PLACE y lo devuelve.
    """
    if not isinstance(camera_dict, dict):
        return camera_dict

    # Campos a nivel cámara
    _procesar_campo(camera_dict, 'password', cifrar_campo)
    _procesar_urls_en_lista(camera_dict.get('streams', []), cifrar_campo)

    for key in ('selected_main_profile', 'selected_sub_profile'):
        _procesar_perfil(camera_dict.get(key), cifrar_campo)
    
    # ⭐ SOPORTE MULTI-LENTE: cifrar URLs dentro de lentes
    _procesar_lentes(camera_dict.get('lentes', []), cifrar_campo)

    return camera_dict


def descifrar_campos_sensibles(camera_dict: dict) -> dict:
    """
    Descifra los campos sensibles de un objeto cámara.
    Si algún campo no estaba cifrado, lo devuelve tal cual.

    Modifica el dict IN-PLACE y lo devuelve.
    """
    if not isinstance(camera_dict, dict):
        return camera_dict

    # Campos a nivel cámara
    _procesar_campo(camera_dict, 'password', descifrar_campo)
    _procesar_urls_en_lista(camera_dict.get('streams', []), descifrar_campo)

    for key in ('selected_main_profile', 'selected_sub_profile'):
        _procesar_perfil(camera_dict.get(key), descifrar_campo)
    
    # ⭐ SOPORTE MULTI-LENTE: descifrar URLs dentro de lentes
    _procesar_lentes(camera_dict.get('lentes', []), descifrar_campo)

    return camera_dict


# ============================================================
# PRUEBA RÁPIDA
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("FIELD CRYPTO - TEST")
    print("=" * 60)

    # Test 1: cifrar/descifrar un string
    original = "rtsp://admin:1234567890@192.168.100.165:10554/tcp/av0_0"
    cifrado = cifrar_campo(original)
    descifrado = descifrar_campo(cifrado)

    print(f"\nTexto original: {original}")
    print(f"Cifrado:        {cifrado[:60]}...")
    print(f"Descifrado:     {descifrado}")
    print(f"¿Coincide?      {'✅' if descifrado == original else '❌'}")

    # Test 2: cifrar un objeto cámara completo CON lentes
    camara = {
        "camera_index": "cam01",
        "ip": "192.168.100.165",
        "username": "admin",
        "password": "1234567890",
        "streams": [
            {"url": "rtsp://admin:1234567890@192.168.100.165:10554/tcp/av0_0"},
            {"url": "rtsp://admin:1234567890@192.168.100.165:10554/tcp/av0_1"}
        ],
        "selected_main_profile": {
            "url": "rtsp://admin:1234567890@192.168.100.165:10554/tcp/av0_0"
        },
        "selected_sub_profile": {
            "url": "rtsp://admin:1234567890@192.168.100.165:10554/tcp/av0_1"
        },
        # ⭐ SOPORTE MULTI-LENTE
        "lentes": [
            {
                "lens_id": 1,
                "source_token": "vs_1",
                "main_stream": {
                    "url": "rtsp://admin:1234567890@192.168.100.165:10554/tcp/av0_0"
                },
                "sub_stream": {
                    "url": "rtsp://admin:1234567890@192.168.100.165:10554/tcp/av0_1"
                },
                "main_profile": {
                    "url": "rtsp://admin:1234567890@192.168.100.165:10554/tcp/av0_0"
                },
                "sub_profile": {
                    "url": "rtsp://admin:1234567890@192.168.100.165:10554/tcp/av0_1"
                },
                "streams": [
                    {"url": "rtsp://admin:1234567890@192.168.100.165:10554/tcp/av0_0"},
                    {"url": "rtsp://admin:1234567890@192.168.100.165:10554/tcp/av0_1"}
                ]
            },
            {
                "lens_id": 2,
                "source_token": "vs_2",
                "main_stream": {
                    "url": "rtsp://admin:1234567890@192.168.100.165:10554/tcp/av1_0"
                },
                "sub_stream": {
                    "url": "rtsp://admin:1234567890@192.168.100.165:10554/tcp/av1_1"
                },
                "main_profile": {
                    "url": "rtsp://admin:1234567890@192.168.100.165:10554/tcp/av1_0"
                },
                "sub_profile": {
                    "url": "rtsp://admin:1234567890@192.168.100.165:10554/tcp/av1_1"
                },
                "streams": [
                    {"url": "rtsp://admin:1234567890@192.168.100.165:10554/tcp/av1_0"},
                    {"url": "rtsp://admin:1234567890@192.168.100.165:10554/tcp/av1_1"}
                ]
            }
        ],
        "personas": {"activo": False}
    }

    import json
    print("\n--- ANTES ---")
    print(json.dumps(camara, indent=2, ensure_ascii=False))

    cifrar_campos_sensibles(camara)
    print("\n--- CIFRADO ---")
    print(json.dumps(camara, indent=2, ensure_ascii=False))

    descifrar_campos_sensibles(camara)
    print("\n--- DESCIFRADO ---")
    print(json.dumps(camara, indent=2, ensure_ascii=False))

    print("\n" + "=" * 60)
    print("TEST COMPLETO")
    print("=" * 60)