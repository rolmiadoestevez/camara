# FIX MULTI-LENTE FINAL
# visioncore_system.py - VERSION CORREGIDA Y CONSISTENTE
#
# ⭐ MULTI-LENTE (2025):
# - _get_prefijo_lente() SIEMPRE usa camXX_lensN. Sin ramas.
# - _crear_camera_workers_individuales() crea N workers (1 por lente).
# - _aplicar_personas_a_worker() usa worker_id como clave del motor IA.
# - Los paths de MediaMTX siempre son camXX_lensN_*.
#
# ⭐ SIN RECLASIFICACION (2025):
# - El scanner YA clasifica main, sub, third, others.
# - VisionCore usa DIRECTAMENTE main_stream, sub_stream, third_stream.

import threading
import time
import json
import os
import subprocess
import traceback
import socket
import yaml
import atexit
import re
import sys
import shutil
from datetime import datetime
from camera_worker import CameraWorker

from zone_service import get_zone_service

from mediamtx_crypto import (
    guardar_config_cifrada,
    leer_config_descifrada,
    lanzar_mediamtx_con_clave,
    detener_mediamtx
)


class VisionCoreSystem:
    """Sistema de vision con MediaMTX - MULTI-LENTE"""

    def __init__(self, model_path, tracker_path=None):
        self.model_path = model_path
        self.tracker_path = tracker_path

        print(f"\n{'='*60}")
        print(f"[VisionCore] INICIANDO SISTEMA")
        print(f"[VisionCore] Modelo: {os.path.basename(model_path) if model_path else 'None'}")

        self.appdata_dir = os.path.join(os.environ['APPDATA'], "VisionCore")
        self.bin_dir = os.path.join(self.appdata_dir, "bin")
        os.makedirs(self.bin_dir, exist_ok=True)

        self.cfg_dir = os.path.join(self.appdata_dir, "cfg")
        os.makedirs(self.cfg_dir, exist_ok=True)

        self.mediamtx_exe = os.path.join(self.bin_dir, "mediamtx.exe")
        self.mediamtx_config = os.path.join(self.bin_dir, "mediamtx.yml")

        from config_manager import CameraConfigManager
        from auth import AuthManager

        self.config_manager = CameraConfigManager()
        self.auth_manager = AuthManager()

        self.config_manager.on_config_change(self._on_camera_config_changed)

        self.config_global = self._cargar_config_global()
        print(f"[VisionCore] Configuracion global cargada desde SQLite")

        config_general = self.config_global.get('general', {})
        config_avanzado = self.config_global.get('avanzado', {})
        campos_vacios_general = []
        campos_vacios_avanzado = []

        if 'zona_horaria' not in config_general or not config_general['zona_horaria']:
            campos_vacios_general.append('zona_horaria')

        if 'backend' not in config_avanzado or not config_avanzado['backend']:
            campos_vacios_avanzado.append('backend')

        if campos_vacios_general or campos_vacios_avanzado:
            print(f"[VisionCore] Detectando campos vacios: general={campos_vacios_general}, avanzado={campos_vacios_avanzado}")
            from hardware_detector import HardwareDetector
            detector_temp = HardwareDetector()

            if 'general' not in self.config_global:
                self.config_global['general'] = {}
            if 'avanzado' not in self.config_global:
                self.config_global['avanzado'] = {}

            if 'zona_horaria' in campos_vacios_general:
                zona_detectada = detector_temp.detectar_zona_horaria()
                self.config_global['general']['zona_horaria'] = zona_detectada
                print(f"[VisionCore] Zona horaria detectada: {zona_detectada}")

            if 'backend' in campos_vacios_avanzado:
                self.config_global['avanzado']['backend'] = 'auto'
                print(f"[VisionCore] Backend inicial: auto")

            if campos_vacios_general:
                self._guardar_config_global('general')
            if campos_vacios_avanzado:
                self._guardar_config_global('avanzado')
            print(f"[VisionCore] Campos vacios llenados y guardados")

        self._verificar_y_actualizar_hardware()

        self.detection_engine = None
        self._inicializar_detection_engine()

        self.all_cameras = []
        self.system_lock = threading.Lock()
        self.dashboard = None
        self.mediamtx_process = None
        self.cameras_config = []

        atexit.register(self._cleanup_on_exit)

        print(f"[VisionCore] Sistema listo")
        print(f"[VisionCore] AppData: {self.appdata_dir}")
        print(f"[VisionCore] BIN: {self.bin_dir}")
        print(f"[VisionCore] MediaMTX exe: {self.mediamtx_exe}")
        print(f"[VisionCore] MediaMTX config: {self.mediamtx_config}")
        print(f"{'='*60}\n")

    # ========================================================================
    # HELPERS DE LENTES
    # ========================================================================
    def _tiene_lentes(self, cam_config):
        lentes = cam_config.get('lentes', [])
        return isinstance(lentes, list) and len(lentes) > 0

    def _get_prefijo_lente(self, stream_num, lens_id, cantidad_lentes):
        """
        ⭐ MULTI-LENTE: SIEMPRE camXX_lensN. Sin ramas.
        """
        return f"cam{stream_num:02d}_lens{lens_id}"

    def _get_path_name(self, stream_num, lens_id, cantidad_lentes, sufijo):
        prefijo = self._get_prefijo_lente(stream_num, lens_id, cantidad_lentes)
        return f"{prefijo}_{sufijo}"

    def _get_ia_lens_id(self, cam_config):
        return cam_config.get('ia_lens_id', 1)

    # ========================================================================
    # CONFIG GLOBAL
    # ========================================================================
    def _cargar_config_global(self) -> dict:
        secciones = [
            'general', 'avanzado', 'seguridad', 'almacenamiento',
            'apariencia', 'notificaciones', 'horarios'
        ]
        config = {}
        for s in secciones:
            try:
                data = self.auth_manager.get_global_config(s)
                config[s] = data if data else {}
            except Exception as e:
                print(f"[VisionCore] Error cargando seccion '{s}': {e}")
                config[s] = {}
        return config

    def _guardar_config_global(self, section: str = None):
        secciones = [section] if section else [
            'general', 'avanzado', 'seguridad', 'almacenamiento',
            'apariencia', 'notificaciones', 'horarios'
        ]
        for s in secciones:
            try:
                data = self.config_global.get(s, {})
                self.auth_manager.save_global_config(s, data, user_id='system')
            except Exception as e:
                print(f"[VisionCore] Error guardando seccion '{s}': {e}")

    def _on_camera_config_changed(self, camera_index=None):
        print(f"\n[VisionCore] CONFIGURACION DE CAMARAS CAMBIO")
        if camera_index:
            print(f"[VisionCore]    Camara afectada: {camera_index}")
        print(f"[VisionCore] Configuracion sincronizada\n")

    # ========================================================================
    # HARDWARE
    # ========================================================================
    def _verificar_y_actualizar_hardware(self):
        try:
            from hardware_detector import HardwareDetector
            detector = HardwareDetector()
            cores_actual = detector.system_info['cpu_info'].get('physical_cores', 0)
            gpu_info = detector.system_info['gpu_info']
            tiene_dedicada_actual = any(not g.get('integrated', True) for g in gpu_info) if gpu_info else False
            cuda_actual = detector.system_info['cuda_available']
            directml_actual = detector.directml_available
            cpu_name = detector.system_info['cpu_info'].get('name', 'Desconocido')
            gpu_name = gpu_info[0]['name'] if gpu_info else 'No detectada'
            config_avanzado = self.config_global.get('avanzado', {})
            hardware_guardado = config_avanzado.get('hardware_detectado', {})
            if not hardware_guardado:
                print(f"\n[VisionCore] PRIMER INICIO - Detectando hardware...")
                self._guardar_hardware(detector, cores_actual, tiene_dedicada_actual, cuda_actual, directml_actual, cpu_name, gpu_name)
                return
            cores_guardado = hardware_guardado.get('cpu_cores', 0)
            dedicada_guardado = hardware_guardado.get('gpu_dedicada', False)
            cuda_guardado = hardware_guardado.get('cuda_available', False)
            directml_guardado = hardware_guardado.get('directml_available', False)
            hardware_cambio = (
                cores_actual != cores_guardado or
                tiene_dedicada_actual != dedicada_guardado or
                cuda_actual != cuda_guardado or
                directml_actual != directml_guardado
            )
            if not hardware_cambio:
                print(f"[VisionCore] Hardware sin cambios")
                return
            print(f"\n[VisionCore] HARDWARE CAMBIO - Actualizando...")
            self._guardar_hardware(detector, cores_actual, tiene_dedicada_actual, cuda_actual, directml_actual, cpu_name, gpu_name)
        except ImportError:
            print(f"[VisionCore] HardwareDetector no disponible")
        except Exception as e:
            print(f"[VisionCore] Error verificando hardware: {e}")

    def _guardar_hardware(self, detector, cores, tiene_dedicada, cuda, directml, cpu_name, gpu_name):
        try:
            cpu_info = detector.system_info['cpu_info']
            gpu_info = detector.system_info['gpu_info']
            if 'general' not in self.config_global:
                self.config_global['general'] = {}
            if 'avanzado' not in self.config_global:
                self.config_global['avanzado'] = {}
            if 'zona_horaria' not in self.config_global['general']:
                zona_detectada = detector.detectar_zona_horaria()
                self.config_global['general']['zona_horaria'] = zona_detectada
                self._guardar_config_global('general')
            if 'backend' not in self.config_global['avanzado']:
                self.config_global['avanzado']['backend'] = 'auto'
            self.config_global['avanzado']['hardware_detectado'] = {
                'cpu': cpu_name,
                'cpu_vendor': cpu_info.get('vendor', 'Desconocido'),
                'cpu_cores': cores,
                'cpu_type': cpu_info.get('type', 'Desconocido'),
                'gpu': gpu_name,
                'gpu_vendor': gpu_info[0].get('vendor', 'Desconocido') if gpu_info else 'N/A',
                'gpu_type': gpu_info[0].get('type', 'Desconocido') if gpu_info else 'N/A',
                'gpu_memory': gpu_info[0].get('total_memory', 0) if gpu_info else 0,
                'gpu_dedicada': tiene_dedicada,
                'cuda_available': cuda,
                'directml_available': directml,
                'fecha_deteccion': datetime.now().isoformat()
            }
            self._guardar_config_global('avanzado')
            print(f"[VisionCore] Hardware guardado")
        except Exception as e:
            print(f"[VisionCore] Error guardando hardware: {e}")

    # ========================================================================
    # DETECTION ENGINE
    # ========================================================================
    def _inicializar_detection_engine(self, num_camaras=None):
        try:
            from detection_engine_hybrid import DetectionEngineHybrid
            from hardware_detector import HardwareDetector
            config_general = self.config_global.get('general', {})
            config_avanzado = self.config_global.get('avanzado', {})
            intervalo = config_avanzado.get('intervalo_valor') or config_general.get('intervalo_valor', 0.150)
            backend = config_avanzado.get('backend', 'auto')
            if num_camaras is None:
                try:
                    camaras_temp = self.config_manager.load_camera_config()
                    num_camaras = len(camaras_temp) if camaras_temp else 0
                except Exception as e:
                    print(f"[VisionCore] Error leyendo cámaras: {e} - asumiendo 0")
                    num_camaras = 0
            detector = HardwareDetector()
            onnx_config = detector.get_device_for_onnx(backend_override=backend)
            providers = onnx_config['providers']
            print(f"[VisionCore] Inicializando DetectionEngine...")
            print(f"[VisionCore]    Cámaras configuradas: {num_camaras}")
            self.detection_engine = DetectionEngineHybrid(
                model_path=self.model_path,
                providers=providers,
                config_global=self.config_global,
                num_camaras=num_camaras,
                backend=providers
            )
            print(f"[VisionCore] DetectionEngine inicializado")
        except Exception as e:
            print(f"[VisionCore] Error creando DetectionEngine: {e}")
            self.detection_engine = None

    def _recrear_motor_deteccion(self):
        print(f"\n[VisionCore] 🔄 RECREANDO MOTOR DE DETECCIÓN")
        if self.detection_engine:
            try:
                self.detection_engine.cleanup()
            except Exception as e:
                print(f"[VisionCore] ⚠️ Error en cleanup: {e}")
            self.detection_engine = None
        num_camaras = len(self.all_cameras) if self.all_cameras else 0
        self._inicializar_detection_engine(num_camaras=num_camaras)
        if not self.detection_engine:
            print(f"[VisionCore] ❌ No se pudo crear el motor nuevo")
            return False
        for cam in self.all_cameras:
            cam.detection_engine = self.detection_engine
        for cam in self.all_cameras:
            try:
                if cam.detection_enabled:
                    self.detection_engine.set_config_personas_completa(
                        cam.worker_id,
                        personas_activo=cam.personas_activo,
                        confianza_personas=cam.confianza_personas,
                        tamano_minimo_personas=cam.tamano_minimo_personas,
                        deteccion_global=cam.deteccion_global,
                        solo_zona_personas=cam.solo_zona_personas,
                        tracking_persistente_personas=cam.tracking_persistente_personas,
                        retencion_id_personas=cam.retencion_id_personas,
                    )
            except Exception as e:
                print(f"[VisionCore] ⚠️ Error re-sincronizando {cam.name}: {e}")
        print(f"[VisionCore] ✅ Motor recreado\n")
        return True

    def _deberia_recrear_motor(self):
        if not self.detection_engine:
            return (False, 0, 0)
        num_camaras = len(self.all_cameras)
        if num_camaras == 0:
            return (False, 0, 0)
        try:
            from adaptive_config import AdaptiveConfig
            from hardware_detector import HardwareDetector
            backend = self.config_global.get('avanzado', {}).get('backend', 'auto')
            detector = HardwareDetector()
            onnx_config = detector.get_device_for_onnx(backend_override=backend)
            providers = onnx_config['providers']
            config_nueva = AdaptiveConfig().get_optimal_config(num_camaras, backend=providers)
            n_optimo = config_nueva['detection_workers']
            n_actual = self.detection_engine.num_workers
            if n_actual == 0:
                return (n_optimo > 0, n_actual, n_optimo)
            ratio = max(n_optimo, n_actual) / min(n_optimo, n_actual)
            deberia = ratio > 1.5
            return (deberia, n_actual, n_optimo)
        except Exception as e:
            print(f"[VisionCore] ⚠️ Error evaluando recreación: {e}")
            return (False, 0, 0)

    def redetectar_hardware(self):
        try:
            from hardware_detector import HardwareDetector
            detector = HardwareDetector()
            cores_actual = detector.system_info['cpu_info'].get('physical_cores', 0)
            gpu_info = detector.system_info['gpu_info']
            tiene_dedicada_actual = any(not g.get('integrated', True) for g in gpu_info) if gpu_info else False
            cuda_actual = detector.system_info['cuda_available']
            directml_actual = detector.directml_available
            cpu_name = detector.system_info['cpu_info'].get('name', 'Desconocido')
            gpu_name = gpu_info[0]['name'] if gpu_info else 'No detectada'
            self._guardar_hardware(detector, cores_actual, tiene_dedicada_actual, cuda_actual, directml_actual, cpu_name, gpu_name)
            self._recrear_motor_deteccion()
        except Exception as e:
            print(f"[VisionCore] Error en re-deteccion: {e}")

    def actualizar_backend(self, backend):
        try:
            if 'avanzado' not in self.config_global:
                self.config_global['avanzado'] = {}
            self.config_global['avanzado']['backend'] = backend
            self._guardar_config_global('avanzado')
            exito = self._recrear_motor_deteccion()
            if exito:
                print(f"[VisionCore] Backend actualizado: {backend}")
        except Exception as e:
            print(f"[VisionCore] Error actualizando backend: {e}")

    def actualizar_intervalo_en_vivo(self, intervalo_valor):
        if self.detection_engine:
            self.detection_engine.set_intervalo_deteccion(intervalo_valor)

    # ========================================================================
    # API MEDIAMTX
    # ========================================================================
    def agregar_path_mediamtx_api(self, path_name, rtsp_url, source_on_demand=False):
        try:
            import requests
            if not self._is_mediamtx_running():
                return False
            payload = {
                "source": rtsp_url,
                "sourceOnDemand": source_on_demand,
                "sourceProtocol": "tcp"
            }
            response = requests.post(
                f"http://127.0.0.1:9997/v3/config/paths/add/{path_name}",
                json=payload, timeout=5, proxies={'http': None, 'https': None}
            )
            if response.status_code in [200, 201]:
                print(f"   Path {path_name} creado")
                return True
            print(f"   Error: {response.status_code}")
            return False
        except Exception as e:
            print(f"   Excepcion: {e}")
            return False

    def eliminar_path_mediamtx_api(self, camera_id):
        return self._eliminar_paths_mediamtx_completo(camera_id)

    def recargar_mediamtx_api(self):
        try:
            import requests
            response = requests.post("http://127.0.0.1:9997/v3/config/reload",
                                     timeout=2, proxies={'http': None, 'https': None})
            return response.status_code == 200
        except Exception:
            return False

    def _setup_mediamtx(self):
        try:
            if getattr(sys, 'frozen', False):
                src_exe = os.path.join(sys._MEIPASS, "bin", "mediamtx.exe")
            else:
                src_exe = os.path.join(os.path.dirname(__file__), "mediamtx.exe")
            if not os.path.exists(self.mediamtx_exe):
                if os.path.exists(src_exe):
                    shutil.copy2(src_exe, self.mediamtx_exe)
                else:
                    return False
            return True
        except Exception:
            return False

    # ========================================================================
    # ELIMINAR CAMARA
    # ========================================================================
    def eliminar_camara_completamente(self, datos):
        ip = datos.get('ip')
        camera_index = datos.get('camera_index')
        name = datos.get('name')
        widget = datos.get('widget')
        print(f"\n[VisionCore] ELIMINANDO CAMARA COMPLETAMENTE")
        self._eliminar_paths_mediamtx_completo(camera_index)
        with self.system_lock:
            camera_to_remove = None
            for cam in self.all_cameras:
                cam_index = getattr(cam, 'camera_index', '')
                if str(cam_index) == str(camera_index):
                    camera_to_remove = cam
                    break
            if camera_to_remove:
                try:
                    if hasattr(camera_to_remove, 'stop_completely'):
                        camera_to_remove.stop_completely()
                    elif hasattr(camera_to_remove, 'stop'):
                        camera_to_remove.stop()
                except Exception as e:
                    print(f"[VisionCore] Error deteniendo: {e}")
                self.all_cameras.remove(camera_to_remove)
        self._eliminar_del_json(camera_index)
        # NUEVO: persistir el YAML sin la camara eliminada
        self.cameras_config = self._cargar_configuracion_camaras()
        try:
            self._create_or_update_mediamtx_config()
            print(f"[VisionCore] YAML actualizado tras eliminar {camera_index}")
        except Exception as e:
            print(f"[VisionCore] Error actualizando YAML tras eliminar: {e}")
        self._limpiar_zone_service(camera_index)
        if self.detection_engine:
            try:
                self.detection_engine.limpiar_por_camara(camera_index)
            except Exception as e:
                print(f"[VisionCore] Error limpiando DetectionEngine: {e}")
        if self.dashboard and hasattr(self.dashboard, 'on_camara_eliminada'):
            try:
                self.dashboard.on_camara_eliminada(ip, camera_index, name, widget)
            except Exception as e:
                print(f"[VisionCore] Error notificando dashboard: {e}")
        self.cameras_config = self._cargar_configuracion_camaras()
        return True

    def _eliminar_paths_mediamtx_completo(self, camera_index):
        try:
            import requests
            stream_num = self._get_stream_number(camera_index)
            prefix = f"cam{stream_num:02d}"
            if not self._is_mediamtx_running():
                self._eliminar_del_yaml(prefix)
                return
            try:
                list_response = requests.get(
                    "http://127.0.0.1:9997/v3/config/paths/list",
                    timeout=2, proxies={'http': None, 'https': None}
                )
                if list_response.status_code == 200:
                    paths_data = list_response.json()
                    paths_to_remove = [p for p in paths_data.keys() if p.startswith(prefix)]
                    for path_name in paths_to_remove:
                        try:
                            requests.post(
                                f"http://127.0.0.1:9997/v3/config/paths/remove/{path_name}",
                                timeout=2, proxies={'http': None, 'https': None}
                            )
                        except Exception:
                            pass
            except Exception as e:
                print(f"[VisionCore] Error en API: {e}")
            self._eliminar_del_yaml(prefix)
        except Exception as e:
            print(f"[VisionCore] Error: {e}")

    def _eliminar_del_yaml(self, prefix):
        try:
            import yaml
            if not os.path.exists(self.mediamtx_config):
                return
            yaml_text = leer_config_descifrada(self.mediamtx_config)
            if yaml_text is None:
                return
            config = yaml.safe_load(yaml_text)
            if config is None or 'paths' not in config:
                return
            paths_to_remove = [p for p in config['paths'].keys() if p.startswith(prefix)]
            if not paths_to_remove:
                return
            for path_name in paths_to_remove:
                del config['paths'][path_name]
            nuevo_yaml_str = yaml.dump(config, default_flow_style=False, sort_keys=False)
            guardar_config_cifrada(self.mediamtx_config, nuevo_yaml_str)
        except Exception as e:
            print(f"[VisionCore] Error eliminando del YAML: {e}")

    def _eliminar_del_json(self, camera_index):
        try:
            config_data = self.config_manager.load_camera_config()
            config_data_filtrado = [
                cam_config for cam_config in config_data
                if str(cam_config.get('camera_index', '')) != str(camera_index)
            ]
            if len(config_data_filtrado) != len(config_data):
                self.config_manager.save_camera_config_completo(config_data_filtrado, camera_index)
        except Exception as e:
            print(f"[VisionCore] Error reescribiendo JSON: {e}")

    def _limpiar_zone_service(self, camera_index):
        try:
            from zone_service import get_zone_service
            zone_service = get_zone_service()
            claves_por_camara = [
                'zona_managers', 'filtros_por_camara', 'alarma_activa',
                '_personas_config', '_ultimo_evento_cruce', '_eventos_activos',
                '_estado_ocupacion', '_alarma_ocupacion_activa',
                '_tipo_ultimo_evento_alarma', '_alarmas_por_tipo_estado',
                '_modos_alarmas_por_camara',
            ]
            for clave in claves_por_camara:
                attr = getattr(zone_service, clave, None)
                if attr is not None and isinstance(attr, dict) and camera_index in attr:
                    try:
                        del attr[camera_index]
                    except Exception:
                        pass
            zonas_por_camara = getattr(zone_service, 'zonas_por_camara', None)
            if isinstance(zonas_por_camara, dict):
                for tipo, dict_inner in zonas_por_camara.items():
                    if isinstance(dict_inner, dict) and camera_index in dict_inner:
                        try:
                            del dict_inner[camera_index]
                        except Exception:
                            pass
        except Exception as e:
            print(f"[VisionCore] Error limpiando ZoneService: {e}")

    # ========================================================================
    # ACTUALIZAR STREAM
    # ========================================================================
    def actualizar_stream_camara(self, camera_index, nueva_url, nuevo_fps=None, path_suffix="_main"):
        try:
            import requests
            stream_num = self._get_stream_number(camera_index)
            # ⭐ MULTI-LENTE: usar lens1 por defecto
            path_name = self._get_path_name(stream_num, 1, 1, "main")
            if not self._is_mediamtx_running():
                return False
            try:
                get_response = requests.get(
                    f"http://127.0.0.1:9997/v3/config/paths/get/{path_name}",
                    timeout=2, proxies={'http': None, 'https': None}
                )
                if get_response.status_code == 200:
                    current_source = get_response.json().get('source', '')
                    if current_source == nueva_url:
                        return True
                else:
                    return False
            except Exception:
                return False
            response = requests.patch(
                f"http://127.0.0.1:9997/v3/config/paths/patch/{path_name}",
                json={"source": nueva_url},
                timeout=5, proxies={'http': None, 'https': None}
            )
            if response.status_code == 200:
                self._actualizar_url_en_yaml(camera_index, nueva_url, path_suffix)
                return True
            return False
        except Exception as e:
            print(f"[VisionCore] Error: {e}")
            return False

    def _actualizar_url_en_yaml(self, camera_index, nueva_url, path_suffix="_main"):
        try:
            import yaml
            stream_num = self._get_stream_number(camera_index)
            path_name = self._get_path_name(stream_num, 1, 1, "main")
            if not os.path.exists(self.mediamtx_config):
                self._create_or_update_mediamtx_config()
                return
            yaml_text = leer_config_descifrada(self.mediamtx_config)
            if yaml_text is None:
                self._create_or_update_mediamtx_config()
                return
            config = yaml.safe_load(yaml_text)
            if config and 'paths' in config and path_name in config['paths']:
                config['paths'][path_name]['source'] = nueva_url
                nuevo_yaml_str = yaml.dump(config, default_flow_style=False, sort_keys=False)
                guardar_config_cifrada(self.mediamtx_config, nuevo_yaml_str)
        except Exception as e:
            print(f"[VisionCore] Error actualizando YAML: {e}")

    # ========================================================================
    # PREPARAR CONFIG
    # ========================================================================
    def _prepare_camera_config_for_group(self, camera_data):
        try:
            if hasattr(camera_data, 'to_dict'):
                cam_dict = camera_data.to_dict()
            elif isinstance(camera_data, dict):
                cam_dict = camera_data
            else:
                return None
            camera_index = cam_dict.get('camera_index', 'cam1')
            ip_address = cam_dict.get('ip', '')
            lentes = cam_dict.get('lentes', [])
            tiene_lentes = isinstance(lentes, list) and len(lentes) > 0
            selected_sub = None
            selected_main = None
            lens_id_ia = 1
            if tiene_lentes:
                lens_id_ia = cam_dict.get('ia_lens_id', 1)
                lente_ia = None
                for l in lentes:
                    if l.get('lens_id') == lens_id_ia:
                        lente_ia = l
                        break
                if lente_ia is None:
                    lente_ia = lentes[0]
                    lens_id_ia = lente_ia.get('lens_id', 1)
                selected_main = lente_ia.get('main_profile')
                selected_sub = lente_ia.get('sub_profile')
            else:
                if 'selected_sub_profile' in cam_dict:
                    selected_sub = cam_dict.get('selected_sub_profile')
                elif 'camera_info' in cam_dict and isinstance(cam_dict['camera_info'], dict):
                    if 'selected_sub_profile' in cam_dict['camera_info']:
                        selected_sub = cam_dict['camera_info'].get('selected_sub_profile')
                selected_main = cam_dict.get('selected_main_profile', {})
            if not selected_sub:
                return None
            original_rtsp = ""
            token_sub = selected_sub.get('token', '')
            streams = cam_dict.get('streams', [])
            if token_sub and streams:
                for stream in streams:
                    if isinstance(stream, dict) and stream.get('token') == token_sub:
                        original_rtsp = stream.get('url', '')
                        break
            if not original_rtsp and 'url' in selected_sub:
                original_rtsp = selected_sub.get('url', '')
            if not original_rtsp:
                return None
            structured_config = {
                'ip': ip_address,
                'camera_name': cam_dict.get('camera_name', f'Camara {ip_address}'),
                'camera_index': camera_index,
                'rtsp_url': original_rtsp,
                'selected_sub_profile': selected_sub,
                'selected_main_profile': selected_main,
                'lens_id_ia': lens_id_ia,
                'tiene_lentes': tiene_lentes,
                'cantidad_lentes': len(lentes) if tiene_lentes else 1,
                'lentes': lentes,
            }
            technical_params = {}
            if selected_sub and isinstance(selected_sub, dict):
                resolution = selected_sub.get('resolution', '')
                if resolution and 'x' in resolution:
                    try:
                        width_str, height_str = resolution.split('x')
                        technical_params['frame_width'] = int(width_str.strip())
                        technical_params['frame_height'] = int(height_str.strip())
                    except:
                        pass
                framerate = selected_sub.get('framerate', '')
                if framerate:
                    try:
                        technical_params['target_fps'] = int(float(framerate))
                    except:
                        pass
                video_encoding = selected_sub.get('video_encoding', '')
                if video_encoding and video_encoding != 'Desconocido':
                    technical_params['video_codec'] = video_encoding
            if technical_params:
                structured_config['technical_params'] = technical_params
            return structured_config
        except Exception as e:
            print(f"[VisionCore] ERROR: {e}")
            traceback.print_exc()
            return None

    # ========================================================================
    # MEDIAMTX YAML
    # ========================================================================
    def _create_or_update_mediamtx_config(self):
        try:
            print(f"[VisionCore] Creando/actualizando mediamtx.yml...")
            os.makedirs(self.bin_dir, exist_ok=True)
            config = {
                'rtsp': True, 'rtspAddress': ':8554',
                'hls': True, 'hlsAddress': ':8888',
                'hlsEncryption': True, 'hlsServerKey': 'server.key', 'hlsServerCert': 'server.crt',
                'webrtc': True, 'webrtcAddress': ':8889',
                'webrtcEncryption': True, 'webrtcServerKey': 'server.key', 'webrtcServerCert': 'server.crt',
                'webrtcAllowOrigins': ['*'],
                'api': True, 'apiAddress': ':9997',
                'metrics': True, 'metricsAddress': ':9998',
                'logLevel': 'info', 'readTimeout': '30s', 'writeTimeout': '30s',
                'writeQueueSize': 1024, 'paths': {}
            }
            if self.cameras_config:
                for cam_config in self.cameras_config:
                    camera_index = cam_config.get('camera_index', 'cam1')
                    stream_num = self._get_stream_number(camera_index)
                    self._publicar_paths_camara(config, cam_config, stream_num)
            config['paths']['all'] = {'source': 'publisher', 'sourceOnDemand': False}
            yaml_str = yaml.dump(config, default_flow_style=False, sort_keys=False)
            guardar_config_cifrada(self.mediamtx_config, yaml_str)
            print(f"[VisionCore] Configuración cifrada y guardada")
            return True
        except Exception as e:
            print(f"[VisionCore] Error creando mediamtx.yml: {e}")
            traceback.print_exc()
            return False

    def _publicar_paths_camara(self, config, cam_config, stream_num):
            """
            FIX UNIFICADO: _publicar_paths_camara
            Un solo metodo para publicar paths, con o sin lentes.
            - main/third/extra -> sourceOnDemand=True
            - sub -> sourceOnDemand=False (para deteccion)
            - Si falta main_url, NO se publica el path main.
            
            RESPETAR LA ELECCION DEL PASO 2:
            Si el lente tiene selected_main/sub/third_profile con token,
            se busca el stream correspondiente en lente['streams'] y se
            sobrescribe main_stream/sub_stream/third_stream.
            """
            camera_index = cam_config.get('camera_index', 'cam1')
            lentes = cam_config.get('lentes', [])
            if not isinstance(lentes, list) or len(lentes) == 0:
                    streams = cam_config.get('streams', [])
                    selected_main = cam_config.get('selected_main_profile', {}) or {}
                    selected_sub = cam_config.get('selected_sub_profile', {}) or {}
                    selected_third = cam_config.get('selected_third_profile')
                    def _get_profile_url(profile_dict):
                            if not profile_dict:
                                    return None
                            url = profile_dict.get('url', '') if isinstance(profile_dict, dict) else ''
                            if url:
                                    return url
                            token = profile_dict.get('token', '') if isinstance(profile_dict, dict) else ''
                            if token and streams:
                                    for stream in streams:
                                            if isinstance(stream, dict) and stream.get('token') == token:
                                                    return stream.get('url', '')
                            return None
                    _main_url = _get_profile_url(selected_main)
                    _sub_url = _get_profile_url(selected_sub)
                    _third_url = _get_profile_url(selected_third) if selected_third else None
                    lentes = [{
                            'lens_id': 1,
                            'source_token': None,
                            'main_stream': {'url': _main_url} if _main_url else {},
                            'sub_stream': {'url': _sub_url} if _sub_url else {},
                            'third_stream': {'url': _third_url} if _third_url else {},
                            'has_third': bool(_third_url),
                            'streams': streams,
                    }]
            cantidad_lentes = len(lentes)
            print(f"[VisionCore]   Camara {camera_index}: {cantidad_lentes} lente(s)")
            for lente in lentes:
                    lens_id = lente.get('lens_id', 1)
                    streams_lente = lente.get('streams', []) or []
                    
                    sel_main = lente.get('selected_main_profile')
                    if isinstance(sel_main, dict) and sel_main.get('token'):
                            tok = sel_main['token']
                            for s in streams_lente:
                                    if isinstance(s, dict) and s.get('token') == tok:
                                            lente['main_stream'] = s
                                            print(f"[VisionCore] Lente {lens_id}: MAIN elegido por usuario")
                                            break
                    
                    sel_sub = lente.get('selected_sub_profile')
                    if isinstance(sel_sub, dict) and sel_sub.get('token'):
                            tok = sel_sub['token']
                            for s in streams_lente:
                                    if isinstance(s, dict) and s.get('token') == tok:
                                            lente['sub_stream'] = s
                                            print(f"[VisionCore] Lente {lens_id}: SUB elegido por usuario")
                                            break
                    
                    sel_third = lente.get('selected_third_profile')
                    if isinstance(sel_third, dict) and sel_third.get('token'):
                            tok = sel_third['token']
                            for s in streams_lente:
                                    if isinstance(s, dict) and s.get('token') == tok:
                                            lente['third_stream'] = s
                                            lente['has_third'] = True
                                            print(f"[VisionCore] Lente {lens_id}: THIRD elegido por usuario")
                                            break
                    
                    main_stream = lente.get('main_stream', {}) or {}
                    sub_stream = lente.get('sub_stream', {}) or {}
                    third_stream = lente.get('third_stream', {}) or {}
                    has_third = lente.get('has_third', False)
                    main_url = main_stream.get('url') if isinstance(main_stream, dict) else None
                    sub_url = sub_stream.get('url') if isinstance(sub_stream, dict) else None
                    third_url = third_stream.get('url') if isinstance(third_stream, dict) else None
                    path_main = self._get_path_name(stream_num, lens_id, cantidad_lentes, "main")
                    path_sub = self._get_path_name(stream_num, lens_id, cantidad_lentes, "sub")
                    path_third = self._get_path_name(stream_num, lens_id, cantidad_lentes, "third")
                    if main_url:
                            config['paths'][path_main] = {
                                    'source': main_url, 'sourceOnDemand': True, 'rtspTransport': 'tcp'
                            }
                    else:
                            print(f"[VisionCore] AVISO {camera_index} lente {lens_id}: sin main_url, no se publica el path main")
                    if sub_url:
                            config['paths'][path_sub] = {
                                    'source': sub_url, 'sourceOnDemand': False, 'rtspTransport': 'tcp'
                            }
                    else:
                            print(f"[VisionCore] AVISO {camera_index} lente {lens_id}: sin sub_url, la deteccion no va a funcionar")
                    if has_third and third_url and third_url not in (main_url, sub_url):
                            config['paths'][path_third] = {
                                    'source': third_url, 'sourceOnDemand': True, 'rtspTransport': 'tcp'
                            }
                    urls_excluir = {main_url, sub_url, third_url}
                    extra_count = 0
                    for stream in lente.get('streams', []) or []:
                            if not isinstance(stream, dict):
                                    continue
                            stream_url = stream.get('url', '')
                            if not stream_url or stream_url in urls_excluir:
                                    continue
                            extra_count += 1
                            sufijo = f"extra_{extra_count + 3:02d}"
                            path_extra = self._get_path_name(stream_num, lens_id, cantidad_lentes, sufijo)
                            config['paths'][path_extra] = {
                                    'source': stream_url, 'sourceOnDemand': True, 'rtspTransport': 'tcp'
                            }
                            urls_excluir.add(stream_url)
    def _actualizar_paths_mediamtx_api(self, cam_config):
        """Actualiza los paths de MediaMTX via PATCH (en caliente)."""
        try:
            import requests
            import time
            if not self._is_mediamtx_running():
                return False
            camera_index = cam_config.get('camera_index', '')
            stream_num = self._get_stream_number(camera_index)
            lentes = cam_config.get('lentes', [])
            cantidad_lentes = len(lentes) if lentes else 1
            for lente in lentes:
                lens_id = lente.get('lens_id', 1)
                for clave_stream, sufijo in (
                    ('main_stream', 'main'),
                    ('sub_stream', 'sub'),
                    ('third_stream', 'third'),
                ):
                    stream = lente.get(clave_stream)
                    if not isinstance(stream, dict):
                        continue
                    url = stream.get('url', '')
                    if not url:
                        continue
                    path_name = self._get_path_name(stream_num, lens_id, cantidad_lentes, sufijo)
                    payload = {
                        "source": url,
                        "sourceOnDemand": sufijo != "sub",
                        "rtspTransport": "tcp"
                    }
                    try:
                        # 1. Intentar PATCH (modifica en caliente)
                        r = requests.patch(
                            f"http://127.0.0.1:9997/v3/config/paths/patch/{path_name}",
                            json=payload,
                            timeout=5,
                            proxies={'http': None, 'https': None}
                        )
                        if r.status_code in [200, 201]:
                            print(f"[LOG-MEDIAMTX] Path PATCH OK: {path_name} -> {url[:80]}")
                        elif r.status_code == 404:
                            # El path no existe: crearlo
                            r2 = requests.post(
                                f"http://127.0.0.1:9997/v3/config/paths/add/{path_name}",
                                json=payload,
                                timeout=5,
                                proxies={'http': None, 'https': None}
                            )
                            if r2.status_code in [200, 201]:
                                print(f"[LOG-MEDIAMTX] Path CREADO: {path_name} -> {url[:80]}")
                            else:
                                print(f"[LOG-MEDIAMTX] Path FALLO crear {path_name}: {r2.status_code}")
                        else:
                            print(f"[LOG-MEDIAMTX] Path FALLO PATCH {path_name}: {r.status_code} -> {url[:80]}")
                    except Exception as e:
                        print(f"[LOG-MEDIAMTX] Error {path_name}: {e}")
            return True
        except Exception as e:
            print(f"[VisionCore] Error en _actualizar_paths_mediamtx_api: {e}")
            return False
    def _cargar_configuracion_camaras(self):
        try:
            config = self.config_manager.load_camera_config()
            return config if isinstance(config, list) else []
        except Exception as e:
            print(f"[VisionCore] Error cargando: {e}")
            return []

    def recargar_configuracion(self):
        self.cameras_config = self.config_manager.load_camera_config()
        print(f"[VisionCore] Configuracion cargada: {len(self.cameras_config)} camaras")
        self._create_or_update_mediamtx_config()
        return self.cameras_config

    def _get_stream_number(self, camera_index):
        if isinstance(camera_index, str):
            if camera_index.startswith('cam'):
                try:
                    return int(camera_index[3:])
                except:
                    return 1
            try:
                return int(camera_index)
            except:
                pass
        return 1

    # ========================================================================
    # APLICAR PERSONAS A WORKER (multi-lente)
    # ========================================================================
    def _aplicar_personas_a_worker(self, camera, camara_indice, camera_data):
        """
        ⭐ MULTI-LENTE: aplica config al worker con worker_id como clave.
        """
        try:
            zone_service = get_zone_service()
            # FIX MULTI-LENTE: leer personas_por_lente
            lens_id = getattr(camera, 'lens_id', 1)
            personas_por_lente = camera_data.get('personas_por_lente', {})
            personas_default = camera_data.get('personas', {})
            if str(lens_id) in personas_por_lente:
                personas_config = personas_por_lente[str(lens_id)]
                print(f"[VisionCore] {camera.worker_id}: usando config del lente {lens_id}")
            else:
                personas_config = personas_default
                print(f"[VisionCore] {camera.worker_id}: usando config default (personas)")
            camera.personas_config = personas_config
            camera.personas_activo = personas_config.get('activo', False)
            camera.detection_enabled = camera.personas_activo
            camera.confianza_personas = personas_config.get('confianza', 60)
            camera.tamano_minimo_personas = personas_config.get('tamano_minimo', 10)
            camera.retencion_id_personas = personas_config.get('retencion_id', 2)
            camera.tracking_persistente_personas = personas_config.get('tracking_persistente', False)
            filtro = personas_config.get('filtro')
            if filtro == 'global':
                camera.deteccion_global = True
                camera.solo_zona_personas = False
            elif filtro == 'zona':
                camera.deteccion_global = False
                camera.solo_zona_personas = True
            else:
                camera.deteccion_global = False
                camera.solo_zona_personas = False
            zonas = personas_config.get('zonas', [])
            # FIX MULTI-LENTE: usar worker_id en zone_service
            wid = getattr(camera, 'worker_id', camara_indice)
            if zonas:
                zone_service.cargar_zonas('personas', wid, zonas)
                camera.zonas_personas = zonas
            zone_service.actualizar_personas_config(wid, personas_config)
            global_config = personas_config.get('global')
            if global_config and isinstance(global_config, dict):
                try:
                    camera.actualizar_configuracion({'global': global_config})
                except Exception as e:
                    print(f"[VisionCore]   ⚠️ Error aplicando global: {e}")
            if self.detection_engine:
                self.detection_engine.set_config_personas_completa(
                    camera.worker_id,
                    personas_activo=camera.personas_activo,
                    confianza_personas=camera.confianza_personas,
                    tamano_minimo_personas=camera.tamano_minimo_personas,
                    deteccion_global=camera.deteccion_global,
                    solo_zona_personas=camera.solo_zona_personas,
                    tracking_persistente_personas=camera.tracking_persistente_personas,
                    retencion_id_personas=camera.retencion_id_personas
                )
            if zone_service and zonas:
                if wid not in zone_service.zona_managers:
                    zone_service.cargar_zonas('personas', wid, zonas)
                zone_service.actualizar_filtros(
                    wid,
                    solo_zona=camera.solo_zona_personas,
                    deteccion_global=camera.deteccion_global
                )
        except Exception as e:
            print(f"[VisionCore] Error cargando configuración de personas: {e}")
            traceback.print_exc()

    # ========================================================================
    # CREAR N WORKERS POR CÁMARA
    # ========================================================================
    def _crear_camera_workers_individuales(self, camera_data, structured_config):
        """
        ⭐ MULTI-LENTE: crea N CameraWorkers (1 por lente).
        Devuelve lista de workers.
        """
        try:
            camara_ip = structured_config.get('ip', '')
            camara_indice = structured_config.get('camera_index', 'cam1')
            camara_nombre = structured_config.get('camera_name', f'Camara {camara_ip}')
            if self.detection_engine is None:
                self._inicializar_detection_engine()
            stream_num = self._get_stream_number(camara_indice)
            tiene_lentes = structured_config.get('tiene_lentes', False)
            cantidad_lentes = structured_config.get('cantidad_lentes', 1)
            lentes = structured_config.get('lentes', [])

            # Si no hay lentes explícitos, armar 1 genérico
            if not lentes:
                lentes = [{
                    'lens_id': 1,
                    'source_token': None,
                    'main_stream': structured_config.get('selected_main_profile'),
                    'sub_stream': structured_config.get('selected_sub_profile'),
                }]
                cantidad_lentes = 1

            workers = []
            for lente in lentes:
                lens_id = lente.get('lens_id', 1)
                source_token = lente.get('source_token')

                # ⭐ SIEMPRE camXX_lensN_sub
                path_name = self._get_path_name(stream_num, lens_id, cantidad_lentes, "sub")
                rtsp_mediamtx = f"rtsp://127.0.0.1:8554/{path_name}"
                worker_id = f"{camara_indice}_lens{lens_id}"

                print(f"   Creando worker: {worker_id} -> {rtsp_mediamtx}")

                camera = CameraWorker(
                    name=f"{camara_nombre} · L{lens_id}" if cantidad_lentes > 1 else camara_nombre,
                    rtsp_url=rtsp_mediamtx,
                    camera_ip=camara_ip,
                    camera_index=camara_indice,
                    detection_engine=self.detection_engine,
                    video_dir=os.path.join(self.appdata_dir, "recordings"),
                    network_type="lan",
                    technical_params=structured_config.get('technical_params', {}),
                    worker_id=worker_id,
                    source_token=source_token,
                    lens_id=lens_id,
                )
                camera.visioncore = self
                camera.rtsp_url_original = structured_config.get('rtsp_url', '')

                if camera.start():
                    self._aplicar_personas_a_worker(camera, camara_indice, camera_data)
                    workers.append(camera)
                else:
                    print(f"   ❌ Worker {worker_id} no arrancó")

            return workers
        except Exception as e:
            print(f"[VisionCore] Error creando CameraWorkers: {e}")
            traceback.print_exc()
            return []

    # ========================================================================
    # AGREGAR CAMARAS DESDE JSON (INICIO)
    # ========================================================================
    def agregar_camara_desde_json(self, camera_data=None):
        try:
            print(f"\n{'='*60}")
            print(f"[VisionCore] AGREGANDO CAMARAS DESDE JSON")
            self.recargar_configuracion()
            if not self.cameras_config:
                return []
            if camera_data is None:
                cameras_to_process = self.cameras_config
            elif isinstance(camera_data, dict):
                cameras_to_process = [camera_data]
            elif isinstance(camera_data, list):
                cameras_to_process = camera_data
            else:
                cameras_to_process = self.cameras_config
            nuevas_camaras = []
            hubo_correccion_indices = False
            for i, camara_data in enumerate(cameras_to_process):
                structured_config = self._prepare_camera_config_for_group(camara_data)
                if not structured_config:
                    continue
                camara_ip = structured_config.get('ip', '')
                camara_indice = structured_config.get('camera_index', 'cam1')
                camara_existente = self._buscar_camara_por_ip_e_indice(camara_ip, camara_indice)
                if camara_existente:
                    nuevas_camaras.append(camara_existente)
                else:
                    indices_en_uso = {
                        str(getattr(c, 'camera_index', ''))
                        for c in self.all_cameras if getattr(c, 'camera_index', '')
                    }
                    if str(camara_indice) in indices_en_uso:
                        nuevo_indice = self._siguiente_camera_index_disponible(indices_en_uso)
                        camara_indice = nuevo_indice
                        structured_config['camera_index'] = camara_indice
                        camara_data['camera_index'] = camara_indice
                        hubo_correccion_indices = True
                    workers = self._crear_camera_workers_individuales(camara_data, structured_config)
                    if workers:
                        self.all_cameras.extend(workers)
                        nuevas_camaras.extend(workers)
            if hubo_correccion_indices:
                try:
                    self.config_manager.save_camera_config_completo(self.cameras_config)
                except Exception as e:
                    print(f"[VisionCore] Error guardando JSON: {e}")
            print(f"\n[VisionCore] Procesadas {len(nuevas_camaras)} camaras")
            print(f"{'='*60}\n")
            return nuevas_camaras
        except Exception as e:
            print(f"[VisionCore] ERROR: {e}")
            traceback.print_exc()
            return []

    def agregar_camaras_desde_memoria(self, cameras_data, es_caliente=False):
        try:
            print(f"\n{'='*60}")
            print(f"[VisionCore] AGREGANDO CAMARAS DESDE MEMORIA")
            if es_caliente:
                if not self._is_mediamtx_running():
                    if not self._start_mediamtx():
                        return []
                    time.sleep(2)
                else:
                    try:
                        import requests
                        response = requests.get("http://127.0.0.1:9997/v3/config/paths/list",
                                                 timeout=2, proxies={'http': None, 'https': None})
                        if response.status_code != 200:
                            self._stop_mediamtx()
                            time.sleep(1)
                            self._start_mediamtx()
                            time.sleep(2)
                    except:
                        self._stop_mediamtx()
                        time.sleep(1)
                        self._start_mediamtx()
                        time.sleep(2)
            nuevos_workers = []
            cameras_list = cameras_data if isinstance(cameras_data, list) else [cameras_data]
            for i, camera_data in enumerate(cameras_list):
                structured_config = self._prepare_camera_config_for_group(camera_data)
                if not structured_config:
                    continue
                camara_ip = structured_config.get('ip', '')
                camara_indice = structured_config.get('camera_index', 'cam1')
                camara_nombre = structured_config.get('camera_name', f'Camara {camara_ip}')
                indices_existentes = set()
                for cam_existente in self.all_cameras:
                    idx_existente = getattr(cam_existente, 'camera_index', '')
                    if idx_existente:
                        indices_existentes.add(str(idx_existente))
                for cfg_existente in self.cameras_config:
                    idx_existente = cfg_existente.get('camera_index', '')
                    if idx_existente:
                        indices_existentes.add(str(idx_existente))
                if str(camara_indice) in indices_existentes:
                    nuevo_indice = self._siguiente_camera_index_disponible(indices_existentes)
                    camara_indice = nuevo_indice
                    structured_config['camera_index'] = camara_indice
                    camera_data['camera_index'] = camara_indice
                if self.detection_engine is None:
                    self._inicializar_detection_engine()
                stream_num = self._get_stream_number(camara_indice)
                tiene_lentes = structured_config.get('tiene_lentes', False)
                cantidad_lentes = structured_config.get('cantidad_lentes', 1)
                lentes = structured_config.get('lentes', [])
                if not lentes:
                    lentes = [{
                        'lens_id': 1,
                        'source_token': None,
                        'main_stream': structured_config.get('selected_main_profile'),
                        'sub_stream': structured_config.get('selected_sub_profile'),
                    }]
                    cantidad_lentes = 1
                # Crear paths en MediaMTX (es_caliente)
                if es_caliente:
                    for lente in lentes:
                        lens_id = lente.get('lens_id', 1)
                        main_stream = lente.get('main_stream', {})
                        sub_stream = lente.get('sub_stream', {})
                        main_url = main_stream.get('url') if isinstance(main_stream, dict) else None
                        sub_url = sub_stream.get('url') if isinstance(sub_stream, dict) else None
                        if main_url:
                            path_main = self._get_path_name(stream_num, lens_id, cantidad_lentes, "main")
                            self.agregar_path_mediamtx_api(path_main, main_url)
                            time.sleep(0.3)
                        if sub_url:
                            path_sub = self._get_path_name(stream_num, lens_id, cantidad_lentes, "sub")
                            self.agregar_path_mediamtx_api(path_sub, sub_url)
                            time.sleep(0.3)
                # Crear N workers
                workers = self._crear_camera_workers_individuales(camera_data, structured_config)
                for w in workers:
                    self.all_cameras.append(w)
                    nuevos_workers.append(w)
            for cam_data in cameras_list:
                if cam_data not in self.cameras_config:
                    self.cameras_config.append(cam_data)
            try:
                self.config_manager.save_camera_config_completo(self.cameras_config)
            except Exception as e:
                print(f"[VisionCore] Error guardando JSON: {e}")
            # NUEVO: persistir los paths en el YAML
            self._create_or_update_mediamtx_config()
            print(f"[VisionCore] YAML actualizado con {len(self.cameras_config)} camaras")
            if es_caliente and nuevos_workers:
                deberia, n_actual, n_optimo = self._deberia_recrear_motor()
                if deberia:
                    self._recrear_motor_deteccion()
            print(f"\n[VisionCore] Total nuevas camaras: {len(nuevos_workers)}")
            print(f"{'='*60}\n")
            return nuevos_workers
        except Exception as e:
            print(f"[VisionCore] ERROR: {e}")
            traceback.print_exc()
            return []

    def _siguiente_camera_index_disponible(self, indices_ocupados):
        numeros_usados = set()
        for idx in indices_ocupados:
            if isinstance(idx, str) and idx.startswith('cam'):
                try:
                    numeros_usados.add(int(idx[3:]))
                except ValueError:
                    continue
        n = 1
        while n in numeros_usados:
            n += 1
        return f"cam{n:02d}"

    def _buscar_camara_por_ip_e_indice(self, ip_address, camera_index):
        for camara in self.all_cameras:
            camara_ip = getattr(camara, 'camera_ip', '')
            camara_indice = getattr(camara, 'camera_index', 'cam1')
            if camara_ip == ip_address and camara_indice == camera_index:
                return camara
        return None

    # ========================================================================
    # CONTROL DEL SISTEMA
    # ========================================================================
    def start_system(self):
        print(f"\n[VisionCore] INICIANDO SISTEMA COMPLETO")
        self.recargar_configuracion()
        mediamtx_ok = self._start_mediamtx()
        if not mediamtx_ok:
            return 0
        time.sleep(2.0)
        if not self.cameras_config:
            return 0
        if not self.all_cameras:
            self.agregar_camara_desde_json()
        success_count = 0
        for i, camera in enumerate(self.all_cameras):
            if hasattr(camera, 'running') and camera.running:
                success_count += 1
                continue
            try:
                if camera.start():
                    success_count += 1
            except Exception as e:
                print(f"[VisionCore]   Excepcion: {e}")
            if i < len(self.all_cameras) - 1:
                time.sleep(0.5)
        print(f"\n[VisionCore] SISTEMA INICIADO: {success_count}/{len(self.all_cameras)}")
        return success_count

    def stop_system(self):
        for camera in self.all_cameras:
            try:
                if hasattr(camera, 'stop'):
                    camera.stop()
            except:
                pass
        self.all_cameras = []
        self._stop_mediamtx()
        if self.detection_engine and hasattr(self.detection_engine, 'cleanup'):
            self.detection_engine.cleanup()
            self.detection_engine = None

    def _cleanup_on_exit(self):
        try:
            self.stop_system()
        except Exception as e:
            print(f"[VisionCore] Error en cleanup final: {e}")

    def set_dashboard(self, dashboard):
        self.dashboard = dashboard

    def get_cameras_config(self):
        return self.cameras_config

    def actualizar_porcentaje_yolo_global(self, nuevo_porcentaje):
        if self.detection_engine and hasattr(self.detection_engine, 'set_porcentaje_yolo'):
            self.detection_engine.set_porcentaje_yolo(nuevo_porcentaje)

    # ========================================================================
    # MEDIAMTX START / STOP
    # ========================================================================
    def _is_mediamtx_running(self):
        try:
            if self.mediamtx_process is None:
                return False
            if self.mediamtx_process.poll() is not None:
                return False
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(0.5)
            result = sock.connect_ex(('127.0.0.1', 8554))
            sock.close()
            return result == 0
        except Exception:
            return False

    def _start_mediamtx(self):
        try:
            if self._is_mediamtx_running():
                return True
            if not os.path.exists(self.mediamtx_exe):
                if not self._setup_mediamtx():
                    return False
            if not os.path.exists(self.mediamtx_config):
                if not self._create_or_update_mediamtx_config():
                    return False
            original_dir = os.getcwd()
            os.chdir(self.bin_dir)
            self.mediamtx_process = lanzar_mediamtx_con_clave(self.mediamtx_exe, self.mediamtx_config)
            os.chdir(original_dir)
            time.sleep(2.0)
            if not self._is_mediamtx_running():
                return False
            return True
        except Exception as e:
            print(f"[VisionCore] Error iniciando MediaMTX: {e}")
            return False

    def _stop_mediamtx(self):
        try:
            if self.mediamtx_process:
                detener_mediamtx(self.mediamtx_process)
                self.mediamtx_process = None
            return True
        except Exception as e:
            print(f"[VisionCore] Error deteniendo MediaMTX: {e}")
            return False

    def _reload_mediamtx(self):
        try:
            import requests
            if not self._is_mediamtx_running():
                return self._start_mediamtx()
            response = requests.post("http://127.0.0.1:9997/v3/config/reload",
                                     timeout=3, proxies={'http': None, 'https': None})
            if response.status_code == 200:
                return True
            self._stop_mediamtx()
            time.sleep(2)
            return self._start_mediamtx()
        except Exception:
            self._stop_mediamtx()
            time.sleep(2)
            return self._start_mediamtx()

    # ========================================================================
    # AGREGAR CAMARAS LOTE
    # ========================================================================
    def agregar_camaras_lote(self, cameras_data_list, es_caliente=False):
        print(f"\n{'='*60}")
        print(f"[VisionCore] 📦 PROCESANDO LOTE DE {len(cameras_data_list)} CÁMARAS")
        resultado = {
            'success': True, 'total': len(cameras_data_list), 'creados': 0,
            'workers': [], 'paths_confirmados': 0, 'paths_fallidos': [], 'errores': []
        }
        workers_data = []
        paths_a_confirmar = []
        indices_asignados_en_lote = set()

        for idx, camera_data in enumerate(cameras_data_list):
            try:
                structured_config = self._prepare_camera_config_for_group(camera_data)
                if not structured_config:
                    resultado['errores'].append(f"Cámara {idx+1}: No se pudo preparar")
                    continue
                camara_ip = structured_config.get('ip', '')
                camara_indice = structured_config.get('camera_index', f'cam{idx+1:02d}')
                camara_nombre = structured_config.get('camera_name', f'Camara {camara_ip}')
                indices_ocupados = set()
                for w in self.all_cameras:
                    if hasattr(w, 'camera_index'):
                        indices_ocupados.add(str(w.camera_index))
                for cfg in self.cameras_config:
                    if isinstance(cfg, dict) and 'camera_index' in cfg:
                        indices_ocupados.add(str(cfg['camera_index']))
                indices_ocupados |= indices_asignados_en_lote
                if str(camara_indice) in indices_ocupados:
                    nuevo_indice = self._siguiente_camera_index_disponible(indices_ocupados)
                    camara_indice = nuevo_indice
                    structured_config['camera_index'] = camara_indice
                    camera_data['camera_index'] = camara_indice
                indices_asignados_en_lote.add(str(camara_indice))
                if self.detection_engine is None:
                    self._inicializar_detection_engine()
                stream_num = self._get_stream_number(camara_indice)
                rtsp_original = structured_config.get('rtsp_url', '')
                tiene_lentes = self._tiene_lentes(camera_data)
                cantidad_lentes = len(camera_data.get('lentes', [])) if tiene_lentes else 1
                lentes = structured_config.get('lentes', [])
                if not lentes:
                    lentes = [{
                        'lens_id': 1,
                        'source_token': None,
                        'main_stream': structured_config.get('selected_main_profile'),
                        'sub_stream': structured_config.get('selected_sub_profile'),
                    }]
                    cantidad_lentes = 1
                # Preparar paths a confirmar
                for lente in lentes:
                    lens_id = lente.get('lens_id', 1)
                    path_main = self._get_path_name(stream_num, lens_id, cantidad_lentes, "main")
                    paths_a_confirmar.append(path_main)
                    sub_stream = lente.get('sub_stream', {})
                    if sub_stream and isinstance(sub_stream, dict) and sub_stream.get('url'):
                        path_sub = self._get_path_name(stream_num, lens_id, cantidad_lentes, "sub")
                        paths_a_confirmar.append(path_sub)
                    has_third = lente.get('has_third', False)
                    if has_third:
                        path_third = self._get_path_name(stream_num, lens_id, cantidad_lentes, "third")
                        paths_a_confirmar.append(path_third)
                workers_data.append({
                    'camera_data': camera_data,
                    'camera_index': camara_indice,
                    'camera_name': camara_nombre,
                    'rtsp_original': rtsp_original,
                    'stream_num': stream_num,
                    'structured_config': structured_config,
                    'tiene_lentes': tiene_lentes,
                    'cantidad_lentes': cantidad_lentes,
                    'lentes': lentes,
                })
                resultado['creados'] += 1
            except Exception as e:
                error_msg = f"Cámara {idx+1}: {str(e)}"
                resultado['errores'].append(error_msg)
                traceback.print_exc()

        # Crear paths en MediaMTX
        if es_caliente and workers_data:
            for wdata in workers_data:
                stream_num = wdata['stream_num']
                cantidad_lentes = wdata['cantidad_lentes']
                for lente in wdata['lentes']:
                    lens_id = lente.get('lens_id', 1)
                    main_stream = lente.get('main_stream', {})
                    sub_stream = lente.get('sub_stream', {})
                    main_url = main_stream.get('url') if isinstance(main_stream, dict) else None
                    sub_url = sub_stream.get('url') if isinstance(sub_stream, dict) else None
                    if main_url:
                        path_main = self._get_path_name(stream_num, lens_id, cantidad_lentes, "main")
                        self.agregar_path_mediamtx_api(path_main, main_url, source_on_demand=True)
                        time.sleep(0.3)
                    if sub_url:
                        path_sub = self._get_path_name(stream_num, lens_id, cantidad_lentes, "sub")
                        self.agregar_path_mediamtx_api(path_sub, sub_url, source_on_demand=False)
                        time.sleep(0.3)

        # Iniciar workers
        if workers_data:
            for wdata in workers_data:
                workers = self._crear_camera_workers_individuales(
                    wdata['camera_data'], wdata['structured_config']
                )
                for w in workers:
                    self.all_cameras.append(w)
                    self.cameras_config.append(wdata['camera_data'])
                    resultado['workers'].append(w)

        try:
            self.config_manager.save_camera_config_completo(self.cameras_config)
        except Exception as e:
            resultado['errores'].append(f"Error guardando: {e}")
        # NUEVO: persistir los paths en el YAML
        try:
            self._create_or_update_mediamtx_config()
            print(f"[VisionCore] YAML actualizado con {len(self.cameras_config)} camaras (lote)")
        except Exception as e:
            resultado['errores'].append(f"Error actualizando YAML: {e}")
        if es_caliente and resultado.get('workers'):
            deberia, n_actual, n_optimo = self._deberia_recrear_motor()
            if deberia:
                self._recrear_motor_deteccion()

        resultado['success'] = len(resultado['errores']) == 0
        return resultado

    # ========================================================================
    # ESPERA PATHS
    # ========================================================================
    def _wait_for_mediamtx_paths(self, paths_esperados, timeout=15.0):
        import requests
        import time
        if not paths_esperados:
            return True
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                response = requests.get("http://127.0.0.1:9997/v3/config/paths/list",
                                         timeout=1.0, proxies={'http': None, 'https': None})
                if response.status_code == 200:
                    data = response.json()
                    paths_activos = set()
                    if isinstance(data, dict):
                        for item in data.get('items', []):
                            nombre = item.get('name') if isinstance(item, dict) else item
                            if nombre:
                                paths_activos.add(nombre)
                    faltantes = [p for p in paths_esperados if p not in paths_activos]
                    if not faltantes:
                        return True
            except Exception:
                pass
            time.sleep(0.4)
        return False

    def _wait_for_mediamtx_path_by_name(self, path_name, timeout=5.0, check_interval=0.3):
        import requests
        import time
        start_time = time.time()
        time.sleep(0.2)
        while time.time() - start_time < timeout:
            try:
                response = requests.get(
                    f"http://127.0.0.1:9997/v3/config/paths/get/{path_name}",
                    timeout=0.5, proxies={'http': None, 'https': None}
                )
                if response.status_code == 200:
                    return True
            except Exception:
                pass
            time.sleep(check_interval)
        return False


if __name__ == "__main__":
    print("Este archivo debe ser importado, no ejecutado directamente")