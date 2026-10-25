# screen_manager.py
# Inventario de monitores, identificación y geometría.

import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import List, Optional, Dict, Any

from PySide6.QtCore import QObject, Signal, QRect, QPoint
from PySide6.QtGui import QGuiApplication, QScreen


@dataclass
class MonitorInfo:
    """Información inmutable de un monitor."""
    name: str                    # p.ej. "\\\\.\\DISPLAY1"
    serial: str = ""             # EDID serial si disponible
    manufacturer: str = ""       # EDID manufacturer
    model: str = ""              # EDID model
    geometry: QRect = field(default_factory=QRect)  # virtual desktop coords
    available_geometry: QRect = field(default_factory=QRect)  # sin taskbar
    dpi: float = 96.0
    scale_factor: float = 1.0
    is_primary: bool = False
    orientation: str = "landscape"  # "landscape" | "portrait"
    internal: bool = False       # laptop panel

    @property
    def key(self) -> str:
        """Clave estable para persistencia: manufacturer#model#serial o name."""
        base = f"{self.manufacturer}#{self.model}#{self.serial}".strip("#")
        return base if base else self.name

    @property
    def display_name(self) -> str:
        parts = [p for p in [self.manufacturer, self.model] if p]
        return " ".join(parts) or self.name

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # QRect no es JSON-serializable
        d["geometry"] = [self.geometry.x(), self.geometry.y(),
                         self.geometry.width(), self.geometry.height()]
        d["available_geometry"] = [self.available_geometry.x(), self.available_geometry.y(),
                                   self.available_geometry.width(), self.available_geometry.height()]
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MonitorInfo":
        geom = d.pop("geometry", [0, 0, 1920, 1080])
        avail = d.pop("available_geometry", [0, 0, 1920, 1080])
        d["geometry"] = QRect(*geom)
        d["available_geometry"] = QRect(*avail)
        return cls(**d)

    def matches(self, other: "MonitorInfo", fuzzy: bool = True) -> bool:
        """Compara si dos monitores son el mismo hardware."""
        if self.key and self.key == other.key:
            return True
        if not fuzzy:
            return False
        # Fallback: misma resolución y posición relativa
        return (self.geometry == other.geometry and
                self.available_geometry == other.available_geometry and
                abs(self.dpi - other.dpi) < 1)


