# dialogo_vista.py - Diálogo para crear/editar vistas, con grilla arrastrable
from PySide6.QtWidgets import *
from PySide6.QtCore import *
from PySide6.QtGui import *

from cuadriculas import obtener_layout, nombres_para_combo, LayoutGrid
from asignaciones_vista import AsignacionesVista

MIME_CAMARA = "application/x-vc-camera"   # arrastre desde el árbol (nueva asignación)
MIME_SLOT = "application/x-vc-slot"       # arrastre desde otra celda (swap)

# ============================================================
# PALETA LIGHT (estilo ventana_grupos.py)
# ============================================================
COLOR_PRIMARIO = "#1E3A5F"
COLOR_ACENTO = "#ec5b13"
COLOR_ACENTO_HOVER = "#d94f0a"
COLOR_BORDE = "#e0e6ed"
COLOR_FONDO = "#f8fafc"
COLOR_CARD = "#ffffff"
COLOR_TEXTO = "#2c3e50"
COLOR_TEXTO_SEC = "#64748b"
COLOR_HINT = "#94a3b8"
COLOR_BORDE_FOCUS = "#ec5b13"
COLOR_GRIS = "#cbd5e1"
COLOR_PELIGRO = "#dc2626"
COLOR_PELIGRO_HOVER = "#ef4444"
COLOR_VERDE = "#2ecc71"


class VideowallPreview(QWidget):
    """Preview visual compacto del videowall - dibuja celdas según plantilla"""
    
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(200, 60)
        self.setMaximumHeight(80)
        self.filas = 2
        self.cols = 4
        self.plantilla_key = "2_monitores_lado"
        
    def set_plantilla(self, plantilla_key: str):
        """Actualiza dimensiones según plantilla"""
        configs = {
            "2_monitores_lado": (2, 4),
            "3_monitores_horizontal": (3, 3),
            "2x2_monitores": (4, 4),
            "personalizado": (self.filas, self.cols),
        }
        if plantilla_key in configs:
            self.filas, self.cols = configs[plantilla_key]
        self.plantilla_key = plantilla_key
        self.update()
        
    def set_custom_dims(self, filas: int, cols: int):
        self.filas = filas
        self.cols = cols
        self.update()
        
    def paintEvent(self, event):
        from PySide6.QtGui import QPainter, QColor, QPen, QFont
        from PySide6.QtCore import Qt, QRectF
        
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        w, h = self.width(), self.height()
        margin = 4
        cell_w = max(1, (w - margin * (self.cols + 1)) / self.cols)
        cell_h = max(1, (h - margin * (self.filas + 1)) / self.filas)
        
        # Color por monitor (mitad y mitad aprox)
        mid = (self.filas * self.cols) // 2
        
        for r in range(self.filas):
            for c in range(self.cols):
                x = margin + c * (cell_w + margin)
                y = margin + r * (cell_h + margin)
                rect = QRectF(x, y, cell_w, cell_h)
                
                idx = r * self.cols + c
                color = QColor(COLOR_ACENTO) if idx < mid else QColor(COLOR_BORDE)
                p.fillRect(rect, color)
                p.setPen(QPen(QColor(COLOR_BORDE), 1))
                p.drawRect(rect)
                
                # Número de celda
                p.setPen(QColor(COLOR_TEXTO_SEC))
                p.setFont(QFont("Segoe UI", 7))
                p.drawText(rect, Qt.AlignmentFlag.AlignCenter, str(idx + 1))


