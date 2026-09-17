"""
Compatibilidad: el control de acceso vive ahora en `permissions.py`, donde cada
capacidad está declarada junto a las demás. Este módulo solo reexporta.
"""
from backend.apps.webapp.permissions import (  # noqa: F401
    admin_required, dashboard_required, tickets_required,
)
