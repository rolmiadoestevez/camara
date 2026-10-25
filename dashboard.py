# dashboard.py - CON VISTA EN VIVO Y DISPOSITIVOS
#
# ⭐ MULTI-LENTE (2025):
# - _crear_workers_para_camara(): 1 DeviceController + N RemoteCameraWorker.
# - set_control_client(): el control_client va a los DeviceController, no a workers.
# - _on_camera_added(), on_camaras_actualizadas(): usan el factory.
# - _on_camera_deleted(), eliminar_camara(): paran el DeviceController del grupo.
#
# ⭐ FIX ALARMA VISUAL (2025):
# - _conectar_widget_camara: UniqueConnection para alarma_detectada.

import sys
import os
import json
import re
import cv2
import numpy as np
import time
import threading
import requests
from PySide6.QtWidgets import *
from PySide6.QtCore import *
from PySide6.QtGui import *
from datetime import datetime

from ui_dashboard import Ui_Dashboard
from dispositivos.camaras.ventana_gestion import VentanaGestion
from camera_info_widget import CameraInfoWidget
from configuracion.configuracion_camara import VentanaConfiguracion_Camara
from dialogo_confirmacion import confirmar_con_mensaje, confirmar_con_nombre

from sidebar_vistas import SidebarVistas
from dialogo_vista import DialogoVista
from cuadriculas import layout_uniforme_para
from preferences_manager import set_usuario_activo, get_preferencias_usuario, get_preferencias_maquina
from tray_manager import TrayController

# Multi-monitor (v05/v06) - package monitores
from monitores import (
    ScreenManager as MultiScreenManager,
    LayoutStore as MultiLayoutStore,
    LayoutController,
    ViewWindow,
    ViewWindow as MultiViewWindow,
    create_view_window,
    get_screen_manager,
    get_layout_controller,
)

from remote_camera_worker import RemoteCameraWorker
from device_controller import DeviceController

from dispositivos.grupos.ventana_grupos import VentanaGrupos
from dashboard_manager.groups_manager import GroupsManager


def normalizar_mac_direccion(mac_address):
    if not mac_address:
        return ""
    return re.sub(r'[:\-\.\s]', '', str(mac_address)).upper()


def cargar_configuracion():
    if getattr(sys, 'frozen', False):
        config_file = os.path.join(os.environ['APPDATA'], "VisionCore", "camera_config.json")
    else:
        config_file = "camera_config.json"

    if not os.path.exists(config_file):
        return []
    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        else:
            return []
    except:
        return []


