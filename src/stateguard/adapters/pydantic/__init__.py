"""Pydantic adapter for StateGuard."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = ["PydanticAdapter"]

if TYPE_CHECKING:
    from stateguard.adapters.pydantic.adapter import PydanticAdapter


def __getattr__(name: str) -> Any:
    if name == "PydanticAdapter":
        try:
            from stateguard.adapters.pydantic.adapter import (  # noqa: PLC0415
                PydanticAdapter,
            )
            return PydanticAdapter
        except ImportError as exc:
            raise ImportError(
                "PydanticAdapter requires pydantic to be installed. "
                'Install it with: pip install "stateguard[pydantic]"'
            ) from exc
    raise AttributeError(
        f"module 'stateguard.adapters.pydantic' has no attribute {name!r}"
    )