class ScreenManager(QObject):
    """
    Gestiona el inventario de monitores y notifica cambios.
    Emite:
        screens_changed(list[MonitorInfo])  - lista completa actual
        screen_added(MonitorInfo)           - un monitor nuevo
        screen_removed(str)                 - key del monitor removido
    """
    screens_changed = Signal(list)
    screen_added = Signal(object)      # MonitorInfo
    screen_removed = Signal(str)       # key

    _instance = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, parent=None):
        if hasattr(self, "_inited"):
            return
        super().__init__(parent)
        self._inited = True

        self._monitors: List[MonitorInfo] = []
        self._identifying = False
        self._identify_windows: List[QWidget] = []

        QGuiApplication.instance().screenAdded.connect(self._on_screen_added)
        QGuiApplication.instance().screenRemoved.connect(self._on_screen_removed)

        self.refresh()

    @classmethod
    def instance(cls) -> "ScreenManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def refresh(self) -> List[MonitorInfo]:
        """Relee todos los monitores desde Qt."""
        app = QGuiApplication.instance()
        if app is None:
            return self._monitors

        new_list = []
        for screen in app.screens():
            info = self._screen_to_info(screen, app)
            new_list.append(info)

        # Detectar añadidos/removidos
        old_keys = {m.key for m in self._monitors}
        new_keys = {m.key for m in new_list}

        for m in new_list:
            if m.key not in old_keys:
                self.screen_added.emit(m)

        for key in old_keys - new_keys:
            self.screen_removed.emit(key)

        self._monitors = new_list
        self.screens_changed.emit(list(self._monitors))
        return list(self._monitors)

    def _screen_to_info(self, screen: QScreen, app) -> MonitorInfo:
        geom = screen.geometry()
        avail = screen.availableGeometry()
        name = screen.name()
        serial = ""
        manufacturer = ""
        model = ""

        # Intentar obtener EDID en Windows
        if sys.platform == "win32":
            edid = self._get_edid_windows(name)
            if edid:
                serial = edid.get("serial", "")
                manufacturer = edid.get("manufacturer", "")
                model = edid.get("model", "")

        is_primary = (screen == app.primaryScreen())
        dpi = screen.logicalDotsPerInch()
        scale = screen.devicePixelRatio()

        # Orientación
        orient = "landscape"
        if geom.height() > geom.width():
            orient = "portrait"

        # Heurística panel interno
        internal = False
        if "builtin" in name.lower() or "internal" in name.lower() or "laptop" in model.lower():
            internal = True

        return MonitorInfo(
            name=name,
            serial=serial,
            manufacturer=manufacturer,
            model=model,
            geometry=geom,
            available_geometry=avail,
            dpi=dpi,
            scale_factor=scale,
            is_primary=is_primary,
            orientation=orient,
            internal=internal,
        )

    def _get_edid_windows(self, qt_name: str) -> Optional[Dict[str, str]]:
        """Intenta leer EDID vía WMI en Windows."""
        try:
            import wmi
            c = wmi.WMI()
            # 1. Win32_DesktopMonitor - más directo
            for m in c.Win32_DesktopMonitor():
                if m.Name and (qt_name in m.Name or m.Name in qt_name):
                    return {
                        "serial": m.SerialNumberID or "",
                        "manufacturer": m.Manufacturer or "",
                        "model": m.Name or "",
                    }
            # 2. WmiMonitorID (EDID real) - más preciso
            for m in c.WmiMonitorID():
                if m.UserFriendlyName and (qt_name in m.UserFriendlyName or m.UserFriendlyName in qt_name):
                    # Decodificar manufacturer (EDID 3 chars)
                    man_id = ""
                    if m.ManufacturerName:
                        try:
                            man_bytes = bytes(m.ManufacturerName)
                            if len(man_bytes) >= 2:
                                man_id = "".join(chr((b >> 2) + 64) + chr(((b & 3) << 3) + 64) for b in man_bytes[:2])
                        except Exception:
                            man_id = "".join(chr(b) for b in m.ManufacturerName if 32 <= b < 127)
                    return {
                        "serial": "".join(chr(b) for b in m.SerialNumberID if 32 <= b < 127) if m.SerialNumberID else "",
                        "manufacturer": man_id,
                        "model": "".join(chr(b) for b in m.UserFriendlyName if 32 <= b < 127) if m.UserFriendlyName else "",
                    }
            # 3. Win32_PnPEntity fallback
            for m in c.Win32_PnPEntity():
                if m.Name and "monitor" in m.Name.lower():
                    if qt_name in m.Name or m.Name in qt_name:
                        return {
                            "serial": "",
                            "manufacturer": m.Manufacturer or "",
                            "model": m.Name or "",
                        }
        except Exception:
            pass
        return None

    def _on_screen_added(self, screen: QScreen):
        self.refresh()

    def _on_screen_removed(self, screen: QScreen):
        self.refresh()

    # ---------- API pública ----------

    def monitors(self) -> List[MonitorInfo]:
        return list(self._monitors)

    def primary(self) -> Optional[MonitorInfo]:
        for m in self._monitors:
            if m.is_primary:
                return m
        return self._monitors[0] if self._monitors else None

    def by_key(self, key: str) -> Optional[MonitorInfo]:
        for m in self._monitors:
            if m.key == key:
                return m
        return None

    def by_geometry(self, rect: QRect) -> Optional[MonitorInfo]:
        """Encuentra monitor que contiene el rectángulo (mayor intersección)."""
        best = None
        best_area = 0
        for m in self._monitors:
            inter = m.geometry.intersected(rect)
            area = inter.width() * inter.height()
            if area > best_area:
                best_area = area
                best = m
        return best

    def containing_point(self, point: QPoint) -> Optional[MonitorInfo]:
        for m in self._monitors:
            if m.geometry.contains(point):
                return m
        return None

    # ---------- Identificación visual ----------

    def identify_monitors(self, duration_ms: int = 3000):
        """Muestra un número grande en cada monitor durante duration_ms."""
        if self._identifying:
            return
        self._identifying = True

        from PySide6.QtWidgets import QWidget, QLabel, QVBoxLayout
        from PySide6.QtCore import Qt, QTimer
        from PySide6.QtGui import QFont, QColor, QPalette

        for i, mon in enumerate(self._monitors):
            w = QWidget(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)
            w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
            w.setGeometry(mon.geometry)

            label = QLabel(str(i + 1), w)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            font = QFont("Segoe UI", 200, QFont.Weight.Bold)
            label.setFont(font)
            label.setStyleSheet("color: #ec5b13; background: rgba(15, 23, 42, 0.85); border: 4px solid #ec5b13; border-radius: 16px;")
            lay = QVBoxLayout(w)
            lay.addWidget(label)
            lay.setContentsMargins(0, 0, 0, 0)

            w.showFullScreen()
            self._identify_windows.append(w)

        QTimer.singleShot(duration_ms, self._stop_identify)

    def _stop_identify(self):
        for w in self._identify_windows:
            try:
                w.close()
                w.deleteLater()
            except Exception:
                pass
        self._identify_windows.clear()
        self._identifying = False


# ---------- Compatibilidad simple ----------
def get_screen_manager() -> ScreenManager:
    return ScreenManager.instance()