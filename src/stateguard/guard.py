"""
ContractGuard -- the user-facing entry point.

``ContractGuard`` is a thin orchestrator: it owns no repair logic of its
own.  It sequences calls to an ``IContractAdapter`` (schema <-> contract
translation) and a ``RepairEngine`` (the repair loop), and is the only
class that touches both.

Typical usage::

    from stateguard import ContractGuard
    from pydantic import BaseModel

    class Weather(BaseModel):
        temperature: float
        humidity: int

    guard = ContractGuard.with_pydantic()
    result = guard.repair(Weather, {"temp_celsius": 31.5, "humidity": 80})

    if result.is_success:
        weather: Weather = result.repaired_output
"""

from __future__ import annotations

import contextlib
from typing import Any

from stateguard.core.engine import RepairEngine
from stateguard.core.errors.results import RepairResult, RepairStatus, ValidationResult
from stateguard.core.interfaces.adapter import IContractAdapter
from stateguard.core.models.config import GuardConfig
from stateguard.core.models.contract import ContractSpec
from stateguard.core.strategies import (
    DefaultValueFillStrategy,
    ExactAliasStrategy,
    FuzzyFieldMatchStrategy,
    StrategyRegistry,
    TypeCoercionStrategy,
)
from stateguard.logging.logger import RepairLogger
from stateguard.logging.repair_history import RepairHistoryRecorder
from stateguard.telemetry.hooks import ITelemetryHook
from stateguard.telemetry.noop import NoopTelemetry

__all__ = ["ContractGuard"]


# Statuses for which the repaired data is fully valid and can be safely
# rehydrated into the framework-native type via IContractAdapter.wrap.
_WRAPPABLE_STATUSES = frozenset({RepairStatus.SUCCESS, RepairStatus.ALREADY_VALID})


