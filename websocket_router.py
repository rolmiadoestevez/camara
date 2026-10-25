# routers/websocket_router.py
# Endpoint: /ws/control (y /ws/{camera_id} legacy)
#
# ⭐ MULTI-LENTE (2025):
# - monitor_camera_detections() recibe una LISTA de workers (1 por lente).
# - Itera sobre cada worker y manda worker_id a broadcast_detecciones.
# - El handler subscribe pasa la lista completa de workers de la cámara.
# - broadcast_alarma ahora acepta worker_id.
#
# ⭐ FIX SYNC AL CONECTAR (2025):
#   - Después del sync_state, se reenvían al cliente las alarmas activas.
#   - Nuevo handler 'request_alarm_state'.
#
# ⭐ FIX LENTES (2025):
#   - request_config ahora incluye 'lentes[]' completo.
#
# ⭐ FIX ZONAS EN CALIENTE (2025):
#   - _handle_config_update y _handle_save_config usan
#     zone_service.actualizar_zonas() (merge) en vez de cargar_zonas()
#     (replace). Preserva tracking, timers y contadores.
#   - Se fuerza detection_worker cuando filtro ∈ {zona, global}.
#   - Se guarda filtro_anterior ANTES de reasignar personas_config
#     para que _detener_todas() del cambio de filtro sí corra.
#   - Helper _extraer_detecciones_de_cam() para la re-evaluación.
#
# ⚠️ NO SE TOCA:
#   - _procesar_global() en zone_service.py
#   - _estado_alarmas en ZoneService
#   - cam.personas_config['global']
#   El flujo global queda intacto.

import asyncio
import json
import time
import uuid
from datetime import datetime

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from shared_state import (
    auth_manager, get_visioncore,
    active_sessions, camera_subscribers, detection_tasks,
    SESSION_TIMEOUT, HEARTBEAT_INTERVAL,
    _format_camera_id, _generate_rtsp_paths,
    broadcast_camera_event, broadcast_zonas_update, broadcast_detecciones,
    broadcast_alarma,
    _cleanup_session,
    config_manager,
)


router = APIRouter()


try:
    from zone_service import get_zone_service
    ZONE_SERVICE_AVAILABLE = True
except ImportError:
    ZONE_SERVICE_AVAILABLE = False
# FIX HIDRATAR PERFILES ONVIF (2026): para leer perfiles en vivo.
try:
    from scanner.profiles import get_media_profiles_detailed
    SCANNER_PROFILES_AVAILABLE = True
except ImportError:
    try:
        from scanner import get_media_profiles_detailed
        SCANNER_PROFILES_AVAILABLE = True
    except ImportError:
        SCANNER_PROFILES_AVAILABLE = False
        print("[WebSocket] Scanner profiles no disponible - resoluciones NO se hidratan en vivo")


# ============================================================
# HELPER: EXTRAER DETECCIONES DE cam.last_detection_results
# ⭐ FIX 2025: unifica el bloque que antes estaba duplicado inline.
# Convierte el formato ultralytics (results.boxes) a la lista
# [{xyxy, conf, cls, track_id}] que espera zone_service.
# ============================================================
def _extraer_detecciones_de_cam(cam):
    detecciones = []
    try:
        if not hasattr(cam, 'last_detection_results') or not cam.last_detection_results:
            return detecciones
        results = cam.last_detection_results
        if not hasattr(results, 'boxes') or not results.boxes:
            return detecciones
        for box in results.boxes:
            try:
                if hasattr(box, 'xyxy') and box.xyxy:
                    det = {
                        'xyxy': [float(v) for v in box.xyxy[0]],
                        'conf': float(box.conf[0]) if box.conf else 0.5,
                        'cls': int(box.cls[0]) if box.cls else 0
                    }
                    if (hasattr(box, 'track_id') and box.track_id
                            and box.track_id[0] is not None):
                        det['track_id'] = int(box.track_id[0])
                    detecciones.append(det)
            except Exception:
                continue
    except Exception as e:
        print(f"[Helper] Error extrayendo detecciones: {e}")
    return detecciones


# ============================================================
# HELPER: FORZAR detection_worker SI EL FILTRO LO PIDE
# ⭐ FIX 2025: sin esto, si el toggle "Detección de personas"
# está apagado, el worker nunca arranca y last_detection_results
# queda en None. Los filtros 'zona' y 'global' IMPLICAN detección.
# ============================================================
_PERFILES_CACHE = {}          # camera_index -> (timestamp, resultado)
_PERFILES_TTL_S = 20.0        # no repetir la consulta ONVIF a la misma camara en este lapso
_PERFILES_TIMEOUT_S = 6.0     # tope total de espera para el cliente


async def _hidratar_perfiles_onvif_async(cam_config):
    """
    Version NO bloqueante de _hidratar_perfiles_onvif. La original hacia consultas ONVIF de red
    (timeout 5 s, hasta 2 por camara) directamente en el event loop: con una camara lenta u offline,
    se congelaban TODOS los websockets (heartbeats, video, alarmas) de todos los clientes; en el log
    se piden 3 configs a la vez al agregar camaras. Ahora corre en un hilo, con tope de espera y una
    cache corta. Si no responde a tiempo, devuelve los perfiles guardados (sin dato en vivo).
    """
    import asyncio as _asyncio
    import time as _time
    clave = cam_config.get('camera_index')
    cache = _PERFILES_CACHE.get(clave)
    if cache and _time.time() - cache[0] < _PERFILES_TTL_S:
        return cache[1]
    try:
        loop = _asyncio.get_running_loop()
        resultado = await _asyncio.wait_for(
            loop.run_in_executor(None, _hidratar_perfiles_onvif, cam_config),
            timeout=_PERFILES_TIMEOUT_S)
    except Exception as e:
        print(f"[WS] Perfiles ONVIF de {clave} no disponibles en vivo ({type(e).__name__}); uso los guardados")
        sel_third = cam_config.get('selected_third_profile') or None
        resultado = {
            'selected_main_profile': cam_config.get('selected_main_profile') or {},
            'selected_sub_profile': cam_config.get('selected_sub_profile') or {},
            'selected_third_profile': sel_third,
            'profiles_live': [],
        }
    _PERFILES_CACHE[clave] = (_time.time(), resultado)
    return resultado


