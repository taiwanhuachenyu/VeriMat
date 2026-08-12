"""Stable domain primitives shared by product and research execution."""

from .events import EventEnvelope, EventValidationError

__all__ = ["EventEnvelope", "EventValidationError"]
