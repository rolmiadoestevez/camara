# routers/vistas_router.py
# Endpoints: /api/vistas/*

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from typing import Dict, Optional

from shared_state import get_visioncore, config_manager
from auth_dependencies import get_current_user


router = APIRouter()


class VistaGuardadaRequest(BaseModel):
    nombre: str
    grid_id: str
    asignaciones: Dict[str, str] = {}
    predeterminada: bool = False
    nombre_anterior: Optional[str] = None


@router.get("/api/vistas")
async def listar_vistas(current_user: dict = Depends(get_current_user)):
    try:
        vc = get_visioncore()
        if not vc or not hasattr(vc, 'config_manager'):
            raise HTTPException(status_code=503, detail="Config manager no disponible")
        vistas = vc.config_manager.load_vistas_config()
        return {"vistas": vistas}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[WebAPI] Error listando vistas: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/vistas")
async def crear_vista(datos: VistaGuardadaRequest, current_user: dict = Depends(get_current_user)):
    try:
        vc = get_visioncore()
        if not vc or not hasattr(vc, 'config_manager'):
            raise HTTPException(status_code=503, detail="Config manager no disponible")
        if not datos.nombre.strip():
            raise HTTPException(status_code=400, detail="El nombre no puede estar vacio")

        ok = vc.config_manager.guardar_vista(
            datos.nombre, datos.grid_id, datos.asignaciones, predeterminada=datos.predeterminada
        )
        if not ok:
            raise HTTPException(status_code=500, detail="No se pudo guardar la vista")

        print(f"[WebAPI] Vista creada: {datos.nombre} ({datos.grid_id}, {len(datos.asignaciones)} camaras)")
        return {"ok": True, "nombre": datos.nombre}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[WebAPI] Error creando vista: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/api/vistas/{nombre_actual}")
async def editar_vista(nombre_actual: str, datos: VistaGuardadaRequest,
                       current_user: dict = Depends(get_current_user)):
    try:
        vc = get_visioncore()
        if not vc or not hasattr(vc, 'config_manager'):
            raise HTTPException(status_code=503, detail="Config manager no disponible")
        if not datos.nombre.strip():
            raise HTTPException(status_code=400, detail="El nombre no puede estar vacio")

        vistas_actuales = vc.config_manager.load_vistas_config()
        if nombre_actual not in vistas_actuales:
            raise HTTPException(status_code=404, detail=f"Vista '{nombre_actual}' no encontrada")

        ok = vc.config_manager.renombrar_vista(
            nombre_actual, datos.nombre, datos.grid_id, datos.asignaciones,
            predeterminada=datos.predeterminada
        )
        if not ok:
            raise HTTPException(status_code=500, detail="No se pudo guardar la vista")

        print(f"[WebAPI] Vista editada: {nombre_actual} -> {datos.nombre} ({datos.grid_id})")
        return {"ok": True, "nombre": datos.nombre}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[WebAPI] Error editando vista: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/vistas/{nombre}/predeterminada")
async def marcar_vista_predeterminada_api(nombre: str,
                                          current_user: dict = Depends(get_current_user)):
    try:
        vc = get_visioncore()
        if not vc or not hasattr(vc, 'config_manager'):
            raise HTTPException(status_code=503, detail="Config manager no disponible")

        ok = vc.config_manager.marcar_vista_predeterminada(nombre)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Vista '{nombre}' no encontrada")

        print(f"[WebAPI] Vista predeterminada: {nombre}")
        return {"ok": True, "nombre": nombre}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[WebAPI] Error marcando predeterminada: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/vistas/{nombre}")
async def borrar_vista_api(nombre: str, current_user: dict = Depends(get_current_user)):
    try:
        vc = get_visioncore()
        if not vc or not hasattr(vc, 'config_manager'):
            raise HTTPException(status_code=503, detail="Config manager no disponible")

        ok = vc.config_manager.eliminar_vista(nombre)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Vista '{nombre}' no encontrada")

        print(f"[WebAPI] Vista borrada: {nombre}")
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[WebAPI] Error borrando vista: {e}")
        raise HTTPException(status_code=500, detail=str(e))