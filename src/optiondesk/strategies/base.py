"""The strategy interface and registry.

A strategy sees each completed bar for each symbol and may return a
:class:`~optiondesk.models.Signal`. It never sizes, selects contracts,
places orders or manages exits; the engine does that, so every strategy gets
the same risk controls.

To add your own, subclass :class:`Strategy` and decorate it with
``@register("my_name")`` in a module of your own, list that module under
``plugins`` in your config, and add a ``[[strategies]]`` entry with
``name = "my_name"``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, ClassVar, TypeVar

from ..models import Bar, Signal

_REGISTRY: dict[str, type[Strategy]] = {}
S = TypeVar("S", bound="type[Strategy]")


class Strategy(ABC):
    name: ClassVar[str] = ""

    def __init__(self, **params: Any) -> None:
        unknown = set(params) - set(self.defaults())
        if unknown:
            raise ValueError(f"{self.name}: unknown parameter(s) {sorted(unknown)}")
        self.params = {**self.defaults(), **params}

    @classmethod
    def defaults(cls) -> dict[str, Any]:
        """Parameter names and default values this strategy accepts."""
        return {}

    @abstractmethod
    def on_bar(self, bar: Bar) -> Signal | None:
        """Called once per completed bar, per symbol, in time order."""


def register(name: str) -> Callable[[S], S]:
    def wrap(cls: S) -> S:
        if name in _REGISTRY:
            raise ValueError(f"strategy {name!r} is already registered")
        cls.name = name
        _REGISTRY[name] = cls
        return cls

    return wrap


def create(name: str, params: dict[str, Any]) -> Strategy:
    try:
        cls = _REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown strategy {name!r}; registered: {sorted(_REGISTRY)}") from None
    return cls(**params)


def registered() -> list[str]:
    return sorted(_REGISTRY)


def load_plugins(modules: list[str]) -> None:
    """Import modules so their ``@register`` decorators run.

    The working directory is searched first, so ``plugins = ["my_strategies"]``
    finds ``./my_strategies.py``.
    """
    import importlib
    import os
    import sys

    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)
    for module in modules:
        try:
            importlib.import_module(module)
        except ImportError as exc:
            raise ValueError(f"could not import plugin {module!r}: {exc}") from exc
