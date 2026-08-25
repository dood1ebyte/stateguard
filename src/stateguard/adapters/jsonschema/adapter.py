"""
``JSONSchemaAdapter`` -- ``IContractAdapter`` over plain JSON Schema dicts.

Source of truth
---------------
**This adapter's ``validate`` delegates to ``ContractValidator``, which makes
StateGuard itself the authority on what "valid" means for a JSON Schema.**
That is a deliberate, recorded decision -- see
``docs/adr/0001-json-schema-source-of-truth.md`` -- and it has a consequence
callers must know:

    A ``SUCCESS`` from StateGuard is **not** a claim of JSON Schema
    compliance.

JSON Schema has no native validator in-process; it is a specification, not a
library. Rather than take a dependency, this adapter implements the subset
MCP tool definitions actually emit (``MCP_ADAPTER_PLAN.md`` §5) and refuses
everything else *loudly*, so the gap between the two is bounded and visible
instead of silent:

* keywords that cannot be represented are rejected with
  ``UnsupportedSchemaError`` (``allOf``, ``not``, ``if``/``then``/``else``,
  ``patternProperties``, ``dependentSchemas``, ``propertyNames``,
  ``unevaluatedProperties``, and any keyword not in the subset);
* ``exclusiveMinimum`` / ``exclusiveMaximum`` are dropped with a
  ``SchemaFeatureWarning``, because an exclusive bound is not an inclusive
  one and rounding it would accept a value the schema forbids;
* ``format`` is advisory in practice and ignored.

Contrast with ``DictContractAdapter``, which also delegates to
``ContractValidator``: there it is authoritative *by definition*, because
StateGuard owns that contract format and there is no external specification
to fall short of. Here it is a stand-in, which is why the gap has to be
enumerated rather than merely noted.

Zero external dependencies: an MCP tool definition arriving over the wire is
just a ``dict``, so nothing here needs a schema library or the MCP SDK.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stateguard.adapters.jsonschema.errors import UnsupportedSchemaError
from stateguard.adapters.jsonschema.extractor import JSONSchemaExtractor
from stateguard.core.errors.results import ValidationResult
from stateguard.core.interfaces.adapter import IContractAdapter
from stateguard.core.models.contract import ContractSpec
from stateguard.core.validator import ContractValidator

__all__ = ["JSONSchemaAdapter"]


class JSONSchemaAdapter(IContractAdapter):
    """
    Adapts a JSON Schema ``dict`` to StateGuard's contract model.

    ``extract_contract`` accepts the schema document; ``wrap`` returns the
    repaired dict unchanged, since there is no framework-native type to
    rehydrate into (the same position ``DictContractAdapter`` is in).
    """

    def __init__(self, extractor: JSONSchemaExtractor | None = None) -> None:
        self._extractor = extractor if extractor is not None else JSONSchemaExtractor()
        self._validator = ContractValidator()

    # ------------------------------------------------------------------
    # IContractAdapter
    # ------------------------------------------------------------------

    def extract_contract(self, schema: Any) -> ContractSpec:
        """
        Convert a JSON Schema document into a ``ContractSpec``.

        Raises ``UnsupportedSchemaError`` / ``SchemaReferenceError`` for
        schemas outside the supported subset, rather than extracting a
        contract that would validate too loosely.
        """
        if not isinstance(schema, Mapping):
            raise UnsupportedSchemaError(
                f"JSONSchemaAdapter expects a JSON Schema object (a dict), "
                f"got {type(schema).__name__}."
            )
        contract = self._extractor.extract(schema)
        # source_ref carries the original document so wrap() and any
        # downstream consumer can recover what was extracted from.
        return ContractSpec(
            fields=contract.fields,
            source_ref=schema,
            strict_mode=contract.strict_mode,
        )

    def validate(
        self,
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> ValidationResult:
        """
        Validate against ``ContractValidator``.

        There is no framework-native validator to delegate to -- see this
        module's docstring and ADR-0001 for what that means and why it is
        acceptable here.
        """
        return self._validator.validate(contract, data)

    def wrap(
        self,
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a copy of *data* unchanged -- there is no native type."""
        return dict(data)
