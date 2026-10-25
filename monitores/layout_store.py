# layout_store.py
# Almacenador versionado y persistente de posiciones de ventana en monitores.

import json
import os
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, Signal, QRectF, QPointF, Qt
from PySide6.QtWidgets import QWidget, QFrame, QPushButton, QHBoxLayout, QVBoxLayout
from PySide6.QtGui import QGuiApplication

from .screen_manager import ScreenManager, MonitorInfo, get_screen_manager
from .ui_view_window import ViewWindow, create_view_window


class LayoutStore(QObject):
    """Gestiona layouts versionados por versión (local DB implícita via JSON + metadatos)."""

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, parent=None):
        if hasattr(self, "_initd"):
            return
        super().__init__(parent)
        self._initd = True

        self.layouts: Dict[str, List[Dict[str, float]]] = {}  # layout_name -> [{screen_key, x, y, w, h}, ...]
        self.monitor_views: Dict[str, str] = {}  # monitor_key -> vista_nombre (v06)
        self.current_version = "1.0.0"
        self._filepath = self._default_filepath()

        # Cargar layouts guardados (migración desde JSON antiguo si existe)
        self._migrate_legacy()
        self._load_from_disk()

    @classmethod
    def instance(cls) -> "LayoutStore":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _default_filepath(self) -> str:
        import tempfile
        appdata = os.environ.get("APPDATA", "")
        dir_path = os.path.join(appdata, "VisionCore", "layouts")
        try:
            os.makedirs(dir_path, exist_ok=True)
        except Exception:
            dir_path = os.path.join(tempfile.gettempdir(), "VisionCore", "layouts")
            os.makedirs(dir_path, exist_ok=True)
        return os.path.join(dir_path, "layouts.json")

    def _migrate_legacy(self):
        """Migra layouts anteriores (si los hubiera) a formato nuevo."""
        legacy = os.path.join(os.path.dirname(self._filepath),
                              "layouts_legacy.json")
        if os.path.exists(legacy):
            try:
                with open(legacy, 'r', encoding='utf-8') as f:
                    legacy_data = json.load(f)
                if not self.layouts:
                    self.layouts = legacy_data
            except Exception:
                pass

    def _load_from_disk(self):
        try:
            if os.path.exists(self._filepath):
                with open(self._filepath, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                self.layouts = data.get("layouts", {})
                self.monitor_views = data.get("monitor_views", {})  # v06
                self.current_version = data.get("current_version", self.current_version)
        except Exception:
            pass  # Si no hay archivo, usamos defaults

    # ---------- API pública ----------

    def save_layout(self, name: str, items: List[Dict[str, float]]):
        """Guarda un layout con nombre dado. items = [{screen_key, x, y, w, h}, ...]"""
        valid = [it for it in items if it and isinstance(it, dict)]
        if valid:
            self.layouts[name] = valid
            self._save_to_disk()

    def load_layout(self, name: str) -> Optional[List[Dict[str, float]]]:
        return self.layouts.get(name)

    def delete_layout(self, name: str):
        if name in self.layouts:
            del self.layouts[name]
            self._save_to_disk()

    def list_layouts(self) -> List[str]:
        return list(self.layouts.keys())

    # ---------- v06: Vista por monitor ----------
    def set_monitor_view(self, monitor_key: str, vista_nombre: str):
        """Asocia una vista guardada a un monitor específico (por EDID/key)."""
        if monitor_key and vista_nombre:
            self.monitor_views[monitor_key] = vista_nombre
        elif monitor_key in self.monitor_views:
            del self.monitor_views[monitor_key]
        self._save_to_disk()

    def get_monitor_view(self, monitor_key: str) -> Optional[str]:
        """Obtiene la vista asignada a un monitor."""
        return self.monitor_views.get(monitor_key)

    def get_all_monitor_views(self) -> Dict[str, str]:
        """Retorna todo el mapping monitor_key -> vista_nombre."""
        return dict(self.monitor_views)

    def clear_monitor_view(self, monitor_key: str):
        """Elimina la asignación de vista para un monitor."""
        if monitor_key in self.monitor_views:
            del self.monitor_views[monitor_key]
            self._save_to_disk()

    # ---------- Versión ----------
    def set_current_version(self, v: str):
        self.current_version = v
        self._save_to_disk()

    def get_current_version(self) -> str:
        return self.current_version

    def _save_to_disk(self):
        try:
            data = {
                "layouts": self.layouts,
                "monitor_views": self.monitor_views,  # v06
                "current_version": self.current_version,
            }
            tmp = self._filepath + ".tmp"
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, self._filepath)
        except Exception as e:
            print(f"[LayoutStore] No se pudo guardar layouts: {e}")


