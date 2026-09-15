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
from dataclasses import replace
from typing import Any

from stateguard.core.engine import RepairEngine
from stateguard.core.errors.results import RepairResult, RepairStatus, ValidationResult
from stateguard.core.interfaces.adapter import IContractAdapter
from stateguard.core.models.config import GuardConfig
from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.strategies import (
    DefaultValueFillStrategy,
    EnumNormalizationStrategy,
    ExactAliasStrategy,
    FuzzyFieldMatchStrategy,
    NormalizedNameStrategy,
    StrategyRegistry,
    TypeCoercionStrategy,
)
from stateguard.core.trust import TrustPolicy
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
        ``GuardConfig.mode`` selects ``AUTO`` (repair and commit, the
        default) or ``SHADOW`` (repair, validate the plan, and report it on
        ``RepairResult.proposed_output`` without committing).
    telemetry:
        Optional telemetry hook.  Defaults to ``NoopTelemetry`` (disabled) --
        StateGuard collects no telemetry unless a hook is explicitly
        supplied.
    policy:
        Optional ``TrustPolicy`` governing how measured evidence becomes a
        score and an apply/abstain/reject decision.  Defaults to one built
        from ``config.repair``: ``score_collision_margin`` sets the margin at
        which a runner-up stops casting doubt, and
        ``min_confidence_threshold`` becomes a floor beneath every risk
        tier.  Pass an explicit policy to override the per-risk bands, which
        ``RepairConfig`` deliberately does not expose field-by-field.
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
        policy: TrustPolicy | None = None,
    ) -> None:
        self._adapter = adapter
        self._config = config if config is not None else GuardConfig()
        self._telemetry: ITelemetryHook = telemetry if telemetry is not None else NoopTelemetry()
        self._history = history
        self._registry = StrategyRegistry(
            [
                ExactAliasStrategy(),
                NormalizedNameStrategy(),
                FuzzyFieldMatchStrategy(),
                TypeCoercionStrategy(),
                EnumNormalizationStrategy(),
                DefaultValueFillStrategy(),
            ]
        )
        # Thresholds live on the policy, not on individual strategies: the
        # strategies report evidence and this decides what it is worth.
        # ``score_collision_margin`` keeps its meaning -- the margin at which
        # a runner-up stops casting doubt -- but now scales trust continuously
        # instead of vetoing a proposal outright.
        self._policy = (
            policy
            if policy is not None
            else TrustPolicy(
                margin_full_credit=self._config.repair.score_collision_margin,
                minimum_trust=self._config.repair.min_confidence_threshold,
            )
        )

    # ------------------------------------------------------------------
    # Factory methods
    # ------------------------------------------------------------------

    @classmethod
    def with_pydantic(
        cls,
        config: GuardConfig | None = None,
        telemetry: ITelemetryHook | None = None,
        history: RepairHistoryRecorder | None = None,
        policy: TrustPolicy | None = None,
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
            policy=policy,
        )

    @classmethod
    def with_dict_schema(
        cls,
        config: GuardConfig | None = None,
        telemetry: ITelemetryHook | None = None,
        history: RepairHistoryRecorder | None = None,
        policy: TrustPolicy | None = None,
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
            policy=policy,
        )

    @classmethod
    def with_json_schema(
        cls,
        config: GuardConfig | None = None,
        telemetry: ITelemetryHook | None = None,
        history: RepairHistoryRecorder | None = None,
        policy: TrustPolicy | None = None,
    ) -> ContractGuard:
        """
        Construct a ``ContractGuard`` using ``JSONSchemaAdapter``.

        Use this to repair payloads against a JSON Schema document -- an MCP
        tool's ``inputSchema``, or any hand-written schema dict.  Requires no
        extra dependencies: a schema arriving over the wire is just a
        ``dict``, so the adapter reads it with stdlib alone.

        ``repair()`` takes the schema document as its *schema* argument::

            guard = ContractGuard.with_json_schema()
            result = guard.repair(tool["inputSchema"], arguments)

        On ``SUCCESS`` / ``ALREADY_VALID``, ``RepairResult.repaired_output``
        is a plain ``dict[str, Any]`` -- there is no framework-native type to
        rehydrate into.

        Important
        ---------
        This adapter validates with StateGuard's own ``ContractValidator``,
        so a ``SUCCESS`` is **not** a claim of JSON Schema compliance.  The
        supported subset is ``MCP_ADAPTER_PLAN.md`` §5; anything outside it
        raises rather than being ignored.  See
        ``docs/adr/0001-json-schema-source-of-truth.md``.
        """
        from stateguard.adapters.jsonschema import JSONSchemaAdapter  # noqa: PLC0415

        return cls(
            adapter=JSONSchemaAdapter(),
            config=config,
            telemetry=telemetry,
            history=history,
            policy=policy,
        )

    @classmethod
    def with_mcp(
        cls,
        config: GuardConfig | None = None,
        telemetry: ITelemetryHook | None = None,
        history: RepairHistoryRecorder | None = None,
        policy: TrustPolicy | None = None,
        cache_size: int | None = None,
    ) -> ContractGuard:
        """
        Construct a ``ContractGuard`` using ``MCPToolAdapter``.

        Repairs the **arguments an agent sends to an MCP tool**, against the
        **server's declared schema**.  ``repair()`` takes either a whole tool
        definition or a bare ``inputSchema``::

            guard = ContractGuard.with_mcp()
            result = guard.repair(tool, arguments)
            result = guard.repair(tool["inputSchema"], arguments)

        Requires no extra dependencies: a tool definition arriving over the
        wire is just a ``dict``.

        Extracted contracts are cached on ``(tool name, schema content)``, so
        a proxy fetches ``tools/list`` once and every subsequent
        ``tools/call`` reuses the walk.  *cache_size* bounds that cache;
        leave it unset for the default.

        To turn a ``RepairResult`` into a forward/hold/escalate/refuse
        decision, use ``stateguard.adapters.mcp.outcome_for`` rather than
        branching on ``status`` at the call site.

        Important
        ---------
        ``inputSchema`` is JSON Schema, so everything ``with_json_schema``
        documents applies here unchanged -- including that a ``SUCCESS`` is
        **not** a claim of JSON Schema compliance, and that keywords outside
        the supported subset raise rather than being ignored.  See
        ``docs/adr/0001-json-schema-source-of-truth.md``.
        """
        from stateguard.adapters.mcp import MCPToolAdapter  # noqa: PLC0415

        adapter = MCPToolAdapter() if cache_size is None else MCPToolAdapter(cache_size=cache_size)
        return cls(
            adapter=adapter,
            config=config,
            telemetry=telemetry,
            history=history,
            policy=policy,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def repair(self, schema: Any, data: Any) -> RepairResult:
        """
        Detect, attempt to repair, and revalidate *data* against *schema*.

        Parameters
        ----------
        schema:
            Framework-native schema (e.g. a ``type[BaseModel]`` subclass).
        data:
            The data to validate and, if necessary, repair.  Never mutated.
            Normally a ``dict``.  A payload whose root is not an object is
            either normalised (a JSON-encoded string such as
            ``'{"a": 1}'``, or a single-element sequence wrapping the
            object) or returned as a ``FAILED`` result carrying a
            ``STRUCTURAL_MISMATCH`` violation — passing one never raises.

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

            Under ``GuardConfig(mode=RepairMode.SHADOW)`` every one of those
            payloads moves to ``proposed_output`` and ``repaired_output`` is
            ``None`` throughout -- the repair is planned and validated but
            never handed back as committed.  See ``RepairMode``.

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

        # Rehydrate whichever field carries the payload. A shadow preview is
        # wrapped exactly as auto would have wrapped it, so switching a
        # deployment from SHADOW to AUTO changes which field holds the value
        # and nothing about the value itself -- which is the only way a week
        # of shadow diffing tells you anything about what auto would do.
        if result.status in _WRAPPABLE_STATUSES:
            if result.repaired_output is not None:
                result.repaired_output = self._adapter.wrap(contract, result.repaired_output)
            elif result.proposed_output is not None:
                result.proposed_output = self._adapter.wrap(contract, result.proposed_output)

        return result

    def validate(self, schema: Any, data: Any) -> ValidationResult:
        """
        Validate *data* against *schema* without attempting repair.

        Uses the same merged validation as the first step of ``repair``
        (adapter-native validation plus ``ContractValidator``'s
        framework-agnostic checks, notably ``UNEXPECTED_FIELD``), so
        ``validate(...).is_valid`` is ``True`` exactly when ``repair(...)``
        would return ``RepairStatus.ALREADY_VALID``.

        A payload whose root is not an object reports a single root-level
        ``STRUCTURAL_MISMATCH`` violation rather than raising.  Note this
        holds even for a root that ``repair`` *could* normalise — e.g.
        ``'{"a": 1}'`` is not valid as-is, so ``is_valid`` is ``False``
        while ``repair`` would return ``SUCCESS``.  That is consistent with
        the rule above: normalising the root is a repair, so the input was
        never ``ALREADY_VALID``.
        """
        contract = self._extract_contract(schema)
        engine = self._build_engine()
        return engine._validate(contract, data, self._adapter)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_contract(self, schema: Any) -> ContractSpec:
        """
        Extract a ``ContractSpec`` from *schema*, with ``strict_mode`` as a
        floor: strict if **either** the schema or the config asks for it.

        This used to let the config overwrite the adapter's answer in both
        directions, which meant a schema saying ``additionalProperties:
        false`` was silently relaxed by ``GuardConfig``'s default of
        ``False`` -- and ``ContractSpec.strict_mode`` documents the opposite
        precedence ("overrides ``GuardConfig.strict_mode`` at the
        per-contract level"), so the two disagreed. Nothing caught it while
        ``PydanticAdapter`` was the only adapter that could ask, because
        Pydantic enforces ``extra='forbid'`` in its own validator rather
        than through ``strict_mode``. For ``JSONSchemaAdapter``,
        ``strict_mode`` is the *only* enforcement path
        (``docs/adr/0001-json-schema-source-of-truth.md``), so the schema's
        answer has to survive.

        A floor rather than "the contract always wins" keeps
        ``GuardConfig.strict_mode=True`` useful as a global tightening for
        schema formats that have no way to say it themselves. What it
        deliberately does not offer is *relaxing* a schema that declared
        itself closed: a contract cannot be loosened by configuration.

        The floor reaches nested contracts too. ``GuardConfig.strict_mode``
        used to be applied to the root ``ContractSpec`` only, so an undeclared
        key at the top level was an ``ERROR`` while the same key one level
        down was a ``WARNING`` -- from a single config flag that says nothing
        about depth. A nested object that declared itself closed keeps that
        either way; only the *config's* tightening had a depth limit.

        When the effective value differs from what the adapter returned, the
        contract is reconstructed via the public ``ContractSpec`` constructor
        (not mutated in place) so that ``contract_id`` is regenerated
        consistently with the active ``strict_mode``. Rebuilding rather than
        mutating also matters because an adapter may hand back a *cached*
        contract -- ``MCPToolAdapter`` does, shared across threads -- and
        tightening one in place would leak this guard's configuration into
        every other holder of it.
        """
        contract = self._adapter.extract_contract(schema)
        if not self._config.strict_mode:
            # Nothing to tighten: the adapter's answer already stands, at
            # every level. Skipping the walk keeps the common path free.
            return contract
        return self._with_strict_floor(contract)

    @classmethod
    def _with_strict_floor(cls, contract: ContractSpec) -> ContractSpec:
        """
        Return *contract* with ``strict_mode`` forced on, recursively.

        Returns the input unchanged when it is already strict all the way
        down, so an unaffected contract is not needlessly rebuilt.
        """
        fields: list[FieldSpec] = []
        changed = False

        for spec in contract.fields:
            if spec.nested_spec is None:
                fields.append(spec)
                continue
            nested = cls._with_strict_floor(spec.nested_spec)
            if nested is spec.nested_spec:
                fields.append(spec)
                continue
            fields.append(replace(spec, nested_spec=nested))
            changed = True

        if contract.strict_mode and not changed:
            return contract

        return ContractSpec(
            fields=fields,
            source_ref=contract.source_ref,
            strict_mode=True,
        )

    def _build_engine(self) -> RepairEngine:
        """Construct a fresh ``RepairEngine`` with its own ``RepairLogger``."""
        return RepairEngine(
            registry=self._registry,
            config=self._config.repair,
            logger=RepairLogger(),
            telemetry=self._telemetry,
            policy=self._policy,
            mode=self._config.mode,
        )
