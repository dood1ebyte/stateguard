"""
Configuration dataclasses for the repair engine and top-level guard.

Design notes
------------
Both ``RepairConfig`` and ``GuardConfig`` are **mutable** dataclasses.
Immutability was considered but rejected: callers must be able to construct
a default config and selectively override individual fields before passing it
to ``ContractGuard``.

Validation is enforced in ``__post_init__`` on construction.  Direct field
mutation after construction bypasses ``__post_init__`` — this is a known
limitation of non-frozen dataclasses.  V1 does not add property-based setters
to avoid over-engineering; callers are expected to construct config objects
correctly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "GuardConfig",
    "RepairConfig",
]


# ---------------------------------------------------------------------------
# RepairConfig
# ---------------------------------------------------------------------------


@dataclass
class RepairConfig:
    """
    Controls the behaviour of the repair engine.

    Attributes
    ----------
    max_attempts:
        Maximum number of repair-loop iterations before the engine gives up.
        Each iteration applies one strategy and revalidates.  Must be >= 1.
    min_confidence_threshold:
        Minimum confidence score ``[0.0, 1.0]`` required for a
        ``FieldOperation`` to be applied.  Operations below this threshold are
        recorded as *rejected* in the ``RepairAttempt`` audit trail but not
        applied to the data.  Must be in ``(0.0, 1.0]``.
    score_collision_margin:
        When ``FuzzyFieldMatchStrategy`` finds two candidate target fields
        within this margin of each other (e.g. both score 0.80 and 0.78), it
        refuses to propose either rename and instead logs a warning.  This
        prevents silent wrong-field repairs.  Must be in ``(0.0, 1.0)``.
    allow_partial_repair:
        If ``True`` (default), a ``RepairResult`` with some violations
        resolved and some remaining is returned as ``RepairStatus.PARTIAL``
        with a non-``None`` ``repaired_output``.
        If ``False``, any unresolved violation yields ``RepairStatus.FAILED``
        and ``repaired_output`` is ``None``.
    include_values_in_log:
        If ``True``, ``RepairLogEntry.data`` dicts may include actual field
        *values* from the input/output data.  Disabled by default to avoid
        inadvertently logging sensitive runtime data (API keys, PII, etc.).
    """

    max_attempts: int = 3
    min_confidence_threshold: float = 0.7
    score_collision_margin: float = 0.15
    allow_partial_repair: bool = True
    include_values_in_log: bool = False

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError(f"max_attempts must be >= 1, got {self.max_attempts!r}")
        if not (0.0 < self.min_confidence_threshold <= 1.0):
            raise ValueError(
                f"min_confidence_threshold must be in (0.0, 1.0], "
                f"got {self.min_confidence_threshold!r}"
            )
        if not (0.0 < self.score_collision_margin < 1.0):
            raise ValueError(
                f"score_collision_margin must be in (0.0, 1.0), got {self.score_collision_margin!r}"
            )


# ---------------------------------------------------------------------------
# GuardConfig
# ---------------------------------------------------------------------------


@dataclass
class GuardConfig:
    """
    Top-level configuration for ``ContractGuard``.

    Attributes
    ----------
    repair:
        Engine-level repair settings.  Defaults to ``RepairConfig()`` with all
        default values.  Each ``GuardConfig`` instance owns its own
        ``RepairConfig`` (created via ``default_factory``).
    strict_mode:
        Controls how unexpected fields (keys present in the data but absent
        from the contract) are treated.

        * ``False`` (default) — unexpected fields produce
          ``ViolationSeverity.WARNING`` violations.  Repair strategies may
          choose to ignore or remove them.
        * ``True`` — unexpected fields produce ``ViolationSeverity.ERROR``
          violations and block a successful result unless explicitly repaired
          (e.g. via ``RemoveStrategy``).
    """

    repair: RepairConfig = field(default_factory=RepairConfig)
    strict_mode: bool = False