def _hidratar_perfiles_onvif(cam_config):
    """
    FIX HIDRATAR PERFILES ONVIF (2026): lee los perfiles del dispositivo EN VIVO
    via ONVIF y completa los 'selected_*_profile' con resolution/codec/fps/bitrate
    ACTUALES. Sin esto, el cliente mostraba la resolucion guardada en la DB
    (que puede estar desactualizada si el dispositivo cambio algo).
    Devuelve un dict con:
      - 'selected_main_profile':  dict completo (con resolution actual)
      - 'selected_sub_profile':   dict completo
      - 'selected_third_profile': dict completo o None
      - 'profiles_live':          lista completa de perfiles leidos del ONVIF
    Si el scanner no esta disponible o la consulta falla, devuelve los
    selected_* con solo el token (sin resolution) para que el cliente
    sepa que no hay dato vivo.
    """
    resultado = {
        'selected_main_profile': None,
        'selected_sub_profile': None,
        'selected_third_profile': None,
        'profiles_live': [],
    }
    # Tomar los tokens guardados
    sel_main = cam_config.get('selected_main_profile') or {}
    sel_sub = cam_config.get('selected_sub_profile') or {}
    sel_third = cam_config.get('selected_third_profile') or {}
    tok_main = sel_main.get('token')
    tok_sub = sel_sub.get('token')
    tok_third = sel_third.get('token') if isinstance(sel_third, dict) else None
    if not SCANNER_PROFILES_AVAILABLE:
        resultado['selected_main_profile'] = sel_main
        resultado['selected_sub_profile'] = sel_sub
        resultado['selected_third_profile'] = sel_third if sel_third else None
        return resultado
    # Armar URL de servicio ONVIF
    ip = cam_config.get('ip', '')
    port = cam_config.get('port', 80)
    username = cam_config.get('username', '')
    password = cam_config.get('password', '')
    media_type = cam_config.get('media_type', 'media1')
    if not ip:
        resultado['selected_main_profile'] = sel_main
        resultado['selected_sub_profile'] = sel_sub
        resultado['selected_third_profile'] = sel_third if sel_third else None
        return resultado
    services = cam_config.get('services', {}) or {}
    media_service_url = services.get('media') or services.get('media2')
    if not media_service_url:
        media_service_url = f'http://{ip}:{port}/onvif/media_service'
    service_url = cam_config.get('service_url', '')
    if not service_url:
        service_url = f'http://{ip}:{port}/onvif/device_service'
    try:
        perfiles = get_media_profiles_detailed(
            service_url, username, password,
            media_service_url=media_service_url,
            media_type=media_type,
        )
        resultado['profiles_live'] = perfiles
    except Exception as e:
        print(f"[WS] Error hidratando perfiles ONVIF para {cam_config.get('camera_index')}: {e}")
        perfiles = []
    def _buscar_perfil(tok):
        if not tok:
            return None
        for p in perfiles:
            if p.get('token') == tok:
                # Normalizar campos al formato que espera el cliente
                res = p.get('resolution', '')
                w = p.get('width')
                h = p.get('height')
                if not res and w and h:
                    res = f"{w}x{h}"
                return {
                    'token': tok,
                    'name': p.get('name', ''),
                    'resolution': res or '',
                    'codec': p.get('encoding', ''),
                    'fps': p.get('fps', ''),
                    'bitrate': p.get('bitrate', ''),
                    'url': p.get('url', ''),
                }
        # No se encontró en vivo → devolver solo el token (sin resolución vieja)
        return {'token': tok, 'name': '', 'resolution': '', 'codec': '', 'fps': '', 'bitrate': '', 'url': ''}
    resultado['selected_main_profile'] = _buscar_perfil(tok_main) or sel_main
    resultado['selected_sub_profile'] = _buscar_perfil(tok_sub) or sel_sub
    resultado['selected_third_profile'] = _buscar_perfil(tok_third) if tok_third else None
    return resultado
def _forzar_detection_worker(cam, filtro_activo):
    # FIX REGLA PERSONAS OFF (2026): si personas_activo=False, NADA funciona.
    # Esta funcion antes forzaba detection_enabled=True cuando el filtro era
    # 'zona' o 'global'. Eso hacia que, con el toggle de personas apagado,
    # se activaran detecciones, aparecieran boxes en el dashboard y se
    # pudieran disparar alarmas de zona/global. Ahora solo AVISA si hay
    # una configuracion contradictoria (filtro activo + personas off), pero
    # NO activa nada.
    if filtro_activo not in ('zona', 'global'):
        return
    if not getattr(cam, 'personas_activo', False):
        print(f"[FIX] Filtro={filtro_activo} pero personas_activo=False → NO se detecta")


# ============================================================
# MONITOR DE DETECCIONES (MULTI-LENTE)
# ============================================================
async def monitor_camera_detections(camera_id: str, cams: list):
    """
    Monitorea las detecciones de TODOS los lentes de una cámara.

    ⭐ MULTI-LENTE: recibe una lista de CameraWorker (uno por lente).
    Itera sobre cada worker y manda worker_id al broadcast.
    """
    last_detections_hash = {}   # {worker_id: hash}
    alarma_activa_anterior = {}  # {worker_id: bool}
    frame_counter = 0
    frame_skip = 2

    for cam in cams:
        wid = getattr(cam, 'worker_id', getattr(cam, 'camera_index', 'unknown'))
        last_detections_hash[wid] = None
        alarma_activa_anterior[wid] = False

    while True:
        try:
            frame_counter += 1
            if frame_counter % frame_skip != 0:
                await asyncio.sleep(0.05)
                continue

            for cam in cams:
                try:
                    wid = getattr(cam, 'worker_id', getattr(cam, 'camera_index', 'unknown'))

                    detection_active = (getattr(cam, 'detection_enabled', False)
                                        or getattr(cam, 'personas_activo', False))

                    results = getattr(cam, 'last_detection_results', None)
                    detections = []

                    if results and hasattr(results, 'boxes') and results.boxes:
                        for box in results.boxes:
                            try:
                                if hasattr(box, 'xyxy') and box.xyxy:
                                    det = {
                                        'xyxy': [float(v) for v in box.xyxy[0]],
                                        'conf': float(box.conf[0]) if box.conf else 0.5,
                                        'cls': int(box.cls[0]) if box.cls else 0
                                    }
                                    if (hasattr(box, 'track_id') and box.track_id
                                            and box.track_id[0] is not None):
                                        det['track_id'] = int(box.track_id[0])
                                    detections.append(det)
                            except Exception:
                                continue

                    personas_config = getattr(cam, 'personas_config', {})
                    filtro_activo = personas_config.get('filtro')
                    zonas = personas_config.get('zonas', [])

                    if filtro_activo is None:
                        if alarma_activa_anterior.get(wid, False):
                            await broadcast_alarma(camera_id, "visual", "stop", worker_id=wid)
                            await broadcast_alarma(camera_id, "sonora_local", "stop", worker_id=wid)
                            await broadcast_alarma(camera_id, "dispositivo", "stop", worker_id=wid)
                            print(f"[Monitor] Alarmas DETENIDAS para {wid} (sin filtro)")
                            alarma_activa_anterior[wid] = False
                        continue

                    if filtro_activo == 'global':
                        current_hash = hash(str(detections))
                        if current_hash != last_detections_hash.get(wid):
                            last_detections_hash[wid] = current_hash
                            await broadcast_detecciones(
                                camera_id, detections, [],
                                frame_size=(cam.frame_width, cam.frame_height),
                                worker_id=wid
                            )
                        continue

                    if filtro_activo == 'zona':
                        if not zonas:
                            if alarma_activa_anterior.get(wid, False):
                                print(f"[Monitor] Alarmas DETENIDAS para {wid} (sin zonas)")
                                alarma_activa_anterior[wid] = False
                            continue

                        current_hash = hash(str(detections))
                        if current_hash != last_detections_hash.get(wid):
                            last_detections_hash[wid] = current_hash
                            await broadcast_detecciones(
                                camera_id, detections, getattr(cam, 'zonas_personas', []),
                                frame_size=(cam.frame_width, cam.frame_height),
                                worker_id=wid
                            )
                        continue

                    else:
                        if alarma_activa_anterior.get(wid, False):
                            await broadcast_alarma(camera_id, "visual", "stop", worker_id=wid)
                            await broadcast_alarma(camera_id, "sonora_local", "stop", worker_id=wid)
                            await broadcast_alarma(camera_id, "dispositivo", "stop", worker_id=wid)
                            print(f"[Monitor] Alarmas DETENIDAS para {wid} (filtro desconocido: {filtro_activo})")
                            alarma_activa_anterior[wid] = False

                except Exception as e:
                    print(f"[Monitor] Error procesando worker: {e}")
                    continue

            await asyncio.sleep(0.05)

        except asyncio.CancelledError:
            print(f"[Monitor] Monitoreo cancelado para {camera_id}")
            for cam in cams:
                wid = getattr(cam, 'worker_id', getattr(cam, 'camera_index', 'unknown'))
                await broadcast_alarma(camera_id, "visual", "stop", worker_id=wid)
                await broadcast_alarma(camera_id, "sonora_local", "stop", worker_id=wid)
                await broadcast_alarma(camera_id, "dispositivo", "stop", worker_id=wid)
            break
        except Exception as e:
            print(f"[Monitor] Error monitoreando {camera_id}: {e}")
            import traceback
            traceback.print_exc()
            await asyncio.sleep(1)