class LayoutController(QObject):
    """Controlador de geometría: clamps ventanas a monitores, evita solapamiento, aplica layout."""

    layout_applied = Signal(str, dict)  # layout_name -> {screen_key, applied: bool, ...}
    window_moved = Signal(str, int, int, int, int)  # window_id, x, y, w, h

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, parent=None):
        if hasattr(self, "_initd"):
            return
        super().__init__(parent)
        self._initd = True
        self._sm = ScreenManager.instance()
        self._store = LayoutStore.instance()
        self._window_states: Dict[int, Dict[str, float]] = {}  # win_id -> geom

    @classmethod
    def instance(cls) -> "LayoutController":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ---------- Estado de ventanas ----------

    def registrar_ventana(self, win_id: int, rect: QRectF):
        """Guarda el estado actual de una ventana cuando se mueve."""
        self._window_states[win_id] = {
            "x": rect.x(), "y": rect.y(),
            "w": rect.width(), "h": rect.height(),
        }

    def get_estado_ventana(self, win_id: int) -> Optional[Dict[str, float]]:
        return self._window_states.get(win_id)

    # ---------- Aplicar layout ----------

    def apply_layout(self, layout_name: str, force: bool = False) -> bool:
        """Aplica un layout guardado. Devuelve True si se aplicó al menos un cambio."""
        items = self._store.load_layout(layout_name)
        if not items:
            # Intentar layout por defecto "default"
            items = self._store.load_layout("default")

        if not items:
            print(f"[LayoutController] No hay items en layout '{layout_name}'")
            return False

        applied_any = False

        for item in items:
            screen_key = item.get("screen_key", "")
            x = float(item.get("x", 0))
            y = float(item.get("y", 0))
            w = float(item.get("w", 1920))
            h = float(item.get("h", 1080))

            # Buscar monitor por key
            mon = self._sm.by_key(screen_key)
            if mon is None:
                # Intentar por geometría cercana
                mon = self._sm.containing_point(QPointF(x + w / 2, y + h / 2))
                if mon is None:
                    mon = self._sm.primary()

            # Calcular geometría dentro del monitor
            ax = max(mon.geometry.x(), min(x, mon.geometry.x() + mon.geometry.width() - w))
            ay = max(mon.geometry.y(), min(y, mon.geometry.y() + mon.geometry.height() - h))
            aw = max(1, w)
            ah = max(1, h)

            # Si forzamos o la geometría cambia
            if force or (ax != x or ay != y or aw != w or ah != h):
                applied_any = True

            # Guardar estado
            if win_id := item.get("_win_id"):
                self._window_states[win_id] = {"x": ax, "y": ay, "w": aw, "h": ah}

            # Aplicar geometría a la ventana referenciada
            # (el caller usará win_id; aquí solo registramos)
            # self.layout_applied.emit(layout_name, {"screen_key": screen_key,
            #                                       "applied": applied_any,
            #                                       "geometry": QRectF(ax, ay, aw, ah)})

        if applied_any:
            self._store._save_to_disk()

        return applied_any

    # ---------- Clamp a monitor ----------

    def clamp_to_monitor(self, x: float, y: float, w: float, h: float,
                         mon: Optional[MonitorInfo] = None) -> tuple:
        """Retorna (ax, ay, aw, ah) dentro del monitor dado o principal."""
        if mon is None:
            mon = self._sm.primary() or self._sm.monitors()[0] if self._sm.monitors() else None

        if mon is None:
            # Sin monitores visibles; devolver como vienen
            return x, y, w, h

        ax = max(mon.geometry.x(), min(x, mon.geometry.x() + mon.geometry.width() - w))
        ay = max(mon.geometry.y(), min(y, mon.geometry.y() + mon.geometry.height() - h))
        aw = max(1, min(w, mon.geometry.width()))
        ah = max(1, min(h, mon.geometry.height()))
        return ax, ay, aw, ah

    # ---------- Encontrar monitor por ventana ----------

    def find_monitor_for_window(self, win_id: int) -> Optional[MonitorInfo]:
        """Dado un HWND/win_id, intenta obtener su posición actual y devolver el monitor."""
        try:
            # Intentar obtener geometría nativa de la ventana
            from PySide6.QtWidgets import QWidget
            # Qt no nos da HWND fácilmente sin winId(), así que usamos el state guardado
            state = self._window_states.get(win_id)
            if state:
                pt = QPointF(state["x"] + state["w"] / 2, state["y"] + state["h"] / 2)
                return self._sm.containing_point(pt)
        except Exception:
            pass
        return None

    # ---------- Crear ventanas por monitor ----------

    def create_view_window(self, parent, layout_name: str = "default",
                          initial_state: Optional[Dict[str, float]] = None,
                          fullscreen: bool = False) -> Optional["ViewWindow"]:
        """Crea una ViewWindow para un monitor específico."""
        # Importar aquí para evitar ciclo
        from .ui_view_window import ViewWindow

        mon = self.find_monitor_for_window(
            id(parent) if hasattr(parent, '__int__') else None
        ) or self._sm.primary()

        if not mon:
            return None

        # Construir geometría
        if initial_state:
            x = initial_state.get("x", mon.geometry.x())
            y = initial_state.get("y", mon.geometry.y())
            w = initial_state.get("w", mon.geometry.width())
            h = initial_state.get("h", mon.geometry.height())
        else:
            # Ocupar monitor completo con margen opcional
            x = mon.geometry.x()
            y = mon.geometry.y()
            w = mon.geometry.width()
            h = mon.geometry.height()

        win = ViewWindow(parent, mon, x=x, y=y, w=w, h=h, fullscreen=fullscreen)
        return win


def get_layout_controller() -> LayoutController:
    return LayoutController.instance()