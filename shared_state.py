# shared_state.py
# Estado compartido entre routers + config manager global.
#
# ⭐ MULTI-LENTE (2025):
# - broadcast_detecciones y broadcast_alarma aceptan worker_id y lo mapean
#   a source_token. El cliente enruta por source_token.
# - _generate_rtsp_paths() SIEMPRE usa camXX_lensN_*.
# - notify_detections acepta worker_id.
#
# ⭐ LAZY INIT (2025):
# - LOCAL_IP, auth_manager y config_manager YA NO se crean al importar.
#   Se crean la primera vez que alguien los usa.

import asyncio
import socket
import sys
import json
import time
import re
from typing import Dict, Set, Optional

if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# ============================================================
# REFERENCIA AL SISTEMA PRINCIPAL
# ============================================================
_visioncore = None


def set_visioncore(instance):
    global _visioncore
    _visioncore = instance


def get_visioncore():
    return _visioncore


# ============================================================
# LOOP PRINCIPAL DE ASYNCIO
# ============================================================
_MAIN_LOOP: Optional[asyncio.AbstractEventLoop] = None


def set_main_loop(loop):
    global _MAIN_LOOP
    _MAIN_LOOP = loop


def get_main_loop():
    return _MAIN_LOOP


# ============================================================
# IP LOCAL (lazy)
# ============================================================
_LOCAL_IP_CACHE = None


def _detect_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_local_ip():
    global _LOCAL_IP_CACHE
    if _LOCAL_IP_CACHE is None:
        _LOCAL_IP_CACHE = _detect_local_ip()
        print(f"[SharedState] IP detectada: {_LOCAL_IP_CACHE}")
    return _LOCAL_IP_CACHE


def __getattr__(name):
    if name == "LOCAL_IP":
        return get_local_ip()
    raise AttributeError(f"module 'shared_state' has no attribute '{name}'")


# ============================================================
# AUTH MANAGER (lazy singleton, via proxy)
# ============================================================
class _LazyAuthManager:
    def __init__(self):
        self._real = None

    def _get_real(self):
        if self._real is None:
            from auth import AuthManager
            self._real = AuthManager()
        return self._real

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return getattr(self._get_real(), name)

    def __bool__(self):
        return True

    def __repr__(self):
        estado = "inicializado" if self._real is not None else "lazy"
        return f"<LazyAuthManager ({estado})>"


# ============================================================
# CONFIG MANAGER (lazy singleton, via proxy)
# ============================================================
class _LazyConfigManager:
    def __init__(self):
        self._real = None

    def _get_real(self):
        if self._real is None:
            from config_manager import get_config_manager
            self._real = get_config_manager()
        return self._real

    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return getattr(self._get_real(), name)

    def __bool__(self):
        return True

    def __repr__(self):
        estado = "inicializado" if self._real is not None else "lazy"
        return f"<LazyConfigManager ({estado})>"


auth_manager = _LazyAuthManager()
config_manager = _LazyConfigManager()


# ============================================================
# ESTADO EN MEMORIA
# ============================================================
active_sessions: Dict[str, dict] = {}
camera_subscribers: Dict[str, Set[str]] = {}
detection_tasks: Dict[str, asyncio.Task] = {}

SESSION_TIMEOUT = 120
HEARTBEAT_INTERVAL = 10


# ============================================================
# HELPERS DE IDs DE CÁMARA / WORKER
# ============================================================
def _format_camera_id(cam):
    cam_id = getattr(cam, 'camera_index', '')
    if isinstance(cam_id, str) and cam_id.startswith('cam'):
        try:
            num = int(cam_id[3:])
            return f"cam{num:02d}"
        except Exception:
            return cam_id
    return str(cam_id)


def _get_stream_number(camera_id):
    if isinstance(camera_id, str):
        if camera_id.startswith('cam'):
            try:
                return int(camera_id[3:])
            except Exception:
                return 1
        try:
            return int(camera_id)
        except Exception:
            pass
    return 1


def _worker_id_a_source_token(worker_id: str, camera_index: str = None) -> str:
    """
    ⭐ MULTI-LENTE: mapea worker_id (cam01_lens2) a source_token real.
    Busca en la config de la cámara el lente correspondiente.
    Si no lo encuentra, devuelve el worker_id tal cual.
    """
    if not worker_id:
        return None

    # Si ya es un source_token (no tiene formato camXX_lensN), devolverlo
    if not re.match(r'^cam\d+_lens\d+$', worker_id):
        return worker_id

    # Parsear worker_id: cam01_lens2
    match = re.match(r'^(cam\d+)_lens(\d+)$', worker_id)
    if not match:
        return worker_id

    cam_id = match.group(1)
    lens_id = int(match.group(2))

    vc = get_visioncore()
    if not vc:
        return worker_id

    try:
        configs = vc.config_manager.load_camera_config()
        for cam_config in configs:
            if cam_config.get('camera_index') != cam_id:
                continue
            lentes = cam_config.get('lentes', [])
            for lente in lentes:
                if lente.get('lens_id') == lens_id:
                    return lente.get('source_token') or worker_id
            break
    except Exception as e:
        print(f"[SharedState] Error mapeando worker_id a source_token: {e}")

    return worker_id