# ============================================================
# WEBSOCKET PRINCIPAL
# ============================================================
@router.websocket("/ws/control")
async def websocket_control_endpoint(websocket: WebSocket):
    await websocket.accept()

    token = websocket.query_params.get("token")
    if not token:
        print(f"[WS] Token no proporcionado")
        await websocket.close(code=4001)
        return

    payload = auth_manager.verify_token(token)
    if not payload:
        print(f"[WS] Token invalido")
        await websocket.close(code=4001)
        return

    session_id = websocket.query_params.get("session_id")

    if session_id and session_id in active_sessions:
        print(f"[WS] Cliente reconectando con sesion {session_id[:8]}...")
        active_sessions[session_id]["websocket"] = websocket
        active_sessions[session_id]["last_heartbeat"] = time.time()
    else:
        session_id = str(uuid.uuid4())
        print(f"[WS] Nueva sesion creada: {session_id[:8]}...")
        active_sessions[session_id] = {
            "websocket": websocket,
            "subscriptions": set(),
            "last_heartbeat": time.time(),
            "user": payload.get("username", "unknown")
        }

    await websocket.send_text(json.dumps({
        "type": "session",
        "session_id": session_id,
        "heartbeat_interval": HEARTBEAT_INTERVAL
    }))

    vc = get_visioncore()
    cameras_list = []
    if vc:
        for cam in vc.all_cameras:
            # ⭐ MULTI-LENTE: 1 entrada por worker_id (lente)
            worker_id = getattr(cam, 'worker_id', None) or _format_camera_id(cam)
            camera_index = getattr(cam, 'camera_index', '')
            lens_id = getattr(cam, 'lens_id', 1)
            source_token = getattr(cam, 'source_token', None)
            zonas = getattr(cam, 'zonas_personas', [])
            rtsp_paths = _generate_rtsp_paths(camera_index)
            camera_info = {
                "id": worker_id,                        # ⭐ cam01_lens1
                "camera_index": camera_index,           # ⭐ cam01
                "lens_id": lens_id,
                "source_token": source_token,
                "name": getattr(cam, 'name', 'Camara'),
                "status": "online" if getattr(cam, 'connected', False) else "offline",
                "ip": getattr(cam, 'camera_ip', ''),
                "zonas": zonas,
                "rtsp_paths": rtsp_paths
            }
            cameras_list.append(camera_info)

    await websocket.send_text(json.dumps({
        "type": "sync_state",
        "cameras": cameras_list,
        "timestamp": time.time()
    }))
    print(f"[WS] Estado sincronizado: {len(cameras_list)} camaras con zonas y paths RTSP")

    if ZONE_SERVICE_AVAILABLE and vc:
        try:
            zone_service = get_zone_service()
            for cam in vc.all_cameras:
                camera_id = _format_camera_id(cam)
                wid = getattr(cam, 'worker_id', None)
                for tipo_alarma in ("visual", "sonora_local", "dispositivo"):
                    if zone_service.esta_alarma_activa(wid, tipo_alarma):
                        await websocket.send_text(json.dumps({
                            "type": "alarma",
                            "camera_id": camera_id,
                            "worker_id": wid,
                            "tipo_alarma": tipo_alarma,
                            "comando": "start",
                            "timestamp": time.time()
                        }))
                        print(f"[WS] Estado alarma reenviado: {camera_id} - {tipo_alarma} - start (wid={wid})")
        except Exception as e:
            print(f"[WS] Error reenviando estado de alarmas: {e}")
            import traceback
            traceback.print_exc()

    async def send_heartbeats():
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                if session_id in active_sessions:
                    await websocket.send_text(json.dumps({"type": "heartbeat"}))
                    print(f"[WS] Heartbeat enviado a {session_id[:8]}...")
            except Exception:
                break

    heartbeat_task = asyncio.create_task(send_heartbeats())

    try:
        while True:
            try:
                message = await asyncio.wait_for(
                    websocket.receive_text(),
                    timeout=HEARTBEAT_INTERVAL + 10
                )
                data = json.loads(message)
                msg_type = data.get('type')
                active_sessions[session_id]["last_heartbeat"] = time.time()

                if msg_type == 'ping':
                    await websocket.send_text(json.dumps({"type": "pong"}))

                elif msg_type == 'subscribe':
                    camera_id = data.get('camera_id')
                    if camera_id:
                        cams_de_camara = []
                        if vc:
                            cams_de_camara = [
                                c for c in vc.all_cameras
                                if _format_camera_id(c) == camera_id
                            ]
                        if not cams_de_camara:
                            print(f"[WS] subscribe: cámara {camera_id} no existe, ignorando")
                            await websocket.send_text(json.dumps({
                                "type": "error",
                                "message": f"Camara {camera_id} no existe"
                            }))
                            continue

                        active_sessions[session_id]["subscriptions"].add(camera_id)
                        if camera_id not in camera_subscribers:
                            camera_subscribers[camera_id] = set()
                        camera_subscribers[camera_id].add(session_id)
                        # FIX: reenviar alarmas activas al suscribirse
                        if ZONE_SERVICE_AVAILABLE and vc:
                            try:
                                zone_service = get_zone_service()
                                for cam in vc.all_cameras:
                                    if _format_camera_id(cam) != camera_id:
                                        continue
                                    wid = getattr(cam, 'worker_id', None)
                                    for tipo_alarma in ("visual", "sonora_local", "dispositivo"):
                                        if zone_service.esta_alarma_activa(wid, tipo_alarma):
                                            await websocket.send_text(json.dumps({
                                                "type": "alarma",
                                                "camera_id": camera_id,
                                                "worker_id": wid,
                                                "tipo_alarma": tipo_alarma,
                                                "comando": "start",
                                                "timestamp": time.time()
                                            }))
                                            print(f"[WS] Alarma reenviada en subscribe: {camera_id} - {tipo_alarma}")
                            except Exception as e:
                                print(f"[WS] Error reenviando alarmas en subscribe: {e}")

                        if vc and camera_id not in detection_tasks:
                            if cams_de_camara:
                                detection_tasks[camera_id] = asyncio.create_task(
                                    monitor_camera_detections(camera_id, cams_de_camara)
                                )
                                print(f"[Monitor] Iniciado para {camera_id} "
                                      f"({len(cams_de_camara)} lentes)")

                        rtsp_paths = _generate_rtsp_paths(camera_id)
                        await websocket.send_text(json.dumps({
                            "type": "rtsp_paths",
                            "camera_id": camera_id,
                            "rtsp_paths": rtsp_paths
                        }))
                        await websocket.send_text(json.dumps({
                            "type": "subscribed",
                            "camera_id": camera_id
                        }))
                        print(f"[WS] Sesion {session_id[:8]} suscrita a {camera_id}")

                elif msg_type == 'unsubscribe':
                    camera_id = data.get('camera_id')
                    if camera_id:
                        active_sessions[session_id]["subscriptions"].discard(camera_id)
                        if camera_id in camera_subscribers:
                            camera_subscribers[camera_id].discard(session_id)
                            if len(camera_subscribers.get(camera_id, set())) == 0:
                                if camera_id in detection_tasks:
                                    detection_tasks[camera_id].cancel()
                                    del detection_tasks[camera_id]
                                    print(f"[Monitor] Detenido para {camera_id}")
                        print(f"[WS] Sesion {session_id[:8]} desuscrita de {camera_id}")

                elif msg_type == 'get_cameras':
                    cameras_list = []
                    if vc:
                        for cam in vc.all_cameras:
                            camera_id = _format_camera_id(cam)
                            cameras_list.append({
                                "id": camera_id,
                                "name": getattr(cam, 'name', 'Camara'),
                                "status": "online" if getattr(cam, 'connected', False) else "offline",
                                "rtsp_paths": _generate_rtsp_paths(camera_id)
                            })
                    await websocket.send_text(json.dumps({
                        "type": "cameras_list",
                        "cameras": cameras_list
                    }))

                elif msg_type == 'request_config':
                    camera_id = data.get('camera_id')
                    worker_id = data.get('worker_id')
                    print(f"[WebSocket] request_config para {camera_id} (worker_id={worker_id})")

                    lens_id = None
                    if worker_id:
                        import re as _re
                        _m = _re.match(r'^cam\d+_lens(\d+)$', worker_id)
                        if _m:
                            lens_id = int(_m.group(1))

                    try:
                        configs = vc.config_manager.load_camera_config() if vc else []

                        for cam_config in configs:
                            if cam_config.get('camera_index') == camera_id:
                                rtsp_paths = _generate_rtsp_paths(camera_id)
                                personas_por_lente = cam_config.get('personas_por_lente', {})
                                personas_default = cam_config.get('personas', {})
                                if lens_id is not None and str(lens_id) in personas_por_lente:
                                    personas = personas_por_lente[str(lens_id)]
                                    print(f"[WebSocket] {camera_id}: usando config del lente {lens_id}")
                                else:
                                    personas = personas_default
                                    print(f"[WebSocket] {camera_id}: usando config default (personas)")
                                _filtro_guardado = personas.get('filtro', None)


                                # FIX HIDRATAR PERFILES ONVIF (2026): leer perfiles EN VIVO antes de
                                # armar la respuesta, para que el cliente vea resoluciones actuales.
                                _perfiles_live = await _hidratar_perfiles_onvif_async(cam_config)

                                config = {
                                    'activo': personas.get('activo', False),
                                    'confianza': personas.get('confianza', 60),
                                    'tamano_minimo': personas.get('tamano_minimo', 10),
                                    'filtro': _filtro_guardado,
                                    'filtro_activo': _filtro_guardado,
                                    'deteccion_global': _filtro_guardado == 'global',
                                    'solo_zona': _filtro_guardado == 'zona',
                                    'tracking_persistente': personas.get('tracking_persistente', False),
                                    'retencion_id': personas.get('retencion_id', 2),
                                    'global': personas.get('global', {
                                        'visual':      {'activa': False},
                                        'sonora':      {'activa': False, 'modo': 'continuo'},
                                        'dispositivo': {'activa': False, 'modo': '10s'},
                                        'notificacion': False,
                                        'grabar': False
                                    }),
                                    'zonas_personas': personas.get('zonas', []),
                                    'rtsp_paths': rtsp_paths,
                                    'scanner_data': {
                                        'camera_name': cam_config.get('camera_name', ''),
                                        'ip': cam_config.get('ip', ''),
                                        'device_info': cam_config.get('device_info', {}),
                                        'analytics': cam_config.get('analytics', {}),
                                        'streams': cam_config.get('streams', []),
                                        'profiles': cam_config.get('profiles', []),
                                        'lentes': cam_config.get('lentes', []),
                                        'selected_main_profile': _perfiles_live['selected_main_profile'],
                                        'selected_sub_profile': _perfiles_live['selected_sub_profile'],
                                        'selected_third_profile': _perfiles_live['selected_third_profile'],
                                        'ia_lens_id': cam_config.get('ia_lens_id', 1),
                                        'ia_profile_token': cam_config.get('ia_profile_token'),
                                        'profile_support': {
                                            'profile_s': True,
                                            'profile_t': cam_config.get('profile_t_support', False),
                                            'profile_m': cam_config.get('profile_m_support', False)
                                        }
                                    }
                                }

                                await websocket.send_text(json.dumps({
                                    "type": "config_data",
                                    "camera_id": camera_id,
                                    "worker_id": worker_id,
                                    "config": config
                                }))
                                print(f"[WebSocket] Configuracion enviada para {camera_id} (incluye lentes[])")
                                break
                        else:
                            await websocket.send_text(json.dumps({
                                "type": "config_data",
                                "camera_id": camera_id,
                                "config": {}
                            }))
                    except Exception as e:
                        print(f"[WebSocket] Error: {e}")
                        await websocket.send_text(json.dumps({
                            "type": "config_data",
                            "camera_id": camera_id,
                            "config": {}
                        }))

                elif msg_type == 'config_update':
                    await _handle_config_update(websocket, data)
                elif msg_type == 'get_config':
                    await _handle_get_config(websocket, data)
                elif msg_type == 'save_config':
                    await _handle_save_config(websocket, data)
                elif msg_type == 'global_config_update':
                    await _handle_global_config_update(websocket, data)
                elif msg_type == 'request_global_config':
                    await _handle_request_global_config(websocket, data)
                elif msg_type == 'delete_camera':
                    await _handle_delete_camera(websocket, data)

                elif msg_type == 'request_alarm_state':
                    camera_id = data.get('camera_id')
                    print(f"[WS] request_alarm_state para {camera_id}")
                    if ZONE_SERVICE_AVAILABLE and vc:
                        try:
                            zone_service = get_zone_service()
                            for cam in vc.all_cameras:
                                if _format_camera_id(cam) != camera_id:
                                    continue
                                wid = getattr(cam, 'worker_id', None)
                                for tipo_alarma in ("visual", "sonora_local", "dispositivo"):
                                    activa = zone_service.esta_alarma_activa(wid, tipo_alarma)
                                    await websocket.send_text(json.dumps({
                                        "type": "alarma",
                                        "camera_id": camera_id,
                                        "worker_id": wid,
                                        "tipo_alarma": tipo_alarma,
                                        "comando": "start" if activa else "stop",
                                        "timestamp": time.time()
                                    }))
                                    print(f"[WS] Estado alarma a request: {camera_id} - {tipo_alarma} - {'start' if activa else 'stop'} (wid={wid})")
                        except Exception as e:
                            print(f"[WS] Error en request_alarm_state: {e}")

            except asyncio.TimeoutError:
                continue
            except json.JSONDecodeError:
                continue

    except WebSocketDisconnect:
        print(f"[WS] Cliente desconectado: {session_id[:8]}...")
    except Exception as e:
        print(f"[WS] Error en WebSocket {session_id[:8]}...: {e}")
    finally:
        heartbeat_task.cancel()

        async def delayed_cleanup():
            await asyncio.sleep(SESSION_TIMEOUT)
            if session_id in active_sessions:
                _cleanup_session(session_id)

        asyncio.create_task(delayed_cleanup())


