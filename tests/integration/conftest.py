"""Integration test fixtures."""

from __future__ import annotations

from typing import Any

import pytest

from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldType


@pytest.fixture
def weather_schema_dict() -> dict[str, Any]:
    """
    Minimal dict representation of a Weather schema.
    Used in tests that exercise the guard before the Pydantic adapter lands.
    """
    return {
        "fields": [
            {"path": "temperature", "type": "float", "required": True},
            {"path": "humidity", "type": "integer", "required": True},
        ]
    }
