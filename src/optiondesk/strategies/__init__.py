"""Strategies. Importing this package registers the built-in examples."""

from . import ema_cross, opening_range
from .base import Strategy, create, load_plugins, register, registered

__all__ = ["Strategy", "create", "ema_cross", "load_plugins", "opening_range", "register", "registered"]