# ============================================================
# HANDLERS
# ============================================================
async def _handle_config_update(websocket, data):
    from shared_state import broadcast_detecciones, broadcast_zonas_update
    camera_id = data.get('camera_id')
    worker_id = data.get('worker_id')
    config = data.get('config', {})
    # FIX PROTOCOLO (2026): el cliente manda los campos con sufijo '_personas'
    # (confianza_personas, tamano_minimo_personas, solo_zona_personas,
    # tracking_persistente_personas, retencion_id_personas) pero este handler
    # los lee SIN sufijo. Sin esta traduccion, esos campos se ignoraban
    # silenciosamente y habia que pulsar Aceptar 2 veces.
    for _k_cli, _k_srv in (
        ('activo', 'personas_activo'),
        ('confianza_personas', 'confianza'),
        ('tamano_minimo_personas', 'tamano_minimo'),
        ('solo_zona_personas', 'solo_zona'),
        ('tracking_persistente_personas', 'tracking_persistente'),
        ('retencion_id_personas', 'retencion_id'),
    ):
        if _k_cli in config and _k_srv not in config:
            config[_k_srv] = config[_k_cli]
    lens_id = None
    if worker_id:
        import re as _re
        _m = _re.match(r'^cam\d+_lens(\d+)$', worker_id)
        if _m:
            lens_id = int(_m.group(1))
    print(f"[WebSocket] Config update para {camera_id} (worker_id={worker_id}, lens_id={lens_id})")
    print(f"[WebSocket] Config keys: {list(config.keys())}")

    vc = get_visioncore()
    if vc:
        for cam in vc.all_cameras:
            if _format_camera_id(cam) != camera_id:
                continue
            if worker_id and getattr(cam, 'worker_id', None) != worker_id:
                continue
            if True:
                if 'personas_activo' in config:
                    cam.personas_activo = config['personas_activo']
                    cam.detection_enabled = config['personas_activo']
                    if cam.detection_engine:
                        cam.detection_engine.set_personas_activo_por_camara(
                            cam.worker_id, config['personas_activo']
                        )
                    if not config['personas_activo']:
                        cam.last_detection_results = None
                        if cam.detection_engine:
                            with cam.detection_engine.results_lock:
                                cam.detection_engine.results_dict.pop(cam.worker_id, None)
                        await broadcast_detecciones(camera_id, [], [],
                                                      worker_id=cam.worker_id)
                    print(f"personas_activo = {config['personas_activo']}")

                if 'confianza' in config:
                    cam.confianza_personas = config['confianza']
                    if cam.detection_engine:
                        cam.detection_engine.set_confianza_personas(cam.worker_id, config['confianza'])

                if 'tamano_minimo' in config:
                    cam.tamano_minimo_personas = config['tamano_minimo']
                    if cam.detection_engine:
                        cam.detection_engine.set_tamaño_minimo_personas(cam.worker_id, config['tamano_minimo'])

                if 'deteccion_global' in config:
                    cam.deteccion_global = config['deteccion_global']
                    if cam.detection_engine:
                        cam.detection_engine.set_deteccion_global(cam.worker_id, config['deteccion_global'])

                # ⭐ FIX 2025: leer también 'solo_zona_personas' (así lo manda el cliente)
                if 'solo_zona' in config or 'solo_zona_personas' in config:
                    solo_zona_val = config.get('solo_zona', config.get('solo_zona_personas', False))
                    cam.solo_zona_personas = solo_zona_val
                    if cam.detection_engine:
                        cam.detection_engine.set_solo_zona_personas(cam.worker_id, solo_zona_val)

                if 'tracking_persistente' in config:
                    cam.tracking_persistente_personas = config['tracking_persistente']
                    if cam.detection_engine:
                        cam.detection_engine.set_tracking_persistente_personas(
                            cam.worker_id, config['tracking_persistente']
                        )

                if 'retencion_id' in config:
                    cam.retencion_id_personas = config['retencion_id']
                    if cam.detection_engine:
                        cam.detection_engine.set_retencion_id_personas(cam.worker_id, config['retencion_id'])

                zonas_procesadas = _procesar_zonas(config.get('zonas_personas', []))
                print(f"[SERVER-RECIBE] config_update {camera_id}: {len(zonas_procesadas)} zonas")
                for z in zonas_procesadas:
                    print(f"[SERVER-RECIBE]   '{z.get('nombre')}' id={z.get('id')} vertices={z.get('vertices')}")
                if 'zonas_personas' in config:
                    zonas_previas = getattr(cam, 'zonas_personas', None)
                    cam.zonas_personas = zonas_procesadas
                    if zonas_procesadas != zonas_previas:
                        await broadcast_zonas_update(camera_id, zonas_procesadas)

                filtro_activo = None
                if config.get('deteccion_global', False):
                    filtro_activo = 'global'
                elif config.get('solo_zona', False) or config.get('solo_zona_personas', False):
                    filtro_activo = 'zona'

                global_norm = config.get('global', {
                    'visual':      {'activa': False},
                    'sonora':      {'activa': False, 'modo': 'continuo'},
                    'dispositivo': {'activa': False, 'modo': '10s'},
                    'notificacion': False,
                    'grabar': False
                })

                if not hasattr(cam, 'personas_config'):
                    cam.personas_config = {}

                # ⭐ FIX 2025: guardar filtro_anterior ANTES de reasignar
                filtro_anterior = cam.personas_config.get('filtro') if isinstance(cam.personas_config, dict) else None

                cam.personas_config = {
                    'activo': config.get('personas_activo', cam.personas_activo),
                    'confianza': config.get('confianza', cam.confianza_personas),
                    'tamano_minimo': config.get('tamano_minimo', cam.tamano_minimo_personas),
                    'filtro': filtro_activo,
                    'tracking_persistente': config.get('tracking_persistente', cam.tracking_persistente_personas),
                    'retencion_id': config.get('retencion_id', cam.retencion_id_personas),
                    'global': global_norm,
                    'zonas': zonas_procesadas if zonas_procesadas else cam.zonas_personas
                }

                try:
                    if hasattr(cam, 'actualizar_configuracion'):
                        cam.actualizar_configuracion({
                            'global': global_norm,
                            'deteccion_global': config.get('deteccion_global', False),
                            'solo_zona_personas': config.get('solo_zona', config.get('solo_zona_personas', False))
                        })
                except Exception as _e:
                    print(f"[WebSocket] ⚠️ Error aplicando global al worker: {_e}")

                # ⭐ FIX REGLA PERSONAS OFF (2026): si personas_activo=False,
                # el filtro (zona/global) NO se aplica. No se detecta, no se
                # procesa zona, no se dispara alarma.
                if not getattr(cam, 'personas_activo', False):
                    if ZONE_SERVICE_AVAILABLE:
                        try:
                            zone_service = get_zone_service()
                            zone_service._detener_todas(cam.worker_id, "personas off")
                        except Exception:
                            pass
                    filtro_activo = None
                    cam.deteccion_global = False
                    cam.solo_zona_personas = False
                    print(f"[FIX] personas_activo=False → filtro desactivado para {cam.worker_id}")

                # FIX FILTRO: detectar cambio y detener alarmas del filtro anterior
                if ZONE_SERVICE_AVAILABLE:
                    try:
                        zone_service = get_zone_service()
                        if filtro_anterior != filtro_activo:
                            print(f"[WebSocket] CAMBIO DE FILTRO: {filtro_anterior} -> {filtro_activo}")
                            zone_service._detener_todas(cam.worker_id, f"cambio filtro {filtro_anterior} -> {filtro_activo}")
                            if filtro_anterior == 'zona' and filtro_activo != 'zona':
                                if cam.worker_id in zone_service.zona_managers:
                                    try:
                                        zone_service.zona_managers[cam.worker_id].limpiar()
                                    except Exception:
                                        pass
                    except Exception as _e_filtro:
                        print(f"[WebSocket] Error en cambio de filtro: {_e_filtro}")

                # ⭐ FIX 2025: usar actualizar_zonas (merge) en vez de cargar_zonas (replace)
                if ZONE_SERVICE_AVAILABLE:
                    try:
                        zone_service = get_zone_service()
                        zone_service.actualizar_personas_config(cam.worker_id, cam.personas_config)
                        zonas_para_deteccion = zonas_procesadas if zonas_procesadas else cam.zonas_personas
                        zone_service.actualizar_zonas('personas', cam.worker_id, zonas_para_deteccion)

                        # FIX RE-EVALUACION: forzar chequeo con detecciones actuales
                        try:
                            detecciones_actuales = _extraer_detecciones_de_cam(cam)
                            frame_shape = (cam.frame_height, cam.frame_width)
                            print(f"[WebSocket] Re-evaluando alarmas para {cam.worker_id}: {len(detecciones_actuales)} detecciones, filtro={filtro_activo}")
                            zone_service.verificar_y_alertar(
                                tipo='personas',
                                worker_id=cam.worker_id,
                                detecciones=detecciones_actuales,
                                frame_shape=frame_shape,
                                camera_name=cam.name,
                                solo_zona=(filtro_activo == 'zona'),
                                deteccion_global=(filtro_activo == 'global')
                            )
                        except Exception as _e_re:
                            print(f"[WebSocket] Error re-evaluando: {_e_re}")
                    except Exception as _e:
                        print(f"[WebSocket] Error ZoneService: {_e}")

                def _guardar_config_update_json(_camera_id=camera_id, _cam=cam, _filtro=filtro_activo, _worker_id=worker_id, _lens_id=lens_id):
                    try:
                        cam_config_json = None
                        all_configs = vc.config_manager.load_camera_config()
                        for cfg in all_configs:
                            if cfg.get('camera_index') == _camera_id:
                                cam_config_json = cfg
                                break

                        if cam_config_json:
                            personas_data = {
                                'activo': _cam.personas_activo,
                                'confianza': _cam.confianza_personas,
                                'tamano_minimo': _cam.tamano_minimo_personas,
                                'filtro': _filtro,
                                'tracking_persistente': _cam.tracking_persistente_personas,
                                'retencion_id': _cam.retencion_id_personas,
                                'global': _cam.personas_config.get('global', {}),
                                'zonas': _cam.zonas_personas
                            }
                            if _lens_id is not None:
                                if 'personas_por_lente' not in cam_config_json:
                                    cam_config_json['personas_por_lente'] = {}
                                cam_config_json['personas_por_lente'][str(_lens_id)] = personas_data
                                print(f"[SERVER-GUARDA] lente {_lens_id} de {_camera_id}")
                                for z in personas_data.get('zonas', []):
                                    print(f"[SERVER-GUARDA]   '{z.get('nombre')}' id={z.get('id')} vertices={z.get('vertices')}")
                                print(f"[WebSocket] Config guardada para lente {_lens_id} de {_camera_id}")
                            else:
                                cam_config_json['personas'] = personas_data
                            cam_config_json['ultima_actualizacion'] = datetime.now().isoformat()
                            vc.config_manager.actualizar_camara_completa(cam_config_json)
                    except Exception as e:
                        print(f"[WebSocket] Error guardando JSON: {e}")

                _enqueue_save(_guardar_config_update_json)

                _filtro_ant = getattr(cam, '_filtro_anterior', None)
                if _filtro_ant is not None and filtro_activo is None:
                    if ZONE_SERVICE_AVAILABLE:
                        try:
                            _zs = get_zone_service()
                            _zs._detener_todas(camera_id, "filtros desactivados")
                        except Exception as _e:
                            print(f"[WebSocket] Error deteniendo alarmas: {_e}")
                cam._filtro_anterior = filtro_activo

                await websocket.send_text(json.dumps({
                    "type": "config_ack",
                    "camera_id": camera_id,
                    "status": "ok",
                    "timestamp": time.time()
                }))
                print(f"[WebSocket] Config ACK enviado para {camera_id}")
                break
        else:
            await websocket.send_text(json.dumps({
                "type": "config_ack",
                "camera_id": camera_id,
                "status": "error",
                "message": "Camara no encontrada"
            }))