class DashboardVigilancia(QMainWindow):
    VISTA_GENERAL_NOMBRE = "Vista General"

    sesion_cerrada = Signal()

    def __init__(self, camera_workers=None):
        super().__init__()

        self.camera_workers = camera_workers or []

        # ⭐ MULTI-LENTE: 1 DeviceController por camera_index
        self.device_controllers = {}   # {camera_index: DeviceController}

        self.vista_activa_nombre = None
        self.vistas_guardadas = {}

        self.modo_ui = "dispositivos"

        self.mostrar_zonas = True
        self.current_user = None
        self.auth_manager = None
        self.auth_flow = None
        self.user_permissions = []

        self.server_host = None
        self.api_token = None
        self.control_client = None

        self.tray = None
        self._saliendo = False
        self._tray_aviso = False

        self.groups_mgr = GroupsManager()

        self.grupos_guardados = {}
        self.grupos_tree_cache = []

        self._aplicar_vista_timer = None

        from alarm_manager import get_alarm_manager
        self.alarm_manager = get_alarm_manager()
        self.alarm_manager.start()
        print("[Dashboard] AlarmManager inicializado y corriendo")

        # Configurar proveedor de pantalla/monitores y layout-store (V05)
        # Usar las clases del paquete monitores (multi-monitor v05/v06)
        self.screen_mgr = get_screen_manager()
        self.layout_store = MultiLayoutStore.instance()
        self.view_windows = []
        self._vista_en_vivo_activa = False

        # Escuchar cambios de pantalla/monitores
        self.screen_mgr.screens_changed.connect(self._on_monitores_cambiados)

        self.ui = Ui_Dashboard()
        self.ui.setup_ui(self, self.camera_workers)
        self.initUI()  # <-- AQUÍ SE HACEN LAS CONEXIONES DE LOS BOTONES

        self._agregar_sidebar_vistas()

        from config_manager import CameraConfigManager
        self.config_manager = CameraConfigManager()
        self.config_global = self.config_manager.load_global_config()

        self.timer = QTimer()
        self.timer.timeout.connect(self.actualizar_hora)
        self.timer.start(1000)
        self.actualizar_hora()

    def initUI(self):
        self.setWindowTitle("Sistema de Vigilancia - Dashboard Integrado")

        self.ui.gestion_clicked.connect(self.abrir_gestion_camaras)
        self.ui.cuadricula_clicked.connect(self.toggle_combo_vistas)
        self.ui.vista_seleccionada.connect(self.cambiar_vista)
        self.ui.cerrar_clicked.connect(self.salir_aplicacion)
        self.ui.mostrar_zonas_changed.connect(self.on_mostrar_zonas_changed)

        self.ui.dispositivos_clicked.connect(self.abrir_dispositivos)
        self.ui.vista_vivo_clicked.connect(self.abrir_vista_en_vivo)

        self.ui.cambiar_password_clicked.connect(self._cambiar_password_usuario)
        self.ui.cerrar_sesion_clicked.connect(self._cerrar_sesion)
        self.ui.cambio_stream_solicitado.connect(self._cambiar_stream_visualizacion)
        self.ui.minimizar_clicked.connect(self.minimizar_ventana)

        self._crear_bandeja()

        if hasattr(self.ui, 'widget_camaras'):
            self.ui.widget_camaras.vista_cambiada.connect(self.on_vista_cambiada)

            for widget in self.ui.widget_camaras.camaras_reales:
                self._conectar_widget_camara(widget)

    # =====================================================================
    # MODO UI
    # =====================================================================
    def abrir_dispositivos(self):
        print("[Dashboard] Modo DISPOSITIVOS")
        self.modo_ui = "dispositivos"
        self._aplicar_modo_ui()

    def abrir_vista_en_vivo(self):
        print("[Dashboard] Modo VISTA EN VIVO")
        self.modo_ui = "vivo"
        self._aplicar_modo_ui()

        # Crear una ViewWindow por cada monitor disponible
        self._vista_en_vivo_activa = True
        self._crear_ventanas_vista_por_monitor()

    def _crear_ventanas_vista_por_monitor(self):
        """Crea una ViewWindow por monitor con la vista asignada guardada."""
        store = MultiLayoutStore.instance()

        # Obtener lista de vistas guardadas para el combo
        vistas_disponibles = list(self.vistas_guardadas.keys())
        if self.VISTA_GENERAL_NOMBRE not in vistas_disponibles:
            vistas_disponibles.insert(0, self.VISTA_GENERAL_NOMBRE)

        monitores = self.screen_mgr.monitors()
        for mon in monitores:
            # Obtener vista asignada a este monitor (por EDID/key)
            vista_asignada = store.get_monitor_view(mon.key)

            # Si no hay vista asignada, usar la vista activa actual o la primera disponible
            if not vista_asignada:
                vista_asignada = self.vista_activa_nombre or (vistas_disponibles[0] if vistas_disponibles else None)

            vw = ViewWindow(
                self,
                monitor=mon,
                x=mon.geometry.x(),
                y=mon.geometry.y(),
                w=mon.geometry.width(),
                h=mon.geometry.height(),
                fullscreen=True,
                title=f"VisionCore - {mon.display_name}",
                available_vistas=vistas_disponibles,
                current_vista=vista_asignada,
                on_vista_change_callback=lambda vn, mk=mon.key: self._on_viewwindow_vista_changed(mk, vn)
            )
            vw.closed.connect(lambda w=vw: self._on_viewwindow_closed(w))
            vw.vista_changed.connect(lambda vn: self._on_vista_changed_en_vivowindow(vn))
            vw.show()
            self.view_windows.append(vw)

            print(f"[Dashboard] ViewWindow creada en monitor {mon.display_name} con vista '{vista_asignada}'")

    def _on_viewwindow_vista_changed(self, monitor_key: str, vista_nombre: str):
        """Callback cuando cambia la vista en una ViewWindow - guarda en LayoutStore."""
        store = MultiLayoutStore.instance()
        store.set_monitor_view(monitor_key, vista_nombre)
        print(f"[Dashboard] Vista '{vista_nombre}' guardada para monitor {monitor_key}")

    def _on_viewwindow_closed(self, ventana):
        """Limpia la lista cuando se cierra una ViewWindow."""
        if ventana in self.view_windows:
            self.view_windows.remove(ventana)

    def _on_vista_changed_en_vivowindow(self, vista_nombre: str):
        """Señal de cambio de vista en ViewWindow (para logging)."""
        print(f"[Dashboard] ViewWindow cambió a vista: {vista_nombre}")

    def _actualizar_viewwindows_vista(self, vista_nombre: str):
        """Actualiza todas las ViewWindows abiertas a la nueva vista."""
        for vw in self.view_windows:
            if vw.isVisible():
                vw.set_current_vista(vista_nombre)
                # También actualizar el layout store para persistencia
                if vw.monitor():
                    MultiLayoutStore.instance().set_monitor_view(vw.monitor().key, vista_nombre)
                print(f"[Dashboard] ViewWindow en {vw.monitor().display_name if vw.monitor() else '?'} -> {vista_nombre}")

    def _aplicar_modo_ui(self):
        if not hasattr(self, 'sidebar'):
            return
        self.sidebar.setVisible(self.modo_ui == "dispositivos")

        # Si salimos del modo vivo, cerrar todas las ViewWindows
        if self.modo_ui != "vivo" and self._vista_en_vivo_activa:
            self._cerrar_viewwindows()
            self._vista_en_vivo_activa = False

    def _cerrar_viewwindows(self):
        """Cierra todas las ViewWindows abiertas."""
        for vw in self.view_windows[:]:
            try:
                vw.close()
            except Exception:
                pass
        self.view_windows.clear()

    def _on_monitores_cambiados(self, monitores):
        """Maneja cambios en el inventario de monitores (hot-plug v07)."""
        from monitores import LayoutStore
        store = LayoutStore.instance()

        activos = {m.key for m in monitores}
        anteriores = {vw.monitor().key for vw in self.view_windows if vw.monitor()}

        # 1. Cerrar ViewWindows de monitores desconectados
        for ventana in self.view_windows[:]:
            if ventana.monitor() and ventana.monitor().key not in activos:
                print(f"[Dashboard] Monitor desconectado: {ventana.monitor().display_name}")
                ventana.close()
                self.view_windows.remove(ventana)
                # Notificar desconexión
                if self.tray and self.tray.disponible:
                    self.tray.notify(
                        "VisionCore",
                        f"Monitor desconectado: {ventana.monitor().display_name}"
                    )

        # 2. Auto-crear ViewWindows para monitores NUEVOS si estamos en modo "Vista en Vivo"
        if self._vista_en_vivo_activa:
            nuevos = activos - anteriores
            for mon in monitores:
                if mon.key in nuevos:
                    print(f"[Dashboard] Monitor NUEVO detectado: {mon.display_name} ({mon.key})")
                    self._crear_ventana_para_monitor(mon)
                    # Notificar conexión
                    if self.tray and self.tray.disponible:
                        self.tray.notify(
                            "VisionCore",
                            f"Monitor conectado: {mon.display_name}\nVista en Vivo activada"
                        )

        # 3. Si no hay modo vivo, solo actualizar vista principal
        if not self._vista_en_vivo_activa:
            self._aplicar_vista_activa()

    def _crear_ventana_para_monitor(self, mon):
        """Crea una ViewWindow para un monitor específico (usado en hot-plug y apertura inicial)."""
        store = MultiLayoutStore.instance()

        vistas_disponibles = list(self.vistas_guardadas.keys())
        if self.VISTA_GENERAL_NOMBRE not in vistas_disponibles:
            vistas_disponibles.insert(0, self.VISTA_GENERAL_NOMBRE)

        # Obtener vista asignada a este monitor (por EDID/key)
        vista_asignada = store.get_monitor_view(mon.key)
        if not vista_asignada:
            vista_asignada = self.vista_activa_nombre or (vistas_disponibles[0] if vistas_disponibles else None)

        vw = ViewWindow(
            self,
            monitor=mon,
            x=mon.geometry.x(),
            y=mon.geometry.y(),
            w=mon.geometry.width(),
            h=mon.geometry.height(),
            fullscreen=True,
            title=f"VisionCore - {mon.display_name}",
            available_vistas=vistas_disponibles,
            current_vista=vista_asignada,
            on_vista_change_callback=lambda vn, mk=mon.key: self._on_viewwindow_vista_changed(mk, vn)
        )
        vw.closed.connect(lambda w=vw: self._on_viewwindow_closed(w))
        vw.vista_changed.connect(lambda vn: self._on_vista_changed_en_vivowindow(vn))
        vw.show()
        self.view_windows.append(vw)

        print(f"[Dashboard] ViewWindow auto-creada en monitor {mon.display_name} con vista '{vista_asignada}'")

    # =====================================================================
    # BARRA LATERAL DE VISTAS
    # =====================================================================
    def _agregar_sidebar_vistas(self):
        print("[Dashboard] Agregando barra lateral de vistas...")

        self.sidebar = SidebarVistas()

        if not hasattr(self.ui, 'layout_principal'):
            raise AttributeError("self.ui.layout_principal no existe. Revisa ui_dashboard.py")

        layout_principal = self.ui.layout_principal

        index = layout_principal.indexOf(self.ui.camaras_container)
        if index == -1:
            raise ValueError("camaras_container no encontrado en layout_principal")

        self.contenedor_sidebar_grid = QWidget()
        contenedor_layout = QHBoxLayout(self.contenedor_sidebar_grid)
        contenedor_layout.setContentsMargins(0, 0, 0, 0)
        contenedor_layout.setSpacing(0)

        contenedor_layout.addWidget(self.sidebar)

        self.ui.camaras_container.setParent(self.contenedor_sidebar_grid)
        contenedor_layout.addWidget(self.ui.camaras_container)

        layout_principal.insertWidget(index, self.contenedor_sidebar_grid)

        print("[Dashboard] Barra lateral insertada correctamente")

        self.sidebar.btn_nueva.clicked.connect(self.abrir_dialogo_nueva_vista)
        self.sidebar.btn_editar.clicked.connect(self.abrir_dialogo_editar_vista)
        self.sidebar.btn_borrar.clicked.connect(self.borrar_vista)
        self.sidebar.tree_vistas.itemClicked.connect(self.aplicar_vista_seleccionada)
        self.sidebar.tree_vistas.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.sidebar.tree_vistas.customContextMenuRequested.connect(self._menu_contextual_vistas)

        if hasattr(self.sidebar, "btn_nuevo_grupo"):
            self.sidebar.btn_nuevo_grupo.clicked.connect(self.abrir_gestion_grupos)
        if hasattr(self.sidebar, "btn_editar_grupo"):
            self.sidebar.btn_editar_grupo.clicked.connect(self.abrir_gestion_grupos)
        if hasattr(self.sidebar, "btn_borrar_grupo"):
            self.sidebar.btn_borrar_grupo.clicked.connect(self._borrar_grupo_seleccionado)

        self.sidebar.camara_doble_click.connect(self._mostrar_camara_fullscreen)

    # =====================================================================
    # VISTAS GUARDADAS
    # =====================================================================
    def _api_vistas_url(self, sufijo=""):
        return f"http://{self.server_host}:8000/api/vistas{sufijo}"

    def _api_headers(self):
        return {'Authorization': f'Bearer {self.api_token}', 'Content-Type': 'application/json'}

    def refrescar_datos_vistas(self):
        if not self.server_host or not self.api_token:
            print("[Dashboard] Sin server_host/api_token, no se pueden cargar vistas")
            return

        try:
            response = requests.get(self._api_vistas_url(), headers=self._api_headers(), timeout=10)
            if response.status_code == 200:
                self.vistas_guardadas = response.json().get("vistas", {})
                print(f"[Dashboard] {len(self.vistas_guardadas)} vista(s) cargadas del servidor")
            else:
                print(f"[Dashboard] No se pudieron cargar vistas: HTTP {response.status_code}")
                self.vistas_guardadas = {}
        except Exception as e:
            print(f"[Dashboard] Error cargando vistas del servidor: {e}")
            self.vistas_guardadas = {}

        self._actualizar_sidebar_con_camaras()

    def _nombre_vista_predeterminada(self):
        for nombre, datos in self.vistas_guardadas.items():
            if datos.get("predeterminada"):
                return nombre
        return self.VISTA_GENERAL_NOMBRE

    def cargar_vistas_desde_servidor(self):
        self.refrescar_datos_vistas()
        self.vista_activa_nombre = self._nombre_vista_predeterminada()
        print(f"[Dashboard] Vista activa al iniciar: {self.vista_activa_nombre}")
        self._aplicar_vista_activa()
        self._marcar_vista_activa_en_arbol()

    def _api_crear_vista(self, nombre, grid_id, asignaciones, predeterminada=False):
        response = requests.post(
            self._api_vistas_url(),
            headers=self._api_headers(),
            json={"nombre": nombre, "grid_id": grid_id, "asignaciones": asignaciones, "predeterminada": predeterminada},
            timeout=10
        )
        return response.status_code == 200

    def _api_editar_vista(self, nombre_actual, nombre_nuevo, grid_id, asignaciones, predeterminada=False):
        response = requests.put(
            self._api_vistas_url(f"/{nombre_actual}"),
            headers=self._api_headers(),
            json={"nombre": nombre_nuevo, "grid_id": grid_id, "asignaciones": asignaciones, "predeterminada": predeterminada},
            timeout=10
        )
        return response.status_code == 200

    def _api_borrar_vista(self, nombre):
        response = requests.delete(
            self._api_vistas_url(f"/{nombre}"),
            headers=self._api_headers(),
            timeout=10
        )
        return response.status_code == 200

    def _api_marcar_predeterminada(self, nombre):
        response = requests.post(
            self._api_vistas_url(f"/{nombre}/predeterminada"),
            headers=self._api_headers(),
            timeout=10
        )
        return response.status_code == 200

    def abrir_dialogo_nueva_vista(self):
        print("[Dashboard] Abriendo dialogo de nueva vista...")
        camaras = self._obtener_camaras_para_dialogo()
        dialogo = DialogoVista(self, modo="crear", camaras=camaras)
        if dialogo.exec() == QDialog.DialogCode.Accepted:
            datos = dialogo.obtener_datos()
            try:
                ok = self._api_crear_vista(datos["nombre"], datos["grid_id"], datos["asignaciones"], datos["predeterminada"])
            except Exception as e:
                print(f"[Dashboard] Error creando vista en el servidor: {e}")
                ok = False

            if ok:
                print(f"[Dashboard] Vista creada en el servidor: {datos['nombre']}")
                self.refrescar_datos_vistas()
            else:
                QMessageBox.warning(self, "Error", "No se pudo guardar la vista en el servidor.")

    def abrir_dialogo_editar_vista(self):
        print("[Dashboard] Abriendo dialogo de editar vista...")

        item_seleccionado = self.sidebar.tree_vistas.currentItem()
        if not item_seleccionado:
            QMessageBox.information(self, "Informacion", "Seleccione una vista para editar")
            return

        _, nombre_actual = self._resolver_item_vista(item_seleccionado)
        datos_guardados = self.vistas_guardadas.get(nombre_actual)
        if not datos_guardados:
            QMessageBox.information(self, "Informacion", "Esta vista no existe en el servidor.")
            return

        vista_existente = dict(datos_guardados)
        vista_existente["nombre"] = nombre_actual

        camaras = self._obtener_camaras_para_dialogo()
        dialogo = DialogoVista(self, modo="editar", camaras=camaras, vista_existente=vista_existente)
        if dialogo.exec() == QDialog.DialogCode.Accepted:
            datos = dialogo.obtener_datos()
            tipo_vista = datos.get("tipo_vista", "grid_normal")
            
            try:
                if tipo_vista == "videowall":
                    ok = self._api_crear_vista(
                        datos["nombre"], 
                        f"videowall_{datos['grid_filas']}x{datos['grid_cols']}", 
                        datos["asignaciones"], 
                        datos["predeterminada"]
                    )
                else:
                    ok = self._api_crear_vista(
                        datos["nombre"], 
                        datos["grid_id"], 
                        datos["asignaciones"], 
                        datos["predeterminada"]
                    )
            except Exception as e:
                print(f"[Dashboard] Error editando vista en el servidor: {e}")
                ok = False

            if ok:
                print(f"[Dashboard] Vista editada: {nombre_actual} -> {datos['nombre']}")
                if self.vista_activa_nombre == nombre_actual and datos["nombre"] != nombre_actual:
                    self.vista_activa_nombre = datos["nombre"]
                self.refrescar_datos_vistas()
                self._aplicar_vista_activa()
                self._marcar_vista_activa_en_arbol()
            else:
                QMessageBox.warning(self, "Error", "No se pudo guardar los cambios en el servidor.")

    def _obtener_camaras_para_dialogo(self):
        """
        ⭐ MULTI-LENTE: devuelve 1 entrada por worker_id (lente).
        Cada lente es un elemento independiente en el grid.
        """
        camaras = []
        for worker in self.camera_workers:
            wid = getattr(worker, 'worker_id', None)
            if wid:
                camaras.append({
                    "id": wid,                                # ⭐ cam01_lens1
                    "camera_index": getattr(worker, 'camera_index', ''),
                    "lens_id": getattr(worker, 'lens_id', 1),
                    "nombre": getattr(worker, 'name', wid)
                })
        return camaras

    def _resolver_item_vista(self, item):
        raiz = item
        while raiz.parent() is not None:
            raiz = raiz.parent()
        nombre_real = raiz.data(0, Qt.ItemDataRole.UserRole) or raiz.text(0)
        return raiz, nombre_real

    def _calcular_vista_general(self):
        camaras = self._obtener_camaras_para_dialogo()
        layout = layout_uniforme_para(len(camaras))
        asignaciones = {i: cam["id"] for i, cam in enumerate(camaras)}
        return layout.id, asignaciones

    def _aplicar_vista_activa(self):
        nombre = self.vista_activa_nombre

        if nombre == self.VISTA_GENERAL_NOMBRE:
            grid_id, asignaciones = self._calcular_vista_general()
        elif nombre in self.vistas_guardadas:
            datos = self.vistas_guardadas[nombre]
            tipo_vista = datos.get("tipo_vista", "grid_normal")
            
            if tipo_vista == "videowall":
                self._aplicar_videowall(datos)
                return
            
            grid_id = datos["grid_id"]
            asignaciones = datos["asignaciones"]
        else:
            return

        self.ui.widget_camaras.aplicar_vista_guardada(grid_id, asignaciones)

    def _aplicar_videowall(self, datos):
        """Aplica un videowall: crea ViewWindows por monitor con celdas correspondientes."""
        if not self._vista_en_vivo_activa:
            return
            
        filas = datos.get("grid_filas", 2)
        cols = datos.get("grid_cols", 4)
        asignaciones = datos.get("asignaciones", {})
        mapeo_auto = datos.get("mapeo_auto", True)
        
        # Obtener monitores actuales
        monitores = self.screen_mgr.monitors()
        if not monitores:
            return
            
        total_celdas = filas * cols
        celdas_por_monitor = max(1, total_celdas // len(monitores))
        
        # Cerrar ViewWindows existentes
        self._cerrar_viewwindows()
        
        # Crear ViewWindows por monitor
        for i, mon in enumerate(monitores):
            inicio = i * celdas_por_monitor
            fin = min(inicio + celdas_por_monitor, total_celdas)
            if inicio >= total_celdas:
                break
                
            # Crear sub-asignaciones para este monitor
            sub_asignaciones = {}
            for idx in range(inicio, fin):
                cam_id = asignaciones.get(str(idx))
                if cam_id:
                    sub_asignaciones[idx - inicio] = cam_id
            
            # Crear ViewWindow para este monitor con su parte del videowall
            vw = ViewWindow(
                self,
                monitor=mon,
                x=mon.geometry.x(),
                y=mon.geometry.y(),
                w=mon.geometry.width(),
                h=mon.geometry.height(),
                fullscreen=True,
                title=f"VisionCore - {mon.display_name} (Videowall {i+1}/{len(monitores)})",
                available_vistas=[],  # Videowall no usa combo de vistas
                current_vista=None,
                on_vista_change_callback=None
            )
            # TODO: Aplicar sub_asignaciones al grid_editor interno de ViewWindow
            vw.closed.connect(lambda w=vw: self._on_viewwindow_closed(w))
            vw.show()
            self.view_windows.append(vw)
            
        print(f"[Dashboard] Videowall aplicado: {filas}x{cols} en {len(monitores)} monitores")

    def _aplicar_vista_activa_debounced(self):
        if self._aplicar_vista_timer is not None:
            self._aplicar_vista_timer.stop()

        self._aplicar_vista_timer = QTimer()
        self._aplicar_vista_timer.setSingleShot(True)
        self._aplicar_vista_timer.timeout.connect(self._aplicar_vista_activa)
        self._aplicar_vista_timer.start(50)

    def _marcar_vista_activa_en_arbol(self):
        nombre = self.vista_activa_nombre
        if nombre is None:
            self.sidebar.tree_vistas.clearSelection()
            return

        for i in range(self.sidebar.tree_vistas.topLevelItemCount()):
            item = self.sidebar.tree_vistas.topLevelItem(i)
            if item.data(0, Qt.ItemDataRole.UserRole) == nombre:
                self.sidebar.tree_vistas.setCurrentItem(item)
                return

    def aplicar_vista_seleccionada(self, item, column):
        _, nombre = self._resolver_item_vista(item)

        if nombre == self.VISTA_GENERAL_NOMBRE:
            self.vista_activa_nombre = self.VISTA_GENERAL_NOMBRE
        elif nombre in self.vistas_guardadas:
            self.vista_activa_nombre = nombre
        else:
            return

        print(f"[Dashboard] Vista activa -> {self.vista_activa_nombre}")
        self._aplicar_vista_activa()

        # Si estamos en modo "vivo", actualizar también las ViewWindows
        if self._vista_en_vivo_activa:
            self._actualizar_viewwindows_vista(self.vista_activa_nombre)

    def _menu_contextual_vistas(self, posicion):
        item = self.sidebar.tree_vistas.itemAt(posicion)
        if not item:
            return

        _, nombre = self._resolver_item_vista(item)
        datos = self.vistas_guardadas.get(nombre)
        if not datos:
            return

        menu = QMenu(self)
        es_predeterminada = bool(datos.get("predeterminada"))
        accion_predeterminada = menu.addAction(
            "Quitar como predeterminada" if es_predeterminada else "Hacer predeterminada"
        )
        menu.addSeparator()
        accion_editar = menu.addAction("Editar vista")
        accion_borrar = menu.addAction("Borrar vista")

        accion = menu.exec(self.sidebar.tree_vistas.viewport().mapToGlobal(posicion))

        if accion == accion_predeterminada:
            if es_predeterminada:
                try:
                    ok = self._api_editar_vista(nombre, nombre, datos["grid_id"], datos["asignaciones"], False)
                except Exception as e:
                    print(f"[Dashboard] Error: {e}")
                    ok = False
            else:
                try:
                    ok = self._api_marcar_predeterminada(nombre)
                except Exception as e:
                    print(f"[Dashboard] Error: {e}")
                    ok = False
            if ok:
                self.refrescar_datos_vistas()
            else:
                QMessageBox.warning(self, "Error", "No se pudo actualizar la vista predeterminada.")

        elif accion == accion_editar:
            self.sidebar.tree_vistas.setCurrentItem(item)
            self.abrir_dialogo_editar_vista()

        elif accion == accion_borrar:
            self.sidebar.tree_vistas.setCurrentItem(item)
            self.borrar_vista()

    def _mostrar_camara_fullscreen(self, camera_id):
        print(f"[Dashboard] Mostrando camara en pantalla completa: {camera_id}")
        self.vista_activa_nombre = None
        self.sidebar.tree_vistas.clearSelection()
        self.ui.widget_camaras.aplicar_vista_guardada("1x1", {0: camera_id})

    def borrar_vista(self):
        print("[Dashboard] Borrando vista...")

        item_seleccionado = self.sidebar.tree_vistas.currentItem()
        if not item_seleccionado:
            QMessageBox.information(self, "Informacion", "Seleccione una vista para borrar")
            return

        _, nombre = self._resolver_item_vista(item_seleccionado)
        respuesta = QMessageBox.question(
            self, "Confirmar borrado", f"Borrar la vista '{nombre}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No
        )

        if respuesta != QMessageBox.StandardButton.Yes:
            return

        try:
            ok = self._api_borrar_vista(nombre)
        except Exception as e:
            print(f"[Dashboard] Error borrando vista: {e}")
            ok = False

        if not ok:
            QMessageBox.warning(self, "Error", "No se pudo borrar la vista en el servidor.")
            return

        if self.vista_activa_nombre == nombre:
            self.vista_activa_nombre = self.VISTA_GENERAL_NOMBRE

        self.refrescar_datos_vistas()
        self._aplicar_vista_activa()
        self._marcar_vista_activa_en_arbol()

    # =====================================================================
    # GRUPOS
    # =====================================================================
    def abrir_gestion_grupos(self):
        if not self.server_host or not self.api_token:
            return
        try:
            ventana = VentanaGrupos(
                parent=self, server_host=self.server_host, api_token=self.api_token,
            )
            if ventana.exec() == QDialog.DialogCode.Accepted:
                self._refrescar_grupos_sidebar()
        except Exception as e:
            print(f"[Dashboard] Error abriendo ventana de grupos: {e}")
            import traceback
            traceback.print_exc()

    def _refrescar_grupos_sidebar(self):
        if not hasattr(self, "groups_mgr"):
            return
        try:
            self.groups_mgr.load_from_server()
            grupos = self.groups_mgr.get_all_groups()
            self.grupos_guardados = {g["id"]: g for g in grupos}
            self.grupos_tree_cache = self.groups_mgr.get_groups_tree()
            if hasattr(self, "sidebar") and hasattr(self.sidebar, "poblar_grupos"):
                self.sidebar.poblar_grupos(self.grupos_tree_cache)
        except Exception as e:
            print(f"[Dashboard] Error refrescando grupos: {e}")

    # NOTA: _armar_arbol_grupos() se elimino. Ahora lo hace
    # self.groups_mgr.get_groups_tree() (GroupsManager)

    def _borrar_grupo_seleccionado(self):
        if not hasattr(self, "sidebar") or not hasattr(self.sidebar, "tree_grupos"):
            return
        item = self.sidebar.tree_grupos.currentItem()
        if not item:
            QMessageBox.information(self, "Info", "Selecciona un grupo en la pestana Grupos.")
            return
        gid = item.data(0, Qt.ItemDataRole.UserRole)
        if not gid or gid == "__sin_asignar__":
            return
        resp = QMessageBox.question(
            self, "Confirmar",
            f"Borrar el grupo '{item.text(0)}'?\n\nLas camaras dentro NO se borran.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if resp != QMessageBox.StandardButton.Yes:
            return
        try:
            if hasattr(self, "groups_mgr"):
                ok = self.groups_mgr.eliminar_grupo(gid)
                if ok:
                    self._refrescar_grupos_sidebar()
            else:
                print("[Dashboard] groups_mgr no disponible")
        except Exception as e:
            print(f"[Dashboard] Error borrando grupo: {e}")

    # =====================================================================
    # ⭐ MULTI-LENTE: FACTORY DE WORKERS
    # =====================================================================
    def _crear_workers_para_camara(self, cam_data):
        """
        Crea 1 DeviceController + N RemoteCameraWorker para una cámara.

        Args:
            cam_data: dict con la info de la cámara (debe tener 'lentes[]')

        Returns:
            (device_controller, [workers])
        """
        camera_index = cam_data.get('camera_index') or cam_data.get('id')
        nombre = cam_data.get('name') or cam_data.get('camera_name') or camera_index
        lentes = cam_data.get('lentes', [])
        ia_lens_id = cam_data.get('ia_lens_id', 1)

        # Si no hay lentes, armar 1 genérico
        if not lentes:
            lentes = [{
                'lens_id': 1,
                'source_token': None,
                'main_stream': cam_data.get('selected_main_profile'),
                'sub_stream': cam_data.get('selected_sub_profile'),
            }]
            print(f"[Dashboard] {camera_index}: sin lentes, armando 1 genérico")

        cantidad_lentes = len(lentes)

        # 1. DeviceController
        device = DeviceController(
            camera_index=camera_index,
            control_client=self.control_client,
            ia_lens_id=ia_lens_id,
        )
        self.device_controllers[camera_index] = device

        # 2. N workers
        workers = []
        for lente in lentes:
            lens_id = lente.get('lens_id', 1)
            source_token = lente.get('source_token')
            worker_id = f"{camera_index}_lens{lens_id}"

            camera_info = {
                "id": worker_id,
                "name": f"{nombre} · L{lens_id}" if cantidad_lentes > 1 else nombre,
                "camera_index": camera_index,
                "lens_id": lens_id,
                "source_token": source_token,
                "cantidad_lentes": cantidad_lentes,
                "ip": cam_data.get('ip', ''),
                "status": cam_data.get('status', 'online'),
            }

            worker = RemoteCameraWorker(
                camera_info,
                self.server_host,
                self.api_token,
                None,  # ⭐ control_client IGNORADO en el worker
            )
            worker.set_mostrar_zonas(self.mostrar_zonas)
            worker.alarma_detectada.connect(
                self.on_alarma_detectada,
                Qt.ConnectionType.UniqueConnection
            )
            worker.frame_updated.connect(self._on_frame_updated)
            # Sonido de desconexión (solo si está activo en preferencias).
            worker.connection_status_changed.connect(
                self._on_worker_connection_status,
                Qt.ConnectionType.UniqueConnection
            )

            # 3. Registrar worker en el DeviceController
            device.registrar_worker(worker)
            worker.device_controller = device  # ⭐ MULTI-LENTE

            worker.start()
            workers.append(worker)

            print(f"[Dashboard] {camera_index}: worker creado {worker_id}")

        # FIX MULTI-LENTE: pedir config por lentes
        QTimer.singleShot(300, device._pedir_config_de_todos_los_lentes)

        return device, workers

    # =====================================================================
    # CONFIGURACION DEL SERVIDOR REMOTO
    # =====================================================================
    def set_server_config(self, server_host, api_token):
        print(f"[Dashboard] ===== set_server_config RECIBIDO =====")
        print(f"[Dashboard] server_host recibido: {server_host}")
        print(f"[Dashboard] api_token recibido: {api_token is not None}")
        self.server_host = server_host
        self.api_token = api_token

        if hasattr(self, "groups_mgr"):
            self.groups_mgr.set_server_config(server_host, api_token)

        self.cargar_vistas_desde_servidor()
        self._refrescar_grupos_sidebar()

        self.modo_ui = "vivo"
        self._aplicar_modo_ui()

    def set_control_client(self, control_client):
        """
        ⭐ MULTI-LENTE: el control_client va a los DeviceController,
        no a los workers.
        """
        self.control_client = control_client

        # Si ya hay DeviceControllers creados, asignarles el control_client
        for camera_index, device in self.device_controllers.items():
            if device.control_client is not control_client:
                old_cc = device.control_client
                if old_cc is not None:
                    try:
                        old_cc.camera_added.disconnect(device._on_camera_added)
                        old_cc.camera_deleted.disconnect(device._on_camera_deleted)
                        old_cc.camera_updated.disconnect(device._on_camera_updated)
                        old_cc.state_sync.disconnect(device._on_sync_state)
                        old_cc.detections_received.disconnect(device._on_detections_received)
                        old_cc.alarma_recibida.disconnect(device._on_alarma_recibida)
                        old_cc.config_data_received.disconnect(device._on_config_received)
                        old_cc.zonas_update.disconnect(device._on_zonas_update)
                        old_cc.connection_status_changed.disconnect(device._on_connection_status)
                    except (TypeError, RuntimeError):
                        pass
                device.control_client = control_client
                device._conectar_senales()
                device._suscribir()
                print(f"[Dashboard] Control client asignado a DeviceController {camera_index}")

        # ⭐ Conectar eventos globales al dashboard
        if control_client:
            try:
                control_client.camera_added.disconnect(self._on_camera_added)
            except (TypeError, RuntimeError):
                pass
            try:
                control_client.camera_deleted.disconnect(self._on_camera_deleted)
            except (TypeError, RuntimeError):
                pass
            try:
                control_client.camera_updated.disconnect(self._on_camera_updated)
            except (TypeError, RuntimeError):
                pass
            try:
                control_client.state_sync.disconnect(self._on_state_sync)
            except (TypeError, RuntimeError):
                pass
            control_client.camera_added.connect(self._on_camera_added)
            control_client.camera_deleted.connect(self._on_camera_deleted)
            control_client.camera_updated.connect(self._on_camera_updated)
            control_client.state_sync.connect(self._on_state_sync)
            print(f"[Dashboard] Eventos globales conectados al dashboard")
        print(f"[Dashboard] Control client configurado")

    # =====================================================================
    # ACCIONES DEL MENU DE USUARIO
    # =====================================================================
    def _cambiar_password_usuario(self):
        if not self.server_host or not self.api_token:
            QMessageBox.warning(self, "Error", "No hay conexion con el servidor")
            return

        from dialogo_confirmacion import input_texto
        from PySide6.QtWidgets import QLineEdit

        old = input_texto("Cambiar contrasena", "Contrasena actual:", self, echo=QLineEdit.Password)
        if not old:
            return
        new = input_texto("Cambiar contrasena", "Nueva contrasena:", self, echo=QLineEdit.Password)
        if not new:
            return
        confirm = input_texto("Cambiar contrasena", "Confirmar nueva contrasena:", self, echo=QLineEdit.Password)
        if new != confirm:
            QMessageBox.warning(self, "Error", "Las contrasenas no coinciden")
            return

        try:
            r = requests.post(
                f"http://{self.server_host}:8000/api/users/change-password",
                json={"old_password": old, "new_password": new},
                headers={"Authorization": f"Bearer {self.api_token}"},
                timeout=10
            )
            if r.status_code == 200:
                QMessageBox.information(self, "Contrasena cambiada",
                    "Tu contrasena fue cambiada exitosamente.\n\nPor seguridad, vas a tener que iniciar sesion de nuevo.")
                self._cerrar_sesion()
            else:
                try:
                    detalle = r.json().get("detail", f"HTTP {r.status_code}")
                except Exception:
                    detalle = f"HTTP {r.status_code}"
                QMessageBox.warning(self, "Error", f"No se pudo cambiar: {detalle}")
        except requests.exceptions.ConnectionError:
            QMessageBox.warning(self, "Error", "No se pudo conectar al servidor")
        except requests.exceptions.Timeout:
            QMessageBox.warning(self, "Error", "Tiempo de espera agotado")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Error inesperado: {e}")

    def _cerrar_sesion(self):
        print(f"[Dashboard] Cerrando sesion (volviendo al login)...")

        # Al cerrar sesión no debe quedar ninguna alarma sonando.
        try:
            self.alarm_manager.stop_sonido_inmediato()
        except Exception:
            pass

        # Parar DeviceControllers
        for camera_index, device in list(self.device_controllers.items()):
            try:
                device.stop()
            except Exception as e:
                print(f"[Dashboard] Error deteniendo DeviceController: {e}")
        self.device_controllers.clear()

        # Parar workers
        for worker in self.camera_workers[:]:
            try:
                worker.stop()
            except Exception as e:
                print(f"[Dashboard] Error deteniendo worker: {e}")
        self.camera_workers.clear()

        if self.control_client:
            try:
                self.control_client.stop()
            except Exception as e:
                print(f"[Dashboard] Error deteniendo control client: {e}")
            self.control_client = None

        if hasattr(self, 'sesion_cerrada'):
            self.sesion_cerrada.emit()

        self.close()

    def _on_state_sync(self, cameras):
        print(f"[Dashboard] Estado sincronizado: {len(cameras)} camaras")
        self.on_camaras_actualizadas(cameras)

    def on_mostrar_zonas_changed(self, mostrar):
        self.mostrar_zonas = mostrar

        for worker in self.camera_workers:
            if hasattr(worker, 'set_mostrar_zonas'):
                worker.set_mostrar_zonas(mostrar)

        print(f"Mostrar zonas: {'ACTIVADO' if mostrar else 'DESACTIVADO'}")

    def _cambiar_stream_visualizacion(self, stream):
        print(f"\n[Dashboard] Cambiando stream de visualización a: {stream}")

        sufijos = {
            "MAIN":   "_main",
            "SUB":    "_sub",
            "3° SUB": "_sub",
        }
        sufijo = sufijos.get(stream, "_sub")

        if not self.camera_workers:
            print("[Dashboard] No hay cámaras para cambiar")
            return

        DELAY_ENTRE_CAMARAS_MS = 150

        def _reconectar_worker(worker):
            try:
                if hasattr(worker, 'cambiar_stream'):
                    worker.cambiar_stream(sufijo)
                    print(f"[Dashboard]   {worker.name} -> {sufijo}")
            except Exception as e:
                print(f"[Dashboard]   Error cambiando {worker.name}: {e}")

        for i, worker in enumerate(self.camera_workers):
            QTimer.singleShot(
                i * DELAY_ENTRE_CAMARAS_MS,
                lambda w=worker: _reconectar_worker(w)
            )

        print(f"[Dashboard] Cambio de stream programado para {len(self.camera_workers)} cámaras\n")

    # =====================================================================
    # CONEXION DE WIDGETS
    # =====================================================================
    def _conectar_widget_camara(self, widget):
        widget.senal_seleccionada.connect(self.on_camara_seleccionada)
        widget.senal_doble_click.connect(self.abrir_camara_flotante)
        widget.senal_config.connect(self.abrir_configuracion_camara)
        widget.senal_eliminar.connect(self.eliminar_camara)
        widget.senal_deshabilitar.connect(self.toggle_deshabilitar_camara)

        if hasattr(widget, 'camera_worker') and widget.camera_worker:
            try:
                widget.camera_worker.frame_updated.disconnect(self._on_frame_updated)
            except:
                pass
            widget.camera_worker.frame_updated.connect(self._on_frame_updated)

            widget.camera_worker.alarma_detectada.connect(
                self.on_alarma_detectada,
                Qt.ConnectionType.UniqueConnection
            )

            print(f"[Dashboard] Senales conectadas para {widget.camera_worker.name}")

    def _on_frame_updated(self, frame, camera_name):
        if frame is None:
            return

        if hasattr(self.ui, 'widget_camaras'):
            for widget in self.ui.widget_camaras.camaras_reales:
                if hasattr(widget, 'camera_worker') and widget.camera_worker:
                    if widget.camera_worker.name == camera_name:
                        if hasattr(widget, 'update_frame'):
                            widget.update_frame(frame)
                        widget.update()
                        break

    # =====================================================================
    # EVENTOS DE CAMARAS
    # =====================================================================
    def _on_camera_added(self, camera_data):
        print(f"\n[Dashboard] Camara agregada por el servidor: {camera_data.get('camera_name')}")

        camera_index = camera_data.get('camera_index', 'cam01')

        # Si ya existe el DeviceController, ignorar
        if camera_index in self.device_controllers:
            print(f"   Camara ya existe, ignorando")
            return

        # Crear DeviceController + N workers
        device, workers = self._crear_workers_para_camara(camera_data)

        # Agregar workers a la lista
        for w in workers:
            self.camera_workers.append(w)

            if hasattr(self.ui, 'widget_camaras'):
                widget = self.ui.widget_camaras.agregar_camara_widget(w)
                if widget:
                    self._conectar_widget_camara(widget)

        self._aplicar_vista_activa_debounced()
        self._actualizar_sidebar_con_camaras()

        print(f"[Dashboard] {len(workers)} worker(s) creado(s) para {camera_index}")

    def _on_camera_deleted(self, camera_id):
        print(f"\n[Dashboard] Camara eliminada por el servidor: {camera_id}")

        # Parar DeviceController
        device = self.device_controllers.pop(camera_id, None)
        if device:
            try:
                device.stop()
            except Exception as e:
                print(f"[Dashboard] Error deteniendo DeviceController: {e}")

        # Parar todos los workers de esa cámara
        workers_a_eliminar = [
            w for w in self.camera_workers
            if str(getattr(w, 'camera_index', '')) == str(camera_id)
        ]
        for worker in workers_a_eliminar:
            try:
                worker.stop()
            except Exception as e:
                print(f"[Dashboard] Error deteniendo worker: {e}")
            self.camera_workers.remove(worker)

            if hasattr(self.ui, 'widget_camaras'):
                self.ui.widget_camaras.quitar_camara_widget(worker)

        self._aplicar_vista_activa_debounced()
        self.refrescar_datos_vistas()

        print(f"[Dashboard] Camara {camera_id} eliminada completamente")

    def _on_camera_updated(self, camera_id, camera_data):
        print(f"[Dashboard] Camara actualizada: {camera_id}")

        # Actualizar nombre en todos los workers de esa cámara
        for worker in self.camera_workers:
            if str(getattr(worker, 'camera_index', '')) == str(camera_id):
                if 'camera_name' in camera_data:
                    old_name = worker.name
                    worker.name = camera_data['camera_name']
                # Reconectar el worker para que tome la nueva URL
                try:
                    if hasattr(worker, 'cambiar_stream'):
                        worker.cambiar_stream('_sub')
                        print(f"[Dashboard] {worker.name}: forzando reconexion al stream actualizado")
                except Exception as e:
                    print(f"[Dashboard] Error reconectando {worker.name}: {e}")

        # Actualizar widgets
        if hasattr(self.ui, 'widget_camaras'):
            for widget in self.ui.widget_camaras.camaras_reales:
                widget_worker = getattr(widget, 'camera_worker', None)
                if widget_worker:
                    if str(getattr(widget_worker, 'camera_index', '')) == str(camera_id):
                        if hasattr(widget, 'actualizar_nombre'):
                            widget.actualizar_nombre(widget_worker.name)
                        widget.update()

        self._actualizar_sidebar_con_camaras()

    # =====================================================================
    # SIDEBAR
    # =====================================================================
    def _actualizar_sidebar_con_camaras(self):
        print("[Dashboard] Actualizando barra lateral...")

        self.sidebar.tree_vistas.clear()

        camaras_dialogo = self._obtener_camaras_para_dialogo()
        camaras_por_id = {c["id"]: c["nombre"] for c in camaras_dialogo}

        self.sidebar.agregar_vista(
            self.VISTA_GENERAL_NOMBRE, len(camaras_dialogo),
            [c["nombre"] for c in camaras_dialogo],
            es_sistema=True
        )

        for nombre, datos in self.vistas_guardadas.items():
            nombres_camaras = [
                camaras_por_id.get(cam_id, cam_id)
                for cam_id in datos.get("asignaciones", {}).values()
            ]
            self.sidebar.agregar_vista(
                nombre, len(nombres_camaras), nombres_camaras,
                predeterminada=bool(datos.get("predeterminada", False))
            )

        # FIX MULTI-LENTE: agrupar por camara para sidebar jerarquico
        camaras_jerarquicas = {}
        for w in self.camera_workers:
            cam_index = getattr(w, 'camera_index', '')
            if not cam_index:
                continue
            if cam_index not in camaras_jerarquicas:
                nombre_cam = cam_index
                for c in camaras_dialogo:
                    if c.get('camera_index') == cam_index:
                        nombre_cam = c.get('nombre', cam_index).split(' · ')[0]
                        break
                camaras_jerarquicas[cam_index] = {
                    'camera_index': cam_index,
                    'nombre': nombre_cam,
                    'online': getattr(w, 'connected', False),
                    'lentes': []
                }
            lens_id = getattr(w, 'lens_id', 1)
            worker_id = getattr(w, 'worker_id', f'{cam_index}_lens{lens_id}')
            camaras_jerarquicas[cam_index]['lentes'].append({
                'worker_id': worker_id,
                'nombre': f'L{lens_id}',
                'online': getattr(w, 'connected', False),
            })
            if getattr(w, 'connected', False):
                camaras_jerarquicas[cam_index]['online'] = True

        self.sidebar.poblar_camaras(list(camaras_jerarquicas.values()))

        self._marcar_vista_activa_en_arbol()

        print(f"[Dashboard] Barra lateral actualizada: {len(self.vistas_guardadas)} vista(s), {len(camaras_jerarquicas)} camara(s) con lentes")

    # =====================================================================
    # CALLBACKS DE WIDGETS DE CAMARA
    # =====================================================================
    def on_camara_seleccionada(self, nombre_camara):
        print(f"Camara seleccionada: {nombre_camara}")

    def abrir_camara_flotante(self, camera_worker):
        if hasattr(self.ui, 'widget_camaras'):
            self.ui.widget_camaras.abrir_flotante(camera_worker)

    def abrir_configuracion_camara(self, camera_worker):
        try:
            from remote_camera_worker import RemoteCameraWorker
            if not isinstance(camera_worker, RemoteCameraWorker):
                print(f"[ERROR] abrir_configuracion_camara recibio {type(camera_worker)}, esperaba RemoteCameraWorker")
                QMessageBox.critical(self, "Error", "Error interno: Tipo de worker incorrecto")
                return

            print(f"[Dashboard] Abriendo configuracion para {camera_worker.name}")

            if hasattr(self.ui, 'widget_camaras'):
                self.ui.widget_camaras.deseleccionar_todas()

            ventana_config = VentanaConfiguracion_Camara()
            ventana_config.deteccion_activada.connect(self.activar_deteccion)
            ventana_config.deteccion_desactivada.connect(self.desactivar_deteccion)
            ventana_config.configuracion_guardada.connect(self.on_configuracion_guardada)
            ventana_config.set_camera_worker(camera_worker)
            ventana_config.setWindowModality(Qt.WindowModality.ApplicationModal)
            ventana_config.show()

        except Exception as e:
            print(f"Error abriendo configuracion: {e}")

    def on_configuracion_guardada(self):
        print("Configuracion guardada, actualizando dashboard...")

        if hasattr(self.ui, 'widget_camaras'):
            for widget in self.ui.widget_camaras.camaras_reales:
                if hasattr(widget, 'camera_worker') and widget.camera_worker:
                    frame = widget.camera_worker.get_frame_copy()
                    if frame is not None and hasattr(widget, 'update_frame'):
                        widget.update_frame(frame)
                    widget.update()
            self.ui.widget_camaras.update()

        print("Dashboard actualizado")

    def activar_deteccion(self, identificador):
        nombre = identificador.get('nombre', '')
        print(f"Activando deteccion para: {nombre}")

    def desactivar_deteccion(self, identificador):
        nombre = identificador.get('nombre', '')
        print(f"Desactivando deteccion para: {nombre}")

    def toggle_deshabilitar_camara(self, datos):
        camera_index = datos.get('camera_index', '')
        widget = datos.get('widget')
        deshabilitada = datos.get('deshabilitada', False)

        print(f"[Dashboard] Toggle disable: {camera_index} -> deshabilitada={deshabilitada}")

        if hasattr(self.ui, 'widget_camaras'):
            worker = None
            for w in self.camera_workers:
                if getattr(w, 'camera_index', '') == camera_index:
                    worker = w
                    break

            if worker:
                self.ui.widget_camaras.deshabilitar_camara_widget(worker, deshabilitada)
            elif widget is not None:
                widget.set_deshabilitada(deshabilitada)

    def eliminar_camara(self, datos_eliminacion):
        print(f"\nEliminando camara: {datos_eliminacion.get('name')}")

        camera_name = datos_eliminacion.get('name')
        camera_index = datos_eliminacion.get('camera_index')

        if not confirmar_con_mensaje(f"Eliminar la camara '{camera_name}'?", self):
            print(f"Eliminacion cancelada")
            return

        for worker in self.camera_workers:
            if hasattr(worker, 'camera_index') and worker.camera_index == camera_index:
                if hasattr(worker, 'eliminar_camara'):
                    print(f"   Enviando solicitud de eliminacion al servidor...")
                    success = worker.eliminar_camara()
                    if success:
                        print(f"   Camara {camera_name} eliminada")
                        try:
                            worker.stop()
                        except Exception as e:
                            print(f"   Error deteniendo worker: {e}")
                        self.camera_workers.remove(worker)
                        if hasattr(self.ui, 'widget_camaras'):
                            self.ui.widget_camaras.quitar_camara_widget(worker)
                        self._aplicar_vista_activa()
                        self.refrescar_datos_vistas()
                    else:
                        print(f"   Error al eliminar")
                        QMessageBox.warning(self, "Error", f"No se pudo eliminar la camara {camera_name}")
                break

    # =====================================================================
    # CUADRICULA / VISTA MANUAL
    # =====================================================================
    def toggle_combo_vistas(self):
        self.ui.combo_vistas.setVisible(not self.ui.combo_vistas.isVisible())
        if self.ui.combo_vistas.isVisible():
            self.ui.combo_vistas.setFocus()

    def cambiar_vista(self, vista):
        self.vista_activa_nombre = None
        self.sidebar.tree_vistas.clearSelection()
        if hasattr(self.ui, 'widget_camaras'):
            self.ui.widget_camaras.cambiar_vista(vista)

    def on_vista_cambiada(self, vista):
        print(f"Vista cambiada a: {vista}")

    # =====================================================================
    # GESTION DE CAMARAS (ventana)
    # =====================================================================
    def abrir_gestion_camaras(self):
        try:
            for widget in QApplication.topLevelWidgets():
                if isinstance(widget, VentanaGestion) and widget.isVisible():
                    widget.raise_()
                    widget.activateWindow()
                    return

            if not self.server_host or not self.api_token:
                QMessageBox.warning(self, "Error", "No hay configuracion de servidor")
                return

            ventana_gestion = VentanaGestion(
                self, server_host=self.server_host, api_token=self.api_token
            )
            ventana_gestion.setWindowModality(Qt.WindowModality.ApplicationModal)
            ventana_gestion.show()

        except Exception as e:
            print(f"Error: {e}")
            import traceback
            traceback.print_exc()
            QMessageBox.critical(self, "Error", f"{e}")

    def on_camaras_actualizadas(self, cameras_data=None):
        print(f"\n{'='*60}")
        print(f"[Dashboard] on_camaras_actualizadas() INICIADO")
        print(f"{'='*60}")

        # IDs actuales = camera_index únicos de los workers
        ids_actuales = set()
        for w in self.camera_workers:
            # ⭐ MULTI-LENTE: usar worker_id (cam01_lens1), no camera_index
            wid = getattr(w, 'worker_id', None)
            if wid:
                ids_actuales.add(str(wid))

        print(f"[Dashboard] Estado ANTES: {len(ids_actuales)} camaras, "
              f"{len(self.camera_workers)} workers")

        if cameras_data is None:
            try:
                response = requests.get(
                    f"http://{self.server_host}:8000/api/cameras",
                    headers={'Authorization': f'Bearer {self.api_token}'},
                    timeout=10
                )
                if response.status_code == 200:
                    cameras_data = response.json()
                else:
                    return
            except Exception as e:
                print(f"[Dashboard] Error pidiendo camaras: {e}")
                return

        if not cameras_data:
            return

        # Construir map worker_id -> cam_data
        # ⭐ MULTI-LENTE: 1 entrada por worker_id (lente)
        camaras_por_id = {}
        for cam in cameras_data:
            wid = cam.get('id') or cam.get('worker_id')
            if wid:
                camaras_por_id[str(wid)] = cam

        ids_nuevos = set(camaras_por_id.keys())

        ids_a_agregar = list(ids_nuevos - ids_actuales)
        ids_a_quitar = list(ids_actuales - ids_nuevos)
        ids_a_mantener = list(ids_actuales & ids_nuevos)

        print(f"[Dashboard] A AGREGAR: {ids_a_agregar}")
        print(f"[Dashboard] A QUITAR:  {ids_a_quitar}")
        print(f"[Dashboard] A MANTENER: {ids_a_mantener}")

        # QUITAR
        if ids_a_quitar:
            camera_indices_a_quitar = set()
            for worker_id in ids_a_quitar:
                if '_lens' in worker_id:
                    camera_index = worker_id.rsplit('_lens', 1)[0]
                else:
                    camera_index = worker_id
                camera_indices_a_quitar.add(camera_index)

                for worker in list(self.camera_workers):
                    if str(getattr(worker, 'worker_id', '')) == worker_id:
                        try:
                            worker.stop()
                        except Exception:
                            pass
                        self.camera_workers.remove(worker)
                        if hasattr(self.ui, 'widget_camaras'):
                            self.ui.widget_camaras.quitar_camara_widget(worker)

            for camera_index in camera_indices_a_quitar:
                device = self.device_controllers.pop(camera_index, None)
                if device:
                    try:
                        device.stop()
                    except Exception:
                        pass

        # AGREGAR
        if ids_a_agregar:
            for i, camera_id in enumerate(ids_a_agregar):
                cam_data = camaras_por_id.get(camera_id, {})

                try:
                    device, workers = self._crear_workers_para_camara(cam_data)

                    for w in workers:
                        self.camera_workers.append(w)
                        if hasattr(self.ui, 'widget_camaras'):
                            widget = self.ui.widget_camaras.agregar_camara_widget(w)
                            if widget:
                                self._conectar_widget_camara(widget)

                    if i < len(ids_a_agregar) - 1:
                        time.sleep(0.1)

                except Exception as e:
                    print(f"      Error agregando {camera_id}: {e}")
                    import traceback
                    traceback.print_exc()

        # Reaplicar vista
        self._aplicar_vista_activa()
        self._actualizar_sidebar_con_camaras()

        print(f"[Dashboard] on_camaras_actualizadas() COMPLETADO")
        print(f"{'='*60}\n")

    # =====================================================================
    # SALIR / BANDEJA
    # =====================================================================
    def _crear_bandeja(self):
        icon_path = os.path.join(os.path.dirname(__file__), "iconos", "bandeja.ico")
        self.tray = TrayController(
            parent=self,
            icon_path=icon_path,
            tooltip="VisionCore - Cliente de vigilancia",
        )
        self.tray.mostrar_solicitado.connect(self._restaurar_desde_bandeja)
        self.tray.configurar_solicitado.connect(self._configurar_desde_bandeja)
        self.tray.salir_solicitado.connect(self._salir_desde_bandeja)
        if self.tray.disponible:
            self.tray.show()

    def minimizar_ventana(self):
        """Minimiza a la bandeja o a la barra de tareas según la preferencia."""
        usar_bandeja = True
        try:
            usar_bandeja = bool(
                get_preferencias_maquina().get("inicio", "minimizar_a_bandeja", True)
            )
        except Exception:
            pass

        if usar_bandeja and self.tray is not None and self.tray.disponible:
            self.hide()
            if not self._tray_aviso:
                self.tray.notify(
                    "VisionCore",
                    "El cliente sigue ejecutándose en la bandeja del sistema.",
                )
                self._tray_aviso = True
        else:
            self.showMinimized()

    def _restaurar_desde_bandeja(self):
        if self.isMaximized():
            self.showMaximized()
        else:
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def _configurar_desde_bandeja(self):
        self._restaurar_desde_bandeja()
        try:
            self.ui.abrir_configuracion_global()
        except Exception as e:
            print(f"[Dashboard] No se pudo abrir configuración desde bandeja: {e}")

    def _salir_desde_bandeja(self):
        # Se restaura para que el diálogo de confirmación sea visible.
        self._restaurar_desde_bandeja()
        self.salir_aplicacion()

    def closeEvent(self, event):
        # Alt+F4 o cierre del sistema: pasa por la confirmación normal.
        if self._saliendo:
            event.accept()
            return
        event.ignore()
        self.salir_aplicacion()

    def _confirmar_cierre_aplicacion(self):
        """Respeta la preferencia 'Confirmación al cerrar' de Mi cuenta."""
        modo = "Solo si hay cambios"
        try:
            modo = get_preferencias_usuario().get("sesion", "confirmar_cierre", modo)
        except Exception:
            pass

        if modo == "Nunca":
            return True

        if modo == "Solo si hay cambios":
            ventana = getattr(self, "ventana_config_global", None)
            hay_cambios = False
            if ventana is not None and ventana.isVisible():
                seccion = getattr(ventana, "seccion_actual", None)
                if seccion is not None and hasattr(seccion, "hay_cambios"):
                    try:
                        hay_cambios = bool(seccion.hay_cambios())
                    except Exception:
                        hay_cambios = False
            if not hay_cambios:
                return True

        return confirmar_con_mensaje("Salir de la aplicacion?", self)

    def salir_aplicacion(self):
        if not self._confirmar_cierre_aplicacion():
            return
        self._saliendo = True
        if self.tray is not None:
            self.tray.hide()
        print("Cerrando aplicacion...")

        try:
            self.alarm_manager.stop_sonido_inmediato()
        except Exception:
            pass

        # Parar DeviceControllers
        for camera_index, device in list(self.device_controllers.items()):
            try:
                device.stop()
            except:
                pass
        self.device_controllers.clear()

        # Parar workers
        for camera_worker in self.camera_workers[:]:
            try:
                camera_worker.stop()
            except:
                pass
        self.camera_workers.clear()

        if self.control_client:
            try:
                self.control_client.stop()
            except:
                pass
            self.control_client = None

        if self.alarm_manager:
            try:
                self.alarm_manager.stop()
                self.alarm_manager.cleanup()
            except:
                pass
            self.alarm_manager = None

        if hasattr(self.ui, 'widget_camaras') and hasattr(self.ui.widget_camaras, 'widget_flotante_actual'):
            if self.ui.widget_camaras.widget_flotante_actual:
                try:
                    self.ui.widget_camaras.widget_flotante_actual.cerrar()
                except:
                    pass

        self.close()
        QApplication.quit()

    # =====================================================================
    # RELOJ
    # =====================================================================
    def actualizar_hora(self):
        try:
            from datetime import timedelta
            prefs = get_preferencias_usuario()
            formato_fecha = prefs.get('idioma_region', 'formato_fecha', 'DD/MM/YYYY')
            formato_hora = prefs.get('idioma_region', 'formato_hora', '24h')
            zona_horaria_texto = prefs.get('idioma_region', 'zona_horaria', 'UTC-03:00 (Buenos Aires, Brasilia, Montevideo)')
            zona_horaria = zona_horaria_texto.split(" ")[0] if zona_horaria_texto else 'UTC-03:00'

            zona_offsets = {
                "UTC-12:00": -12, "UTC-11:00": -11, "UTC-10:00": -10, "UTC-09:00": -9, "UTC-08:00": -8,
                "UTC-07:00": -7, "UTC-06:00": -6, "UTC-05:00": -5, "UTC-04:00": -4, "UTC-03:00": -3,
                "UTC-02:00": -2, "UTC-01:00": -1, "UTC+00:00": 0, "UTC+01:00": 1, "UTC+02:00": 2,
                "UTC+03:00": 3, "UTC+04:00": 4, "UTC+05:00": 5, "UTC+06:00": 6, "UTC+07:00": 7,
                "UTC+08:00": 8, "UTC+09:00": 9, "UTC+10:00": 10, "UTC+11:00": 11, "UTC+12:00": 12
            }
            offset_horas = zona_offsets.get(zona_horaria, -3)

            ahora_utc = datetime.utcnow()
            ahora = ahora_utc + timedelta(hours=offset_horas)

            mapa_fecha = {"DD/MM/YYYY": "%d/%m/%Y", "MM/DD/YYYY": "%m/%d/%Y", "YYYY-MM-DD": "%Y-%m-%d"}
            formato_fecha_str = mapa_fecha.get(formato_fecha, "%d/%m/%Y")

            if formato_hora == "24h":
                formato_hora_str = "%H:%M:%S"
            else:
                formato_hora_str = "%I:%M:%S %p"

            fecha_str = ahora.strftime(formato_fecha_str)
            hora_str = ahora.strftime(formato_hora_str)
            texto_final = f"{fecha_str}    {hora_str}"
            self.ui.fecha_label.setText(texto_final)

        except Exception:
            ahora = datetime.now().strftime("%d/%m/%Y    %H:%M:%S")
            self.ui.fecha_label.setText(ahora)

    def recargar_configuracion_global(self):
        try:
            self.config_global = self.config_manager.load_global_config()
            self.actualizar_hora()
            print("[Dashboard] Configuracion global recargada")
        except Exception as e:
            print(f"Error recargando: {e}")

    def set_visioncore_system(self, vision_system):
        pass

    def reconectar_senales_camaras(self):
        print(f"\n[Dashboard] Reconectando senales")
        if not hasattr(self.ui, 'widget_camaras'):
            return
        for widget in self.ui.widget_camaras.camaras_reales:
            try:
                self._conectar_widget_camara(widget)
            except Exception as e:
                print(f"   Error: {e}")
        print(f"[Dashboard] Senales reconectadas")

    def obtener_widget_por_nombre(self, camera_name):
        if hasattr(self.ui, 'widget_camaras'):
            for widget in self.ui.widget_camaras.camaras_reales:
                if hasattr(widget, 'camera_worker') and widget.camera_worker:
                    if widget.camera_worker.name == camera_name:
                        return widget
        return None

    def on_alarma_detectada(self, camera_name, tipo_alarma, comando="start"):
        if tipo_alarma == "visual":
            widget = self.obtener_widget_por_nombre(camera_name)
            if widget:
                if comando == "start":
                    widget.mostrar_alarma_visual(True)
                elif comando == "stop":
                    widget.mostrar_alarma_visual(False)
        elif tipo_alarma == "sonora_local":
            pass

    def _on_worker_connection_status(self, camera_name, connected, status):
        """Corta alarmas y avisa cuando se pierde el stream de una cámara."""
        if connected:
            return
        try:
            self.alarm_manager.cliente_desconectado(f"stream {camera_name}")
        except Exception as e:
            print(f"[Dashboard] Error sonido desconexión: {e}")

    @Slot(list)
    def on_cameras_info_updated(self, cameras_data):
        pass

    def set_current_user(self, user_data: dict):
        if not user_data:
            return
        self.current_user = user_data
        set_usuario_activo(user_data.get('username'), user_data.get('id'))
        try:
            self.alarm_manager.apply_preferences()
        except Exception as e:
            print(f"[Dashboard] No se pudieron aplicar preferencias de sonido: {e}")
        if hasattr(self.ui, 'admin_label'):
            self.ui.admin_label.setText(user_data.get('username', 'Admin'))
        rol = user_data.get('role', 'viewer')
        if hasattr(self.ui, 'conectado_label'):
            self.ui.conectado_label.setText(f"Rol: {rol}")
        self.user_permissions = user_data.get('permissions', [])
        self._apply_role_restrictions(rol)

    def set_auth_manager(self, auth_manager):
        self.auth_manager = auth_manager

    def set_auth_flow(self, auth_flow):
        self.auth_flow = auth_flow

    def _apply_role_restrictions(self, role: str):
        if role not in ['super_admin', 'admin']:
            if hasattr(self.ui, 'btn_borrar'):
                self.ui.btn_borrar.hide()
            if hasattr(self.ui, 'widget_camaras'):
                for widget in self.ui.widget_camaras.camaras_reales:
                    if hasattr(widget, 'boton_borrar'):
                        widget.boton_borrar.hide()
        else:
            if hasattr(self.ui, 'btn_borrar'):
                self.ui.btn_borrar.show()

    def has_permission(self, permission_str: str) -> bool:
        if not self.current_user:
            return False
        if self.current_user.get('role') == 'super_admin':
            return True
        return permission_str in self.user_permissions