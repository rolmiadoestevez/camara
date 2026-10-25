# config_manager.py
# Manager unificado de configuración del sistema.
# Reemplaza a: global_config.json.enc, camera_config.json.enc, vistas_config.json.enc
# Todo vive ahora en AppData\VisionCore\system\visioncore.db (SQLite).
#
# VERSIÓN UNIFICADA: una sola clase (CameraConfigManager) que maneja
#    cámaras + global config + vistas + grupos. Sin capas separadas.
#
# CIFRADO DE CAMPOS: password + URLs RTSP de cada cámara se cifran antes
#    de guardar en SQLite (con field_crypto.py) y se descifran al leer.
#
# AVANZADO (2025): se agrega la sección 'avanzado' a los defaults.
#
# FASE 0.9 - SIN REESTRUCTURACION DE STREAMS:
# - Se elimino _ensure_streams_structured().
# - Se elimino _normalize_streams().
# - El servidor NO reestructura streams ni rellena selected_*.
# - El cliente es responsable de mandar todo completo (lentes[] + selected_*).

import os
import json
import threading
import time
import sqlite3
import uuid
from datetime import datetime
from typing import List, Optional, Dict, Any
from contextlib import contextmanager

# Cifrado de campos sensibles
try:
    from field_crypto import (
        cifrar_campos_sensibles,
        descifrar_campos_sensibles,
    )
    FIELD_CRYPTO_DISPONIBLE = True
except ImportError as e:
    print(f"[ConfigManager] field_crypto no disponible: {e}")
    FIELD_CRYPTO_DISPONIBLE = False
    def cifrar_campos_sensibles(cam): return cam
    def descifrar_campos_sensibles(cam): return cam