def _procesar_zonas(zonas_raw):
    zonas_procesadas = []
    for zona in zonas_raw:
        if 'config_especifica' not in zona:
            zona['config_especifica'] = {}

        tipo_deteccion = zona.get('tipo_deteccion', 'entrada_area')

        if tipo_deteccion == 'entrada_area':
            zona['config_especifica'].setdefault('tiempo_min', 0.5)
            zona['config_especifica'].setdefault('tolerancia_entrada', 3)
            zona['config_especifica'].setdefault('grabar', True)
            zona['config_especifica'].setdefault('notificacion', False)
            zona['config_especifica'].setdefault('visual', {'activa': False})
            zona['config_especifica'].setdefault('sonora', {'activa': False, 'modo': 'continuo'})
            zona['config_especifica'].setdefault('dispositivo', {'activa': False, 'modo': '10s'})

        elif tipo_deteccion == 'cruce_linea':
            zona['config_especifica'].setdefault('tolerancia_distancia', 3)
            zona['config_especifica'].setdefault('cooldown_objeto', 1.0)
            zona['config_especifica'].setdefault('tiempo_linea', 1.0)
            zona['config_especifica'].setdefault('grabar', True)
            zona['config_especifica'].setdefault('notificacion', False)
            zona['config_especifica'].setdefault('visual', {'activa': False})
            zona['config_especifica'].setdefault('sonora', {'activa': False, 'modo': 'continuo'})
            zona['config_especifica'].setdefault('dispositivo', {'activa': False, 'modo': '10s'})

        elif tipo_deteccion == 'ocupacion':
            zona['config_especifica'].setdefault('umbral_alto', 15)
            zona['config_especifica'].setdefault('grabar', True)
            zona['config_especifica'].setdefault('notificacion', False)
            zona['config_especifica'].setdefault('visual', {'activa': False})
            zona['config_especifica'].setdefault('sonora', {'activa': False, 'modo': 'continuo'})
            zona['config_especifica'].setdefault('dispositivo', {'activa': False, 'modo': '10s'})
            zona.setdefault('segmentos_ocupacion', [])

        if 'conteo_activo' not in zona:
            zona['conteo_activo'] = False
        if 'config_conteo' not in zona:
            zona['config_conteo'] = {}

        config_conteo = zona['config_conteo']
        config_conteo.setdefault('periodo_modo', 'tiempo_real')
        config_conteo.setdefault('periodo_intervalo', 15)
        config_conteo.setdefault('reinicio_modo', 'manual')
        config_conteo.setdefault('reinicio_cada', 1)
        config_conteo.setdefault('umbral_advertencia', 30)
        config_conteo.setdefault('umbral_critico', 50)
        config_conteo.setdefault('direccion', 'ambas')

        zonas_procesadas.append(zona)

    return zonas_procesadas


