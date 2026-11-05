"""Portal profiles: the small amount of per-portal knowledge the session keeper needs."""
from __future__ import annotations

from .base import Health, LoginError, PortalProfile


def get_portal(name: str, **overrides) -> PortalProfile:
    if name == "ehospital":
        from .ehospital import EHospitalPortal
        portal = EHospitalPortal(**overrides)
    elif name == "openmrs":
        from .openmrs import OpenMRSPortal
        portal = OpenMRSPortal(**overrides)
    else:
        raise ValueError(f"unknown portal {name!r}")
    return portal


__all__ = ["Health", "LoginError", "PortalProfile", "get_portal"]
