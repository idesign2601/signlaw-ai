"""Versioned API router.

Feature routers are mounted here as the phases land: coverage and question
answering in Phase 5, documents, compare and admin in Phase 6.

Every route under this router requires ``X-API-Key``. The guard is applied here
rather than per-endpoint so that a route added later is protected by default —
forgetting to add a dependency is a much easier mistake to make than
deliberately removing one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.v1.ask import router as ask_router
from app.api.v1.municipalities import router as municipalities_router
from app.api.v1.phase3 import router as phase3_router
from app.core.security import require_api_key

__all__ = ["api_router"]

api_router = APIRouter(dependencies=[Depends(require_api_key)])
api_router.include_router(municipalities_router)
api_router.include_router(ask_router)
api_router.include_router(phase3_router)