# ⭐ MULTI-LENTE: _generate_rtsp_paths SIEMPRE usa camXX_lensN_*
def _generate_rtsp_paths(camera_id):
    stream_num = _get_stream_number(camera_id)
    base_url = "rtsp://127.0.0.1:8554"

    # ⭐ SIEMPRE lensN
    paths = {
        'main': f"{base_url}/cam{stream_num:02d}_lens1_main",
        'sub':  f"{base_url}/cam{stream_num:02d}_lens1_sub",
        'extras': []
    }

    vc = get_visioncore()
    if not vc or not hasattr(vc, 'config_manager'):
        return paths

    try:
        configs = vc.config_manager.load_camera_config()
        for cam_config in configs:
            if cam_config.get('camera_index') != camera_id:
                continue

            lentes = cam_config.get('lentes', [])
            if not isinstance(lentes, list) or len(lentes) == 0:
                # Sin lentes: lente genérico con paths lens1
                paths['main'] = f"{base_url}/cam{stream_num:02d}_lens1_main"
                paths['sub']  = f"{base_url}/cam{stream_num:02d}_lens1_sub"
                break

            ia_lens_id = cam_config.get('ia_lens_id', 1)
            lente_ia = None
            for l in lentes:
                if l.get('lens_id') == ia_lens_id:
                    lente_ia = l
                    break
            if lente_ia is None:
                lente_ia = lentes[0]
                ia_lens_id = lente_ia.get('lens_id', 1)

            # Main/sub del lente IA
            paths['main'] = f"{base_url}/cam{stream_num:02d}_lens{ia_lens_id}_main"
            paths['sub']  = f"{base_url}/cam{stream_num:02d}_lens{ia_lens_id}_sub"

            # FIX: exponer main/sub de TODOS los lentes
            for lente in lentes:
                lens_id = lente.get('lens_id', 1)
                if lens_id == ia_lens_id:
                    continue
                paths['extras'].append({
                    'lens_id': lens_id,
                    'name': f'lens{lens_id}_main',
                    'url': f"{base_url}/cam{stream_num:02d}_lens{lens_id}_main"
                })
                paths['extras'].append({
                    'lens_id': lens_id,
                    'name': f'lens{lens_id}_sub',
                    'url': f"{base_url}/cam{stream_num:02d}_lens{lens_id}_sub"
                })

            # Extras adicionales
            for lente in lentes:
                lens_id = lente.get('lens_id', 1)
                all_streams = lente.get('streams', [])
                for stream in all_streams:
                    stream_url = stream.get('url', '')
                    if not stream_url:
                        continue
                    if (lente is lente_ia and
                        (stream_url == lente_ia.get('main_stream', {}).get('url') or
                         stream_url == lente_ia.get('sub_stream', {}).get('url'))):
                        continue

                    profile = stream.get('profile', {})
                    profile_name = profile.get('name', 'extra')
                    clean_name = profile_name.replace(' ', '_').lower()
                    clean_name = re.sub(r'[^a-z0-9_]', '', clean_name)
                    if len(clean_name) > 20:
                        clean_name = clean_name[:20]
                    if not clean_name:
                        clean_name = 'extra'

                    extra_path = f"{base_url}/cam{stream_num:02d}_lens{lens_id}_{clean_name}"
                    paths['extras'].append({
                        'lens_id': lens_id,
                        'name': clean_name,
                        'url': extra_path
                    })
            break
    except Exception as e:
        print(f"[SharedState] Error en _generate_rtsp_paths: {e}")

    return paths


# ============================================================
# BROADCAST
# ============================================================
async def broadcast_camera_event(event_type: str, camera_id: str, camera_data: dict = None):
    print(f"[WS] broadcast_camera_event: {event_type} para {camera_id}")
    message = json.dumps({
        "type": event_type,
        "camera_id": camera_id,
        "camera": camera_data,
        "timestamp": time.time()
    })
    dead_sessions = []
    for sid, session in list(active_sessions.items()):
        try:
            await session["websocket"].send_text(message)
        except Exception:
            dead_sessions.append(sid)
    for sid in dead_sessions:
        _cleanup_session(sid)


async def broadcast_zonas_update(camera_id: str, zonas: list):
    print(f"[WS] broadcast_zonas_update: {camera_id} - {len(zonas)} zonas")
    message = json.dumps({
        "type": "zonas_update",
        "camera_id": camera_id,
        "zonas": zonas,
        "timestamp": time.time()
    })
    if camera_id in camera_subscribers:
        dead_sessions = []
        for session_id in list(camera_subscribers[camera_id]):
            if session_id in active_sessions:
                try:
                    await active_sessions[session_id]["websocket"].send_text(message)
                except Exception:
                    dead_sessions.append(session_id)
        for sid in dead_sessions:
            if sid in camera_subscribers.get(camera_id, set()):
                camera_subscribers[camera_id].discard(sid)