def _enqueue_save(fn):
    try:
        from routers.cameras_router import _get_camera_ops_executor
        _get_camera_ops_executor().submit(fn)
    except Exception as e:
        print(f"[WebSocket] Error encolando guardado: {e}")


async def _handle_get_config(websocket, data):
    camera_id = data.get('camera_id')
    print(f"[WebSocket] Solicitud de configuracion para {camera_id}")

    vc = get_visioncore()
    if not vc:
        await websocket.send_text(json.dumps({
            "type": "config_data", "camera_id": camera_id, "config": {}
        }))
        return

    for cam in vc.all_cameras:
        if _format_camera_id(cam) == camera_id:
            config_data = vc.config_manager.load_camera_config()
            for cam_json in config_data:
                if cam_json.get('camera_index') == camera_id:
                    rtsp_paths = _generate_rtsp_paths(camera_id)
                    personas = cam_json.get('personas', {})
                    config = {
                        'activo': personas.get('activo', False),
                        'confianza': personas.get('confianza', 60),
                        'tamano_minimo': personas.get('tamano_minimo', 10),
                        'filtro': personas.get('filtro', None),
                        'tracking_persistente': personas.get('tracking_persistente', False),
                        'retencion_id': personas.get('retencion_id', 2),
                        'global': personas.get('global', {
                            'visual':      {'activa': False},
                            'sonora':      {'activa': False, 'modo': 'continuo'},
                            'dispositivo': {'activa': False, 'modo': '10s'},
                            'notificacion': False,
                            'grabar': False
                        }),
                        'zonas_personas': personas.get('zonas', []),
                        'rtsp_paths': rtsp_paths
                    }
                    await websocket.send_text(json.dumps({
                        "type": "config_data", "camera_id": camera_id, "config": config
                    }))
                    return

            config = {
                'activo': getattr(cam, 'personas_activo', False),
                'confianza': getattr(cam, 'confianza_personas', 60),
                'tamano_minimo': getattr(cam, 'tamano_minimo_personas', 10),
                'filtro': None,
                'tracking_persistente': getattr(cam, 'tracking_persistente_personas', False),
                'retencion_id': getattr(cam, 'retencion_id_personas', 2),
                'global': {
                    'visual':      {'activa': False},
                    'sonora':      {'activa': False, 'modo': 'continuo'},
                    'dispositivo': {'activa': False, 'modo': '10s'},
                    'notificacion': False,
                    'grabar': False
                },
                'zonas_personas': getattr(cam, 'zonas_personas', []),
                'rtsp_paths': _generate_rtsp_paths(camera_id)
            }
            await websocket.send_text(json.dumps({
                "type": "config_data", "camera_id": camera_id, "config": config
            }))
            break
    else:
        await websocket.send_text(json.dumps({
            "type": "config_data", "camera_id": camera_id, "config": {}
        }))


