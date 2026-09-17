"""
Errors raised while translating a JSON Schema into a ``ContractSpec``.

Why these exist at all
----------------------
StateGuard's other adapters raise bare ``ValueError`` / ``TypeError``, and
that is the house convention.  This adapter needs something a caller can
*catch specifically*, because it is the first one whose input arrives over a
network from a third party: an MCP server's ``inputSchema`` is untrusted
data, and "this schema uses a construct we do not support" is an outcome a
proxy has to handle deliberately rather than crash on.

Every class here subclasses ``ValueError``, so the house convention still
holds for anyone catching that.

Loud, never silent
------------------
``MCP_ADAPTER_PLAN.md`` §5 is explicit that an unsupported keyword must be an
error rather than an omission.  Silently ignoring ``allOf`` or ``not`` would
mean StateGuard reports ``SUCCESS`` on a payload the schema's author
intended to forbid -- under-validating without telling anyone.  Since
``ContractValidator`` is the source of truth for this adapter
(``docs/adr/0001-json-schema-source-of-truth.md``), there is no second
validator downstream to catch what we drop.  These errors are that
guarantee.

Zero external dependencies.
"""

from __future__ import annotations

__all__ = [
    "JSONSchemaError",
    "SchemaFeatureWarning",
    "SchemaReferenceError",
    "UnsupportedSchemaError",
]


class SchemaFeatureWarning(UserWarning):
    """
    A keyword was understood but could not be represented, and was dropped.

    Distinct from ``UnsupportedSchemaError``: an error means the schema is
    refused outright, while this means extraction continued with slightly
    *less* validation than the schema asked for.

    The only cases are ``exclusiveMinimum`` / ``exclusiveMaximum``, which
    have no ``FieldConstraintType`` -- an exclusive bound is not expressible
    as an inclusive one, and silently rounding it to ``MINIMUM`` would accept
    a value the schema forbids. Warning rather than raising keeps a schema
    that merely tightens a bound usable, while making sure the gap is never
    invisible.

    A caller who wants these to be fatal can turn them into errors::

        warnings.simplefilter("error", SchemaFeatureWarning)
    """


class JSONSchemaError(ValueError):
    """
    Base for every JSON Schema translation failure.

    Subclasses ``ValueError`` to match the convention used by
    ``DictContractAdapter`` and ``PydanticAdapter``.
    """


class UnsupportedSchemaError(JSONSchemaError):
    """
    The schema uses a construct this adapter deliberately does not support.

    Raised for the keywords on §5's reject list (``allOf``, ``not``,
    ``if``/``then``/``else``, ``patternProperties``, ``dependentSchemas``,
    ``propertyNames``, ``unevaluatedProperties``), and for property names
    that cannot be addressed by StateGuard's dot-notation paths.

    This is not a bug report -- it is the adapter refusing to under-validate.
    """


class SchemaReferenceError(JSONSchemaError):
    """
    A ``$ref`` could not be resolved, or resolving it would not terminate.

    Covers three cases, all of which must fail rather than hang or guess:

    * a remote reference (``http://``, ``file://``, or any pointer that does
      not start with ``#``) -- fetching one would turn schema extraction into
      a network operation against an untrusted URL;
    * a pointer that does not resolve within the document;
    * a reference cycle, either ``$ref``-to-``$ref`` or a recursive schema
      whose expansion has no fixed point.
    """