async def broadcast_alarma(camera_id: str, tipo_alarma: str, comando: str = "start",
                            worker_id: str = None):
    """
    ⭐ MULTI-LENTE: acepta worker_id y lo mapea a source_token.
    El cliente enruta por source_token.
    """
    source_token = _worker_id_a_source_token(worker_id, camera_id) if worker_id else None

    print(f"[WS] broadcast_alarma: {camera_id} - {tipo_alarma} - {comando} "
          f"(worker_id={worker_id}, source_token={source_token})")

    message = json.dumps({
        "type": "alarma",
        "camera_id": camera_id,
        "worker_id": worker_id,
        "source_token": source_token,
        "tipo_alarma": tipo_alarma,
        "comando": comando,
        "timestamp": time.time()
    })
    if camera_id in camera_subscribers:
        dead_sessions = []
        for session_id in list(camera_subscribers[camera_id]):
            if session_id in active_sessions:
                try:
                    await active_sessions[session_id]["websocket"].send_text(message)
                except Exception:
                    dead_sessions.append(session_id)
        for sid in dead_sessions:
            if sid in camera_subscribers.get(camera_id, set()):
                camera_subscribers[camera_id].discard(sid)


async def broadcast_detecciones(camera_id: str, detections: list, zonas: list,
                                 frame_size=None, worker_id: str = None):
    """
    ⭐ MULTI-LENTE: acepta worker_id y lo mapea a source_token.
    """
    source_token = _worker_id_a_source_token(worker_id, camera_id) if worker_id else None

    mensaje_dict = {
        "type": "detections",
        "camera_id": camera_id,
        "worker_id": worker_id,
        "source_token": source_token,
        "detections": detections,
        "zonas": zonas,
        "timestamp": time.time()
    }
    if frame_size is not None:
        mensaje_dict["frame_size"] = list(frame_size)
    message = json.dumps(mensaje_dict)

    if camera_id in camera_subscribers:
        dead_sessions = []
        for session_id in list(camera_subscribers[camera_id]):
            if session_id in active_sessions:
                try:
                    await active_sessions[session_id]["websocket"].send_text(message)
                except Exception:
                    dead_sessions.append(session_id)
        for sid in dead_sessions:
            if sid in camera_subscribers.get(camera_id, set()):
                camera_subscribers[camera_id].discard(sid)


def notify_detections(camera_id: str, detections: list, zonas: list,
                       frame_size=None, worker_id: str = None):
    """
    Notifica detecciones desde un thread (no async).
    Acepta worker_id o camera_id (compatibilidad).
    """
    loop = get_main_loop()
    if loop is None or not loop.is_running():
        return
    try:
        asyncio.run_coroutine_threadsafe(
            broadcast_detecciones(camera_id, detections, zonas, frame_size, worker_id),
            loop
        )
    except Exception as e:
        print(f"[SharedState] Error en notify_detections: {e}")


def enviar_alarma(camera_id: str, tipo_alarma: str, comando: str = "start",
                   worker_id: str = None):
    print(f"[WebAPI] Enviando alarma: {camera_id} - {tipo_alarma} - {comando} "
          f"(worker_id={worker_id})")
    loop = get_main_loop()
    if loop is None or not loop.is_running():
        print(f"[WebAPI] _MAIN_LOOP no disponible, alarma no enviada")
        return
    try:
        asyncio.run_coroutine_threadsafe(
            broadcast_alarma(camera_id, tipo_alarma, comando, worker_id),
            loop
        )
    except Exception as e:
        print(f"[WebAPI] Error en enviar_alarma: {e}")


def enviar_stop_alarma(camera_id: str, tipo_alarma: str, worker_id: str = None):
    print(f"[WebAPI] Enviando STOP ALARMA: {camera_id} (worker_id={worker_id})")
    enviar_alarma(camera_id, tipo_alarma, "stop", worker_id)


# ============================================================
# LIMPIEZA DE SESIONES
# ============================================================
def _cleanup_session(session_id: str):
    if session_id in active_sessions:
        for cam_id in active_sessions[session_id]["subscriptions"]:
            if cam_id in camera_subscribers:
                camera_subscribers[cam_id].discard(session_id)
                if len(camera_subscribers.get(cam_id, set())) == 0:
                    if cam_id in detection_tasks:
                        detection_tasks[cam_id].cancel()
                        del detection_tasks[cam_id]
                        print(f"[Monitor] Detenido para {cam_id}")
        del active_sessions[session_id]
        print(f"[WS] Sesion {session_id[:8]}... eliminada")