class ContractGuard:
    """
    Orchestrates contract validation and repair for a single framework.

    Parameters
    ----------
    adapter:
        Framework adapter (e.g. ``PydanticAdapter``).  Determines how
        schemas are translated and how repaired data is rehydrated.
    config:
        Guard-level configuration.  Defaults to ``GuardConfig()``.
    telemetry:
        Optional telemetry hook.  Defaults to ``NoopTelemetry`` (disabled) --
        StateGuard collects no telemetry unless a hook is explicitly
        supplied.
    history:
        Optional ``RepairHistoryRecorder``.  Defaults to ``None``
        (disabled) -- StateGuard writes no local repair history unless a
        recorder is explicitly supplied, mirroring the ``telemetry``
        default-disabled pattern. When supplied, every ``repair()`` call
        appends one record per applied operation to the recorder's
        configured file. Recording failures (filesystem errors, etc.) are
        swallowed and never propagate -- see
        ``stateguard.logging.RepairHistoryRecorder`` for details.

    The repair-strategy registry (``ExactAliasStrategy``,
    ``FuzzyFieldMatchStrategy``, ``TypeCoercionStrategy``,
    ``DefaultValueFillStrategy``) is constructed once at initialisation
    time, using ``config.repair`` to parameterise
    ``FuzzyFieldMatchStrategy`` so its proposal threshold matches the
    engine's acceptance threshold.
    """

    def __init__(
        self,
        adapter: IContractAdapter,
        config: GuardConfig | None = None,
        telemetry: ITelemetryHook | None = None,
        history: RepairHistoryRecorder | None = None,
    ) -> None:
        self._adapter = adapter
        self._config = config if config is not None else GuardConfig()
        self._telemetry: ITelemetryHook = (
            telemetry if telemetry is not None else NoopTelemetry()
        )
        self._history = history
        self._registry = StrategyRegistry([
            ExactAliasStrategy(),
            FuzzyFieldMatchStrategy(
                min_confidence_threshold=self._config.repair.min_confidence_threshold,
                score_collision_margin=self._config.repair.score_collision_margin,
            ),
            TypeCoercionStrategy(),
            DefaultValueFillStrategy(),
        ])

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def with_pydantic(
        cls,
        config: GuardConfig | None = None,
        telemetry: ITelemetryHook | None = None,
        history: RepairHistoryRecorder | None = None,
    ) -> ContractGuard:
        """
        Construct a ``ContractGuard`` using ``PydanticAdapter``.

        Requires the ``pydantic`` extra to be installed
        (``pip install stateguard[pydantic]``).

        Raises
        ------
        ImportError
            If ``pydantic`` is not installed, with installation guidance.
        """
        try:
            from stateguard.adapters.pydantic import PydanticAdapter  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "ContractGuard.with_pydantic() requires pydantic to be "
                "installed. Install it with: pip install stateguard[pydantic]"
            ) from exc

        return cls(
            adapter=PydanticAdapter.with_defaults(),
            config=config,
            telemetry=telemetry,
            history=history,
        )

    @classmethod
    def with_dict_schema(
        cls,
        config: GuardConfig | None = None,
        telemetry: ITelemetryHook | None = None,
        history: RepairHistoryRecorder | None = None,
    ) -> ContractGuard:
        """
        Construct a ``ContractGuard`` using ``DictContractAdapter``.

        Use this factory when you want to describe a contract as a plain
        Python dict (or a loaded JSON file) rather than a Pydantic model.
        See ``stateguard.adapters.dict_adapter`` for the schema format.

        On ``SUCCESS`` / ``ALREADY_VALID``, ``RepairResult.repaired_output``
        is a plain ``dict[str, Any]`` (``DictContractAdapter.wrap`` returns
        the data dict unchanged -- there is no framework-native type to
        rehydrate into).
        """
        from stateguard.adapters.dict_adapter import DictContractAdapter  # noqa: PLC0415

        return cls(
            adapter=DictContractAdapter(),
            config=config,
            telemetry=telemetry,
            history=history,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def repair(self, schema: Any, data: dict[str, Any]) -> RepairResult:
        """
        Detect, attempt to repair, and revalidate *data* against *schema*.

        Parameters
        ----------
        schema:
            Framework-native schema (e.g. a ``type[BaseModel]`` subclass).
        data:
            The data to validate and, if necessary, repair.  Never mutated.

        Returns
        -------
        RepairResult
            On ``SUCCESS`` or ``ALREADY_VALID``, ``repaired_output`` is the
            framework-native object produced by ``adapter.wrap`` (e.g. a
            validated ``BaseModel`` instance) rather than a plain dict.
            On ``PARTIAL``, ``repaired_output`` remains a plain
            ``dict[str, Any]`` (it does not pass full validation, so
            ``wrap`` is not attempted).  On ``FAILED``, ``repaired_output``
            is ``None``.

        Notes
        -----
        If a ``history`` recorder was supplied at construction time, this
        method also appends a record of the repair outcome to it. Any
        failure while doing so is swallowed -- a broken or unwritable
        history file never causes ``repair()`` itself to fail.
        """
        contract = self._extract_contract(schema)
        engine = self._build_engine()
        result = engine.repair(contract, data, self._adapter)

        if self._history is not None:
            with contextlib.suppress(Exception):
                # Belt-and-suspenders: RepairHistoryRecorder.record already
                # swallows its own exceptions, but a misbehaving custom
                # subclass must still never be allowed to break a repair.
                self._history.record(result)

        if result.status in _WRAPPABLE_STATUSES and result.repaired_output is not None:
            result.repaired_output = self._adapter.wrap(contract, result.repaired_output)

        return result

    def validate(self, schema: Any, data: dict[str, Any]) -> ValidationResult:
        """
        Validate *data* against *schema* without attempting repair.

        Uses the same merged validation as the first step of ``repair``
        (adapter-native validation plus ``ContractValidator``'s
        framework-agnostic checks, notably ``UNEXPECTED_FIELD``), so
        ``validate(...).is_valid`` is ``True`` exactly when ``repair(...)``
        would return ``RepairStatus.ALREADY_VALID``.
        """
        contract = self._extract_contract(schema)
        engine = self._build_engine()
        return engine._validate(contract, data, self._adapter)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_contract(self, schema: Any) -> ContractSpec:
        """
        Extract a ``ContractSpec`` from *schema*, applying
        ``GuardConfig.strict_mode`` if it differs from the adapter's
        default extraction.

        When ``strict_mode`` differs, the contract is reconstructed via the
        public ``ContractSpec`` constructor (not mutated in place) so that
        ``contract_id`` is regenerated consistently with the active
        ``strict_mode``.
        """
        contract = self._adapter.extract_contract(schema)
        if contract.strict_mode != self._config.strict_mode:
            contract = ContractSpec(
                fields=contract.fields,
                source_ref=contract.source_ref,
                strict_mode=self._config.strict_mode,
            )
        return contract

    def _build_engine(self) -> RepairEngine:
        """Construct a fresh ``RepairEngine`` with its own ``RepairLogger``."""
        return RepairEngine(
            registry=self._registry,
            config=self._config.repair,
            logger=RepairLogger(),
            telemetry=self._telemetry,
        )
