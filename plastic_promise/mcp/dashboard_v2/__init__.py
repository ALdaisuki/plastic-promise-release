"""Scoped, read-only Dashboard V2 for Plastic Promise."""

from plastic_promise.mcp.dashboard_v2.config import (
    DashboardAccessError,
    DashboardConfigurationError,
    DashboardScope,
    DashboardSettings,
    resolve_local_scope,
)
from plastic_promise.mcp.dashboard_v2.routes import create_dashboard_v2_routes

__all__ = [
    "DashboardAccessError",
    "DashboardConfigurationError",
    "DashboardScope",
    "DashboardSettings",
    "create_dashboard_v2_routes",
    "resolve_local_scope",
]
