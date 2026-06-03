"""HTTP surface — FastAPI app exposing ``POST /run`` for remote callers."""

from src.api.app import create_app

__all__ = ["create_app"]
