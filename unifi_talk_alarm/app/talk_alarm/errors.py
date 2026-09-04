"""Bounded service errors exposed through the HTTP API."""

from __future__ import annotations


class ServiceError(Exception):
    """An expected error safe to return to the Home Assistant integration."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

