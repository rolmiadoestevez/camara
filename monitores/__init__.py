# monitores - Paquete de gestión multi-monitor
# Inventario, layouts persistentes y ventanas de vista por monitor

from .screen_manager import ScreenManager, MonitorInfo, get_screen_manager
from .layout_store import LayoutStore, LayoutController, ViewWindow, get_layout_controller
from .ui_view_window import ViewWindow as ViewWindowUI, create_view_window

__all__ = [
    "ScreenManager",
    "MonitorInfo",
    "get_screen_manager",
    "LayoutStore",
    "LayoutController",
    "ViewWindow",
    "get_layout_controller",
    "ViewWindowUI",
    "create_view_window",
]