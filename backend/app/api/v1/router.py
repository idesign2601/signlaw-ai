"""Versioned API router.

Feature routers are mounted here as the phases land: documents and ingestion in
Phase 2, search in Phase 4, chat, compare and admin in Phase 6.
"""

from __future__ import annotations

from fastapi import APIRouter

__all__ = ["api_router"]

api_router = APIRouter()
