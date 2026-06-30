"""
StateGuard — Runtime contract reliability SDK for AI system components.

Quickstart::

    from stateguard import ContractGuard
    from pydantic import BaseModel

    class Weather(BaseModel):
        temperature: float
        humidity: int

    guard = ContractGuard.with_pydantic()
    result = guard.repair(Weather, {"temp_celsius": 31.5, "humidity": 80})
    # result.status -> RepairStatus.SUCCESS
    # result.repaired_output -> Weather(temperature=31.5, humidity=80)
"""

from stateguard.core.errors.results import (
    RepairAttempt,
    RepairResult,
    RepairStatus,
    ValidationResult,
)
from stateguard.core.errors.violations import (
    ContractViolation,
    ViolationSeverity,
    ViolationType,
)
from stateguard.core.models.config import GuardConfig, RepairConfig
from stateguard.guard import ContractGuard
from stateguard.logging.repair_history import RepairHistoryRecorder

__version__ = "0.1.0"

__all__ = [
    "__version__",
    # Guard
    "ContractGuard",
    # Results
    "RepairResult",
    "RepairStatus",
    "ValidationResult",
    "RepairAttempt",
    # Violations
    "ContractViolation",
    "ViolationType",
    "ViolationSeverity",
    # Config
    "GuardConfig",
    "RepairConfig",
    # Local repair history (optional)
    "RepairHistoryRecorder",
]
