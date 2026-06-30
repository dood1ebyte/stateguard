"""Shared Pydantic model fixtures for adapter tests."""

from __future__ import annotations

from typing import Any, List, Literal, Optional

import pytest
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Simple models
# ---------------------------------------------------------------------------


class Weather(BaseModel):
    """The canonical V1 example: two required primitive fields."""

    temperature: float
    humidity: int


class WeatherWithDefault(BaseModel):
    """humidity has a declared default; temperature does not."""

    temperature: float
    humidity: int = 60


class WeatherOptional(BaseModel):
    """temperature is Optional[float] with default None."""

    temperature: Optional[float] = None
    humidity: int


# ---------------------------------------------------------------------------
# Aliased models
# ---------------------------------------------------------------------------


class WeatherAliased(BaseModel):
    """temperature has Field(alias="temp_c"); humidity is plain."""

    temperature: float = Field(alias="temp_c")
    humidity: int


class WeatherValidationAlias(BaseModel):
    """humidity has an explicit validation_alias different from alias."""

    temperature: float
    humidity: int = Field(validation_alias="rh")


# ---------------------------------------------------------------------------
# Nested models
# ---------------------------------------------------------------------------


class Address(BaseModel):
    city: str
    zip_code: str


class User(BaseModel):
    name: str
    address: Address


class Country(BaseModel):
    code: str


class AddressWithCountry(BaseModel):
    city: str
    country: Country


class UserDeep(BaseModel):
    name: str
    address: AddressWithCountry


# ---------------------------------------------------------------------------
# Constrained models
# ---------------------------------------------------------------------------


class Bounded(BaseModel):
    value: int = Field(ge=0, le=100)


class StringConstrained(BaseModel):
    code: str = Field(min_length=2, max_length=5, pattern=r"^[A-Z]+$")


class WithLiteral(BaseModel):
    status: Literal["active", "inactive", "pending"]


class WithIntLiteral(BaseModel):
    level: Literal[1, 2, 3]


# ---------------------------------------------------------------------------
# Complex / mixed models
# ---------------------------------------------------------------------------


class Order(BaseModel):
    id: str
    items: List[str]
    total: float
    metadata: dict = {}


class WithDefaultFactory(BaseModel):
    tags: List[str] = Field(default_factory=list)


class WithAny(BaseModel):
    payload: Any


class StrictExtra(BaseModel):
    model_config = {"extra": "forbid"}

    x: int


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def weather_model() -> type[Weather]:
    return Weather


@pytest.fixture
def weather_with_default_model() -> type[WeatherWithDefault]:
    return WeatherWithDefault


@pytest.fixture
def weather_aliased_model() -> type[WeatherAliased]:
    return WeatherAliased


@pytest.fixture
def user_model() -> type[User]:
    return User


@pytest.fixture
def bounded_model() -> type[Bounded]:
    return Bounded