class ListaCamarasArrastrable(QListWidget):
    """Lista de cámaras disponibles. El arrastre exporta el id de cámara
    con el mime type MIME_CAMARA, para que las celdas de la grilla lo
    reciban sin ambigüedad con el drag interno de Qt."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)

    def startDrag(self, supportedActions):
        item = self.currentItem()
        if not item:
            return
        camera_id = item.data(Qt.ItemDataRole.UserRole)
        if not camera_id:
            return

        mime = QMimeData()
        mime.setData(MIME_CAMARA, camera_id.encode("utf-8"))

        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.CopyAction)


class CeldaSlot(QFrame):
    """Una celda de la grilla de preview. Acepta que le suelten una
    cámara (desde el árbol o desde otra celda) y también puede arrastrarse
    a sí misma hacia otra celda para reordenar/intercambiar."""

    cambio = Signal()  # se emite cuando esta celda cambia de cámara asignada

    def __init__(self, slot_index: int, parent=None):
        super().__init__(parent)
        self.slot_index = slot_index
        self.camera_id = None
        self.camera_nombre = ""
        self._drag_start_pos = None

        self.setAcceptDrops(True)
        self.setFrameShape(QFrame.Shape.Box)
        self.setMinimumSize(60, 45)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(2)

        self.label = QLabel("Vacío")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True)
        layout.addWidget(self.label)

        self.btn_limpiar = QPushButton("×")
        self.btn_limpiar.setFixedSize(16, 16)
        self.btn_limpiar.setStyleSheet(f"QPushButton {{ border: none; color: {COLOR_TEXTO_SEC}; font-weight: bold; }}")
        self.btn_limpiar.clicked.connect(self._on_limpiar_clicked)
        self.btn_limpiar.hide()

        top_row = QHBoxLayout()
        top_row.addStretch()
        top_row.addWidget(self.btn_limpiar)
        layout.insertLayout(0, top_row)

        self._actualizar_estilo(ocupada=False, hover=False)

    def set_camara(self, camera_id, camera_nombre):
        self.camera_id = camera_id
        self.camera_nombre = camera_nombre
        if camera_id:
            self.label.setText(camera_nombre or camera_id)
            self.btn_limpiar.show()
        else:
            self.label.setText("Vacío")
            self.btn_limpiar.hide()
        self._actualizar_estilo(ocupada=bool(camera_id), hover=False)

    def _on_limpiar_clicked(self):
        self.set_camara(None, "")
        self.cambio.emit()

    def _actualizar_estilo(self, ocupada: bool, hover: bool):
        if hover:
            self.setStyleSheet(f"""
                QFrame {{ background-color: rgba(236, 91, 19, 0.20); border: 2px dashed {COLOR_ACENTO}; border-radius: 4px; }}
                QLabel {{ color: {COLOR_ACENTO}; font-size: 11px; font-weight: bold; border: none; }}
            """)
        elif ocupada:
            self.setStyleSheet(f"""
                QFrame {{ background-color: rgba(236, 91, 19, 0.12); border: 1px solid {COLOR_ACENTO}; border-radius: 4px; }}
                QLabel {{ color: {COLOR_ACENTO}; font-size: 11px; font-weight: bold; border: none; }}
            """)
        else:
            self.setStyleSheet(f"""
                QFrame {{ background-color: {COLOR_CARD}; border: 1px solid {COLOR_BORDE}; border-radius: 4px; }}
                QLabel {{ color: {COLOR_TEXTO_SEC}; font-size: 11px; border: none; }}
            """)

    # ---- Recibir un drop (desde el árbol o desde otra celda) ----

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat(MIME_CAMARA) or event.mimeData().hasFormat(MIME_SLOT):
            event.acceptProposedAction()
            self._actualizar_estilo(ocupada=bool(self.camera_id), hover=True)

    def dragLeaveEvent(self, event):
        self._actualizar_estilo(ocupada=bool(self.camera_id), hover=False)

    def dropEvent(self, event):
        editor = self._editor_padre()
        if editor is None:
            return

        if event.mimeData().hasFormat(MIME_SLOT):
            slot_origen = int(bytes(event.mimeData().data(MIME_SLOT)).decode("utf-8"))
            editor.intercambiar_slots(slot_origen, self.slot_index)
        elif event.mimeData().hasFormat(MIME_CAMARA):
            camera_id = bytes(event.mimeData().data(MIME_CAMARA)).decode("utf-8")
            editor.asignar_camara(self.slot_index, camera_id)

        event.acceptProposedAction()

    # ---- Iniciar un drag desde esta celda (para reordenar) ----

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.camera_id:
            self._drag_start_pos = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not (event.buttons() & Qt.MouseButton.LeftButton):
            return
        if self._drag_start_pos is None or not self.camera_id:
            return
        if (event.position().toPoint() - self._drag_start_pos).manhattanLength() < QApplication.startDragDistance():
            return

        mime = QMimeData()
        mime.setData(MIME_SLOT, str(self.slot_index).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)

    def _editor_padre(self):
        widget = self.parent()
        while widget is not None and not isinstance(widget, GridEditorWidget):
            widget = widget.parent()
        return widget


class GridEditorWidget(QWidget):
    """Dibuja la grilla real (usando cuadriculas.py) y delega toda la
    lógica de asignación a AsignacionesVista. dialogo_vista.py solo
    conecta señales, nunca decide "quién va dónde" - eso vive en
    asignaciones_vista.py para poder testearlo sin Qt."""

    asignaciones_cambiaron = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout_actual: LayoutGrid = obtener_layout("2x2")
        self.asignaciones = AsignacionesVista(self.layout_actual.total_camaras)
        self.camaras_por_id = {}  # id -> nombre, para mostrar en las celdas
        self.celdas = []

        self.grid_layout = QGridLayout(self)
        self.grid_layout.setSpacing(6)

    def set_camaras_disponibles(self, camaras):
        """camaras: lista de dicts {'id':..., 'nombre':...}"""
        self.camaras_por_id = {c["id"]: c.get("nombre", c["id"]) for c in camaras}

    def construir(self, layout_id: str, asignaciones_previas: dict = None):
        self.layout_actual = obtener_layout(layout_id)

        if asignaciones_previas is not None:
            self.asignaciones = AsignacionesVista.from_dict(
                self.layout_actual.total_camaras, asignaciones_previas
            )
        else:
            # Al cambiar de grid se conserva lo que entra, se pierde lo que sobra
            self.asignaciones.redimensionar(self.layout_actual.total_camaras)

        for i in reversed(range(self.grid_layout.count())):
            w = self.grid_layout.itemAt(i).widget()
            if w:
                w.setParent(None)
        self.celdas = []

        for idx, celda in enumerate(self.layout_actual.celdas):
            slot = CeldaSlot(idx, self)
            slot.cambio.connect(self._on_slot_cambio_manual)
            self.grid_layout.addWidget(slot, celda.row, celda.col, celda.row_span, celda.col_span)
            self.celdas.append(slot)

        for i in range(self.layout_actual.columnas):
            self.grid_layout.setColumnStretch(i, 1)
        for i in range(self.layout_actual.filas):
            self.grid_layout.setRowStretch(i, 1)

        self._refrescar_celdas()

    def _refrescar_celdas(self):
        for idx, celda_widget in enumerate(self.celdas):
            camera_id = self.asignaciones.camara_en(idx)
            nombre = self.camaras_por_id.get(camera_id, camera_id) if camera_id else ""
            celda_widget.set_camara(camera_id, nombre)

    def _on_slot_cambio_manual(self):
        # Se llama cuando el usuario limpia una celda a mano (botón "x")
        slot_widget = self.sender()
        self.asignaciones.limpiar_slot(slot_widget.slot_index)
        self.asignaciones_cambiaron.emit()

    # ---- API llamada por CeldaSlot ----

    def asignar_camara(self, slot_index, camera_id):
        self.asignaciones.asignar(slot_index, camera_id)
        self._refrescar_celdas()
        self.asignaciones_cambiaron.emit()

    def intercambiar_slots(self, slot_a, slot_b):
        self.asignaciones.intercambiar(slot_a, slot_b)
        self._refrescar_celdas()
        self.asignaciones_cambiaron.emit()

    # ---- Acciones de los botones ----

    def autocompletar(self):
        self.asignaciones.autocompletar(list(self.camaras_por_id.keys()))
        self._refrescar_celdas()
        self.asignaciones_cambiaron.emit()

    def limpiar_todo(self):
        self.asignaciones.limpiar_todo()
        self._refrescar_celdas()
        self.asignaciones_cambiaron.emit()

    def construir_videowall(self, filas: int, cols: int):
        """Construye grid para videowall con filas/cols específicas (no layout_id)"""
        from cuadriculas import LayoutGrid, CeldaGrid
        
        # Crear layout manual para videowall
        celdas = []
        for r in range(filas):
            for c in range(cols):
                celdas.append(CeldaGrid(r, c, 1, 1))
        
        self.layout_actual = LayoutGrid(
            layout_id=f"videowall_{filas}x{cols}",
            nombre=f"Videowall {filas}x{cols}",
            filas=filas,
            columnas=cols,
            celdas=celdas
        )
        
        self.asignaciones = AsignacionesVista(self.layout_actual.total_camaras)
        
        for i in reversed(range(self.grid_layout.count())):
            w = self.grid_layout.itemAt(i).widget()
            if w:
                w.setParent(None)
        self.celdas = []

        for idx, celda in enumerate(self.layout_actual.celdas):
            slot = CeldaSlot(idx, self)
            slot.cambio.connect(self._on_slot_cambio_manual)
            self.grid_layout.addWidget(slot, celda.row, celda.col, celda.row_span, celda.col_span)
            self.celdas.append(slot)

        for i in range(self.layout_actual.columnas):
            self.grid_layout.setColumnStretch(i, 1)
        for i in range(self.layout_actual.filas):
            self.grid_layout.setRowStretch(i, 1)

        self._refrescar_celdas()


class DialogoVista(QDialog):
    """Diálogo para crear o editar una vista guardada."""

    def __init__(self, parent=None, modo="crear", camaras=None, vista_existente=None):
        super().__init__(parent, Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.modo = modo
        self.camaras = camaras or []
        self.vista_existente = vista_existente or {}
        self._drag_pos = None

        self.setMinimumWidth(680)
        self.setMinimumHeight(520)

        self._init_ui()
        self._precargar_si_corresponde()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = event.globalPosition().toPoint()
            event.accept()

    def mouseMoveEvent(self, event):
        if event.buttons() == Qt.MouseButton.LeftButton and self._drag_pos:
            self.move(self.pos() + event.globalPosition().toPoint() - self._drag_pos)
            self._drag_pos = event.globalPosition().toPoint()
            event.accept()

    def _init_ui(self):
        layout_externo = QVBoxLayout(self)
        layout_externo.setContentsMargins(0, 0, 0, 0)

        self.central = QFrame()
        self.central.setObjectName("central")
        self.central.setStyleSheet(f"""
            #central {{
                background-color: {COLOR_FONDO};
                border: 2px solid {COLOR_PRIMARIO};
                border-radius: 12px;
            }}
            #central QLabel {{
                color: {COLOR_TEXTO_SEC};
                font-size: 12px;
                border: none;
                background: transparent;
            }}
            #central QLineEdit, #central QComboBox, #central QSpinBox {{
                background-color: {COLOR_CARD};
                border: 1px solid {COLOR_BORDE};
                border-radius: 4px;
                padding: 6px 8px;
                color: {COLOR_TEXTO};
                font-size: 12px;
            }}
            #central QLineEdit:focus, #central QComboBox:focus, #central QSpinBox:focus {{
                border: 1px solid {COLOR_BORDE_FOCUS};
            }}
            #central QPushButton {{
                background-color: transparent;
                border: 1px solid {COLOR_BORDE};
                border-radius: 4px;
                padding: 7px 14px;
                color: {COLOR_PRIMARIO};
                font-size: 12px;
            }}
            #central QPushButton:hover {{
                border-color: {COLOR_ACENTO};
                background: rgba(236,91,19,0.08);
            }}
            #central QTabWidget::pane {{
                border: 1px solid {COLOR_BORDE};
                border-radius: 4px;
                background: {COLOR_CARD};
            }}
            #central QTabBar::tab {{
                background: {COLOR_FONDO};
                border: 1px solid {COLOR_BORDE};
                padding: 8px 16px;
                color: {COLOR_TEXTO_SEC};
            }}
            #central QTabBar::tab:selected {{
                background: {COLOR_CARD};
                border-bottom-color: {COLOR_ACENTO};
                color: {COLOR_ACENTO};
            }}
            #central QTabBar::tab:hover {{
                color: {COLOR_ACENTO};
            }}
            #central QGroupBox {{
                border: 1px solid {COLOR_BORDE};
                border-radius: 6px;
                margin-top: 8px;
            }}
            #central QGroupBox::title {{
                subcontrol-origin: margin;
                left: 10px;
                color: {COLOR_PRIMARIO};
                font-size: 11px;
                font-weight: 500;
            }}
            #central QListWidget {{
                background: {COLOR_CARD};
                border: 1px solid {COLOR_BORDE};
                border-radius: 4px;
                color: {COLOR_TEXTO};
                font-size: 12px;
            }}
            #central QListWidget::item {{
                padding: 8px;
            }}
            #central QListWidget::item:hover {{
                background: rgba(236,91,19,0.06);
            }}
            #central QListWidget::item:selected {{
                background: rgba(236,91,19,0.16);
                color: {COLOR_PRIMARIO};
            }}
            #central QSpinBox::up-button, #central QSpinBox::down-button {{
                background: {COLOR_FONDO};
                border: none;
                width: 16px;
            }}
            #central QSpinBox::up-button:hover, #central QSpinBox::down-button:hover {{
                background: rgba(236,91,19,0.08);
            }}
        """)

        layout = QVBoxLayout(self.central)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        titulo = QLabel("Nueva vista" if self.modo == "crear" else "Editar vista")
        titulo.setStyleSheet(f"color: {COLOR_PRIMARIO}; font-size: 14px; font-weight: bold; border: none;")
        layout.addWidget(titulo)

        layout.addWidget(QLabel("Nombre:"))
        self.input_nombre = QLineEdit()
        self.input_nombre.setPlaceholderText("Ej: Oficina Principal")
        layout.addWidget(self.input_nombre)

        # Tabs: Grid normal | Videowall
        self.tabs_tipo = QTabWidget()
        self.tabs_tipo.setStyleSheet(f"""
            QTabWidget::pane {{ border: 1px solid {COLOR_BORDE}; border-radius: 4px; background: {COLOR_CARD}; }}
            QTabBar::tab {{ background: {COLOR_FONDO}; border: 1px solid {COLOR_BORDE}; padding: 8px 16px; color: {COLOR_TEXTO_SEC}; }}
            QTabBar::tab:selected {{ background: {COLOR_CARD}; border-bottom-color: {COLOR_ACENTO}; color: {COLOR_ACENTO}; }}
            QTabBar::tab:hover {{ color: {COLOR_ACENTO}; }}
        """)

        # TAB 1: Grid Normal (existente) - side-by-side original
        tab_grid = QWidget()
        tab_grid_layout = QHBoxLayout(tab_grid)
        tab_grid_layout.setContentsMargins(8, 8, 8, 8)
        tab_grid_layout.setSpacing(12)

        # Izquierda: lista de cámaras
        col_izq = QVBoxLayout()
        col_izq.addWidget(QLabel("Cámaras disponibles:"))
        self.lista_camaras = ListaCamarasArrastrable()
        self.lista_camaras.setStyleSheet(f"""
            QListWidget {{ background: {COLOR_CARD}; border: 1px solid {COLOR_BORDE}; border-radius: 4px; color: {COLOR_TEXTO}; font-size: 12px; }}
            QListWidget::item {{ padding: 8px; }}
            QListWidget::item:hover {{ background: rgba(236,91,19,0.06); }}
            QListWidget::item:selected {{ background: rgba(236,91,19,0.16); color: {COLOR_PRIMARIO}; }}
        """)
        col_izq.addWidget(self.lista_camaras)

        # Derecha: Grid Editor
        col_der = QVBoxLayout()
        col_der.addWidget(QLabel("Grid:"))
        self.combo_grid = QComboBox()
        for layout_id, nombre in nombres_para_combo():
            self.combo_grid.addItem(nombre, layout_id)
        self.combo_grid.currentIndexChanged.connect(self._on_grid_cambiado)
        col_der.addWidget(self.combo_grid)

        self.grid_editor = GridEditorWidget()
        self.grid_editor.set_camaras_disponibles(self.camaras)
        self.grid_editor.asignaciones_cambiaron.connect(self._actualizar_lista_camaras)
        col_der.addWidget(self.grid_editor, 1)

        tab_grid_layout = QHBoxLayout()
        tab_grid_layout.setContentsMargins(8, 8, 8, 8)
        tab_grid_layout.setSpacing(12)
        tab_grid_layout.addLayout(col_izq, 1)
        tab_grid_layout.addLayout(col_der, 2)

        tab_grid_layout.addWidget(self.grid_editor, 1)

        tab_grid_layout.addWidget(self.grid_editor, 1)

        tab_grid.setLayout(tab_grid_layout)
        self.tabs_tipo.addTab(tab_grid, "Grid normal")

        # TAB 2: Videowall (nuevo)
        tab_videowall = self._crear_tab_videowall()
        self.tabs_tipo.addTab(tab_videowall, "Videowall")

        layout.addWidget(self.tabs_tipo, 1)

        botones = QHBoxLayout()
        self.btn_autocompletar = QPushButton("Autocompletar")
        self.btn_autocompletar.clicked.connect(self._on_autocompletar)
        botones.addWidget(self.btn_autocompletar)

        self.btn_limpiar = QPushButton("Limpiar grilla")
        self.btn_limpiar.clicked.connect(self._on_limpiar)
        botones.addWidget(self.btn_limpiar)

        self.check_predeterminada = QCheckBox("Marcar como predeterminada")
        self.check_predeterminada.setStyleSheet(f"""
            QCheckBox {{ color: {COLOR_TEXTO_SEC}; font-size: 12px; }}
            QCheckBox::indicator {{ width: 14px; height: 14px; }}
        """)
        botones.addWidget(self.check_predeterminada)

        botones.addStretch()

        self.btn_cancelar = QPushButton("Cancelar")
        self.btn_cancelar.clicked.connect(self.reject)
        self.btn_cancelar.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {COLOR_PRIMARIO}; border: 1px solid {COLOR_BORDE}; border-radius: 6px; font-size: 12px; font-weight: 500; padding: 8px 16px; }}
            QPushButton:hover {{ background: rgba(236,91,19,0.08); border-color: {COLOR_ACENTO}; }}
        """)
        botones.addWidget(self.btn_cancelar)

        self.btn_aceptar = QPushButton("Crear" if self.modo == "crear" else "Guardar cambios")
        self.btn_aceptar.setStyleSheet(f"""
            QPushButton {{ background: {COLOR_ACENTO}; color: white; border: none; border-radius: 6px; padding: 8px 16px; font-weight: bold; font-size: 12px; }}
            QPushButton:hover {{ background: {COLOR_ACENTO_HOVER}; }}
        """)
        self.btn_aceptar.clicked.connect(self._on_aceptar)
        botones.addWidget(self.btn_aceptar)

        layout.addLayout(botones)

        self._llenar_lista_camaras()
        self.grid_editor.construir(self.combo_grid.currentData())
        
        # Conectar cambio de tab para actualizar grid_editor activo
        self.tabs_tipo.currentChanged.connect(self._on_tab_changed)

    def _crear_tab_videowall(self):
        """Crea el tab Videowall con Wizard 3 pasos (QStackedWidget)"""
        w = QWidget()
        l = QVBoxLayout(w)
        l.setContentsMargins(8, 8, 8, 8)
        l.setSpacing(0)

        # ---- Progress bar ----
        self.vw_progress = QProgressBar()
        self.vw_progress.setFixedHeight(4)
        self.vw_progress.setTextVisible(False)
        self.vw_progress.setStyleSheet(f"""
            QProgressBar {{ background: {COLOR_BORDE}; border-radius: 2px; }}
            QProgressBar::chunk {{ background: {COLOR_ACENTO}; border-radius: 2px; }}
        """)
        self.vw_progress.setValue(33)
        l.addWidget(self.vw_progress)

        # ---- StackedWidget para los 3 pasos ----
        self.vw_stack = QStackedWidget()
        l.addWidget(self.vw_stack, 1)

        # ========================================
        # PASO 1: Monitor Wall (Físico)
        # ========================================
        paso1 = QWidget()
        p1_layout = QVBoxLayout(paso1)
        p1_layout.setContentsMargins(8, 16, 8, 8)
        p1_layout.setSpacing(16)

        # Título del paso
        lbl_paso1 = QLabel("1️⃣ Monitor Wall (Físico)")
        lbl_paso1.setStyleSheet(f"color: {COLOR_PRIMARIO}; font-size: 14px; font-weight: bold;")
        p1_layout.addWidget(lbl_paso1)

        # Nombre
        p1_layout.addWidget(QLabel("Nombre:"))
        self.vw_input_nombre = QLineEdit()
        self.vw_input_nombre.setPlaceholderText("Ej: Videowall Sala Control")
        p1_layout.addWidget(self.vw_input_nombre)

        # Detectar monitores
        btn_detectar = QPushButton("Detectar monitores")
        btn_detectar.setStyleSheet(f"""
            QPushButton {{ background: {COLOR_FONDO}; color: {COLOR_PRIMARIO}; border: 1px solid {COLOR_BORDE}; border-radius: 6px; padding: 10px; font-weight: 500; }}
            QPushButton:hover {{ background: rgba(236,91,19,0.08); border-color: {COLOR_ACENTO}; }}
        """)
        btn_detectar.clicked.connect(self._vw_detectar_monitores)
        p1_layout.addWidget(btn_detectar)

        # Lista de monitores detectados
        self.vw_monitor_list = QListWidget()
        self.vw_monitor_list.setStyleSheet(f"""
            QListWidget {{ background: {COLOR_CARD}; border: 1px solid {COLOR_BORDE}; border-radius: 4px; color: {COLOR_TEXTO}; font-size: 12px; }}
            QListWidget::item {{ padding: 8px; }}
        """)
        self.vw_monitor_list.setMaximumHeight(160)
        p1_layout.addWidget(QLabel("Monitores detectados:"))
        p1_layout.addWidget(self.vw_monitor_list)

        # Disposición
        disp_layout = QHBoxLayout()
        disp_layout.addWidget(QLabel("Disposición:"))
        self.vw_combo_disposicion = QComboBox()
        self.vw_combo_disposicion.addItems(["2×2", "3×2", "2×3", "4×1", "1×4", "Personalizado..."])
        self.vw_combo_disposicion.currentIndexChanged.connect(self._vw_on_disposicion_changed)
        disp_layout.addWidget(self.vw_combo_disposicion)
        disp_layout.addStretch()
        p1_layout.addLayout(disp_layout)

        # Preview visual del wall
        self.vw_wall_preview = QWidget()
        self.vw_wall_preview.setFixedHeight(120)
        self.vw_wall_preview.setStyleSheet(f"background: {COLOR_CARD}; border: 1px solid {COLOR_BORDE}; border-radius: 6px;")
        p1_layout.addWidget(QLabel("Vista previa del wall:"))
        p1_layout.addWidget(self.vw_wall_preview)

        # Bezels
        bezels_layout = QHBoxLayout()
        bezels_layout.addWidget(QLabel("Compensación bezels (mm):"))
        self.sp_bezel = QSpinBox()
        self.sp_bezel.setRange(0, 50)
        self.sp_bezel.setValue(0)
        self.sp_bezel.setFixedWidth(60)
        bezels_layout.addWidget(self.sp_bezel)
        bezels_layout.addStretch()
        p1_layout.addLayout(bezels_layout)

        p1_layout.addStretch()
        self.vw_stack.addWidget(paso1)

        # ========================================
        # PASO 2: Grid Lógico (Cámaras) - MISMO LAYOUT QUE GRID NORMAL
        # ========================================
        paso2 = QWidget()
        p2_layout = QHBoxLayout(paso2)
        p2_layout.setContentsMargins(8, 8, 8, 8)
        p2_layout.setSpacing(12)

        # IZQUIERDA: Cámaras disponibles (COMPARTIDA)
        col_izq = QVBoxLayout()
        col_izq.addWidget(QLabel("Cámaras disponibles:"))
        self.vw_lista_camaras = ListaCamarasArrastrable()
        self.vw_lista_camaras.setStyleSheet(f"""
            QListWidget {{ background: {COLOR_CARD}; border: 1px solid {COLOR_BORDE}; border-radius: 4px; color: {COLOR_TEXTO}; font-size: 12px; }}
            QListWidget::item {{ padding: 8px; }}
            QListWidget::item:hover {{ background: rgba(236,91,19,0.06); }}
            QListWidget::item:selected {{ background: rgba(236,91,19,0.16); color: {COLOR_PRIMARIO}; }}
        """)
        col_izq.addWidget(self.vw_lista_camaras)

        # DERECHA: Grid Lógico + Controles
        col_der = QVBoxLayout()
        
        # Controles de grid
        grid_ctrl = QHBoxLayout()
        grid_ctrl.addWidget(QLabel("Filas:"))
        self.vw_sp_filas = QSpinBox()
        self.vw_sp_filas.setRange(1, 8)
        self.vw_sp_filas.setValue(5)
        self.vw_sp_filas.setFixedWidth(60)
        self.vw_sp_filas.valueChanged.connect(self._vw_on_grid_dim_changed)
        grid_ctrl.addWidget(self.vw_sp_filas)

        grid_ctrl.addWidget(QLabel("Columnas:"))
        self.vw_sp_cols = QSpinBox()
        self.vw_sp_cols.setRange(1, 8)
        self.vw_sp_cols.setValue(6)
        self.vw_sp_cols.setFixedWidth(60)
        self.vw_sp_cols.valueChanged.connect(self._vw_on_grid_dim_changed)
        grid_ctrl.addWidget(self.vw_sp_cols)

        grid_ctrl.addStretch()
        self.lbl_vw_grid_info = QLabel("5×6 = 30 celdas")
        self.lbl_vw_grid_info.setStyleSheet(f"color: {COLOR_ACENTO}; font-size: 11px; font-weight: bold; min-width: 120px;")
        grid_ctrl.addWidget(self.lbl_vw_grid_info)
        col_der.addLayout(grid_ctrl)

        # Grid Editor Videowall (side-by-side 2×2 por monitor)
        self.grid_editor_vw = GridEditorWidget()
        self.grid_editor_vw.set_camaras_disponibles(self.camaras)
        self.grid_editor_vw.asignaciones_cambiaron.connect(self._actualizar_lista_camaras_vw)
        col_der.addWidget(self.grid_editor_vw, 1)

        p2_layout.addLayout(col_izq, 1)
        p2_layout.addLayout(col_der, 2)

        self.vw_stack.addWidget(paso2)

        # ========================================
        # PASO 3: Confirmación y Mapeo
        # ========================================
        paso3 = QWidget()
        p3_layout = QVBoxLayout(paso3)
        p3_layout.setContentsMargins(8, 16, 8, 8)
        p3_layout.setSpacing(16)

        lbl_paso3 = QLabel("3️⃣ Confirmación y Mapeo")
        lbl_paso3.setStyleSheet(f"color: {COLOR_PRIMARIO}; font-size: 14px; font-weight: bold;")
        p3_layout.addWidget(lbl_paso3)

        # Resumen
        self.vw_lbl_resumen = QLabel()
        self.vw_lbl_resumen.setWordWrap(True)
        self.vw_lbl_resumen.setStyleSheet(f"color: {COLOR_TEXTO}; font-size: 12px; background: {COLOR_FONDO}; border: 1px solid {COLOR_BORDE}; border-radius: 6px; padding: 16px;")
        p3_layout.addWidget(self.vw_lbl_resumen)

        # Mapeo
        map_layout = QHBoxLayout()
        self.vw_chk_mapeo_auto = QCheckBox("Mapeo automático (geometría/EDID)")
        self.vw_chk_mapeo_auto.setChecked(True)
        map_layout.addWidget(self.vw_chk_mapeo_auto)
        map_layout.addStretch()
        p3_layout.addLayout(map_layout)

        p3_layout.addStretch()
        self.vw_stack.addWidget(paso3)

        # ---- Botones de navegación del wizard ----
        self.vw_nav = QHBoxLayout()
        self.vw_nav.addStretch()

        self.vw_btn_back = QPushButton("Atrás")
        self.vw_btn_back.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {COLOR_PRIMARIO}; border: 1px solid {COLOR_BORDE}; border-radius: 6px; font-size: 12px; font-weight: 500; padding: 8px 16px; }}
            QPushButton:hover {{ background: rgba(236,91,19,0.08); border-color: {COLOR_ACENTO}; }}
        """)
        self.vw_btn_back.clicked.connect(self._vw_go_back)
        self.vw_nav.addWidget(self.vw_btn_back)

        self.vw_btn_next = QPushButton("Siguiente >")
        self.vw_btn_next.setStyleSheet(f"""
            QPushButton {{ background: {COLOR_ACENTO}; color: white; border: none; border-radius: 6px; padding: 8px 16px; font-weight: bold; font-size: 12px; }}
            QPushButton:hover {{ background: {COLOR_ACENTO_HOVER}; }}
        """)
        self.vw_btn_next.clicked.connect(self._vw_go_next)
        self.vw_nav.addWidget(self.vw_btn_next)

        self.vw_btn_guardar = QPushButton("Guardar")
        self.vw_btn_guardar.setStyleSheet(f"""
            QPushButton {{ background: {COLOR_ACENTO}; color: white; border: none; border-radius: 6px; padding: 8px 16px; font-weight: bold; font-size: 12px; }}
            QPushButton:hover {{ background: {COLOR_ACENTO_HOVER}; }}
        """)
        self.vw_btn_guardar.clicked.connect(self._vw_guardar)
        self.vw_btn_guardar.setVisible(False)
        self.vw_nav.addWidget(self.vw_btn_guardar)

        self.vw_btn_vivo = QPushButton("Abrir en Vivo ▶")
        self.vw_btn_vivo.setStyleSheet(f"""
            QPushButton {{ background: {COLOR_VERDE}; color: white; border: none; border-radius: 6px; padding: 8px 16px; font-weight: bold; font-size: 12px; }}
            QPushButton:hover {{ background: #27ae60; }}
        """)
        self.vw_btn_vivo.clicked.connect(self._vw_abrir_vivo)
        self.vw_btn_vivo.setVisible(False)
        self.vw_nav.addWidget(self.vw_btn_vivo)

        l.addLayout(self.vw_nav)

        return w

    def _on_tab_changed(self, index):
        """Cambia el grid_editor activo según el tab"""
        if index == 0:  # Grid normal
            self._grid_editor_activo = self.grid_editor
        else:  # Videowall
            self._grid_editor_activo = self.grid_editor_vw
        # Actualizar lista de cámaras con el editor activo
        self._actualizar_lista_camaras()

    def _on_plantilla_changed(self, idx):
        """Sincroniza preview + grid editor según plantilla seleccionada"""
        plantillas = [
            ("2_monitores_lado", 2, 4),      # 2×4 = 8
            ("3_monitores_horizontal", 3, 3), # 3×3 = 9
            ("2x2_monitores", 4, 4),          # 4×4 = 16
            ("personalizado", None, None),    # muestra Filas/Cols
        ]
        key, filas, cols = plantillas[idx]
        
        # Preview mini
        self.vw_preview.set_plantilla(key)
        if filas and cols:
            self.lbl_vw_info.setText(f"{filas}×{cols} = {filas*cols} celdas")
            self.grid_editor_vw.construir_videowall(filas, cols)
        
        # Mostrar/ocultar Filas/Cols solo si Personalizado
        is_custom = (key == "personalizado")
        self.sp_filas.setVisible(is_custom)
        self.sp_cols.setVisible(is_custom)
        
        if is_custom:
            self.sp_filas.setValue(4)
            self.sp_cols.setValue(4)
            self.vw_preview.set_custom_dims(4, 4)
            self.grid_editor_vw.construir_videowall(4, 4)
            self.lbl_vw_info.setText("4×4 = 16 celdas")

    def _on_custom_dim_changed(self):
        """Actualiza preview y grid al cambiar filas/cols en modo personalizado"""
        filas = self.sp_filas.value()
        cols = self.sp_cols.value()
        self.vw_preview.set_custom_dims(filas, cols)
        self.grid_editor_vw.construir_videowall(filas, cols)
        self.lbl_vw_info.setText(f"{filas}×{cols} = {filas*cols} celdas")

    def _actualizar_lista_camaras_vw(self):
        """Actualiza lista de cámaras cuando cambia el grid videowall"""
        if self.tabs_tipo.currentIndex() == 1:  # Solo si tab Videowall activo
            self._actualizar_lista_camaras()

    def _llenar_lista_camaras(self):
        self.lista_camaras.clear()
        for cam in self.camaras:
            item = QListWidgetItem(cam.get("nombre", cam["id"]))
            item.setData(Qt.ItemDataRole.UserRole, cam["id"])
            self.lista_camaras.addItem(item)
        self._actualizar_lista_camaras()

    def _actualizar_lista_camaras(self):
        """Marca con un tilde las cámaras que ya están asignadas a algún
        slot, sin sacarlas de la lista (el usuario puede reasignarlas)."""
        editor = getattr(self, '_grid_editor_activo', self.grid_editor)
        asignadas = set(editor.asignaciones.camaras_asignadas())
        for i in range(self.lista_camaras.count()):
            item = self.lista_camaras.item(i)
            camera_id = item.data(Qt.ItemDataRole.UserRole)
            nombre_base = next((c.get("nombre", camera_id) for c in self.camaras if c["id"] == camera_id), camera_id)
            if camera_id in asignadas:
                item.setText(f"✓ {nombre_base}")
                item.setForeground(QColor("#5a6a7d"))
            else:
                item.setText(nombre_base)
                item.setForeground(QColor("#e8f4fc"))

    def _on_grid_cambiado(self):
        nuevo_layout_id = self.combo_grid.currentData()
        self.grid_editor.construir(nuevo_layout_id)
        self._actualizar_lista_camaras()

    def _on_autocompletar(self):
        self.grid_editor.autocompletar()

    def _on_limpiar(self):
        self.grid_editor.limpiar_todo()

    def _precargar_si_corresponde(self):
        if self.modo != "editar" or not self.vista_existente:
            return

        self.input_nombre.setText(self.vista_existente.get("nombre", ""))

        grid_id = self.vista_existente.get("grid_id", "2x2")
        idx = self.combo_grid.findData(grid_id)
        if idx >= 0:
            self.combo_grid.setCurrentIndex(idx)

        asignaciones = self.vista_existente.get("asignaciones", {})
        self.grid_editor.construir(grid_id, asignaciones_previas=asignaciones)
        self._actualizar_lista_camaras()

        self.check_predeterminada.setChecked(bool(self.vista_existente.get("predeterminada", False)))

    NOMBRE_RESERVADO_SISTEMA = "Vista General"  # debe coincidir con DashboardVigilancia.VISTA_GENERAL_NOMBRE

    def _on_aceptar(self):
        nombre = self.input_nombre.text().strip()
        if not nombre:
            QMessageBox.warning(self, "Falta el nombre", "Poné un nombre para la vista antes de guardar.")
            return

        if nombre == self.NOMBRE_RESERVADO_SISTEMA:
            QMessageBox.warning(
                self, "Nombre reservado",
                f'"{self.NOMBRE_RESERVADO_SISTEMA}" es un nombre reservado del sistema. Elegí otro nombre.'
            )
            return

        if not self.grid_editor.asignaciones.camaras_asignadas():
            respuesta = QMessageBox.question(
                self, "Vista vacía",
                "No asignaste ninguna cámara a la grilla. ¿Guardar igual?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if respuesta != QMessageBox.StandardButton.Yes:
                return

        self.accept()

    def obtener_datos(self) -> dict:
        """Llamar después de exec() == Accepted."""
        # Determinar qué tab está activo y devolver datos correspondientes
        tab_idx = self.tabs_tipo.currentIndex()
        
        base = {
            "nombre": self.input_nombre.text().strip(),
            "predeterminada": self.check_predeterminada.isChecked(),
        }
        
        if tab_idx == 0:  # Grid normal
            base.update({
                "tipo_vista": "grid_normal",
                "grid_id": self.combo_grid.currentData(),
                "asignaciones": self.grid_editor.asignaciones.to_dict(),
            })
        else:  # Videowall
            base.update({
                "tipo_vista": "videowall",
                "grid_filas": self.vw_sp_filas.value(),
                "grid_cols": self.vw_sp_cols.value(),
                "plantilla": self.vw_combo_disposicion.currentText(),
                "mapeo_auto": self.vw_chk_mapeo_auto.isChecked(),
                "asignaciones": self.grid_editor_vw.asignaciones.to_dict(),
            })
        
        return base

    # ============================================================
    # VIDEOWALL WIZARD HANDLERS
    # ============================================================

    def _vw_detectar_monitores(self):
        """Detecta monitores usando ScreenManager"""
        from monitores import get_screen_manager
        from PySide6.QtCore import QTimer
        sm = get_screen_manager()
        monitores = sm.refresh()
        
        self.vw_monitor_list.clear()
        for m in monitores:
            primario = " ● PRIMARIO" if m.is_primary else ""
            item_text = f"{m.display_name} - {m.geometry.width()}x{m.geometry.height()} @ ({m.geometry.x()},{m.geometry.y()}){primario}"
            item = QListWidgetItem(item_text)
            item.setData(Qt.ItemDataRole.UserRole, m.key)
            self.vw_monitor_list.addItem(item)
        
        # Actualizar preview del wall
        self._vw_actualizar_wall_preview()

    def _vw_on_disposicion_changed(self, idx):
        """Actualiza preview al cambiar disposición"""
        self._vw_actualizar_wall_preview()

    def _vw_actualizar_wall_preview(self):
        """Dibuja preview visual del wall en self.vw_wall_preview"""
        QTimer.singleShot(0, self._vw_pintar_wall_preview)

    def _vw_pintar_wall_preview(self):
        """Pinta la disposición de monitores en el widget preview"""
        if not self.vw_wall_preview or self.vw_wall_preview.width() <= 1:
            return
            
        from PySide6.QtGui import QPainter, QColor, QPen, QFont, QPixmap
        from PySide6.QtCore import Qt, QRectF
        
        w = self.vw_wall_preview.width()
        h = self.vw_wall_preview.height()
        
        # Crear pixmap y pintar
        pixmap = QPixmap(w, h)
        pixmap.fill(QColor(COLOR_CARD))
        
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Obtener disposición
        disp_texto = self.vw_combo_disposicion.currentText()
        filas_wall, cols_wall = 2, 2
        if "3×2" in disp_texto:
            filas_wall, cols_wall = 2, 3
        elif "2×3" in disp_texto:
            filas_wall, cols_wall = 3, 2
        elif "4×1" in disp_texto:
            filas_wall, cols_wall = 1, 4
        elif "1×4" in disp_texto:
            filas_wall, cols_wall = 4, 1
        
        margin = 8
        cell_w = max(1, (w - margin * (cols_wall + 1)) / cols_wall)
        cell_h = max(1, (h - margin * (filas_wall + 1)) / filas_wall)
        
        for r in range(filas_wall):
            for c in range(cols_wall):
                x = margin + c * (cell_w + margin)
                y = margin + r * (cell_h + margin)
                rect = QRectF(x, y, cell_w, cell_h)
                
                idx = r * cols_wall + c
                color = QColor(COLOR_ACENTO) if idx % 2 == 0 else QColor(COLOR_PRIMARIO)
                painter.fillRect(rect, color)
                painter.setPen(QPen(QColor(COLOR_BORDE), 2))
                painter.drawRect(rect)
                
                # Label
                painter.setPen(QColor(COLOR_TEXTO))
                painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
                painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"M{idx + 1}")
        
        painter.end()
        # Asignar como background
        self.vw_wall_preview.setAutoFillBackground(True)
        palette = self.vw_wall_preview.palette()
        palette.setBrush(self.vw_wall_preview.backgroundRole(), pixmap)
        self.vw_wall_preview.setPalette(palette)

    def _vw_on_disposicion_changed(self, idx):
        """Al cambiar disposición, actualiza preview"""
        self._vw_actualizar_wall_preview()

    def _vw_on_grid_dim_changed(self):
        """Actualiza grid lógico al cambiar filas/columnas"""
        filas = self.vw_sp_filas.value()
        cols = self.vw_sp_cols.value()
        self.lbl_vw_grid_info.setText(f"{filas}×{cols} = {filas*cols} celdas")
        self.grid_editor_vw.construir_videowall(filas, cols)

    def _vw_go_back(self):
        """Navega al paso anterior"""
        current = self.vw_stack.currentIndex()
        if current > 0:
            self.vw_stack.setCurrentIndex(current - 1)
            self.vw_progress.setValue(33 * (current))
            self._vw_actualizar_botones_nav()

    def _vw_go_next(self):
        """Avanza al siguiente paso"""
        current = self.vw_stack.currentIndex()
        if current < 2:
            # Validar paso actual antes de avanzar
            if current == 0 and not self._vw_validar_paso1():
                return
            if current == 1 and not self._vw_validar_paso2():
                return
            self.vw_stack.setCurrentIndex(current + 1)
            self.vw_progress.setValue(33 * (current + 2))
            self._vw_actualizar_botones_nav()
            # Si vamos a paso 3, actualizar resumen
            if current == 1:
                self._vw_actualizar_resumen()

    def _vw_validar_paso1(self):
        """Valida paso 1: al menos un monitor detectado"""
        if self.vw_monitor_list.count() == 0:
            QMessageBox.warning(self, "Faltan monitores", "Detecte monitores antes de continuar.")
            return False
        return True

    def _vw_validar_paso2(self):
        """Valida paso 2: al menos una cámara asignada"""
        if not self.grid_editor_vw.asignaciones.camaras_asignadas():
            respuesta = QMessageBox.question(
                self, "Grid vacío",
                "No hay cámaras asignadas al grid lógico. ¿Continuar igual?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            return respuesta == QMessageBox.StandardButton.Yes
        return True

    def _vw_actualizar_botones_nav(self):
        """Actualiza visibilidad de botones según paso actual"""
        current = self.vw_stack.currentIndex()
        self.vw_btn_back.setVisible(current > 0)
        self.vw_btn_next.setVisible(current < 2)
        self.vw_btn_guardar.setVisible(current == 2)
        self.vw_btn_vivo.setVisible(current == 2)

    def _vw_actualizar_resumen(self):
        """Actualiza el resumen en paso 3"""
        filas = self.vw_sp_filas.value()
        cols = self.vw_sp_cols.value()
        total = filas * cols
        monitores = self.vw_monitor_list.count()
        disp = self.vw_combo_disposicion.currentText()
        
        texto = f"""
<b>Resumen del Videowall:</b><br>
• Nombre: {self.vw_input_nombre.text() or '(sin nombre)'}<br>
• Monitor Wall: {disp} ({monitores} monitores)<br>
• Grid Lógico: {filas}×{cols} = {total} celdas<br>
• Mapeo: {'Automático (geometría/EDID)' if self.vw_chk_mapeo_auto.isChecked() else 'Manual'}<br>
• Cámaras asignadas: {len(self.grid_editor_vw.asignaciones.camaras_asignadas())}
        """
        self.vw_lbl_resumen.setText(texto.strip())

    def _vw_guardar(self):
        """Guarda el videowall y cierra diálogo aceptando"""
        # Sincronizar nombre del input del wizard al input principal
        if self.vw_input_nombre.text().strip():
            self.input_nombre.setText(self.vw_input_nombre.text().strip())
        # Forzar tab actual a videowall para que obtener_datos() tome los datos correctos
        self.tabs_tipo.setCurrentIndex(1)
        self.accept()

    def _vw_abrir_vivo(self):
        """Guarda y abre en vivo"""
        self._vw_guardar()
        # El dashboard manejará esto al recibir accept()