async def _handle_save_config(websocket, data):
    camera_id = data.get('camera_id')
    worker_id = data.get('worker_id')
    config = data.get('config', {})

    # FIX PROTOCOLO (2026): el cliente manda los campos con sufijo '_personas'
    # (confianza_personas, tamano_minimo_personas, solo_zona_personas,
    # tracking_persistente_personas, retencion_id_personas) pero este handler
    # los lee SIN sufijo. Sin esta traduccion, esos campos se ignoraban
    # silenciosamente y habia que pulsar Aceptar 2 veces.
    for _k_cli, _k_srv in (
        ('confianza_personas', 'confianza'),
        ('tamano_minimo_personas', 'tamano_minimo'),
        ('solo_zona_personas', 'solo_zona'),
        ('tracking_persistente_personas', 'tracking_persistente'),
        ('retencion_id_personas', 'retencion_id'),
    ):
        if _k_cli in config and _k_srv not in config:
            config[_k_srv] = config[_k_cli]
    lens_id = None
    if worker_id:
        import re as _re
        _m = _re.match(r'^cam\d+_lens(\d+)$', worker_id)
        if _m:
            lens_id = int(_m.group(1))
    print(f"[WebSocket] save_config para {camera_id} (worker_id={worker_id}, lens_id={lens_id})")

    vc = get_visioncore()
    if not vc:
        return

    for cam in vc.all_cameras:
        if _format_camera_id(cam) != camera_id:
            continue
        if worker_id and getattr(cam, 'worker_id', None) != worker_id:
            continue
        if True:
            camera_name = getattr(cam, 'name', '')
            camera_ip = getattr(cam, 'camera_ip', '')
            camera_index = getattr(cam, 'camera_index', '')

            filtro_activo = None
            if config.get('deteccion_global', False):
                filtro_activo = 'global'
            elif config.get('solo_zona', False) or config.get('solo_zona_personas', False):
                filtro_activo = 'zona'

            zonas_procesadas = _procesar_zonas(config.get('zonas_personas', []))

            global_norm = config.get('global', {
                'visual':      {'activa': False},
                'sonora':      {'activa': False, 'modo': 'continuo'},
                'dispositivo': {'activa': False, 'modo': '10s'},
                'notificacion': False,
                'grabar': False
            })

            personas_data = {
                'activo': config.get('activo', False),
                'confianza': config.get('confianza', 60),
                'tamano_minimo': config.get('tamano_minimo', 10),
                'filtro': filtro_activo,
                'tracking_persistente': config.get('tracking_persistente', False),
                'retencion_id': config.get('retencion_id', 2),
                'global': global_norm,
                'zonas': zonas_procesadas
            }

            def _guardar_save_config_json(_personas=personas_data, _camera_index=camera_index, _camera_id=camera_id, _lens_id=lens_id):
                try:
                    # Config FRESCA al ejecutarse (antes: snapshot tomado al recibir el mensaje, que se
                    # escribia despues sobre el JSON completo y podia pisar cambios de otros lentes/guardados)
                    _cam_config = None
                    for _cfg in vc.config_manager.load_camera_config():
                        if _cfg.get('camera_index') == _camera_index:
                            _cam_config = _cfg
                            break
                    if not _cam_config:
                        print(f"[WebSocket] ⚠️ save_config: camara {_camera_index} no encontrada; NO se guardo")
                        return
                    if _lens_id is not None:
                        if 'personas_por_lente' not in _cam_config:
                            _cam_config['personas_por_lente'] = {}
                        _cam_config['personas_por_lente'][str(_lens_id)] = _personas
                        print(f"[WebSocket] Config guardada para lente {_lens_id} de {_camera_id}")
                    else:
                        _cam_config['personas'] = _personas
                    _cam_config['ultima_actualizacion'] = datetime.now().isoformat()
                    vc.config_manager.actualizar_camara_completa(_cam_config)
                    print(f"[WebSocket] Personas guardadas en JSON para {_camera_id}")
                except Exception as e:
                    print(f"[WebSocket] Error guardando JSON: {e}")

            _enqueue_save(_guardar_save_config_json)

            cam.personas_activo = config.get('activo', False)
            cam.detection_enabled = config.get('activo', False)
            cam.confianza_personas = config.get('confianza', 60)
            cam.tamano_minimo_personas = config.get('tamano_minimo', 10)
            cam.retencion_id_personas = config.get('retencion_id', 2)
            cam.tracking_persistente_personas = config.get('tracking_persistente', False)
            cam.deteccion_global = config.get('deteccion_global', False)
            cam.solo_zona_personas = config.get('solo_zona', config.get('solo_zona_personas', False))
            cam.zonas_personas = zonas_procesadas

            if not hasattr(cam, 'personas_config'):
                cam.personas_config = {}

            # ⭐ FIX 2025: guardar filtro_anterior ANTES de reasignar
            filtro_anterior = cam.personas_config.get('filtro') if isinstance(cam.personas_config, dict) else None

            cam.personas_config = personas_data

            if cam.detection_engine:
                cam.detection_engine.set_config_personas_completa(
                    cam.worker_id,
                    personas_activo=cam.personas_activo,
                    confianza_personas=cam.confianza_personas,
                    tamano_minimo_personas=cam.tamano_minimo_personas,
                    deteccion_global=cam.deteccion_global,
                    solo_zona_personas=cam.solo_zona_personas,
                    tracking_persistente_personas=cam.tracking_persistente_personas,
                    retencion_id_personas=cam.retencion_id_personas
                )

            # ⭐ FIX 2025: forzar detection_worker si el filtro lo pide
            _forzar_detection_worker(cam, filtro_activo)

            # ⭐ FIX 2025: usar actualizar_zonas (merge) en vez de cargar_zonas (replace)
            if ZONE_SERVICE_AVAILABLE:
                try:
                    zone_service = get_zone_service()
                    zone_service.actualizar_personas_config(cam.worker_id, personas_data)
                    zone_service.actualizar_zonas('personas', cam.worker_id, zonas_procesadas)

                    # FIX RE-EVALUACION: forzar chequeo con detecciones actuales
                    try:
                        detecciones_actuales = _extraer_detecciones_de_cam(cam)
                        frame_shape = (cam.frame_height, cam.frame_width)
                        zone_service.verificar_y_alertar(
                            tipo='personas',
                            worker_id=cam.worker_id,
                            detecciones=detecciones_actuales,
                            frame_shape=frame_shape,
                            camera_name=cam.name,
                            solo_zona=(filtro_activo == 'zona'),
                            deteccion_global=(filtro_activo == 'global')
                        )
                    except Exception as _e_re:
                        print(f"[WebSocket] Error re-evaluando (save): {_e_re}")
                except Exception as _e:
                    print(f"[WebSocket] Error ZoneService save_config: {_e}")

            try:
                if hasattr(cam, 'actualizar_configuracion'):
                    cam.actualizar_configuracion({
                        'global': global_norm,
                        'deteccion_global': config.get('deteccion_global', False),
                        'solo_zona_personas': config.get('solo_zona', config.get('solo_zona_personas', False))
                    })
            except Exception as _e:
                print(f"[WebSocket] ⚠️ Error aplicando global al worker (save_config): {_e}")

            rtsp_paths = _generate_rtsp_paths(camera_id)
            config_response = {
                'activo': cam.personas_activo,
                'confianza': cam.confianza_personas,
                'tamano_minimo': cam.tamano_minimo_personas,
                'filtro': filtro_activo,
                'filtro_activo': filtro_activo,
                'deteccion_global': filtro_activo == 'global',
                'solo_zona': filtro_activo == 'zona',
                'tracking_persistente': cam.tracking_persistente_personas,
                'retencion_id': cam.retencion_id_personas,
                'global': cam.personas_config.get('global', {}),
                'zonas_personas': cam.zonas_personas,
                'rtsp_paths': rtsp_paths
            }

            # ⭐ FIX 2025: comparar contra el filtro_anterior guardado
            if filtro_anterior != filtro_activo:
                if ZONE_SERVICE_AVAILABLE:
                    try:
                        _zs = get_zone_service()
                        _zs._detener_todas(cam.worker_id, f"cambio filtro {filtro_anterior} -> {filtro_activo}")
                    except Exception as _e:
                        print(f"[WebSocket] Error deteniendo alarmas: {_e}")
            cam._filtro_anterior = filtro_activo

            if camera_id in camera_subscribers:
                broadcast_message = json.dumps({
                    "type": "config_data",
                    "camera_id": camera_id,
                    "config": config_response
                })
                dead_sessions = []
                for session_id in list(camera_subscribers[camera_id]):
                    if session_id in active_sessions:
                        try:
                            await active_sessions[session_id]["websocket"].send_text(broadcast_message)
                        except Exception:
                            dead_sessions.append(session_id)
                for sid in dead_sessions:
                    camera_subscribers[camera_id].discard(sid)

            await websocket.send_text(json.dumps({
                "type": "save_ack",
                "camera_id": camera_id,
                "status": "ok"
            }))
            print(f"[WebSocket] Configuracion guardada para {camera_id}")
            break


