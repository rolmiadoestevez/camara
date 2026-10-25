# ui_dashboard.py - CON BOTÓN VISTA EN VIVO Y DISPOSITIVOS
#
# ⭐ MULTI-LENTE (2025):
# - 1 widget por worker_id (lente). Sin panorámicos.
# - _reconstruir() usa worker_id.
# - obtener_widget_por_worker_id() es la única búsqueda.
#
# ⭐ Sin WidgetCamaraPseudoPanoramica: cada lente es independiente.

from PySide6.QtWidgets import *
from PySide6.QtCore import *
from PySide6.QtGui import *
from PySide6.QtSvg import QSvgRenderer
import os
import cv2
import numpy as np
import time

from cuadriculas import obtener_layout, nombres_para_combo


# ============================================================
# ========== WIDGETS VISUALES (SIN LÓGICA DE NEGOCIO) =======
# ============================================================

class OverlayInfoTransparente(QWidget):
    """SOLO VISUAL - Muestra información superpuesta"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        self.setFixedSize(220, 80)
        self.resolucion = ""
        self.fps = 0
        self.stream_type = ""
        self.camera_name = ""
        self.fps_real = 0
        self.dragging = False
        self.drag_position = QPoint()
        self.update_timer = QTimer()
        self.update_timer.timeout.connect(self.update)
        self.update_timer.start(1000)
        self.setMouseTracking(True)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QBrush(QColor(0, 0, 0, 204)))
        painter.setPen(QPen(QColor(51, 65, 85), 1))
        painter.drawRoundedRect(0, 0, self.width(), self.height(), 4, 4)
        painter.setPen(QColor(255, 255, 255))
        font = QFont("Monospace")
        font.setPixelSize(10)
        painter.setFont(font)
        info_lines = [
            f"Cámara: {self.camera_name}",
            f"Resolucion: {self.resolucion}",
            f"FPS: {self.fps_real} (Config: {self.fps}) | {self.stream_type}"
        ]
        y_pos = 20
        for line in info_lines:
            painter.drawText(10, y_pos, line)
            y_pos += 20

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = True
            self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event):
        if self.dragging and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = False
            event.accept()

    def actualizar_info(self, resolucion="", fps=0, stream_type="", camera_name="", fps_real=0):
        if resolucion: self.resolucion = resolucion
        if fps: self.fps = fps
        if stream_type: self.stream_type = stream_type
        if camera_name: self.camera_name = camera_name
        if fps_real: self.fps_real = fps_real
        self.update()


class WidgetCamaraVacia(QFrame):
    """SOLO VISUAL - Widget para celda sin cámara"""
    def __init__(self, numero_celda):
        super().__init__()
        self.numero_celda = numero_celda
        self.initUI()

    def initUI(self):
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumSize(180, 135)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.label_vacia = QLabel()
        self.label_vacia.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label_vacia.setStyleSheet("background-color: #0f172a;")

        icon_svg = '''
        <svg width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="#334155" stroke-width="2" opacity="0.2">
            <path d="m16 13 5.223 3.482a.5.5 0 0 0 .777-.416V7.934a.5.5 0 0 0-.777-.416L16 11"/>
            <rect x="2" y="6" width="14" height="12" rx="2"/>
            <line x1="2" y1="2" x2="22" y2="22"/>
        </svg>
        '''

        svg_bytes = icon_svg.encode('utf-8')
        renderer = QSvgRenderer(svg_bytes)
        pixmap = QPixmap(100, 100)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        self.label_vacia.setPixmap(pixmap)
        layout.addWidget(self.label_vacia)

        self.texto_vacio = QLabel("Sin cámara", self)
        self.texto_vacio.setStyleSheet("color: #4a5a6e; font-size: 12px; font-weight: bold; background: transparent;")

        self.setMouseTracking(True)
        self.actualizar_estilo()

    def actualizar_estilo(self):
        self.setStyleSheet("""
            WidgetCamaraVacia {
                background-color: #0f172a;
                border: 0.5px solid #1e293b;
                border-radius: 0px;
            }
            WidgetCamaraVacia:hover {
                border: 2px solid #ec5b13;
            }
        """)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'texto_vacio'):
            self.texto_vacio.move((self.width() - self.texto_vacio.width()) // 2, self.height() - 35)


class WidgetCamaraIndividual(QFrame):
    """SOLO VISUAL - Widget para una cámara que OCUPA TODO EL ESPACIO"""
    senal_seleccionada = Signal(str)
    senal_doble_click = Signal(object)
    senal_config = Signal(object)
    senal_eliminar = Signal(dict)
    senal_deshabilitar = Signal(dict)

    def __init__(self, camera_worker, parent_widget_camaras=None):
        super().__init__()
        self.camera_worker = camera_worker
        self.numero_camara = camera_worker.name
        self.is_seleccionada = False
        self.parent_widget_camaras = parent_widget_camaras
        self.info_widget = None
        self.estilo_original = ""
        self._alarma_visual_activa = False
        self.icono_alarma = None
        self._deshabilitada = False
        self.initUI()

        self.update_timer = QTimer(self)
        self.update_timer.timeout.connect(self.actualizar_frame_video)
        self.update_timer.start(33)

    def initUI(self):
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setMinimumSize(100, 75)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.video_label = QLabel()
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.video_label.setStyleSheet("background-color: #0f172a; color: #8a9bb5; border: none;")
        layout.addWidget(self.video_label)

        self.label_info = QLabel(f"Cam {self.numero_camara}", self.video_label)
        self.label_info.move(10, 10)
        self.label_info.setStyleSheet("""
            color: #e8f4fc;
            font-weight: bold;
            font-size: 12px;
            background-color: rgba(0, 0, 0, 0.7);
            border-radius: 4px;
            padding: 3px 8px;
        """)
        self.label_info.adjustSize()
        self.label_info.hide()

        self.crear_badge_estado()
        self.crear_iconos_flotantes()
        self.crear_icono_alarma()
        self.setMouseTracking(True)
        self.video_label.setMouseTracking(True)
        self.actualizar_estilo()
        self.estilo_original = self.styleSheet()

    def crear_icono_alarma(self):
        self.icono_alarma = QLabel(self.video_label)
        self.icono_alarma.setFixedSize(40, 40)
        self.icono_alarma.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icono_alarma.setStyleSheet("background: transparent; border: none;")
        self.icono_alarma.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)

        icon_path = os.path.join("iconos", "alarma.svg")
        if os.path.exists(icon_path):
            renderer = QSvgRenderer(icon_path)
            pixmap = QPixmap(40, 40)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            renderer.render(painter)
            painter.end()

            painter = QPainter(pixmap)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(pixmap.rect(), QColor("#EF4444"))
            painter.end()

            self.icono_alarma.setPixmap(pixmap)
        else:
            self.icono_alarma.setText("!")
            self.icono_alarma.setStyleSheet(
                "color: #EF4444; font-size: 28px; font-weight: bold; background: transparent;"
            )

        self.icono_alarma.hide()

    def crear_badge_estado(self):
        self.status_badge = QFrame(self.video_label)
        self.status_badge.setFixedSize(90, 24)
        self.status_badge.setStyleSheet("background-color: rgba(0, 0, 0, 0.7); border-radius: 4px;")
        badge_layout = QHBoxLayout(self.status_badge)
        badge_layout.setContentsMargins(8, 4, 8, 4)

        self.circulo_estado = QFrame()
        self.circulo_estado.setFixedSize(8, 8)
        self.circulo_estado.setStyleSheet("background-color: #10b981; border-radius: 4px;")
        badge_layout.addWidget(self.circulo_estado)

        self.badge_texto = QLabel("LIVE")
        self.badge_texto.setStyleSheet("color: #cbd5e1; font-family: monospace; font-size: 11px; font-weight: bold; background: transparent;")
        badge_layout.addWidget(self.badge_texto)

        self.status_badge.show()
        self.status_badge.move(8, self.video_label.height() - 32)

    def _cargar_icono_disable(self, nombre_svg):
        icon_path = os.path.join("iconos", f"{nombre_svg}.svg")
        if os.path.exists(icon_path):
            renderer = QSvgRenderer(icon_path)
            pixmap = QPixmap(self.icon_size - 12, self.icon_size - 12)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            renderer.render(painter)
            painter.end()

            painter = QPainter(pixmap)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(pixmap.rect(), QColor("#ec5b13"))
            painter.end()

            return QIcon(pixmap)
        return None

    def crear_iconos_flotantes(self):
        self.icon_size = 40
        self.icon_spacing = 15

        self.boton_config = QPushButton(self.video_label)
        self.boton_config.setFixedSize(self.icon_size, self.icon_size)
        self.boton_config.setCursor(Qt.CursorShape.PointingHandCursor)
        self.boton_config.clicked.connect(self.on_config_clicked)
        self.boton_config.hide()

        config_icon_path = os.path.join("iconos", "settings.svg")
        if os.path.exists(config_icon_path):
            renderer = QSvgRenderer(config_icon_path)
            pixmap = QPixmap(self.icon_size - 12, self.icon_size - 12)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            renderer.render(painter)
            painter.end()

            painter = QPainter(pixmap)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(pixmap.rect(), QColor("#ec5b13"))
            painter.end()

            self.boton_config.setIcon(QIcon(pixmap))
            self.boton_config.setIconSize(QSize(self.icon_size - 12, self.icon_size - 12))
        else:
            self.boton_config.setText("?")

        self.boton_config.setStyleSheet(f"""
            QPushButton {{
                background-color: rgba(0, 0, 0, 0.6);
                border: none;
                border-radius: {self.icon_size//2}px;
            }}
            QPushButton:hover {{
                background-color: rgba(236, 91, 19, 0.5);
            }}
        """)

        self.boton_borrar = QPushButton(self.video_label)
        self.boton_borrar.setFixedSize(self.icon_size, self.icon_size)
        self.boton_borrar.setCursor(Qt.CursorShape.PointingHandCursor)
        self.boton_borrar.clicked.connect(self.on_borrar_clicked)
        self.boton_borrar.hide()

        icono_inicial = self._cargar_icono_disable("disable_off")
        if icono_inicial is None:
            icono_inicial = self._cargar_icono_disable("disable_on")
        if icono_inicial is None:
            delete_icon_path = os.path.join("iconos", "delete.svg")
            if os.path.exists(delete_icon_path):
                renderer = QSvgRenderer(delete_icon_path)
                pixmap = QPixmap(self.icon_size - 12, self.icon_size - 12)
                pixmap.fill(Qt.GlobalColor.transparent)
                painter = QPainter(pixmap)
                renderer.render(painter)
                painter.end()

                painter = QPainter(pixmap)
                painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
                painter.fillRect(pixmap.rect(), QColor("#ec5b13"))
                painter.end()
                icono_inicial = QIcon(pixmap)
            else:
                self.boton_borrar.setText("X")
                icono_inicial = None

        if icono_inicial is not None:
            self.boton_borrar.setIcon(icono_inicial)
            self.boton_borrar.setIconSize(QSize(self.icon_size - 12, self.icon_size - 12))

        self.boton_borrar.setToolTip("Deshabilitar de la vista")

        self.boton_borrar.setStyleSheet(f"""
            QPushButton {{
                background-color: rgba(0, 0, 0, 0.6);
                border: none;
                border-radius: {self.icon_size//2}px;
            }}
            QPushButton:hover {{
                background-color: rgba(236, 91, 19, 0.5);
            }}
        """)

    def on_borrar_clicked(self):
        nueva_deshabilitada = not self._deshabilitada

        self.senal_deshabilitar.emit({
            'camera_index': getattr(self.camera_worker, 'camera_index', ''),
            'widget': self,
            'deshabilitada': nueva_deshabilitada,
        })

        if nueva_deshabilitada:
            print(f"[WidgetCamara] {self.numero_camara} deshabilitada de la vista (local)")
        else:
            print(f"[WidgetCamara] {self.numero_camara} habilitada en la vista (local)")

    def set_deshabilitada(self, valor):
        self._deshabilitada = bool(valor)

        if self._deshabilitada:
            icono = self._cargar_icono_disable("disable_on")
            self.boton_borrar.setToolTip("Habilitar en la vista")
        else:
            icono = self._cargar_icono_disable("disable_off")
            self.boton_borrar.setToolTip("Deshabilitar de la vista")

        if icono is None:
            icono = self.boton_borrar.icon()

        self.boton_borrar.setIcon(icono)
        self.boton_borrar.setIconSize(QSize(self.icon_size - 12, self.icon_size - 12))

        if self._deshabilitada:
            self.actualizar_badge("DISABLED")
            self.mostrar_placeholder_deshabilitada()
        else:
            self.actualizar_badge("LIVE")

    def mostrar_placeholder_deshabilitada(self):
        try:
            label_width = self.video_label.width()
            label_height = self.video_label.height()

            if label_width <= 0 or label_height <= 0:
                return

            pixmap = QPixmap(label_width, label_height)
            pixmap.fill(QColor("#1e3a5f"))

            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)

            icon_svg = '''
            <svg width="100" height="100" viewBox="0 0 24 24" fill="none" stroke="#3b82f6" stroke-width="2" opacity="0.7">
                <path d="m16 13 5.223 3.482a.5.5 0 0 0 .777-.416V7.934a.5.5 0 0 0-.777-.416L16 11"/>
                <rect x="2" y="6" width="14" height="12" rx="2"/>
                <line x1="2" y1="2" x2="22" y2="22"/>
            </svg>
            '''

            svg_bytes = icon_svg.encode('utf-8')
            renderer = QSvgRenderer(svg_bytes)
            icon_size = min(100, label_width // 4, label_height // 4)
            icon_pixmap = QPixmap(icon_size, icon_size)
            icon_pixmap.fill(Qt.GlobalColor.transparent)
            icon_painter = QPainter(icon_pixmap)
            renderer.render(icon_painter)
            icon_painter.end()

            icon_x = (label_width - icon_size) // 2
            icon_y = (label_height - icon_size) // 2 - 20
            painter.drawPixmap(icon_x, icon_y, icon_pixmap)

            painter.setPen(QColor("#60a5fa"))
            font = QFont("Arial", 14, QFont.Weight.Bold)
            painter.setFont(font)
            text_rect = QRect(0, icon_y + icon_size + 10, label_width, 30)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignCenter, "Deshabilitada")

            painter.end()

            self.video_label.setPixmap(pixmap)

        except Exception:
            pass

    def on_config_clicked(self):
        self.senal_config.emit(self.camera_worker)

    def actualizar_badge(self, estado="LIVE"):
        if not hasattr(self, 'status_badge'):
            return

        if estado == "LIVE":
            self.circulo_estado.setStyleSheet("background-color: #10b981; border-radius: 4px;")
            self.badge_texto.setText("LIVE")
        elif estado == "DISABLED":
            self.circulo_estado.setStyleSheet("background-color: #64748b; border-radius: 4px;")
            self.badge_texto.setText("DISABLED")
        else:
            self.circulo_estado.setStyleSheet("background-color: #ef4444; border-radius: 4px;")
            self.badge_texto.setText("DESCONECTADO")

        self.status_badge.show()

    def actualizar_frame_video(self):
        try:
            if self._deshabilitada:
                return

            if (hasattr(self.camera_worker, 'connected') and self.camera_worker.connected and
                hasattr(self.camera_worker, 'display_frame') and self.camera_worker.display_frame is not None):

                frame = self.camera_worker.get_frame_copy()
                if frame is not None and frame.size > 0:
                    label_width = self.video_label.width()
                    label_height = self.video_label.height()

                    if label_width > 0 and label_height > 0:
                        frame_redimensionado = cv2.resize(frame, (label_width, label_height), interpolation=cv2.INTER_LINEAR)
                        frame_rgb = cv2.cvtColor(frame_redimensionado, cv2.COLOR_BGR2RGB)
                        h, w, ch = frame_rgb.shape
                        bytes_per_line = ch * w
                        q_img = QImage(frame_rgb.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
                        pixmap = QPixmap.fromImage(q_img)
                        self.video_label.setPixmap(pixmap)
                        self.actualizar_badge("LIVE")
            else:
                self.mostrar_imagen_sin_video()
                self.actualizar_badge("DESCONECTADO")
        except:
            pass

    def mostrar_imagen_sin_video(self):
        try:
            empty_pixmap = QPixmap(self.video_label.size())
            empty_pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(empty_pixmap)
            painter.setPen(QColor("#8a9bb5"))
            painter.setFont(QFont("Arial", 12))
            painter.drawText(empty_pixmap.rect(), Qt.AlignmentFlag.AlignCenter, "Sin señal")
            painter.end()
            self.video_label.setPixmap(empty_pixmap)
        except:
            pass

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self.parent_widget_camaras and hasattr(self.parent_widget_camaras, 'seleccionar_camara'):
                self.parent_widget_camaras.seleccionar_camara(self)
            else:
                self.is_seleccionada = True
                self.aplicar_efecto_seleccion()
            self.senal_seleccionada.emit(self.numero_camara)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.senal_doble_click.emit(self.camera_worker)
        super().mouseDoubleClickEvent(event)

    def aplicar_efecto_seleccion(self):
        self.setStyleSheet("border: 3px solid #4a90e2;")

    def deseleccionar(self):
        self.is_seleccionada = False
        self.setGraphicsEffect(None)
        self.actualizar_estilo()

    def actualizar_estilo(self):
        self.setStyleSheet("background-color: #0f172a; border: 0.5px solid #1e293b;")

    def enterEvent(self, event):
        if not self.is_seleccionada:
            shadow = QGraphicsDropShadowEffect()
            shadow.setBlurRadius(15)
            shadow.setColor(QColor(236, 91, 19, 80))
            self.setGraphicsEffect(shadow)
            self.setStyleSheet("border: 2px solid #ec5b13;")

        self.label_info.show()
        self.posicionar_iconos()
        self.boton_borrar.show()
        self.boton_config.show()
        super().enterEvent(event)

    def leaveEvent(self, event):
        if not self.is_seleccionada:
            self.setGraphicsEffect(None)
            self.actualizar_estilo()

        self.label_info.hide()
        self.boton_borrar.hide()
        self.boton_config.hide()
        super().leaveEvent(event)

    def posicionar_iconos(self):
        if not hasattr(self, 'boton_borrar'):
            return
        try:
            label_width = self.video_label.width()
            label_height = self.video_label.height()

            if hasattr(self, 'status_badge') and self.status_badge:
                self.status_badge.move(8, label_height - 32)

            if hasattr(self, 'icono_alarma') and self.icono_alarma:
                self.icono_alarma.move(label_width - self.icono_alarma.width() - 8, 8)

            icon_size = self.icon_size
            spacing = self.icon_spacing

            total_width = (icon_size * 2) + spacing
            start_x = (label_width - total_width) // 2
            y_pos = label_height - icon_size - 15

            if hasattr(self, 'boton_config') and self.boton_config:
                self.boton_config.move(start_x, y_pos)

            if hasattr(self, 'boton_borrar') and self.boton_borrar:
                self.boton_borrar.move(start_x + icon_size + spacing, y_pos)

        except Exception as e:
            pass

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.posicionar_iconos()

    @Slot(str)
    def actualizar_nombre(self, nuevo_nombre):
        self.numero_camara = nuevo_nombre
        self.label_info.setText(f"Cam {nuevo_nombre}")
        self.label_info.adjustSize()

    def mostrar_alarma_visual(self, activar):
        try:
            if activar:
                self._alarma_visual_activa = True
                if self.icono_alarma:
                    self.icono_alarma.show()
                if self.parent_widget_camaras and hasattr(self.parent_widget_camaras, 'registrar_alarma'):
                    self.parent_widget_camaras.registrar_alarma(self)
            else:
                self._alarma_visual_activa = False
                if self.icono_alarma:
                    self.icono_alarma.hide()
                if self.parent_widget_camaras and hasattr(self.parent_widget_camaras, 'desregistrar_alarma'):
                    self.parent_widget_camaras.desregistrar_alarma(self)
        except RuntimeError:
            pass

    def toggle_icono_alarma(self):
        try:
            if not self._alarma_visual_activa or not self.icono_alarma:
                return
            self.icono_alarma.setVisible(not self.icono_alarma.isVisible())
        except RuntimeError:
            pass


class WidgetCamaras(QWidget):
    vista_cambiada = Signal(str)

    def __init__(self, camera_workers=None):
        super().__init__()
        self.camaras_reales = []
        self.camaras_vacias = []
        self.todos_los_widgets = []
        self.vista_actual = "2x2"
        self.camara_seleccionada_actual = None
        self.camera_workers = camera_workers or []
        self.widget_flotante_actual = None
        self.dashboard_principal = None

        self._layout_activo_id = "2x2"
        self._asignaciones_activas = {}

        self._resize_timer = None

        self.camaras_deshabilitadas = set()

        self.widgets_en_alarma = []
        self._timer_parpadeo = QTimer(self)
        self._timer_parpadeo.setInterval(250)
        self._timer_parpadeo.timeout.connect(self._toggle_parpadeo_global)
        self._timer_parpadeo.start()

        print(f"🔍 WidgetCamaras recibió {len(self.camera_workers)} workers")

        self.initUI()

        for worker in self.camera_workers:
            if worker is not None:
                print(f"   - Creando widget para: {worker.name}")
                widget = WidgetCamaraIndividual(worker, self)
                self.camaras_reales.append(widget)

        QTimer.singleShot(50, self._reconstruir)

    def initUI(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.widget_grid = QWidget()
        self.widget_grid.setStyleSheet("background-color: #0a0a0a;")
        self.grid_layout = QGridLayout(self.widget_grid)
        self.grid_layout.setSpacing(1)
        self.grid_layout.setContentsMargins(1, 1, 1, 1)

        self.grid_layout.setRowStretch(0, 1)
        self.grid_layout.setColumnStretch(0, 1)

        layout.addWidget(self.widget_grid, 1)

    def registrar_alarma(self, widget):
        if widget not in self.widgets_en_alarma:
            self.widgets_en_alarma.append(widget)

    def desregistrar_alarma(self, widget):
        if widget in self.widgets_en_alarma:
            self.widgets_en_alarma.remove(widget)

    def _toggle_parpadeo_global(self):
        for widget in list(self.widgets_en_alarma):
            try:
                if hasattr(widget, 'toggle_icono_alarma'):
                    widget.toggle_icono_alarma()
            except RuntimeError:
                if widget in self.widgets_en_alarma:
                    self.widgets_en_alarma.remove(widget)

    def set_vista(self, layout_id, asignaciones):
        self._layout_activo_id = layout_id
        self._asignaciones_activas = {}
        for k, v in dict(asignaciones).items():
            try:
                self._asignaciones_activas[int(k)] = v
            except (ValueError, TypeError):
                self._asignaciones_activas[k] = v
        self._reconstruir()

    def cambiar_vista(self, layout_id):
        layout = obtener_layout(layout_id)
        ids = [
            getattr(w.camera_worker, 'worker_id', None)
            for w in self.camaras_reales
            if getattr(w.camera_worker, 'worker_id', None)
        ]
        asignaciones = {i: cid for i, cid in enumerate(ids[:layout.total_camaras])}
        self.set_vista(layout_id, asignaciones)

    def aplicar_vista_guardada(self, layout_id, asignaciones):
        self.set_vista(layout_id, asignaciones)

    def agregar_camara_widget(self, camera_worker):
        print(f"   🧩 [WidgetCamaras] agregar_camara_widget llamado para {camera_worker.name if camera_worker else None}")
        if camera_worker is None:
            print("   ❌ camera_worker es None")
            return None
        widget = WidgetCamaraIndividual(camera_worker, self)
        self.camaras_reales.append(widget)
        print(f"   📋 camaras_reales ahora tiene {len(self.camaras_reales)} widgets")
        return widget

    def quitar_camara_widget(self, camera_worker):
        for w in list(self.camaras_reales):
            if getattr(w, 'camera_worker', None) is camera_worker:
                self.camaras_reales.remove(w)
                self.desregistrar_alarma(w)
                return True
        return False

    def deshabilitar_camara_widget(self, camera_worker, deshabilitada):
        if camera_worker is None:
            return

        camera_index = getattr(camera_worker, 'camera_index', '')
        if not camera_index:
            return

        for w in self.camaras_reales:
            if getattr(w, 'camera_worker', None) is camera_worker:
                w.set_deshabilitada(deshabilitada)

                if deshabilitada:
                    self.camaras_deshabilitadas.add(camera_index)
                else:
                    self.camaras_deshabilitadas.discard(camera_index)

                return

    def obtener_widget_por_worker_id(self, worker_id):
        """
        ⭐ MULTI-LENTE: busca el widget por worker_id (cam01_lens1).
        """
        for w in self.camaras_reales:
            worker = getattr(w, 'camera_worker', None)
            if not worker:
                continue
            wid = getattr(worker, 'worker_id', None)
            if wid and str(wid) == str(worker_id):
                return w
        return None

    def deseleccionar_todas(self):
        for widget in self.camaras_reales:
            if hasattr(widget, 'deseleccionar'):
                widget.deseleccionar()
        self.camara_seleccionada_actual = None

    def seleccionar_camara(self, widget):
        for w in self.camaras_reales:
            if w is not widget and hasattr(w, 'deseleccionar'):
                w.deseleccionar()
        widget.is_seleccionada = True
        widget.aplicar_efecto_seleccion()
        self.camara_seleccionada_actual = widget

    def abrir_flotante(self, camera_worker):
        if self.widget_flotante_actual:
            self.widget_flotante_actual.cerrar()
        self.widget_flotante_actual = WidgetCamaraFlotante(camera_worker, self)
        self.widget_flotante_actual.show()

    def actualizar_tamanos_celdas(self):
        if not self.todos_los_widgets:
            return

        layout = obtener_layout(self.vista_actual)
        grid_width = self.widget_grid.width()
        grid_height = self.widget_grid.height()

        if grid_width <= 0 or grid_height <= 0:
            return

        ancho_unidad = (grid_width - (layout.columnas - 1)) // layout.columnas
        alto_unidad = (grid_height - (layout.filas - 1)) // layout.filas

        if ancho_unidad <= 0 or alto_unidad <= 0:
            return

        for widget, celda in zip(self.todos_los_widgets, layout.celdas):
            try:
                if widget.parent() is None:
                    continue
                ancho = ancho_unidad * celda.col_span + (celda.col_span - 1)
                alto = alto_unidad * celda.row_span + (celda.row_span - 1)
                if ancho > 0 and alto > 0:
                    widget.setFixedSize(ancho, alto)
            except RuntimeError:
                pass

    def _reconstruir(self):
        print(f"   🔄 [WidgetCamaras] _reconstruir() layout={self._layout_activo_id}, "
              f"camaras_reales={len(self.camaras_reales)}, "
              f"asignaciones={len(self._asignaciones_activas)}, "
              f"deshabilitadas={len(self.camaras_deshabilitadas)}")

        layout = obtener_layout(self._layout_activo_id)
        total_slots = layout.total_camaras

        widgets_actuales = set()
        for i in reversed(range(self.grid_layout.count())):
            item = self.grid_layout.itemAt(i)
            if item and item.widget():
                widgets_actuales.add(item.widget())
                item.widget().setParent(None)

        self.todos_los_widgets = []
        for slot in range(total_slots):
            worker_id = self._asignaciones_activas.get(slot)
            widget = None

            if worker_id is not None:
                # ⭐ MULTI-LENTE: 1 widget por worker_id (lente)
                widget = self.obtener_widget_por_worker_id(worker_id)

            if widget is None:
                widget = WidgetCamaraVacia(len(self.todos_los_widgets) + 1)

            self.todos_los_widgets.append(widget)

        widgets_a_usar = set(self.todos_los_widgets)
        for w in widgets_actuales:
            if w not in widgets_a_usar:
                es_camara_viva = any(w is cw for cw in self.camaras_reales)
                if not es_camara_viva:
                    try:
                        w.deleteLater()
                    except Exception:
                        pass

        self.camaras_vacias = []

        for celda, widget in zip(layout.celdas, self.todos_los_widgets):
            self.grid_layout.addWidget(
                widget, celda.row, celda.col, celda.row_span, celda.col_span
            )
            widget.show()

        for c in range(self.grid_layout.columnCount()):
            self.grid_layout.setColumnStretch(c, 0)
        for r in range(self.grid_layout.rowCount()):
            self.grid_layout.setRowStretch(r, 0)
        for c in range(layout.columnas):
            self.grid_layout.setColumnStretch(c, 1)
        for r in range(layout.filas):
            self.grid_layout.setRowStretch(r, 1)

        self.vista_actual = layout.id
        self.vista_cambiada.emit(layout.id)

        print(f"   🖼️ Grid reconstruido: {len(self.todos_los_widgets)} widgets, "
              f"layout={layout.id} ({layout.filas}x{layout.columnas})")

        if self._resize_timer is not None:
            self._resize_timer.stop()

        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self.actualizar_tamanos_celdas)
        self._resize_timer.start(50)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._resize_timer is not None:
            self._resize_timer.stop()

        self._resize_timer = QTimer()
        self._resize_timer.setSingleShot(True)
        self._resize_timer.timeout.connect(self.actualizar_tamanos_celdas)
        self._resize_timer.start(50)


class WidgetCamaraFlotante(QMainWindow):
    def __init__(self, camera_worker, parent=None):
        super().__init__(parent)
        self.camera_worker = camera_worker
        self.numero_camara = camera_worker.name if camera_worker else "Sin cámara"
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint)

        self.using_main_stream = False
        self.conexion_main = None
        self.display_timer = QTimer()
        self.display_timer.timeout.connect(self.actualizar_display)
        self.overlay_info = OverlayInfoTransparente(self)

        self._frames_contados = 0
        self._fps_last_check = time.time()

        self.initUI()

        if camera_worker:
            self.overlay_info.camera_name = self.numero_camara

        screen = QApplication.primaryScreen().geometry()
        self.setGeometry(screen)
        self.showFullScreen()
        self.display_timer.start(33)
        self.overlay_info.show()
        self._posicionar_overlay()

        if camera_worker and getattr(camera_worker, 'camera_index', None) and getattr(camera_worker, 'server_host', None):
            from remote_camera_worker import ConexionStreamPrincipal
            self.conexion_main = ConexionStreamPrincipal(
                camera_worker.camera_index, camera_worker.server_host, self.numero_camara,
                lens_id=getattr(camera_worker, 'lens_id', 1),
                cantidad_lentes=getattr(camera_worker, 'cantidad_lentes', 1),
            )
            self.conexion_main.start()

    def initUI(self):
        self.setWindowTitle(f"Cámara {self.numero_camara}")
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        self.video_label = QLabel("Cargando...")
        self.video_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.video_label.setStyleSheet("background-color: black; color: white;")
        layout.addWidget(self.video_label)

    def _posicionar_overlay(self):
        margen = 16
        x = self.width() - self.overlay_info.width() - margen
        y = margen
        self.overlay_info.move(self.mapToGlobal(QPoint(x, y)))

    def resizeEvent(self, event):
        self._posicionar_overlay()
        super().resizeEvent(event)

    def actualizar_display(self):
        try:
            frame = None
            usando_main_ahora = False

            if self.conexion_main and self.conexion_main.connected:
                frame_main = self.conexion_main.get_frame_copy()
                if frame_main is not None and frame_main.size > 0:
                    frame = frame_main
                    usando_main_ahora = True

            if frame is None and self.camera_worker and hasattr(self.camera_worker, 'get_frame_copy'):
                frame = self.camera_worker.get_frame_copy()

            if frame is None or frame.size == 0:
                return

            self.using_main_stream = usando_main_ahora

            if usando_main_ahora:
                sub_w = getattr(self.camera_worker, 'frame_width', 0) or 1
                sub_h = getattr(self.camera_worker, 'frame_height', 0) or 1
                main_h, main_w = frame.shape[:2]
                escala_x = main_w / sub_w
                escala_y = main_h / sub_h
                if hasattr(self.camera_worker, '_dibujar_detecciones'):
                    frame = self.camera_worker._dibujar_detecciones(frame.copy(), escala_x, escala_y)

            frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = frame_rgb.shape
            q_img = QImage(frame_rgb.data, w, h, ch * w, QImage.Format.Format_RGB888)
            pixmap = QPixmap.fromImage(q_img)

            label_w = self.video_label.width()
            label_h = self.video_label.height()
            if label_w > 0 and label_h > 0:
                scaled = pixmap.scaled(label_w, label_h, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
                self.video_label.setPixmap(scaled)

            self._frames_contados += 1
            ahora = time.time()
            if ahora - self._fps_last_check >= 1.0:
                self.overlay_info.fps_real = self._frames_contados
                self._frames_contados = 0
                self._fps_last_check = ahora

            self.overlay_info.camera_name = self.numero_camara
            self.overlay_info.resolucion = f"{w}x{h}"
            self.overlay_info.stream_type = "MAIN" if usando_main_ahora else "SUB"
            self.overlay_info.fps = getattr(self.camera_worker, 'fps_configurado', 0) if self.camera_worker else 0
        except Exception:
            pass

    def cerrar(self):
        self.display_timer.stop()
        if self.conexion_main:
            self.conexion_main.stop()
            self.conexion_main = None
        if self.overlay_info:
            self.overlay_info.close()
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.cerrar()


# ============================================================
# ========== UI DASHBOARD PRINCIPAL ==========================
# ============================================================

class Ui_Dashboard(QObject):
    """INTERFAZ PRINCIPAL - CON VISTA EN VIVO Y DISPOSITIVOS"""

    gestion_clicked = Signal()
    cuadricula_clicked = Signal()
    cerrar_clicked = Signal()
    minimizar_clicked = Signal()
    vista_seleccionada = Signal(str)
    mostrar_zonas_changed = Signal(bool)
    dispositivos_clicked = Signal()
    vista_vivo_clicked = Signal()
    cambiar_password_clicked = Signal()
    cerrar_sesion_clicked = Signal()
    cambio_stream_solicitado = Signal(str)

    def __init__(self):
        super().__init__()
        self.is_maximized = False
        self.dragging = False
        self.drag_position = QPoint()
        self.normal_size = None
        self.MainWindow = None
        self.camera_workers = []
        self.widget_camaras = None
        self.fullscreen_mode = False
        self.btn_fullscreen = None
        self._stream_actual = "SUB"

    def abrir_configuracion_global(self):
        try:
            from configuracion.configuracion_global import VentanaConfiguracionGlobal

            control_client = getattr(self.MainWindow, 'control_client', None)
            server_host = getattr(self.MainWindow, 'server_host', None)
            api_token = getattr(self.MainWindow, 'api_token', None)

            current_user_role = 'viewer'
            current_user_dict = None
            if hasattr(self.MainWindow, 'current_user') and self.MainWindow.current_user:
                current_user_dict = self.MainWindow.current_user
                current_user_role = current_user_dict.get('role', 'viewer')

            self.ventana_config_global = VentanaConfiguracionGlobal(
                parent=self.MainWindow,
                control_client=control_client,
                server_host=server_host,
                api_token=api_token,
                current_user_role=current_user_role,
                current_user=current_user_dict
            )

            if hasattr(self.MainWindow, 'recargar_configuracion_global'):
                self.ventana_config_global.config_guardada.connect(
                    self.MainWindow.recargar_configuracion_global
                )

            self.ventana_config_global.show()
        except Exception as e:
            print(f"Error abriendo configuración global: {e}")
            import traceback
            traceback.print_exc()

    def setup_ui(self, MainWindow, camera_workers=None):
        self.MainWindow = MainWindow
        self.camera_workers = camera_workers or []

        print(f"🔍 UI recibió {len(self.camera_workers)} workers")
        for w in self.camera_workers:
            print(f"   - Worker: {w.name if w else 'None'}")

        MainWindow.setWindowFlags(Qt.WindowType.FramelessWindowHint)
        MainWindow.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        MainWindow.setMinimumSize(1024, 768)

        MainWindow.keyPressEvent = self.keyPressEvent

        MainWindow.central_widget = QWidget()
        MainWindow.central_widget.setObjectName("central")
        MainWindow.central_widget.setStyleSheet("""
            #central {
                background-color: #1E3A5F;
                border: 2px solid #1E3A5F;
                border-radius: 12px;
            }
        """)

        MainWindow.layout_principal = QVBoxLayout(MainWindow.central_widget)
        self.layout_principal = MainWindow.layout_principal
        MainWindow.layout_principal.setContentsMargins(2, 2, 2, 2)
        MainWindow.layout_principal.setSpacing(0)

        self.header = QFrame()
        self.header.setFixedHeight(70)
        self.header.setStyleSheet("""
            QFrame {
                background-color: #1E3A5F;
                border-top-left-radius: 10px;
                border-top-right-radius: 10px;
            }
        """)

        self.header.mousePressEvent = self.header_press
        self.header.mouseMoveEvent = self.header_move
        self.header.mouseReleaseEvent = self.header_release
        self.header.mouseDoubleClickEvent = self.header_double_click

        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(24, 12, 24, 12)
        header_layout.setSpacing(12)

        self.btn_security = self.crear_boton_svg("security", 40)
        header_layout.addWidget(self.btn_security)

        self.logo_deepsentinel = QLabel()
        self.logo_deepsentinel.setAlignment(Qt.AlignmentFlag.AlignLeft)
        self.logo_deepsentinel.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)

        logo_path = os.path.join("iconos", "deepsentinel.svg")
        self.logo_pixmap_original = None

        if os.path.exists(logo_path):
            renderer = QSvgRenderer(logo_path)
            self.logo_pixmap_original = QPixmap(180, 45)
            self.logo_pixmap_original.fill(Qt.GlobalColor.transparent)
            painter = QPainter(self.logo_pixmap_original)
            renderer.render(painter)
            painter.end()

            painter = QPainter(self.logo_pixmap_original)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(self.logo_pixmap_original.rect(), QColor("#ec5b13"))
            painter.end()

            self.logo_deepsentinel.setPixmap(self.logo_pixmap_original)
        else:
            self.logo_deepsentinel.setText("Deep Sentinel")
            self.logo_deepsentinel.setStyleSheet("color: #ffffff; font-size: 18px; font-weight: bold;")

        header_layout.addWidget(self.logo_deepsentinel)

        self.version = QLabel("v4.2.0")
        self.version.setStyleSheet("color: #cbd5e1; font-size: 14px; margin-left: 4px;")
        header_layout.addWidget(self.version)

        header_layout.addSpacing(15)

        self.btn_inicio = self.crear_boton_nav("home", "INICIO")
        self.btn_inicio.texto_original = "INICIO"
        header_layout.addWidget(self.btn_inicio)

        self.btn_dispositivos = self.crear_boton_nav("videocam", "DISPOSITIVOS")
        self.btn_dispositivos.texto_original = "DISPOSITIVOS"
        self.btn_dispositivos.clicked.connect(self.dispositivos_clicked)
        header_layout.addWidget(self.btn_dispositivos)

        self.btn_vista_vivo = self.crear_boton_nav("live", "VISTA EN VIVO")
        self.btn_vista_vivo.texto_original = "VISTA EN VIVO"
        self.btn_vista_vivo.clicked.connect(self.vista_vivo_clicked)
        header_layout.addWidget(self.btn_vista_vivo)

        self.btn_gestion = self.crear_boton_nav("videocam", "GESTIÓN")
        self.btn_gestion.texto_original = "GESTIÓN"
        self.btn_gestion.clicked.connect(self.gestion_clicked)
        header_layout.addWidget(self.btn_gestion)

        self.btn_cuadricula = self.crear_boton_nav("grid_view", "CUADRÍCULA")
        self.btn_cuadricula.texto_original = "CUADRÍCULA"
        self.btn_cuadricula.clicked.connect(self.cuadricula_clicked)
        header_layout.addWidget(self.btn_cuadricula)

        self.btn_config = self.crear_boton_nav("settings", "CONFIG")
        self.btn_config.texto_original = "CONFIG"
        self.btn_config.clicked.connect(self.abrir_configuracion_global)
        header_layout.addWidget(self.btn_config)

        self.btn_alertas = self.crear_boton_nav("notifications", "ALERTAS")
        self.btn_alertas.texto_original = "ALERTAS"
        header_layout.addWidget(self.btn_alertas)

        self.btn_borrar = self.crear_boton_nav("delete", "ELIMINAR")
        self.btn_borrar.texto_original = "ELIMINAR"
        header_layout.addWidget(self.btn_borrar)

        self.btn_red = self.crear_boton_nav("lan", "RED")
        self.btn_red.texto_original = "RED"
        header_layout.addWidget(self.btn_red)

        header_layout.addStretch()

        self.combo_vistas = QComboBox()
        for layout_id, nombre in nombres_para_combo():
            self.combo_vistas.addItem(nombre, layout_id)
        self.combo_vistas.setCurrentIndex(self.combo_vistas.findData("2x2"))
        self.combo_vistas.hide()
        self.combo_vistas.currentIndexChanged.connect(
            lambda _idx: self.vista_seleccionada.emit(self.combo_vistas.currentData())
        )
        header_layout.addWidget(self.combo_vistas)

        user_layout = QVBoxLayout()
        user_layout.setSpacing(0)
        self.admin_label = QLabel("Admin User")
        self.admin_label.setStyleSheet("color: #ffffff; font-size: 12px; font-weight: bold;")
        user_layout.addWidget(self.admin_label)
        self.conectado_label = QLabel("Conectado")
        self.conectado_label.setStyleSheet("color: #2ecc71; font-size: 10px;")
        user_layout.addWidget(self.conectado_label)
        user_widget = QWidget()
        user_widget.setLayout(user_layout)
        header_layout.addWidget(user_widget)

        self.btn_person = self.crear_boton_svg_redondo("person", 40, "rgba(236,91,19,0.2)", "1px solid rgba(236,91,19,0.4)")
        self.btn_person.setCursor(Qt.CursorShape.PointingHandCursor)
        self.btn_person.clicked.connect(self._mostrar_menu_usuario)
        header_layout.addWidget(self.btn_person)

        self.sep = QFrame()
        self.sep.setFixedWidth(1)
        self.sep.setFixedHeight(32)
        self.sep.setStyleSheet("background-color: #cbd5e1;")
        header_layout.addWidget(self.sep)

        self.btn_minimizar = self.crear_boton_svg_redondo("minimizar", 40, "rgba(236,91,19,0.2)", "1px solid rgba(236,91,19,0.4)")
        self.btn_minimizar.setToolTip("Minimizar a la bandeja o a la barra de tareas")
        self.btn_minimizar.clicked.connect(lambda: self.minimizar_clicked.emit())
        header_layout.addWidget(self.btn_minimizar)

        self.btn_maximizar = self.crear_boton_svg_redondo("maximizar", 40, "rgba(236,91,19,0.2)", "1px solid rgba(236,91,19,0.4)")
        self.btn_maximizar.clicked.connect(self.toggle_maximize)
        header_layout.addWidget(self.btn_maximizar)

        self.btn_cerrar = self.crear_boton_svg_redondo("logout", 40, "rgba(236,91,19,0.2)", "1px solid rgba(236,91,19,0.4)")
        self.btn_cerrar.clicked.connect(lambda: self.cerrar_clicked.emit())
        header_layout.addWidget(self.btn_cerrar)

        MainWindow.layout_principal.addWidget(self.header)

        self.camaras_container = QWidget()
        self.camaras_container.setStyleSheet("background-color: #0a0a0a;")
        self.camaras_layout = QVBoxLayout(self.camaras_container)
        self.camaras_layout.setContentsMargins(0, 0, 0, 0)

        self.widget_camaras = WidgetCamaras(self.camera_workers)
        self.camaras_layout.addWidget(self.widget_camaras)

        MainWindow.layout_principal.addWidget(self.camaras_container, 1)

        self.footer = QFrame()
        self.footer.setFixedHeight(48)
        self.footer.setStyleSheet("background-color: #1E3A5F; border-bottom-left-radius: 10px; border-bottom-right-radius: 10px; ")

        footer_layout = QHBoxLayout(self.footer)
        footer_layout.setContentsMargins(24, 0, 24, 0)

        iconos_footer = ["zoom_in", "fiber_manual_record", "fullscreen", "volume_up"]
        self.footer_buttons = []

        for icono in iconos_footer:
            btn = self.crear_boton_svg_footer(icono, 32)
            footer_layout.addWidget(btn)
            self.footer_buttons.append(btn)

            if icono == "fullscreen":
                self.btn_fullscreen = btn
                btn.clicked.connect(self.toggle_fullscreen_ui)

        self.btn_mostrar_zonas = self.crear_boton_svg_footer("mapa_zona", 32)
        self.btn_mostrar_zonas.setCheckable(True)
        self.btn_mostrar_zonas.setChecked(True)
        self.btn_mostrar_zonas.setToolTip("Mostrar zonas de detección")
        self.btn_mostrar_zonas.clicked.connect(self.toggle_mostrar_zonas)
        footer_layout.addWidget(self.btn_mostrar_zonas)

        footer_layout.addSpacing(16)

        self.stream_widget = QWidget()
        self.stream_widget.setStyleSheet("background: transparent;")
        stream_widget_layout = QHBoxLayout(self.stream_widget)
        stream_widget_layout.setContentsMargins(0, 0, 0, 0)
        stream_widget_layout.setSpacing(6)

        self.btn_stream = self.crear_boton_svg_footer("stream", 32)
        self.btn_stream.setToolTip("Cambiar stream de visualización")
        self.btn_stream.clicked.connect(self._abrir_menu_stream)
        stream_widget_layout.addWidget(self.btn_stream)

        self.stream_texto = QLabel("SUB")
        self.stream_texto.setStyleSheet("""
            QLabel {
                color: #ffffff;
                font-size: 11px;
                font-weight: 700;
                letter-spacing: 0.5px;
                background: transparent;
            }
        """)
        stream_widget_layout.addWidget(self.stream_texto)

        footer_layout.addWidget(self.stream_widget)

        footer_layout.addStretch()

        self.fecha_label = QLabel()
        self.fecha_label.setStyleSheet("color: #ffffff; font-family: monospace; font-size: 16px; font-weight: bold;")
        footer_layout.addWidget(self.fecha_label)

        MainWindow.layout_principal.addWidget(self.footer)
        MainWindow.setCentralWidget(MainWindow.central_widget)

        MainWindow.showNormal()
        MainWindow.resize(1280, 720)
        self.normal_size = QSize(1280, 720)
        print(f"🏁 Tamaño normal guardado: 1280x720")

        MainWindow.showMaximized()
        self.is_maximized = True
        self.actualizar_icono_maximizar(True)

        MainWindow.installEventFilter(self)

    def toggle_mostrar_zonas(self, checked):
        color = "#ec5b13" if checked else "#94a3b8"

        icon_path = os.path.join("iconos", "mapa_zona.svg")
        if os.path.exists(icon_path):
            renderer = QSvgRenderer(icon_path)
            pixmap = QPixmap(22, 22)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            renderer.render(painter)
            painter.end()

            painter = QPainter(pixmap)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(pixmap.rect(), QColor(color))
            painter.end()

            self.btn_mostrar_zonas.setIcon(QIcon(pixmap))

        self.mostrar_zonas_changed.emit(checked)

    def eventFilter(self, obj, event):
        if obj == self.MainWindow and event.type() == QEvent.Type.Resize:
            self._escalar_elementos()
        return super().eventFilter(obj, event)

    def toggle_fullscreen_ui(self):
        self.fullscreen_mode = not self.fullscreen_mode

        if self.fullscreen_mode:
            self.header.hide()
            self.footer.hide()

            self.MainWindow.central_widget.setStyleSheet("""
                #central {
                    background-color: #1E3A5F;
                    border: none;
                    border-radius: 0px;
                }
            """)
            self.MainWindow.layout_principal.setContentsMargins(0, 0, 0, 0)

        else:
            self.header.show()
            self.footer.show()

            self.MainWindow.central_widget.setStyleSheet("""
                #central {
                    background-color: #1E3A5F;
                    border: 2px solid #1E3A5F;
                    border-radius: 12px;
                }
            """)
            self.MainWindow.layout_principal.setContentsMargins(2, 2, 2, 2)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape and self.fullscreen_mode:
            self.toggle_fullscreen_ui()
        elif event.key() == Qt.Key.Key_Escape and hasattr(self, 'widget_camaras') and self.widget_camaras.widget_flotante_actual:
            self.widget_camaras.widget_flotante_actual.cerrar()
        else:
            event.ignore()

    def _escalar_elementos(self):
        ancho_ventana = self.MainWindow.width()

        if hasattr(self, 'logo_pixmap_original') and self.logo_pixmap_original:
            if ancho_ventana < 700:
                logo_ancho = 100
            elif ancho_ventana < 900:
                logo_ancho = 140
            else:
                logo_ancho = min(max(int(ancho_ventana * 0.12), 120), 200)
            logo_alto = int(logo_ancho * 0.25)
            scaled = self.logo_pixmap_original.scaled(logo_ancho, logo_alto, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            self.logo_deepsentinel.setPixmap(scaled)

        if ancho_ventana < 700:
            mostrar_texto = False
            tamano_fuente = 0
            padding = "0px"
            min_width = "32px"
        elif ancho_ventana < 900:
            mostrar_texto = True
            tamano_fuente = 9
            padding = "4px 4px"
            min_width = "0px"

            textos = {
                "INICIO": "IN",
                "DISPOSITIVOS": "DIS",
                "VISTA EN VIVO": "VIVO",
                "GESTIÓN": "GES",
                "CUADRÍCULA": "CUA",
                "CONFIG": "CFG",
                "ALERTAS": "ALT",
                "ELIMINAR": "DEL",
                "RED": "RD"
            }
        else:
            mostrar_texto = True
            tamano_fuente = 12
            padding = "8px 10px"
            min_width = "0px"

            textos = {
                "INICIO": "INICIO",
                "DISPOSITIVOS": "DISPOSITIVOS",
                "VISTA EN VIVO": "VISTA EN VIVO",
                "GESTIÓN": "GESTIÓN",
                "CUADRÍCULA": "CUADRÍCULA",
                "CONFIG": "CONFIG",
                "ALERTAS": "ALERTAS",
                "ELIMINAR": "ELIMINAR",
                "RED": "RED"
            }

        botones_nav = [
            (self.btn_inicio, "INICIO"),
            (self.btn_dispositivos, "DISPOSITIVOS"),
            (self.btn_vista_vivo, "VISTA EN VIVO"),
            (self.btn_gestion, "GESTIÓN"),
            (self.btn_cuadricula, "CUADRÍCULA"),
            (self.btn_config, "CONFIG"),
            (self.btn_alertas, "ALERTAS"),
            (self.btn_borrar, "ELIMINAR"),
            (self.btn_red, "RED")
        ]

        for btn, texto_original in botones_nav:
            if btn:
                if mostrar_texto:
                    texto_mostrar = textos.get(texto_original, texto_original)
                    btn.setText(texto_mostrar)
                    btn.setStyleSheet(f"""
                        QPushButton {{
                            background-color: transparent;
                            color: #ffffff;
                            border: none;
                            padding: {padding};
                            font-size: {tamano_fuente}px;
                            font-weight: 500;
                            border-radius: 8px;
                            text-align: left;
                            min-width: {min_width};
                        }}
                        QPushButton:hover {{
                            background-color: rgba(236, 91, 19, 0.1);
                        }}
                    """)
                else:
                    btn.setText("")
                    btn.setStyleSheet(f"""
                        QPushButton {{
                            background-color: transparent;
                            color: #ffffff;
                            border: none;
                            padding: {padding};
                            font-size: 0px;
                            font-weight: 500;
                            border-radius: 8px;
                            min-width: 32px;
                            max-width: 32px;
                        }}
                        QPushButton:hover {{
                            background-color: rgba(236, 91, 19, 0.1);
                        }}
                    """)

        if hasattr(self, 'version'):
            if ancho_ventana < 700:
                nuevo_tamano = 8
            elif ancho_ventana < 900:
                nuevo_tamano = 10
            else:
                nuevo_tamano = max(10, min(int(ancho_ventana * 0.012), 14))
            self.version.setStyleSheet(f"color: #cbd5e1; font-size: {nuevo_tamano}px; margin-left: 4px;")

        if hasattr(self, 'admin_label'):
            if ancho_ventana < 700:
                nuevo_tamano = 8
            elif ancho_ventana < 900:
                nuevo_tamano = 10
            else:
                nuevo_tamano = max(10, min(int(ancho_ventana * 0.01), 12))
            self.admin_label.setStyleSheet(f"color: #cbd5e1; font-size: {nuevo_tamano}px; font-weight: bold;")

        if hasattr(self, 'conectado_label'):
            if ancho_ventana < 700:
                nuevo_tamano = 6
            elif ancho_ventana < 900:
                nuevo_tamano = 8
            else:
                nuevo_tamano = max(8, min(int(ancho_ventana * 0.008), 10))
            self.conectado_label.setStyleSheet(f"color: #2ecc71; font-size: {nuevo_tamano}px;")

        if hasattr(self, 'combo_vistas'):
            if ancho_ventana < 700:
                nuevo_tamano = 9
            elif ancho_ventana < 900:
                nuevo_tamano = 10
            else:
                nuevo_tamano = max(10, min(int(ancho_ventana * 0.01), 12))
            self.combo_vistas.setStyleSheet(f"""
                QComboBox {{
                    background-color: #2c3e50;
                    color: white;
                    border: 1px solid #3a4e62;
                    border-radius: 4px;
                    padding: 5px;
                    font-size: {nuevo_tamano}px;
                    min-width: 70px;
                }}
                QComboBox::drop-down {{
                    border: none;
                }}
                QComboBox QAbstractItemView {{
                    background-color: #1a2738;
                    color: white;
                    selection-background-color: #ec5b13;
                }}
            """)

        if hasattr(self, 'fecha_label'):
            if ancho_ventana < 700:
                nuevo_tamano = 14
            elif ancho_ventana < 900:
                nuevo_tamano = 16
            else:
                nuevo_tamano = 18
            self.fecha_label.setStyleSheet(f"color: #ffffff; font-family: monospace; font-size: {nuevo_tamano}px; font-weight: bold;")

        if hasattr(self, 'btn_security') and hasattr(self, 'btn_person') and hasattr(self, 'btn_maximizar') and hasattr(self, 'btn_cerrar'):
            if ancho_ventana < 700:
                btn_size = 32
            elif ancho_ventana < 900:
                btn_size = 36
            else:
                btn_size = min(max(int(ancho_ventana * 0.028), 32), 48)
            btn_icon_size = btn_size - 8

            for btn in [self.btn_security, self.btn_person, self.btn_minimizar, self.btn_maximizar, self.btn_cerrar]:
                if btn:
                    btn.setFixedSize(btn_size, btn_size)
                    btn.setIconSize(QSize(btn_icon_size, btn_icon_size))
                    btn.setStyleSheet(f"""
                        QPushButton {{
                            background-color: rgba(255,255,255,0.1);
                            border: 1px solid rgba(255,255,255,0.2);
                            border-radius: {btn_size//2}px;
                        }}
                        QPushButton:hover {{
                            background-color: rgba(255,255,255,0.2);
                        }}
                    """)

    def crear_boton_svg_redondo(self, nombre, size=40, bg="transparent", border="none"):
        btn = QPushButton()
        btn.setFixedSize(size, size)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)

        icon_path = os.path.join("iconos", f"{nombre}.svg")
        if os.path.exists(icon_path):
            renderer = QSvgRenderer(icon_path)
            pixmap = QPixmap(size-8, size-8)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            renderer.render(painter)
            painter.end()

            painter = QPainter(pixmap)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(pixmap.rect(), QColor("#ec5b13"))
            painter.end()

            btn.setIcon(QIcon(pixmap))
            btn.setIconSize(QSize(size-8, size-8))
        else:
            btn.setText("?")

        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {bg};
                border: {border};
                border-radius: {size//2}px;
            }}
            QPushButton:hover {{
                background-color: rgba(255,255,255,0.2);
            }}
        """)
        return btn

    def toggle_maximize(self):
        if self.is_maximized:
            self.MainWindow.showNormal()
            if self.normal_size:
                QTimer.singleShot(50, lambda: self.MainWindow.resize(self.normal_size.width(), self.normal_size.height()))
            self.is_maximized = False
            self._cambiar_icono_maximizar(False)
        else:
            self.normal_size = self.MainWindow.size()
            self.MainWindow.showMaximized()
            self.is_maximized = True
            self._cambiar_icono_maximizar(True)

    def _cambiar_icono_maximizar(self, maximizado):
        icon_name = "restaurar" if maximizado else "maximizar"

        ancho_ventana = self.MainWindow.width()
        if ancho_ventana < 700:
            btn_size = 32
        elif ancho_ventana < 900:
            btn_size = 36
        else:
            btn_size = min(max(int(ancho_ventana * 0.028), 32), 48)

        nuevo_btn = self.crear_boton_svg_redondo(icon_name, btn_size, "rgba(236,91,19,0.2)", "1px solid rgba(236,91,19,0.4)")
        nuevo_btn.clicked.connect(self.toggle_maximize)

        layout = self.btn_maximizar.parent().layout()
        index = layout.indexOf(self.btn_maximizar)
        layout.replaceWidget(self.btn_maximizar, nuevo_btn)
        self.btn_maximizar.deleteLater()
        self.btn_maximizar = nuevo_btn

    def header_press(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self.is_maximized:
                ratio_x = event.pos().x() / self.MainWindow.width()
                global_x = event.globalPosition().x()
                global_y = event.globalPosition().y()
                drag_pos = event.globalPosition().toPoint() - self.MainWindow.frameGeometry().topLeft()

                self.MainWindow.showNormal()
                self.is_maximized = False
                self._cambiar_icono_maximizar(False)

                QTimer.singleShot(50, lambda: self._mover_desde_max(ratio_x, global_x, global_y, drag_pos))
            else:
                self.drag_position = event.globalPosition().toPoint() - self.MainWindow.frameGeometry().topLeft()
                self.dragging = True
            event.accept()

    def _mover_desde_max(self, ratio_x, global_x, global_y, drag_pos):
        if self.normal_size:
            self.MainWindow.resize(self.normal_size.width(), self.normal_size.height())
        new_x = int(global_x - (self.MainWindow.width() * ratio_x))
        new_y = int(global_y - 20)
        self.MainWindow.move(new_x, new_y)
        self.drag_position = drag_pos
        self.dragging = True

    def header_move(self, event):
        if self.dragging and event.buttons() & Qt.MouseButton.LeftButton:
            self.MainWindow.move(event.globalPosition().toPoint() - self.drag_position)
            event.accept()

    def header_release(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.dragging = False
            event.accept()

    def header_double_click(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_maximize()
            event.accept()

    def _mostrar_menu_usuario(self):
        menu = QMenu(self.btn_person)
        menu.setStyleSheet("""
            QMenu {
                background-color: #1e1e1e;
                color: #e0e0e0;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 6px;
            }
            QMenu::item {
                padding: 8px 24px 8px 16px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background-color: #ec5b13;
                color: white;
            }
            QMenu::separator {
                height: 1px;
                background-color: #334155;
                margin: 4px 8px;
            }
        """)

        accion_password = menu.addAction("🔑  Cambiar contraseña")
        menu.addSeparator()
        accion_logout = menu.addAction("🚪  Cerrar sesión")

        pos = self.btn_person.mapToGlobal(
            QPoint(self.btn_person.width() - menu.sizeHint().width(),
                   self.btn_person.height())
        )
        accion = menu.exec(pos)

        if accion == accion_password:
            self.cambiar_password_clicked.emit()
        elif accion == accion_logout:
            self.cerrar_sesion_clicked.emit()

    def _abrir_menu_stream(self):
        menu = QMenu(self.btn_stream)
        menu.setStyleSheet("""
            QMenu {
                background-color: #1e1e1e;
                color: #e0e0e0;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 6px;
            }
            QMenu::item {
                padding: 8px 24px 8px 16px;
                border-radius: 4px;
                font-size: 11px;
                font-weight: 700;
                letter-spacing: 0.5px;
            }
            QMenu::item:selected {
                background-color: #ec5b13;
                color: white;
            }
            QMenu::separator {
                height: 1px;
                background-color: #334155;
                margin: 4px 8px;
            }
        """)

        stream_actual = getattr(self, '_stream_actual', 'SUB')

        accion_main = menu.addAction("MAIN")
        accion_sub = menu.addAction("SUB")
        accion_3sub = menu.addAction("3° SUB")

        if stream_actual == "MAIN":
            accion_main.setText("✓  MAIN")
        elif stream_actual == "SUB":
            accion_sub.setText("✓  SUB")
        elif stream_actual == "3° SUB":
            accion_3sub.setText("✓  3° SUB")

        pos = self.btn_stream.mapToGlobal(
            QPoint(0, -menu.sizeHint().height() - 4)
        )
        opcion = menu.exec(pos)

        if opcion is None:
            return

        if opcion == accion_main:
            nuevo = "MAIN"
        elif opcion == accion_sub:
            nuevo = "SUB"
        elif opcion == accion_3sub:
            nuevo = "3° SUB"
        else:
            return

        self._stream_actual = nuevo
        self.stream_texto.setText(nuevo)

        self.cambio_stream_solicitado.emit(nuevo)

    def actualizar_icono_maximizar(self, maximizado):
        self._cambiar_icono_maximizar(maximizado)

    def crear_boton_svg(self, nombre, size=40, bg="transparent", border="none"):
        btn = QPushButton()
        btn.setFixedSize(size, size)
        path = os.path.join("iconos", f"{nombre}.svg")
        if os.path.exists(path):
            renderer = QSvgRenderer(path)
            pixmap = QPixmap(size-8, size-8)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            renderer.render(painter)
            painter.end()
            painter = QPainter(pixmap)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(pixmap.rect(), QColor("#ec5b13"))
            painter.end()
            btn.setIcon(QIcon(pixmap))
            btn.setIconSize(QSize(size-8, size-8))
        else:
            btn.setText("?")

        btn.setStyleSheet(f"""
            QPushButton {{
                background-color: {bg};
                border: {border};
                border-radius: {size//2}px;
            }}
            QPushButton:hover {{
                background-color: rgba(236, 91, 19, 0.1);
            }}
        """)
        return btn

    def crear_boton_svg_footer(self, nombre, size=32):
        btn = QPushButton()
        btn.setFixedSize(size, size)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)

        path = os.path.join("iconos", f"{nombre}.svg")
        if os.path.exists(path):
            renderer = QSvgRenderer(path)
            pixmap = QPixmap(size-8, size-8)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            renderer.render(painter)
            painter.end()
            painter = QPainter(pixmap)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(pixmap.rect(), QColor("#ec5b13"))
            painter.end()
            btn.setIcon(QIcon(pixmap))
            btn.setIconSize(QSize(size-8, size-8))
        else:
            btn.setText("?")

        btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                border: none;
                border-radius: 4px;
            }
            QPushButton:hover {
                background-color: rgba(236, 91, 19, 0.1);
            }
        """)
        return btn

    def crear_boton_nav(self, nombre, texto):
        btn = QPushButton(texto)
        path = os.path.join("iconos", f"{nombre}.svg")
        if os.path.exists(path):
            renderer = QSvgRenderer(path)
            pixmap = QPixmap(20, 20)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            renderer.render(painter)
            painter.end()

            painter = QPainter(pixmap)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(pixmap.rect(), QColor("#ec5b13"))
            painter.end()

            btn.setIcon(QIcon(pixmap))
            btn.setIconSize(QSize(20, 20))

        btn.setStyleSheet("""
            QPushButton {
                background-color: transparent;
                color: #ffffff;
                border: none;
                padding: 8px 10px;
                font-size: 12px;
                font-weight: 500;
                border-radius: 8px;
                text-align: left;
            }
            QPushButton:hover {
                background-color: rgba(236, 91, 19, 0.1);
            }
        """)
        return btn