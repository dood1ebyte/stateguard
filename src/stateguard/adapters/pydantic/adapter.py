"""
PydanticAdapter -- the V1 reference ``IContractAdapter`` implementation.

This is the *only* place where ``stateguard.core`` interfaces are bound to
Pydantic.  It composes the other three adapter modules:

* ``PydanticContractExtractor`` -- inward translation (schema -> ContractSpec)
* ``PydanticViolationMapper``   -- inward translation (ValidationError -> violations)
* (this module)                 -- outward translation (repaired dict -> BaseModel)

No repair logic lives here. ``validate`` reports what Pydantic itself
considers valid; ``wrap`` rehydrates a repaired dict into the original
``BaseModel`` subclass.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from stateguard.adapters.pydantic.extractor import PydanticContractExtractor
from stateguard.adapters.pydantic.violation_mapper import PydanticViolationMapper
from stateguard.core.errors.results import ValidationResult
from stateguard.core.errors.violations import ViolationSeverity
from stateguard.core.interfaces.adapter import IContractAdapter
from stateguard.core.models.contract import ContractSpec

__all__ = ["PydanticAdapter"]


class PydanticAdapter(IContractAdapter):
    """
    ``IContractAdapter`` implementation backed by Pydantic v2.

    Stateless -- a single instance can be reused across repair sessions
    for any number of distinct ``BaseModel`` subclasses.
    """

    @classmethod
    def with_defaults(cls) -> PydanticAdapter:
        """Construct a ``PydanticAdapter`` with default (i.e. no) configuration."""
        return cls()

    # ------------------------------------------------------------------
    # IContractAdapter
    # ------------------------------------------------------------------

    def extract_contract(self, schema: Any) -> ContractSpec:
        """
        Extract a ``ContractSpec`` from *schema*.

        Parameters
        ----------
        schema:
            Must be a ``type[BaseModel]`` subclass (the class itself, not
            an instance).

        Raises
        ------
        TypeError
            If *schema* is not a ``BaseModel`` subclass.
        """
        if not (isinstance(schema, type) and issubclass(schema, BaseModel)):
            raise TypeError(
                "PydanticAdapter.extract_contract expects a type[BaseModel] "
                f"subclass, got {schema!r}."
            )
        return PydanticContractExtractor.extract(schema)

    def validate(
        self,
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> ValidationResult:
        """
        Validate *data* against *contract* using ``model_validate``.

        ``contract.source_ref`` must be the ``type[BaseModel]`` subclass
        produced by ``extract_contract`` (or an equivalent model).

        On success, returns ``ValidationResult(is_valid=True, violations=[])``.
        On ``pydantic.ValidationError``, maps every error entry via
        ``PydanticViolationMapper`` -- all such violations have
        ``severity = ViolationSeverity.ERROR``, so ``is_valid`` is ``False``.

        Note
        ----
        Pydantic's default ``extra="ignore"`` behaviour means extra keys in
        *data* do not, by themselves, cause a ``ValidationError`` here.
        ``UNEXPECTED_FIELD`` detection is the responsibility of
        ``ContractValidator``; ``RepairEngine._validate`` merges both
        validators' results.
        """
        model_class = self._model_class(contract)

        try:
            model_class.model_validate(data)
        except ValidationError as exc:
            violations = PydanticViolationMapper.map(exc, contract)
            is_valid = not any(
                v.severity is ViolationSeverity.ERROR for v in violations
            )
            return ValidationResult(
                is_valid=is_valid,
                violations=violations,
                raw_input=dict(data),
                contract_id=contract.contract_id,
            )

        return ValidationResult(
            is_valid=True,
            violations=[],
            raw_input=dict(data),
            contract_id=contract.contract_id,
        )

    def wrap(
        self,
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> BaseModel:
        """
        Rehydrate *data* into an instance of ``contract.source_ref``.

        Raises
        ------
        RuntimeError
            If ``model_validate`` fails.  This indicates a bug in the
            engine: ``wrap`` is only called after revalidation has already
            confirmed the data is valid.
        """
        model_class = self._model_class(contract)

        try:
            return model_class.model_validate(data)
        except ValidationError as exc:
            raise RuntimeError(
                f"PydanticAdapter.wrap: failed to rehydrate "
                f"{model_class.__name__} from repaired data, even though "
                f"revalidation reported success. This indicates an engine "
                f"bug. Underlying error: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _model_class(contract: ContractSpec) -> type[BaseModel]:
        """
        Recover the ``type[BaseModel]`` subclass from ``contract.source_ref``.

        Raises
        ------
        TypeError
            If ``source_ref`` is not a ``BaseModel`` subclass -- indicates
            *contract* was not produced by ``PydanticAdapter.extract_contract``.
        """
        source_ref = contract.source_ref
        if not (isinstance(source_ref, type) and issubclass(source_ref, BaseModel)):
            raise TypeError(
                "PydanticAdapter requires ContractSpec.source_ref to be a "
                f"type[BaseModel] subclass, got {source_ref!r}. "
                "Was this contract produced by PydanticAdapter.extract_contract?"
            )
        return source_ref
