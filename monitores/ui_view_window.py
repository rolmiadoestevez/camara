# ui_view_window.py
# Ventana de vista independiente para monitores adicionales (frameless, fullscreen opcional).

from typing import Optional, List

from PySide6.QtCore import QRectF, QPointF, Signal, Qt, QSize
from PySide6.QtWidgets import QWidget, QFrame, QPushButton, QHBoxLayout, QVBoxLayout, QComboBox
from PySide6.QtGui import QFont, QColor, QPalette, QPainter, QPen

from .screen_manager import MonitorInfo


class ViewWindow(QWidget):
    """Ventana independiente, frameless, para un solo monitor.

    Se usa para mostrar cámaras o vistas en monitores adicionales
    mientras el dashboard principal permanece en el monitor principal.
    """

    closed = Signal()
    geometry_changed = Signal(QRectF)
    vista_changed = Signal(str)  # v06: emite nombre de vista seleccionada

    def __init__(self, parent=None, monitor: Optional[MonitorInfo] = None,
                 x: int = 0, y: int = 0, w: int = 1920, h: int = 1080,
                 fullscreen: bool = False, title: str = "VisionCore View",
                 available_vistas: Optional[List[str]] = None,
                 current_vista: Optional[str] = None,
                 on_vista_change_callback=None,
                 *args, **kwargs):
        super().__init__(parent, *args, **kwargs)

        self._monitor = monitor
        self._layout_name = None
        self._is_dragging = False
        self._drag_pos = QPointF()
        self._available_vistas = available_vistas or []
        self._current_vista = current_vista
        self._on_vista_change_callback = on_vista_change_callback

        # Configuración de ventana
        self.setWindowTitle(title)
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Geometría inicial
        self.setGeometry(x, y, w, h)

        # Si fullscreen, ocupar el monitor
        if fullscreen and self._monitor:
            self.setGeometry(self._monitor.geometry)

        # Barra mínima de control
        self._setup_ui()

        # Mover a frente
        self.raise_()
        self.activateWindow()

    def _setup_ui(self):
        """Barra superior mínima con combo de vistas, botón cerrar y título."""
        self._bar = QFrame(self)
        self._bar.setFixedHeight(36)
        self._bar.setStyleSheet("""
            QFrame {
                background: rgba(15, 23, 42, 0.92);
                border: 1px solid rgba(236, 91, 19, 0.3);
                border-radius: 8px;
            }
        """)
        lay = QHBoxLayout(self._bar)
        lay.setContentsMargins(12, 0, 12, 0)

        self._title_label = QPushButton("VisionCore View", self._bar)
        self._title_label.setEnabled(False)
        self._title_label.setStyleSheet("""
            QPushButton:disabled {
                color: #e8f4f8;
                background: transparent;
                font-weight: 600;
                font-size: 13px;
                border: none;
            }
        """)
        lay.addWidget(self._title_label)

        # Combo de vistas (v06) - visible solo si hay vistas disponibles
        self._vista_combo = QComboBox(self._bar)
        self._vista_combo.setFixedWidth(160)
        self._vista_combo.setStyleSheet("""
            QComboBox {
                color: #e8f4f8;
                background: rgba(236, 91, 19, 0.15);
                border: 1px solid rgba(236, 91, 19, 0.3);
                border-radius: 4px;
                padding: 2px 8px;
                font-size: 11px;
            }
            QComboBox:hover { border-color: #ec5b13; }
            QComboBox::drop-down { border: none; width: 20px; }
            QComboBox QAbstractItemView {
                color: #e8f4f8;
                background: #1e2a4a;
                border: 1px solid #ec5b13;
                selection-background-color: #ec5b13;
            }
        """)
        self._vista_combo.addItems(self._available_vistas)
        if self._current_vista:
            idx = self._vista_combo.findText(self._current_vista)
            if idx >= 0:
                self._vista_combo.setCurrentIndex(idx)
        self._vista_combo.currentTextChanged.connect(self._on_vista_combo_changed)
        self._vista_combo.setVisible(len(self._available_vistas) > 0)
        lay.addWidget(self._vista_combo)

        lay.addStretch()

        btn_identificar = QPushButton("Identificar", self._bar)
        btn_identificar.setFixedSize(88, 26)
        btn_identificar.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_identificar.setStyleSheet("""
            QPushButton {
                color: #ec5b13;
                background: transparent;
                border: 1px solid #ec5b13;
                border-radius: 4px;
                font-size: 11px;
            }
            QPushButton:hover { background: rgba(236, 91, 19, 0.15); }
        """)
        btn_identificar.clicked.connect(self._identificar_monitor)
        lay.addWidget(btn_identificar)

        btn_cerrar = QPushButton("✕", self._bar)
        btn_cerrar.setFixedSize(26, 26)
        btn_cerrar.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_cerrar.setStyleSheet("""
            QPushButton {
                color: #ec5b13;
                background: transparent;
                border: none;
                font-size: 14px;
                font-weight: bold;
            }
            QPushButton:hover { color: #ff4444; }
        """)
        btn_cerrar.clicked.connect(self._on_cerrar)
        lay.addWidget(btn_cerrar)

        # Posicionar barra
        self._bar.move(8, 8)
        self._bar.show()

    def _identificar_monitor(self):
        """Muestra el número de monitor brevemente."""
        if not self._monitor:
            return
        from screen_manager import get_screen_manager
        get_screen_manager().identify_monitors(2000)

    def _on_vista_combo_changed(self, vista_nombre: str):
        """Callback cuando cambia el combo de vistas."""
        if vista_nombre and vista_nombre != self._current_vista:
            self._current_vista = vista_nombre
            self.vista_changed.emit(vista_nombre)
            if self._on_vista_change_callback:
                try:
                    self._on_vista_change_callback(vista_nombre)
                except Exception as e:
                    print(f"[ViewWindow] Error en callback vista_change: {e}")

    def _on_cerrar(self):
        self.closed.emit()
        self.close()
        self.deleteLater()

    # ---------- v06: Métodos para vistas ----------
    def set_available_vistas(self, vistas: List[str]):
        """Actualiza la lista de vistas disponibles en el combo."""
        self._available_vistas = vistas or []
        self._vista_combo.clear()
        self._vista_combo.addItems(self._available_vistas)
        self._vista_combo.setVisible(len(self._available_vistas) > 0)
        if self._current_vista:
            idx = self._vista_combo.findText(self._current_vista)
            if idx >= 0:
                self._vista_combo.setCurrentIndex(idx)

    def set_current_vista(self, vista_nombre: str):
        """Establece la vista actual programáticamente."""
        if vista_nombre in self._available_vistas:
            self._current_vista = vista_nombre
            idx = self._vista_combo.findText(vista_nombre)
            if idx >= 0:
                self._vista_combo.setCurrentIndex(idx)

    def current_vista(self) -> Optional[str]:
        return self._current_vista

    def update_title(self, title: str):
        """Actualiza el título de la ventana."""
        self._title_label.setText(title)

    # ---------- Helpers ----------

    def set_monitor(self, monitor: MonitorInfo):
        """Cambia el monitor de referencia y ajusta geometría."""
        self._monitor = monitor
        if monitor:
            self.setGeometry(monitor.geometry)

    def monitor(self) -> Optional[MonitorInfo]:
        return self._monitor

    def set_layout_name(self, name: str):
        self._layout_name = name

    def layout_name(self) -> Optional[str]:
        return self._layout_name


# Para compatibilidad si se importa desde layout_store
# Para compatibilidad si se importa desde layout_store
def create_view_window(parent, monitor: Optional[MonitorInfo] = None,
                       fullscreen: bool = True, title: str = "VisionCore View",
                       available_vistas: Optional[List[str]] = None,
                       current_vista: Optional[str] = None,
                       on_vista_change_callback=None) -> Optional[ViewWindow]:
    if not monitor:
        from screen_manager import get_screen_manager
        monitor = get_screen_manager().primary()
    if not monitor:
        return None
    return ViewWindow(parent, monitor, x=monitor.geometry.x(), y=monitor.geometry.y(),
                      w=monitor.geometry.width(), h=monitor.geometry.height(),
                      fullscreen=fullscreen, title=title,
                      available_vistas=available_vistas,
                      current_vista=current_vista,
                      on_vista_change_callback=on_vista_change_callback)