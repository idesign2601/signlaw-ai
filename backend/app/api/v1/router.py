"""Versioned API router.

Feature routers are mounted here as the phases land: coverage and question
answering in Phase 5, documents, compare and admin in Phase 6.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.ask import router as ask_router
from app.api.v1.municipalities import router as municipalities_router

__all__ = ["api_router"]

api_router = APIRouter()
api_router.include_router(municipalities_router)
api_router.include_router(ask_router)
