"""
IContractAdapter — the single extension point for framework integrations.

Every framework adapter (Pydantic, LangChain, JSON Schema, …) implements
exactly this interface.  The core engine and ContractGuard orchestrator
depend only on this ABC; they never touch framework-native types.

Invariant
---------
Implementations of this interface are the **only** place where framework
imports (pydantic, langchain, etc.) are permitted.  The core engine must
never import from an adapter.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from stateguard.core.errors.results import ValidationResult
from stateguard.core.models.contract import ContractSpec

__all__ = ["IContractAdapter"]


class IContractAdapter(ABC):
    """
    Abstract base class for framework-specific contract adapters.

    An adapter has exactly two responsibilities:

    1. **Inward translation** — convert a framework-native schema into a
       ``ContractSpec`` the engine can reason over (``extract_contract``).

    2. **Validation + outward translation** — validate a data dict against
       the contract using the framework's own validator (``validate``), and
       convert a repaired dict back into the framework-native type
       (``wrap``).

    Repair logic must never appear inside an adapter.  If an adapter finds
    itself making repair decisions it is violating the boundary.

    See Also
    --------
    ``stateguard.adapters.pydantic.PydanticAdapter`` — V1 reference
    implementation.
    """

    @abstractmethod
    def extract_contract(self, schema: Any) -> ContractSpec:
        """
        Convert a framework-native schema into a normalised ``ContractSpec``.

        This method is called once per ``ContractGuard.repair()`` invocation
        (or on each call if the caller does not cache).  Implementations
        should be idempotent: calling with the same schema must always
        return an equivalent ``ContractSpec``.

        Parameters
        ----------
        schema:
            The framework-native schema object.  For the Pydantic adapter
            this is ``type[BaseModel]``; for a JSON Schema adapter it would
            be a ``dict``; etc.

        Returns
        -------
        ContractSpec
            Normalised contract.  ``source_ref`` must be set to *schema* so
            that ``wrap()`` can rehydrate the framework-native type.
        """

    @abstractmethod
    def validate(
        self,
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> ValidationResult:
        """
        Validate *data* against *contract* using the framework's own validator.

        This method is called **twice** per repair attempt:

        * Before repair begins (initial violation detection).
        * After each strategy application (revalidation).

        The framework's native validator is the **source of truth** for
        what "valid" means.  The engine's own ``ContractValidator`` is used
        only for pre-repair violation analysis; it does not override this
        method's result.

        Parameters
        ----------
        contract:
            The normalised contract.  Use ``contract.source_ref`` to recover
            the original schema object if the framework's validator requires
            it.
        data:
            The data dict to validate.  Must not be mutated.

        Returns
        -------
        ValidationResult
            Contains ``is_valid``, ``violations``, and a snapshot of *data*.
        """

    @abstractmethod
    def wrap(
        self,
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> Any:
        """
        Convert a repaired data dict back into the framework-native type.

        Called only when ``RepairResult.status`` is ``SUCCESS`` (or
        ``PARTIAL`` with ``allow_partial_repair=True``).

        Parameters
        ----------
        contract:
            The normalised contract.  Use ``contract.source_ref`` to
            recover the original schema for rehydration.
        data:
            The repaired dict that passed revalidation.

        Returns
        -------
        Any
            Framework-native object.  For the Pydantic adapter this is a
            validated ``BaseModel`` instance; for a dict-based adapter it
            may simply be *data* unchanged.

        Raises
        ------
        RuntimeError
            If rehydration fails after a successful repair.  This should
            not happen in normal operation; it indicates an engine bug if
            it does.
        """