# ============================================================
# MANAGER ÚNICO
# ============================================================
class CameraConfigManager:
    """
    Manager unificado de configuración:
      - global_config (secciones: general, avanzado, seguridad, almacenamiento, ...)
      - camera_config (cámaras)
      - vistas_config (vistas guardadas)
      - groups + group_members (grupos de cámaras)

    Todo en SQLite: AppData/VisionCore/system/visioncore.db
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance

    def __init__(self, camera_config_file=None):
        if self._initialized:
            return
        self._initialized = True

        self.appdata_dir = os.path.join(os.environ['APPDATA'], "VisionCore")
        os.makedirs(self.appdata_dir, exist_ok=True)

        self.system_dir = os.path.join(self.appdata_dir, "system")
        os.makedirs(self.system_dir, exist_ok=True)

        self.db_path = os.path.join(self.system_dir, "visioncore.db")

        self.camera_config_file = os.path.join(self.appdata_dir, "camera_config.json")
        self.global_config_file = os.path.join(self.appdata_dir, "global_config.json")
        self.vistas_config_file = os.path.join(self.appdata_dir, "vistas_config.json")
        self.cfg_dir = os.path.join(self.appdata_dir, "cfg")
        os.makedirs(self.cfg_dir, exist_ok=True)

        self._on_config_change_callbacks = []

        self._cache_global = {}
        self._cache_lock = threading.Lock()
        self._cache_ttl = 60
        self._last_cache_load = 0

        self._init_database()

        print(f"[ConfigManager] Iniciado (SQLite: {self.db_path})")

    # ============================================================
    # CONEXIÓN SQLITE
    # ============================================================
    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_database(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS global_config (
                    section TEXT PRIMARY KEY,
                    config TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT,
                    version INTEGER DEFAULT 1
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS config_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    section TEXT NOT NULL,
                    old_config TEXT,
                    new_config TEXT,
                    user_id TEXT,
                    ip_address TEXT,
                    changed_at TEXT NOT NULL
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS camera_config (
                    camera_index TEXT PRIMARY KEY,
                    data TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT,
                    version INTEGER DEFAULT 1
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS camera_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    camera_index TEXT NOT NULL,
                    old_data TEXT,
                    new_data TEXT,
                    user_id TEXT,
                    changed_at TEXT NOT NULL
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vistas_config (
                    nombre TEXT PRIMARY KEY,
                    grid_id TEXT NOT NULL,
                    asignaciones TEXT NOT NULL,
                    predeterminada INTEGER DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vistas_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    nombre TEXT NOT NULL,
                    old_data TEXT,
                    new_data TEXT,
                    user_id TEXT,
                    changed_at TEXT NOT NULL
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS groups (
                    id TEXT PRIMARY KEY,
                    nombre TEXT NOT NULL,
                    parent_id TEXT,
                    color TEXT DEFAULT '#ec5b13',
                    descripcion TEXT DEFAULT '',
                    orden INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS group_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT NOT NULL,
                    camera_index TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(group_id, camera_index)
                )
            """)

            cursor.execute("CREATE INDEX IF NOT EXISTS idx_config_history_section ON config_history(section)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_camera_history_idx ON camera_history(camera_index)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_vistas_history_nombre ON vistas_history(nombre)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_groups_parent ON groups(parent_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_group_members_group ON group_members(group_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_group_members_camera ON group_members(camera_index)")

            self._init_default_global_config(cursor)

    def _init_default_global_config(self, cursor):
        cursor.execute("SELECT COUNT(*) FROM global_config")
        if cursor.fetchone()[0] > 0:
            return

        now = datetime.now().isoformat()
        defaults = {
            'seguridad': {
                'auth_tipo': 'Local',
                'admin_user': 'admin',
                'policy': 'Fuerte',
                'two_factor': False,
                'session_timeout': '30',
                'audit_log': True,
                'lockout_enabled': True,
                'lockout_attempts': 5,
                'password_min_len': 8,
                'pwd_require_upper': True,
                'pwd_require_lower': True,
                'pwd_require_digit': True,
                'pwd_require_special': False,
                'permisos_roles': {},
                'usuarios': []
            },
            'general': {
                'idioma': 'Español',
                'formato_fecha': 'DD/MM/YYYY',
                'formato_hora': '24h',
                'zona_horaria': '',
            },
            'avanzado': {
                'backend': 'auto',
                'porcentaje_yolo': 50,
                'nivel_logging': 'Info',
                'logs_dias': '30',
                'notificar_errores': True,
                'actualizaciones': 'Automático',
                'hardware_detectado': {},
            },
            'almacenamiento': {
                'ruta_grabaciones': '',
                'ruta_snapshots': '',
                'espacio_maximo': '100',
                'unidad_espacio': 'GB',
                'accion_limite': 'Sobrescribir más antiguos',
                'dias_retencion': '30'
            },
            'apariencia': {
                'tema': 'Oscuro',
                'color_acento': '#ec5b13',
                'vista_default': '2x2',
                'mostrar_nombres': True,
                'mostrar_fps': True
            },
            'notificaciones': {
                'email_notifications': False,
                'push_notifications': True,
                'sound_alerts': True
            },
            'horarios': {
                'modo_24h': True,
                'zona_horaria': 'America/Mexico_City'
            }
        }
        for section, config in defaults.items():
            cursor.execute("""
                INSERT INTO global_config (section, config, updated_at, version)
                VALUES (?, ?, ?, 1)
            """, (section, json.dumps(config, ensure_ascii=False), now))
        print("[ConfigManager] Config global inicializada con defaults")

    # ============================================================
    # CALLBACKS
    # ============================================================
    def on_config_change(self, callback):
        self._on_config_change_callbacks.append(callback)
        print(f"[ConfigManager] Callback registrado: {callback.__name__}")

    def _notify_config_change(self, camera_index=None):
        for cb in self._on_config_change_callbacks:
            try:
                cb(camera_index)
            except Exception as e:
                print(f"[ConfigManager] Error en callback {cb.__name__}: {e}")

    # ============================================================
    # CONFIG GLOBAL
    # ============================================================
    def _load_cache_global(self):
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT section, config FROM global_config")
                with self._cache_lock:
                    self._cache_global = {
                        row['section']: json.loads(row['config'])
                        for row in cursor.fetchall()
                    }
                    self._last_cache_load = time.time()
        except Exception as e:
            print(f"[ConfigManager] Error cache global: {e}")

    def get_global_config(self, section: str) -> dict:
        if time.time() - self._last_cache_load > self._cache_ttl:
            self._load_cache_global()
        with self._cache_lock:
            return self._cache_global.get(section, {})

    def get_all_global_configs(self) -> dict:
        if time.time() - self._last_cache_load > self._cache_ttl:
            self._load_cache_global()
        with self._cache_lock:
            return self._cache_global.copy()

    def save_global_config(self, section: str, config: dict,
                           user_id: str = None, ip_address: str = "") -> bool:
        now = datetime.now().isoformat()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT config, version FROM global_config WHERE section = ?",
                    (section,)
                )
                old_row = cursor.fetchone()
                old_config = json.loads(old_row['config']) if old_row else None
                old_version = old_row['version'] if old_row else 0

                cursor.execute("""
                    INSERT OR REPLACE INTO global_config
                    (section, config, updated_at, updated_by, version)
                    VALUES (?, ?, ?, ?, ?)
                """, (section, json.dumps(config, ensure_ascii=False),
                      now, user_id, old_version + 1))

                if old_config:
                    cursor.execute("""
                        INSERT INTO config_history
                        (section, old_config, new_config, user_id, ip_address, changed_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (section,
                          json.dumps(old_config, ensure_ascii=False),
                          json.dumps(config, ensure_ascii=False),
                          user_id, ip_address, now))

            with self._cache_lock:
                self._cache_global[section] = config
            return True
        except Exception as e:
            print(f"[ConfigManager] Error save_global_config: {e}")
            return False

    def get_config_history(self, section: str = None, limit: int = 50) -> List[Dict]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if section:
                cursor.execute("""
                    SELECT * FROM config_history WHERE section = ?
                    ORDER BY changed_at DESC LIMIT ?
                """, (section, limit))
            else:
                cursor.execute("""
                    SELECT * FROM config_history
                    ORDER BY changed_at DESC LIMIT ?
                """, (limit,))
            return [dict(row) for row in cursor.fetchall()]

    def load_global_config(self) -> dict:
        return self.get_all_global_configs()

    def get_global_section(self, section_name):
        return self.get_global_config(section_name)

    def update_global_section(self, section_name, section_data) -> bool:
        current = self.get_global_config(section_name) or {}
        current.update(section_data)
        return self.save_global_config(section_name, current, user_id='system')

    # ============================================================
    # CÁMARAS
    # ============================================================
    def load_camera_config(self) -> List[Dict]:
        """Devuelve TODAS las camaras tal cual estan en la DB."""
        cameras = []
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT data FROM camera_config ORDER BY camera_index ASC")
            for row in cursor.fetchall():
                try:
                    cam = json.loads(row['data'])
                    if FIELD_CRYPTO_DISPONIBLE:
                        descifrar_campos_sensibles(cam)
                    cameras.append(cam)
                except Exception as e:
                    print(f"[ConfigManager] Error decodificando camara: {e}")
        return cameras

    def get_camera_config(self, camera_index: str) -> Optional[Dict]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT data FROM camera_config WHERE camera_index = ?",
                           (camera_index,))
            row = cursor.fetchone()
            if row:
                cam = json.loads(row['data'])
                if FIELD_CRYPTO_DISPONIBLE:
                    descifrar_campos_sensibles(cam)
                return cam
            return None

    def save_camera_config_completo(self, cameras_list: List[Dict],
                                     user_id: str = None) -> bool:
        """
        Guarda la lista COMPLETA de camaras.
        Borra las que no esten en la lista.
        """
        now = datetime.now().isoformat()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT camera_index FROM camera_config")
                indices_en_db = {row['camera_index'] for row in cursor.fetchall()}

                indices_nuevos = set()
                for cam in cameras_list:
                    idx = cam.get('camera_index')
                    if not idx:
                        continue
                    indices_nuevos.add(idx)

                    cam_copia = json.loads(json.dumps(cam))
                    if FIELD_CRYPTO_DISPONIBLE:
                        cifrar_campos_sensibles(cam_copia)

                    cursor.execute(
                        "SELECT data, version FROM camera_config WHERE camera_index = ?",
                        (idx,)
                    )
                    row = cursor.fetchone()
                    if row:
                        old_data = row['data']
                        old_version = row['version']
                        cursor.execute("""
                            UPDATE camera_config
                            SET data = ?, updated_at = ?, updated_by = ?, version = ?
                            WHERE camera_index = ?
                        """, (json.dumps(cam_copia, ensure_ascii=False), now,
                              user_id, old_version + 1, idx))
                        cursor.execute("""
                            INSERT INTO camera_history
                            (camera_index, old_data, new_data, user_id, changed_at)
                            VALUES (?, ?, ?, ?, ?)
                        """, (idx, old_data,
                              json.dumps(cam_copia, ensure_ascii=False),
                              user_id, now))
                    else:
                        cursor.execute("""
                            INSERT INTO camera_config
                            (camera_index, data, updated_at, updated_by, version)
                            VALUES (?, ?, ?, ?, 1)
                        """, (idx, json.dumps(cam_copia, ensure_ascii=False),
                              now, user_id))

                a_borrar = indices_en_db - indices_nuevos
                for idx in a_borrar:
                    cursor.execute("DELETE FROM camera_config WHERE camera_index = ?", (idx,))
                    cursor.execute("DELETE FROM group_members WHERE camera_index = ?", (idx,))
                    print(f"[ConfigManager] Camara {idx} eliminada")

            self._notify_config_change()
            return True
        except Exception as e:
            print(f"[ConfigManager] Error save_camera_config_completo: {e}")
            return False

    def actualizar_camara_completa(self, camera_data: Dict,
                                    user_id: str = None) -> bool:
        """
        Actualiza UNA camara.
        Preserva 'lentes[]' y 'streams[]' tal cual.
        """
        if not isinstance(camera_data, dict):
            return False
        idx = camera_data.get('camera_index')
        if not idx:
            print("[ConfigManager] actualizar_camara_completa sin camera_index")
            return False

        cam_copia = json.loads(json.dumps(camera_data))
        if FIELD_CRYPTO_DISPONIBLE:
            cifrar_campos_sensibles(cam_copia)

        now = datetime.now().isoformat()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT data, version FROM camera_config WHERE camera_index = ?",
                    (idx,)
                )
                row = cursor.fetchone()
                if row:
                    old_data = row['data']
                    old_version = row['version']
                    cursor.execute("""
                        UPDATE camera_config
                        SET data = ?, updated_at = ?, updated_by = ?, version = ?
                        WHERE camera_index = ?
                    """, (json.dumps(cam_copia, ensure_ascii=False), now,
                          user_id, old_version + 1, idx))
                    cursor.execute("""
                        INSERT INTO camera_history
                        (camera_index, old_data, new_data, user_id, changed_at)
                        VALUES (?, ?, ?, ?, ?)
                    """, (idx, old_data,
                          json.dumps(cam_copia, ensure_ascii=False),
                          user_id, now))
                else:
                    cursor.execute("""
                        INSERT INTO camera_config
                        (camera_index, data, updated_at, updated_by, version)
                        VALUES (?, ?, ?, ?, 1)
                    """, (idx, json.dumps(cam_copia, ensure_ascii=False),
                          now, user_id))

            self._notify_config_change(idx)
            return True
        except Exception as e:
            print(f"[ConfigManager] Error actualizar_camara_completa: {e}")
            return False

    def eliminar_camara(self, camera_index: str, user_id: str = None) -> bool:
        now = datetime.now().isoformat()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT data FROM camera_config WHERE camera_index = ?",
                               (camera_index,))
                row = cursor.fetchone()
                if not row:
                    return False
                old_data = row['data']
                cursor.execute("DELETE FROM camera_config WHERE camera_index = ?",
                               (camera_index,))
                cursor.execute("""
                    INSERT INTO camera_history
                    (camera_index, old_data, new_data, user_id, changed_at)
                    VALUES (?, ?, NULL, ?, ?)
                """, (camera_index, old_data, user_id, now))
                cursor.execute("DELETE FROM group_members WHERE camera_index = ?", (camera_index,))

            self._notify_config_change(camera_index)
            return True
        except Exception as e:
            print(f"[ConfigManager] Error eliminar_camara: {e}")
            return False

    def buscar_camara_por_index(self, camera_index: str):
        cameras = self.load_camera_config()
        for i, cam in enumerate(cameras):
            if cam.get('camera_index') == camera_index:
                return i, cam
        return -1, None

    def buscar_camara_exacta(self, name, ip, index):
        cameras = self.load_camera_config()
        for i, cam in enumerate(cameras):
            if not isinstance(cam, dict):
                continue
            if (cam.get('camera_name') == name
                    and cam.get('ip') == ip
                    and cam.get('camera_index') == index):
                return i, cam
        return -1, None

    def guardar_personas_config(self, camera_index, personas_data):
        pos, cam = self.buscar_camara_por_index(camera_index)
        if cam is None:
            print(f"[ConfigManager] No se encontró cámara {camera_index}")
            return False
        cam['personas'] = personas_data
        cam['ultima_actualizacion'] = datetime.now().isoformat()
        return self.actualizar_camara_completa(cam)

    def obtener_personas_config(self, camera_index):
        _, cam = self.buscar_camara_por_index(camera_index)
        if cam:
            return cam.get('personas', {})
        return {}

    def get_dock_settings_exacto(self, name, ip, index):
        pos, camera = self.buscar_camara_exacta(name, ip, index)
        if pos >= 0 and camera:
            return camera.get('dock_settings', {})
        return {}

    def guardar_dock_settings_exacto(self, name, ip, index, dock_settings):
        try:
            cameras = self.load_camera_config()
            encontrada = False
            for i, cam in enumerate(cameras):
                if not isinstance(cam, dict):
                    continue
                if (cam.get('camera_name') == name
                        and cam.get('ip') == ip
                        and cam.get('camera_index') == index):
                    cameras[i]['dock_settings'] = dock_settings
                    cameras[i]['ultima_actualizacion_dock'] = datetime.now().isoformat()
                    encontrada = True
                    break
            if encontrada:
                return self.save_camera_config_completo(cameras)
            return False
        except Exception as e:
            print(f"[ConfigManager] Error guardar_dock_settings_exacto: {e}")
            return False

    def _limpiar_dock_settings(self, camera_data: dict) -> dict:
        if not isinstance(camera_data, dict):
            return camera_data
        for key in ('dock_settings', 'detection_config', 'grabacion_config', 'alarma_config'):
            if key in camera_data:
                del camera_data[key]
        return camera_data

    # ⭐ _normalize_streams() y _ensure_streams_structured() ELIMINADOS.
    # El cliente es responsable de mandar streams[] y selected_* completos.

    # ============================================================
    # VISTAS
    # ============================================================
    def load_vistas_config(self) -> Dict[str, Dict]:
        result = {}
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT nombre, grid_id, asignaciones, predeterminada FROM vistas_config")
            for row in cursor.fetchall():
                result[row['nombre']] = {
                    'grid_id': row['grid_id'],
                    'asignaciones': json.loads(row['asignaciones']),
                    'predeterminada': bool(row['predeterminada'])
                }
        return result

    def save_vistas_config(self, vistas: Dict[str, Dict], user_id: str = None) -> bool:
        now = datetime.now().isoformat()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT nombre FROM vistas_config")
                nombres_en_db = {row['nombre'] for row in cursor.fetchall()}

                nombres_nuevos = set()
                for nombre, data in vistas.items():
                    nombres_nuevos.add(nombre)
                    grid_id = data.get('grid_id', '')
                    asignaciones = data.get('asignaciones', {})
                    predeterminada = 1 if data.get('predeterminada', False) else 0
                    cursor.execute("""
                        INSERT OR REPLACE INTO vistas_config
                        (nombre, grid_id, asignaciones, predeterminada, updated_at, updated_by)
                        VALUES (?, ?, ?, ?, ?, ?)
                    """, (nombre, grid_id, json.dumps(asignaciones, ensure_ascii=False),
                          predeterminada, now, user_id))

                for nombre in (nombres_en_db - nombres_nuevos):
                    cursor.execute("DELETE FROM vistas_config WHERE nombre = ?", (nombre,))
            return True
        except Exception as e:
            print(f"[ConfigManager] Error save_vistas_config: {e}")
            return False

    def guardar_vista(self, nombre, grid_id, asignaciones, predeterminada=False, user_id=None):
        now = datetime.now().isoformat()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                if predeterminada:
                    cursor.execute("UPDATE vistas_config SET predeterminada = 0")
                cursor.execute("""
                    INSERT OR REPLACE INTO vistas_config
                    (nombre, grid_id, asignaciones, predeterminada, updated_at, updated_by)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (nombre, grid_id, json.dumps(asignaciones, ensure_ascii=False),
                      1 if predeterminada else 0, now, user_id))
            return True
        except Exception as e:
            print(f"[ConfigManager] Error guardar_vista: {e}")
            return False

    def renombrar_vista(self, nombre_viejo, nombre_nuevo, grid_id, asignaciones,
                         predeterminada=False, user_id=None):
        now = datetime.now().isoformat()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                if nombre_viejo != nombre_nuevo:
                    cursor.execute("DELETE FROM vistas_config WHERE nombre = ?", (nombre_viejo,))
                if predeterminada:
                    cursor.execute("UPDATE vistas_config SET predeterminada = 0")
                cursor.execute("""
                    INSERT OR REPLACE INTO vistas_config
                    (nombre, grid_id, asignaciones, predeterminada, updated_at, updated_by)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (nombre_nuevo, grid_id, json.dumps(asignaciones, ensure_ascii=False),
                      1 if predeterminada else 0, now, user_id))
            return True
        except Exception as e:
            print(f"[ConfigManager] Error renombrar_vista: {e}")
            return False

    def eliminar_vista(self, nombre, user_id=None):
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM vistas_config WHERE nombre = ?", (nombre,))
                if not cursor.fetchone():
                    return False
                cursor.execute("DELETE FROM vistas_config WHERE nombre = ?", (nombre,))
            return True
        except Exception as e:
            print(f"[ConfigManager] Error eliminar_vista: {e}")
            return False

    def marcar_vista_predeterminada(self, nombre, user_id=None):
        now = datetime.now().isoformat()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM vistas_config WHERE nombre = ?", (nombre,))
                if not cursor.fetchone():
                    return False
                cursor.execute("UPDATE vistas_config SET predeterminada = 0")
                cursor.execute(
                    "UPDATE vistas_config SET predeterminada = 1, updated_at = ? WHERE nombre = ?",
                    (now, nombre)
                )
            return True
        except Exception as e:
            print(f"[ConfigManager] Error marcar_vista_predeterminada: {e}")
            return False

    def obtener_vista_predeterminada(self):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT nombre, grid_id, asignaciones
                FROM vistas_config WHERE predeterminada = 1 LIMIT 1
            """)
            row = cursor.fetchone()
            if row:
                return row['nombre'], {
                    'grid_id': row['grid_id'],
                    'asignaciones': json.loads(row['asignaciones']),
                    'predeterminada': True
                }
            return None, None

    # ============================================================
    # GRUPOS
    # ============================================================
    def cargar_grupos(self) -> List[Dict]:
        grupos = []
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, nombre, parent_id, color, descripcion, orden
                FROM groups ORDER BY orden ASC, nombre ASC
            """)
            for row in cursor.fetchall():
                grupo = dict(row)
                cursor.execute(
                    "SELECT camera_index FROM group_members WHERE group_id = ?",
                    (grupo['id'],)
                )
                grupo['cameras'] = [r['camera_index'] for r in cursor.fetchall()]
                grupos.append(grupo)
        return grupos

    def obtener_grupo(self, group_id: str) -> Optional[Dict]:
        grupos = self.cargar_grupos()
        for g in grupos:
            if g.get('id') == group_id:
                return g
        return None

    def _generar_group_id(self) -> str:
        return f"grp_{uuid.uuid4().hex[:8]}"

    def crear_grupo(self, nombre: str, parent_id: str = None,
                    color: str = "#ec5b13", descripcion: str = "") -> Optional[str]:
        now = datetime.now().isoformat()
        nuevo_id = self._generar_group_id()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                if parent_id:
                    cursor.execute("SELECT 1 FROM groups WHERE id = ?", (parent_id,))
                    if not cursor.fetchone():
                        print(f"[ConfigManager] parent_id {parent_id} no existe")
                        return None
                cursor.execute("""
                    INSERT INTO groups (id, nombre, parent_id, color, descripcion, orden, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 0, ?, ?)
                """, (nuevo_id, nombre, parent_id, color, descripcion, now, now))
            return nuevo_id
        except Exception as e:
            print(f"[ConfigManager] Error creando grupo: {e}")
            return None

    def editar_grupo(self, group_id: str, nombre: str = None,
                     parent_id: str = None, color: str = None,
                     descripcion: str = None) -> bool:
        now = datetime.now().isoformat()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM groups WHERE id = ?", (group_id,))
                if not cursor.fetchone():
                    return False

                campos = []
                valores = []
                if nombre is not None:
                    campos.append("nombre = ?")
                    valores.append(nombre)
                if parent_id is not None:
                    if parent_id == group_id:
                        print("[ConfigManager] No se puede asignar un grupo como su propio padre")
                        return False
                    campos.append("parent_id = ?")
                    valores.append(parent_id if parent_id else None)
                if color is not None:
                    campos.append("color = ?")
                    valores.append(color)
                if descripcion is not None:
                    campos.append("descripcion = ?")
                    valores.append(descripcion)

                if campos:
                    campos.append("updated_at = ?")
                    valores.append(now)
                    valores.append(group_id)
                    cursor.execute(f"UPDATE groups SET {', '.join(campos)} WHERE id = ?", tuple(valores))
            return True
        except Exception as e:
            print(f"[ConfigManager] Error editando grupo: {e}")
            return False

    def _eliminar_grupo_recursivo(self, cursor, group_id: str):
        cursor.execute("SELECT id FROM groups WHERE parent_id = ?", (group_id,))
        hijos = [r['id'] for r in cursor.fetchall()]
        for hijo in hijos:
            self._eliminar_grupo_recursivo(cursor, hijo)
        cursor.execute("DELETE FROM group_members WHERE group_id = ?", (group_id,))
        cursor.execute("DELETE FROM groups WHERE id = ?", (group_id,))

    def eliminar_grupo(self, group_id: str) -> bool:
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM groups WHERE id = ?", (group_id,))
                if not cursor.fetchone():
                    return False
                self._eliminar_grupo_recursivo(cursor, group_id)
            return True
        except Exception as e:
            print(f"[ConfigManager] Error eliminando grupo: {e}")
            return False

    def agregar_camara_a_grupo(self, group_id: str, camera_index: str) -> bool:
        now = datetime.now().isoformat()
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT 1 FROM groups WHERE id = ?", (group_id,))
                if not cursor.fetchone():
                    return False
                cursor.execute("SELECT 1 FROM camera_config WHERE camera_index = ?", (camera_index,))
                if not cursor.fetchone():
                    return False
                cursor.execute("""
                    INSERT OR IGNORE INTO group_members (group_id, camera_index, created_at)
                    VALUES (?, ?, ?)
                """, (group_id, camera_index, now))
            return True
        except Exception as e:
            print(f"[ConfigManager] Error agregando cámara al grupo: {e}")
            return False

    def quitar_camara_de_grupo(self, group_id: str, camera_index: str) -> bool:
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "DELETE FROM group_members WHERE group_id = ? AND camera_index = ?",
                    (group_id, camera_index)
                )
            return True
        except Exception as e:
            print(f"[ConfigManager] Error quitando cámara del grupo: {e}")
            return False

    # ============================================================
    # DEPURACIÓN
    # ============================================================
    def print_configuration_summary(self):
        cameras = self.load_camera_config()
        global_config = self.load_global_config()
        grupos = self.cargar_grupos()
        print(f"\n[ConfigManager] RESUMEN")
        print("=" * 60)
        print(f"\nCAMARAS: {len(cameras)}")
        for i, cam in enumerate(cameras):
            if not isinstance(cam, dict):
                continue
            print(f"   {i+1}. {cam.get('camera_name', 'Sin nombre')}")
            print(f"      IP: {cam.get('ip', 'Sin IP')}")
            print(f"      Index: {cam.get('camera_index', 'Sin index')}")
            print(f"      Lentes: {len(cam.get('lentes', []))}")
            print(f"      Streams: {len(cam.get('streams', []))}")
        print(f"\nGRUPOS: {len(grupos)}")
        for g in grupos:
            print(f"   [{g.get('id')}] {g.get('nombre')} ({len(g.get('cameras', []))} cámaras)")
        general = global_config.get('general', {})
        print(f"\nGENERAL:")
        print(f"   Idioma: {general.get('idioma', 'N/A')}")
        print(f"   Zona horaria: {general.get('zona_horaria', 'N/A')}")
        avanzado = global_config.get('avanzado', {})
        print(f"\nAVANZADO:")
        print(f"   Backend: {avanzado.get('backend', 'N/A')}")
        print(f"   Porcentaje YOLO: {avanzado.get('porcentaje_yolo', 50)}%")
        print(f"   Nivel logging: {avanzado.get('nivel_logging', 'N/A')}")
        print("\n" + "=" * 60)


# ============================================================
# FACTORY
# ============================================================
def get_config_manager():
    return CameraConfigManager()


def get_config_db_manager():
    return CameraConfigManager()


if __name__ == "__main__":
    manager = CameraConfigManager()
    manager.print_configuration_summary()