async def _handle_global_config_update(websocket, data):
    section = data.get('section')
    config = data.get('config', {})
    print(f"[WebSocket] Config global update: {section}")

    auth_manager.save_global_config(section, config)

    broadcast_message = json.dumps({
        "type": "global_config_updated",
        "section": section,
        "config": config,
        "timestamp": time.time()
    })

    dead_sessions = []
    for sid, session in list(active_sessions.items()):
        try:
            await session["websocket"].send_text(broadcast_message)
        except Exception:
            dead_sessions.append(sid)

    for sid in dead_sessions:
        _cleanup_session(sid)

    await websocket.send_text(json.dumps({
        "type": "global_config_ack",
        "section": section,
        "status": "ok"
    }))


async def _handle_request_global_config(websocket, data):
    section = data.get('section')
    print(f"[WebSocket] Request global config: {section}")

    config = auth_manager.get_global_config(section)
    await websocket.send_text(json.dumps({
        "type": "global_config_updated",
        "section": section,
        "config": config if config else {}
    }))


async def _handle_delete_camera(websocket, data):
    camera_id = data.get('camera_id')
    print(f"[WebSocket] Solicitud de eliminacion: {camera_id}")

    vc = get_visioncore()
    if not vc:
        return

    camera_to_delete = None
    for cam in vc.all_cameras:
        if _format_camera_id(cam) == camera_id:
            camera_to_delete = cam
            break

    if not camera_to_delete:
        await websocket.send_text(json.dumps({
            "type": "delete_ack",
            "camera_id": camera_id,
            "status": "error",
            "message": "Camara no encontrada"
        }))
        return

    try:
        camera_index = getattr(camera_to_delete, 'camera_index', '')
        camera_name = getattr(camera_to_delete, 'name', '')
        print(f"[WebSocket] Eliminando: {camera_name} ({camera_index})")

        from shared_state import detection_tasks
        if camera_id in detection_tasks:
            detection_tasks[camera_id].cancel()
            del detection_tasks[camera_id]
            print(f"[WebSocket] detection_task cancelada para {camera_id}")


        # FIX HERENCIA ALARMAS (2026): limpiar ZoneService ANTES de borrar el worker.
        # Sin esto, _estado_alarmas[worker_id] quedaba con visual/sonora/dispositivo
        # en True. Si luego se re-escaneaba la misma camara (mismo worker_id), el
        # ZoneService encontraba el estado viejo y reenviaba las alarmas como si
        # siguieran activas (heredadas).
        if ZONE_SERVICE_AVAILABLE:
            try:
                zone_service = get_zone_service()
                wid = getattr(camera_to_delete, 'worker_id', None)
                if wid:
                    zone_service._detener_todas(wid, "camara eliminada")
                    zone_service.limpiar(wid)
                    print(f"[WebSocket] ZoneService limpiado para {wid}")
            except Exception as _e_zs:
                print(f"[WebSocket] Error limpiando ZoneService: {_e_zs}")
        camera_to_delete.stop()
        vc.all_cameras.remove(camera_to_delete)

        try:
            configs = vc.config_manager.load_camera_config()
            nuevas_configs = []
            for cfg in configs:
                if str(cfg.get('camera_index', '')) != str(camera_index):
                    nuevas_configs.append(cfg)

            if len(nuevas_configs) != len(configs):
                vc.config_manager.save_camera_config_completo(nuevas_configs)
                print(f"[WebSocket] JSON actualizado (cifrado)")
            vc.cameras_config = nuevas_configs
        except Exception as e:
            print(f"[WebSocket] Error actualizando JSON cifrado: {e}")

        if hasattr(vc, 'eliminar_path_mediamtx_api'):
            vc.eliminar_path_mediamtx_api(camera_id)

        await broadcast_camera_event("camera_deleted", camera_id, None)

        await websocket.send_text(json.dumps({
            "type": "delete_ack",
            "camera_id": camera_id,
            "status": "ok"
        }))
        print(f"[WebSocket] Camara {camera_id} eliminada")
    except Exception as e:
        print(f"[WebSocket] Error eliminando: {e}")
        await websocket.send_text(json.dumps({
            "type": "delete_ack",
            "camera_id": camera_id,
            "status": "error",
            "message": str(e)
        }))


# ============================================================
# WEBSOCKET LEGACY /ws/{camera_id}
# ============================================================
active_ws_connections = {}


@router.websocket("/ws/{camera_id}")
async def websocket_endpoint(websocket: WebSocket, camera_id: str):
    await websocket.accept()
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001)
        return

    payload = auth_manager.verify_token(token)
    if not payload:
        await websocket.close(code=4001)
        return

    if camera_id not in active_ws_connections:
        active_ws_connections[camera_id] = []

    active_ws_connections[camera_id].append(websocket)
    print(f"[WS] Cliente legacy conectado: {camera_id}")

    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        if camera_id in active_ws_connections:
            if websocket in active_ws_connections[camera_id]:
                active_ws_connections[camera_id].remove(websocket)
            if len(active_ws_connections[camera_id]) == 0:
                del active_ws_connections[camera_id]
        print(f"[WS] Cliente legacy desconectado: {camera_id}")