"""
Record MCP traffic to a versioned, redacted, replayable JSON-lines file.

A recording is the input to measurement on traffic StateGuard did not
generate itself. It is evidence only about the traffic it holds: a recording
of a synthetic workload is a synthetic workload, whatever replays it. Every
file therefore declares its **provenance** in its header, and nothing that
reads one may report it as more than that.

Format (``stateguard.mcp.recording``, version 1)
-------------------------------------------------
One JSON object per line, keys sorted, no NaN. The first line is the
``header``; the last, when the recorder was closed cleanly, is ``end``.
Between them, in the order they happened (``seq`` strictly increasing):

``message``
    One line on the wire. ``dir`` is where it went (``client_to_relay``,
    ``relay_to_server``, ``server_to_relay``, ``relay_to_client``); ``gen``
    the server process generation (it changes on a restart); ``kind``
    ``request`` / ``notification`` / ``response`` / ``error`` / ``unparsed``
    / ``other``; ``id``, ``method`` and ``tool`` lifted out for indexing;
    ``message`` the redacted message (absent for ``unparsed``).
``audit``
    A relay audit event (StateGuard's decisions, execution-state changes,
    restarts), redacted.
``fault``
    A failure injected by a test harness -- the ground truth of a
    fault-injection recording. Real captures have none.
``label``
    Ground truth for one request supplied by whoever built the workload
    (for example the arguments that should have executed). Redacted.

Three kinds of content are kept apart:

* **Replay-required** -- the top-level fields above and the redacted
  ``message``. Deterministic: recording the same traffic twice gives the
  same records.
* **Diagnostic** -- everything under ``diag`` (line length; with
  ``deterministic=False`` also a monotonic timestamp and ``digest``, an
  HMAC-SHA256 of the raw line under a random key that lives only in the
  recorder's memory and is never written). Never needed to replay, and
  dropped by nothing that replays. The digest shows two lines in one
  recording were byte-identical -- that a call reached the server exactly
  as the client sent it -- without the content, and is meaningless
  across recordings: without the key it cannot be checked against a guess.
* **Sensitive** -- never written. Redaction happens before anything
  reaches the sink; the header says which policy ran and ``end`` how many
  values it changed.

Redaction
---------
Deterministic and rule-based -- there is no probabilistic secret detector.
Three layers, applied to every value at every depth:

1. **Key names.** A value under a key whose normalized name marks it as a
   credential (``authorization``, ``api_key``, ``password``, ``*token*``,
   ``*secret*``, ``cookie``, ``key`` ...) is replaced -- whole subtree,
   keeping only JSON types: strings become ``"[redacted]"``, numbers ``0``,
   booleans ``false``.
2. **Value patterns.** A string that contains a recognised credential shape
   (``Bearer ...``, a JWT, AWS / GitHub / Slack / Stripe / ``sk-`` keys, a
   PEM private key, ``user:pass@`` in a URL, ``?token=`` in a query,
   ``password=...``) is replaced whole, wherever it is -- even where the
   structure says the value is safe to keep.
3. **Free-form values** (default mode ``mask``): any string not on a short
   allowlist of protocol structure (``jsonrpc``, ``method``, tool names,
   protocol versions, content types, schema keywords ...) is masked
   shape-preservingly -- letters become ``a`` / ``A``, digits ``1``,
   punctuation and length are kept -- so the recording holds no user text,
   yet a type or format check sees the same shape. Integers of seven or
   more digits are reduced to their order of magnitude.
4. **Numbers** (``mask_numbers``; always on for ``real_capture``): every
   number is reduced to its sign and order of magnitude (``482913`` ->
   ``100000``, ``3.14`` -> ``1.0``, ``0.042`` -> ``0.01``), however
   innocuous its key. Kept verbatim only where the value is protocol or
   contract, never user data: JSON-RPC ids and ``error.code``; tool
   schemas from a listing; an argument value the tool's schema lists in
   ``enum`` / ``const``; HTTP statuses (100-599) under the status keys of
   ``error.data`` (what the auth classifier reads); the relay's own audit
   counters; the host's ``config``. Without it, numbers below 1,000,000 are
   kept -- right for generated traffic, unsafe for real traffic.

A tool argument is kept verbatim only when the tool's own schema vouches
that it is not user data: it equals, up to case and separators, or is
within two edits of, a member of the argument's ``enum`` / ``const``; or
the schema says the field is a number or boolean and the string is a small
number or a boolean word. Anything the redactor cannot classify -- an
unexpected type, nesting beyond 64 levels -- is replaced. A JSON object or
array sent as a string is redacted as the structure it holds and re-encoded
(no longer byte-identical, but its declared keys survive).

``mode="secrets_only"`` keeps free-form values and applies layers 1 and 2
only. It is for traffic known to hold no personal data, and the header says
it ran.

Recorder failures
-----------------
``Recorder`` never raises into its caller. If redaction or the sink fails,
it stops recording, counts the failure (by exception type -- never a value),
and marks the file incomplete; the traffic it was observing is unaffected.

Zero external dependencies.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import math
import queue
import re
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import IO, Any, TypeVar

from stateguard.adapters.mcp.failures import AUTH_CODES, STATUS_KEYS, signal_spans

_T = TypeVar("_T")

__all__ = [
    "FORMAT",
    "FORMAT_VERSION",
    "MAX_LINE_BYTES",
    "REDACTED",
    "SUPPORTED_VERSIONS",
    "Direction",
    "MalformedRecordingError",
    "Provenance",
    "Recorder",
    "RecorderCloseTimeoutError",
    "RecorderOverflowError",
    "Recording",
    "RecordingError",
    "RedactionMode",
    "RedactionPolicy",
    "Redactor",
    "UnsupportedRecordingVersionError",
    "mask_string",
    "parse_recording",
    "read_recording",
    "sensitive_key",
    "secret_pattern",
]

FORMAT = "stateguard.mcp.recording"
FORMAT_VERSION = 1
SUPPORTED_VERSIONS = frozenset({1})
#: A line longer than this is malformed: refused, never parsed.
MAX_LINE_BYTES = 8 * 1024 * 1024
#: Values nested deeper than this are replaced, never walked.
MAX_DEPTH = 64
REDACTED = "[redacted]"


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------


class Provenance(StrEnum):
    """
    Where the traffic in a recording came from. Only REAL_CAPTURE is real traffic.

    Two families, never mixed in one file:

    * **Generated** -- ``synthetic``, ``fault_injection``, ``replay``: a
      harness or a replay produced the traffic. The first two may carry
      ground truth (``fault`` and ``label`` records) because their builder
      knows it.
    * **Observed** -- ``local_development``, ``real_capture``: the relay saw
      traffic it did not generate. Nobody knows its ground truth, so these
      files carry no ``fault`` or ``label`` records: the recorder refuses to
      write them and the parser refuses a file that has them.
    """

    SYNTHETIC = "synthetic"
    FAULT_INJECTION = "fault_injection"
    #: Traffic produced by replaying a recording through a model server
    #: (the TapeServer): a derivative of its source, never new evidence.
    REPLAY = "replay"
    LOCAL_DEVELOPMENT = "local_development"
    REAL_CAPTURE = "real_capture"

    @property
    def is_real_traffic(self) -> bool:
        return self is Provenance.REAL_CAPTURE

    @property
    def observed(self) -> bool:
        """Traffic the relay saw rather than generated: it has no ground truth."""
        return self in (Provenance.LOCAL_DEVELOPMENT, Provenance.REAL_CAPTURE)

    @property
    def admits_ground_truth(self) -> bool:
        """Whether ``fault`` / ``label`` records may appear: only where a harness made them."""
        return self in (Provenance.SYNTHETIC, Provenance.FAULT_INJECTION)

    @property
    def evidence(self) -> str:
        """How a measurement on this traffic may be described."""
        return {
            Provenance.SYNTHETIC: "synthetic workload -- not real-world evidence",
            Provenance.FAULT_INJECTION: (
                "deterministic fault injection on a synthetic workload -- not real-world evidence"
            ),
            Provenance.REPLAY: (
                "replay of a recording through a model server -- a derivative of its source "
                "recording, not new evidence"
            ),
            Provenance.LOCAL_DEVELOPMENT: (
                "local development traffic -- real servers, not production use; "
                "not representative evidence"
            ),
            Provenance.REAL_CAPTURE: (
                "captured real traffic (shadow mode) -- evidence of what happened, "
                "not proof of reliability improvement"
            ),
        }[self]


class Direction(StrEnum):
    CLIENT_TO_RELAY = "client_to_relay"
    RELAY_TO_SERVER = "relay_to_server"
    SERVER_TO_RELAY = "server_to_relay"
    RELAY_TO_CLIENT = "relay_to_client"


class RedactionMode(StrEnum):
    #: Mask every free-form value; keep only protocol structure (default).
    MASK = "mask"
    #: Remove recognised credentials only; free-form values are persisted.
    SECRETS_ONLY = "secrets_only"


MESSAGE_KINDS = frozenset({"request", "notification", "response", "error", "unparsed", "other"})
RECORD_TYPES = frozenset({"header", "message", "audit", "fault", "label", "end"})


# ---------------------------------------------------------------------------
# Redaction rules
# ---------------------------------------------------------------------------

#: Normalized key names (lowercase, alphanumerics only) that are credentials.
_SENSITIVE_EXACT = frozenset(
    {"auth", "pwd", "pass", "pin", "otp", "totp", "mfa", "jwt", "sid", "ssn", "cvv", "cvc", "key"}
)
#: Normalized key fragments that mark a credential wherever they occur.
_SENSITIVE_PARTS = (
    "password",
    "passwd",
    "passphrase",
    "secret",
    "token",
    "apikey",
    "accesskey",
    "privatekey",
    "privkey",
    "signingkey",
    "credential",
    "cookie",
    "authorization",
    "authenticat",
    "bearer",
    "signature",
    "sessionid",
    "passcode",
    "verificationcode",
    "securitycode",
    "recoverycode",
    "backupcode",
    "mfacode",
    "otpcode",
    "creditcard",
    "cardnumber",
    "socialsecurity",
    "connectionstring",
)

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern))
    for name, pattern in (
        ("auth_scheme", r"(?i)\b(?:bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}"),
        ("jwt", r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]*"),
        ("aws_key", r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
        ("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
        ("slack_token", r"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
        ("stripe_key", r"\b[sprk]k_(?:live|test)_[A-Za-z0-9]{10,}"),
        ("sk_key", r"\bsk-[A-Za-z0-9_-]{16,}"),
        ("google_key", r"\bAIza[0-9A-Za-z_-]{30,}"),
        ("private_key", r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
        ("url_userinfo", r"[A-Za-z][A-Za-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@"),
        (
            "query_secret",
            r"(?i)[?&;](?:access_token|refresh_token|id_token|token|api_key|apikey|key|secret|"
            r"password|passwd|sig|signature|auth|code)=[^&\s]+",
        ),
        (
            "assignment",
            r"(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?key|token|auth)"
            r"\s*[:=]\s*\S+",
        ),
    )
)

#: A number written as a string. Under mask_numbers it is never an identifier.
_NUMERIC = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)$")
#: Strings kept verbatim where the structure allows it must look like this.
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_./:$\-\[\]*]{0,128}$")
#: Dict keys kept verbatim (outside a schema that declares them) must look like
#: this: a word-like identifier of at most 40 characters and at most 3 digits.
#: An opaque token used as a key (``{"a8f93c0d7e...": ...}``) does not.
_IDENTIFIER_KEY = re.compile(r"^(?=(?:[^0-9]*[0-9]){0,3}[^0-9]*$)[A-Za-z0-9_.:$/\-]{1,40}$")
_BOOLEAN_WORDS = frozenset(
    {"true", "false", "yes", "no", "y", "n", "t", "f", "on", "off", "1", "0"}
)
_SCHEMA_TEXT_KEYS = frozenset({"description", "title", "$comment"})
_SCHEMA_SAMPLE_KEYS = frozenset({"default", "examples", "example", "const", "enum"})
_SCHEMA_NAME_MAPS = frozenset({"properties", "patternProperties", "$defs", "definitions"})


def _normalize_key(key: str) -> str:
    return "".join(ch for ch in key.lower() if ch.isalnum())


def sensitive_key(key: str) -> bool:
    """Whether a value under *key* is a credential, by name alone."""
    normalized = _normalize_key(key)
    if not normalized:
        return False
    if normalized in _SENSITIVE_EXACT:
        return True
    if normalized.startswith("auth") and not normalized.startswith("author"):
        return True
    return any(part in normalized for part in _SENSITIVE_PARTS)


def secret_pattern(text: str) -> str | None:
    """The name of the first credential shape found in *text*, or None."""
    for name, pattern in _SECRET_PATTERNS:
        if pattern.search(text):
            return name
    return None


def mask_string(text: str) -> str:
    """Shape-preserving mask: letters to a/A, digits to 1, everything else kept."""
    out = []
    for ch in text:
        if ch.isalpha():
            out.append("A" if ch.isupper() else "a")
        elif ch.isalnum() or ch.isnumeric():  # digits, and the likes of "¼" or "Ⅻ"
            out.append("1")
        else:
            out.append(ch)
    return "".join(out)


def _mask_keeping_auth_signals(text: str) -> str:
    """
    Mask *text*, except the spans the auth classifier's text rules match
    (``401 Unauthorized``, ``HTTP 403``, ``error="invalid_token"``, a leading
    ``Forbidden:`` ...). Those are fixed vocabulary, never user data, and
    keeping them means a recording classifies as the live traffic did.
    Masking cannot create such a span: masked text holds only a / A / 1.
    """
    spans = sorted(signal_spans(text))
    if not spans:
        return mask_string(text)
    out: list[str] = []
    at = 0
    for start, end in spans:
        if start < at:
            start = at
        if start >= end:
            continue
        out.append(mask_string(text[at:start]))
        out.append(text[start:end])
        at = end
    out.append(mask_string(text[at:]))
    return "".join(out)


def _osa_distance(a: str, b: str, limit: int) -> int:
    """Optimal string alignment distance, cut off just above *limit*."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous2: list[int] = []
    previous = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            current[j] = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                current[j] = min(current[j], previous2[j - 2] + 1)
        if min(current) > limit:
            return limit + 1
        previous2, previous = previous, current
    return previous[-1]


def _schema_types(schema: Mapping[str, Any]) -> set[str]:
    declared = schema.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list):
        return {t for t in declared if isinstance(t, str)}
    return set()


def _branches(schema: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The schema and its immediate anyOf / oneOf / allOf branches."""
    found = [schema]
    for keyword in ("anyOf", "oneOf", "allOf"):
        branches = schema.get(keyword)
        if isinstance(branches, list):
            found.extend(b for b in branches if isinstance(b, Mapping))
    return found


def _schema_vouches(value: str, schema: Mapping[str, Any] | None, *, numbers: bool = True) -> bool:
    """
    Whether the argument's own schema shows *value* is not free-form user data.
    ``numbers=False`` (``mask_numbers``): a numeric string in a numeric field
    is not vouched for -- a field typed ``integer`` can hold a PIN.
    """
    if schema is None:
        return False
    members: list[str] = []
    types: set[str] = set()
    for branch in _branches(schema):
        types |= _schema_types(branch)
        enum = branch.get("enum")
        if isinstance(enum, list):
            members.extend(m for m in enum if isinstance(m, str))
        if isinstance(branch.get("const"), str):
            members.append(branch["const"])
    if members:
        normalized = _normalize_key(value)
        for member in members:
            if value == member or (normalized and normalized == _normalize_key(member)):
                return True
            if len(value) >= 4 and _osa_distance(value.lower(), member.lower(), 2) <= 2:
                return True
    if numbers and types & {"number", "integer"} and len(value) <= 12:
        try:
            number = float(value.strip())
        except ValueError:
            number = math.nan
        if math.isfinite(number) and abs(number) < 1_000_000:
            return True
    return "boolean" in types and value.strip().lower() in _BOOLEAN_WORDS


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _schema_lists_number(value: int | float, schema: Mapping[str, Any] | None) -> bool:
    """Whether the schema publishes *value* itself in ``enum`` / ``const`` (same JSON type)."""
    if schema is None:
        return False
    for branch in _branches(schema):
        members = list(branch["enum"]) if isinstance(branch.get("enum"), list) else []
        if "const" in branch:
            members.append(branch["const"])
        for member in members:
            if (
                _is_number(member)
                and member == value
                and isinstance(member, int) is isinstance(value, int)
            ):
                return True
    return False


def _magnitude(value: int | float) -> int | float:
    """Sign and order of magnitude only: 482913 -> 100000, -3.5 -> -1.0, 0.042 -> 0.01."""
    if value == 0:
        return value
    size = abs(value)
    if isinstance(value, int):
        reduced: int | float = 10 ** (len(str(size)) - 1)
    elif size >= 1:
        reduced = float(10 ** (len(str(int(size))) - 1))
    else:
        reduced = float(10 ** math.floor(math.log10(size)))
    return reduced if value > 0 else -reduced


@dataclass(frozen=True)
class RedactionPolicy:
    """What the redactor removes. Deterministic: same policy, same input, same output."""

    mode: RedactionMode = RedactionMode.MASK
    #: Extra key names (any spelling; normalized) treated as credentials.
    extra_sensitive_keys: frozenset[str] = frozenset()
    #: Reduce every number to sign and order of magnitude, except protocol and
    #: contract values (see "Numbers" in the module docs). Forced on for
    #: real_capture.
    mask_numbers: bool = False

    def describe(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "rules": ["key_names", "value_patterns"]
            + (["mask_free_form", "keep_auth_signals"] if self.mode is RedactionMode.MASK else [])
            + (["mask_numbers"] if self.mask_numbers else []),
            "patterns": [name for name, _ in _SECRET_PATTERNS],
            "extra_sensitive_keys": sorted(_normalize_key(k) for k in self.extra_sensitive_keys),
        }


class Redactor:
    """
    Redacts MCP messages and relay audit events. One per recording: the
    tool schemas it learns from listings, and the aliases it gives ids that
    look like credentials, never cross from one recording into another.
    """

    def __init__(self, policy: RedactionPolicy | None = None) -> None:
        self.policy = policy or RedactionPolicy()
        self._extra = frozenset(_normalize_key(k) for k in self.policy.extra_sensitive_keys)
        self._input_schemas: dict[str, Mapping[str, Any]] = {}
        self._id_aliases: dict[str, str] = {}
        #: Values changed so far (a count; never the values).
        self.redactions = 0

    @property
    def _mask(self) -> bool:
        return self.policy.mode is RedactionMode.MASK

    def _sensitive(self, key: str) -> bool:
        return sensitive_key(key) or _normalize_key(key) in self._extra

    def learn_tools(self, tools: Any) -> None:
        """Remember input schemas from a raw (unredacted) listing -- in memory only."""
        if not isinstance(tools, list):
            return
        for tool in tools:
            if isinstance(tool, Mapping) and isinstance(tool.get("name"), str):
                schema = tool.get("inputSchema")
                if isinstance(schema, Mapping):
                    self._input_schemas[tool["name"]] = schema

    # -- scalars ----------------------------------------------------------------

    def _changed(self, value: _T) -> _T:
        self.redactions += 1
        return value

    def _scrub(self, value: Any) -> Any:
        """A credential subtree: keep JSON types and keys, drop every value."""
        if isinstance(value, Mapping):
            out: dict[str, Any] = {}
            for k, v in value.items():
                self._put(out, k, self._scrub(v))
            return out
        if isinstance(value, list):
            return [self._scrub(v) for v in value]
        if value is None:
            return None
        if isinstance(value, bool):
            return self._changed(False) if value else False  # one bit, still not kept
        if isinstance(value, int):
            return self._changed(0) if value else 0
        if isinstance(value, float):
            return self._changed(0.0)
        return self._changed(REDACTED)

    def key(self, key: Any, *, declared: bool = False) -> str:
        """
        A dict key. Structure is needed to replay, so a key is kept when a
        schema declares it or it looks like an identifier; otherwise (an
        email address used as a key, free text) it is masked like a value.
        A key that is itself a credential is replaced by an alias.
        """
        text = str(key)
        if secret_pattern(text) is not None:
            return self._changed(self._alias(text, "key"))
        if self.policy.mask_numbers and not declared and _NUMERIC.match(text):
            return self._changed(mask_string(text))  # a number used as a key
        if declared or not self._mask or _IDENTIFIER_KEY.match(text):
            return text
        return self._changed(mask_string(text))

    def _put(self, out: dict[str, Any], key: Any, value: Any, *, declared: bool = False) -> None:
        """Set ``out[key(key)]``; never let two masked keys overwrite each other."""
        name = self.key(key, declared=declared)
        if name in out:
            n = 2
            while f"{name}~{n}" in out:
                n += 1
            name = f"{name}~{n}"
        out[name] = value

    def _alias(self, text: str, kind: str) -> str:
        if text not in self._id_aliases:
            self._id_aliases[text] = f"[redacted-{kind}-{len(self._id_aliases) + 1}]"
        return self._id_aliases[text]

    def string(self, text: str, *, keep: bool = False) -> str:
        if secret_pattern(text) is not None:
            return self._changed(REDACTED)
        if keep or not self._mask or text in AUTH_CODES:
            return text
        masked = _mask_keeping_auth_signals(text)
        return self._changed(masked) if masked != text else masked

    def _number(self, value: int | float) -> int | float:
        if isinstance(value, float) and not math.isfinite(value):
            return self._changed(0.0)
        if self.policy.mask_numbers:
            scale = _magnitude(value)
            return scale if scale == value else self._changed(scale)
        if not self._mask or abs(value) < 1_000_000:
            return value
        magnitude = 10 ** (len(str(int(abs(value)))) - 1)
        reduced: int | float = magnitude if isinstance(value, int) else float(magnitude)
        return self._changed(reduced if value > 0 else -reduced)

    def identifier(self, value: Any) -> Any:
        """A protocol identifier: kept if it looks like one, else masked."""
        if isinstance(value, str):
            keep = bool(_IDENTIFIER.match(value)) and not (
                self.policy.mask_numbers and _NUMERIC.match(value)
            )
            return self.string(value, keep=keep)
        return self.value(value)

    def request_id(self, value: Any) -> Any:
        """A JSON-RPC id: identity for replay, kept unless it looks like a credential."""
        if isinstance(value, str) and secret_pattern(value) is not None:
            return self._changed(self._alias(value, "id"))
        if isinstance(value, (str, int)) and not isinstance(value, bool) or value is None:
            return value
        return self.value(value)

    # -- generic ----------------------------------------------------------------

    def value(
        self,
        value: Any,
        *,
        schema: Mapping[str, Any] | None = None,
        depth: int = 0,
    ) -> Any:
        """Any JSON value. *schema* (an input schema) lets arguments it vouches for stay."""
        if depth > MAX_DEPTH:
            return self._changed(REDACTED)
        if isinstance(value, Mapping):
            properties = schema.get("properties") if isinstance(schema, Mapping) else None
            out: dict[str, Any] = {}
            for k, v in value.items():
                sub = properties.get(k) if isinstance(properties, Mapping) else None
                declared = isinstance(sub, Mapping)
                if self._sensitive(str(k)):
                    self._put(out, k, self._scrub(v), declared=declared)
                    continue
                self._put(
                    out,
                    k,
                    self.value(v, schema=sub if declared else None, depth=depth + 1),
                    declared=declared,
                )
            return out
        if isinstance(value, list):
            items = schema.get("items") if isinstance(schema, Mapping) else None
            sub = items if isinstance(items, Mapping) else None
            return [self.value(v, schema=sub, depth=depth + 1) for v in value]
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            if self.policy.mask_numbers and _schema_lists_number(value, schema):
                return value  # a published enum / const member: contract, not user data
            return self._number(value)
        if isinstance(value, str):
            embedded = self._embedded_json(value, schema, depth)
            if embedded is not None:
                return embedded
            return self.string(
                value, keep=_schema_vouches(value, schema, numbers=not self.policy.mask_numbers)
            )
        return self._changed(REDACTED)  # not JSON: nothing to vouch for it

    def _embedded_json(self, text: str, schema: Mapping[str, Any] | None, depth: int) -> str | None:
        """
        A JSON object or array sent as a string (a common drift): redacted as
        the structure it holds, so declared keys survive, then re-encoded.
        A string that is itself a credential never gets here.
        """
        stripped = text.strip()
        if not stripped or stripped[0] not in "[{" or secret_pattern(text) is not None:
            return None
        try:
            parsed = json.loads(stripped, parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            return None
        if not isinstance(parsed, (dict, list)):
            return None
        self.redactions += 1
        return json.dumps(self.value(parsed, schema=schema, depth=depth + 1), allow_nan=False)

    # -- MCP structure ----------------------------------------------------------

    def message(self, message: Any) -> Any:
        """A JSON-RPC message, with the MCP structure that replay needs kept."""
        if not isinstance(message, Mapping):
            return self.value(message)
        out: dict[str, Any] = {}
        for k, v in message.items():
            if k == "jsonrpc" and v == "2.0":
                out[k] = v  # the protocol constant
            elif k == "jsonrpc" or k == "method":
                out[k] = self.identifier(v)
            elif k == "id":
                out[k] = self.request_id(v)
            elif k == "params" and isinstance(v, Mapping):
                out[k] = self._params(v)
            elif k == "result" and isinstance(v, Mapping):
                out[k] = self._result(v)
            elif k == "error" and isinstance(v, Mapping):
                out[k] = self._error(v)
            else:
                self._put(out, k, self._scrub(v) if self._sensitive(str(k)) else self.value(v))
        return out

    def _meta(self, meta: Any) -> Any:
        if not isinstance(meta, Mapping):
            return self.value(meta)
        out: dict[str, Any] = {}
        for k, v in meta.items():
            if self._sensitive(str(k)):
                self._put(out, k, self._scrub(v))
            elif str(k).startswith("io.modelcontextprotocol/") or str(k).startswith(
                "io.stateguard/"
            ):
                self._put(out, k, self.identifier(v) if isinstance(v, str) else self.value(v))
            else:
                self._put(out, k, self.value(v))
        return out

    def _params(self, params: Mapping[str, Any]) -> dict[str, Any]:
        tool = params.get("name")
        schema = self._input_schemas.get(tool) if isinstance(tool, str) else None
        out: dict[str, Any] = {}
        for k, v in params.items():
            if k == "_meta":
                out[k] = self._meta(v)
            elif k in ("name", "protocolVersion"):
                out[k] = self.identifier(v)
            elif k == "arguments":
                out[k] = self.arguments(v, schema)
            elif self._sensitive(str(k)):
                self._put(out, k, self._scrub(v))
            else:
                self._put(out, k, self.value(v))
        return out

    def arguments(self, arguments: Any, schema: Mapping[str, Any] | None) -> Any:
        """Tool arguments, kept where the tool's schema vouches for them."""
        return self.value(arguments, schema=schema)

    def arguments_for(self, tool: str, arguments: Any) -> Any:
        return self.arguments(arguments, self._input_schemas.get(tool))

    def _result(self, result: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in result.items():
            if k in ("resultType", "protocolVersion") or (k == "isError" and isinstance(v, bool)):
                out[k] = self.identifier(v)
            elif k == "supportedVersions" and isinstance(v, list):
                out[k] = [self.identifier(x) for x in v]
            elif k == "tools" and isinstance(v, list):
                out[k] = [self.tool_definition(t) for t in v]
            elif k == "serverInfo" and isinstance(v, Mapping):
                out[k] = self.server_info(v)
            elif k == "content" and isinstance(v, list):
                out[k] = [self._content(item) for item in v]
            elif k == "_meta":
                out[k] = self._meta(v)
            elif self._sensitive(str(k)):
                self._put(out, k, self._scrub(v))
            else:
                self._put(out, k, self.value(v))
        return out

    def _content(self, item: Any) -> Any:
        if not isinstance(item, Mapping):
            return self.value(item)
        out: dict[str, Any] = {}
        for k, v in item.items():
            if k in ("type", "mimeType"):
                out[k] = self.identifier(v)
            elif self._sensitive(str(k)):
                self._put(out, k, self._scrub(v))
            else:
                self._put(out, k, self.value(v))
        return out

    def _error(self, error: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in error.items():
            if k == "code" and isinstance(v, int) and not isinstance(v, bool):
                out[k] = v
            elif k == "data" and isinstance(v, Mapping):
                data = self._meta(v)
                # An HTTP status where the auth classifier reads one: protocol
                # vocabulary, kept so a recording classifies as the traffic did.
                for name in STATUS_KEYS:
                    status = v.get(name)
                    if (
                        isinstance(status, int)
                        and not isinstance(status, bool)
                        and 100 <= status <= 599
                        and name in data
                    ):
                        data[name] = status
                out[k] = data
            elif self._sensitive(str(k)):
                self._put(out, k, self._scrub(v))
            else:
                self._put(out, k, self.value(v))
        return out

    def server_info(self, info: Mapping[str, Any]) -> dict[str, Any]:
        """
        The server's self-description: ``name`` and ``version`` kept when they
        look like identifiers (they say which server a recording is evidence
        about); everything else (title, description, URLs, icons) redacted.
        """
        out: dict[str, Any] = {}
        for k, v in info.items():
            if k in ("name", "version") and isinstance(v, str):
                # Server-published, so a numeric version ("2.0") is kept too.
                out[k] = self.string(v, keep=bool(_IDENTIFIER.match(v)))
            elif self._sensitive(str(k)):
                self._put(out, k, self._scrub(v))
            else:
                self._put(out, k, self.value(v))
        return out

    def tool_definition(self, tool: Any) -> Any:
        """A tool from a listing: name and schemas kept (schema text masked), the rest redacted."""
        if not isinstance(tool, Mapping):
            return self.value(tool)
        out: dict[str, Any] = {}
        for k, v in tool.items():
            if k == "name":
                out[k] = self.identifier(v)
            elif k in ("inputSchema", "outputSchema"):
                out[k] = self.schema(v)
            elif k == "annotations" and isinstance(v, Mapping):
                hints: dict[str, Any] = {}
                for a, b in v.items():
                    self._put(hints, a, b if isinstance(b, bool) else self.value(b))
                out[k] = hints
            elif self._sensitive(str(k)):
                self._put(out, k, self._scrub(v))
            else:
                self._put(out, k, self.value(v))
        return out

    def schema(self, schema: Any, depth: int = 0, *, credential: bool = False) -> Any:
        """
        A JSON Schema: kept -- it is the server's published contract, and
        replay validates against it -- except prose (description, title,
        $comment), which is masked, any value matching a credential shape,
        and sample values (default, examples, const, enum) of a property
        whose name marks it as a credential.
        """
        if depth > MAX_DEPTH:
            return self._changed(REDACTED)
        if isinstance(schema, Mapping):
            out: dict[str, Any] = {}
            for k, v in schema.items():
                if k in _SCHEMA_TEXT_KEYS and isinstance(v, str):
                    out[k] = self.string(v)
                elif credential and k in _SCHEMA_SAMPLE_KEYS:
                    out[k] = self._scrub(v)
                elif k in _SCHEMA_NAME_MAPS and isinstance(v, Mapping):
                    named: dict[str, Any] = {}
                    for name, sub in v.items():
                        self._put(
                            named,
                            name,
                            self.schema(sub, depth + 2, credential=self._sensitive(str(name))),
                            declared=True,
                        )
                    out[k] = named
                else:
                    self._put(
                        out, k, self.schema(v, depth + 1, credential=credential), declared=True
                    )
            return out
        if isinstance(schema, list):
            return [self.schema(v, depth + 1, credential=credential) for v in schema]
        if isinstance(schema, str):
            return self.string(schema, keep=True)
        if isinstance(schema, float) and not math.isfinite(schema):
            return self._changed(0.0)
        if schema is None or isinstance(schema, (bool, int, float)):
            return schema
        return self._changed(REDACTED)

    #: Audit fields kept verbatim when their value is a scalar identifier.
    _AUDIT_KEEP = frozenset(
        {
            "event",
            "category",
            "tool",
            "action",
            "auto_action",
            "listing",
            "disposition",
            "decision",
            "op",
            "trust",
            "path",
            "generation",
            "attempt",
            "attempts",
            "restarts_used",
            "in_flight_unknown",
            "surfaced",
            "acted",
            "observed",
            "is_error",
            "ok",
            "structured_checked",
            "kind",
            "source",
            "direction",
            "method",
            "mode",
            "breaking",
            "state",
            "effect",
            "declared",
            "reason",
            "where",
            "version",
            "expected",
            "got",
            "description_changed",
            "paths",
            "tools",
            "failure",
            "signal",
            "proposal",
        }
    )

    #: Audit fields whose numbers the relay itself computes (counters, trust,
    #: a listing's change counts): never traffic values, kept under mask_numbers.
    _AUDIT_METRICS = frozenset(
        {
            "generation",
            "attempt",
            "attempts",
            "restarts_used",
            "in_flight_unknown",
            "trust",
            "added",
            "removed",
            "changed",
            "unchanged",
            "unusable",
            "breaking",
        }
    )

    def audit(self, event: Mapping[str, Any], depth: int = 0) -> Any:
        """A relay audit event: decisions and states kept, free text masked."""
        if depth > MAX_DEPTH:
            return self._changed(REDACTED)
        out: dict[str, Any] = {}
        for k, v in event.items():
            if self._sensitive(str(k)):
                redacted = self._scrub(v)
            elif k == "id":
                redacted = self.request_id(v)
            elif k in self._AUDIT_METRICS and _is_number(v) and math.isfinite(v):
                redacted = v
            elif isinstance(v, Mapping):
                redacted = self.audit(v, depth + 1)
            elif isinstance(v, list):
                redacted = [
                    self.audit(x, depth + 1)
                    if isinstance(x, Mapping)
                    else self.identifier(x)
                    if k in self._AUDIT_KEEP
                    else self.value(x, depth=depth + 1)
                    for x in v
                ]
            elif k in self._AUDIT_KEEP:
                redacted = self.identifier(v)
            else:
                redacted = self.value(v)
            self._put(out, k, redacted)
        return out


# ---------------------------------------------------------------------------
# Recorder
# ---------------------------------------------------------------------------


def _dumps(record: Mapping[str, Any]) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _canonical_id(identifier: Any) -> str:
    return json.dumps(identifier, sort_keys=True)


#: Request ids remembered to label their responses with method and tool.
_PENDING_LIMIT = 4096


@dataclass
class _Pending:
    method: str
    tool: str | None


class RecorderOverflowError(RuntimeError):
    """The background queue was full: a record was dropped rather than block the host."""


class RecorderCloseTimeoutError(RuntimeError):
    """The background writer did not finish in time (a blocked sink)."""


_CLOSE = object()


class Recorder:
    """
    Writes one recording. Thread-safe; **never raises** into the code that
    feeds it. On any failure it stops, counts it, and leaves the file
    without a complete ``end`` record, so nothing replays a recording with
    a gap as if it were whole.

    ``background=True`` moves redaction and writing to a writer thread behind
    a bounded queue, so a slow or blocked sink can never stall the host: when
    the queue is full the record is dropped and recording stops (the file is
    then incomplete). Without it, each call redacts and writes before it
    returns -- simplest, and deterministic, but the host waits for the sink.

    Construction is the one place it does raise (``ValueError``), for a
    configuration that must never record at all: ``real_capture`` with any
    redaction but ``mask``. Real traffic holds user text; ``secrets_only``
    would persist it. ``real_capture`` also always masks numbers
    (``mask_numbers``), whatever the policy passed in says.

    On a provenance that admits no ground truth -- the observed ones
    (``local_development``, ``real_capture``) and ``replay`` -- ``fault()``
    and ``label()`` write nothing: nobody knows the ground truth of traffic
    the relay did not generate, so none may be attached. Each refused call
    is counted (``ground_truth_refused`` in ``end``).
    """

    def __init__(
        self,
        sink: IO[str],
        *,
        provenance: Provenance,
        session: str,
        policy: RedactionPolicy | None = None,
        deterministic: bool = True,
        config: Mapping[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        background: bool = False,
        queue_size: int = 10_000,
        close_timeout: float = 5.0,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        policy = policy or RedactionPolicy()
        provenance = Provenance(provenance)
        if provenance is Provenance.REAL_CAPTURE:
            if policy.mode is not RedactionMode.MASK:
                raise ValueError(
                    "real_capture recordings require mask redaction: real traffic holds user "
                    f"text, and redaction mode {policy.mode.value!r} would persist it"
                )
            # Not optional: a number under an innocuous key can be a PIN.
            policy = replace(policy, mask_numbers=True)
        self._sink = sink
        self._lock = threading.RLock()
        self._failure_lock = threading.Lock()
        self.redactor = Redactor(policy)
        self.provenance = provenance
        #: fault()/label() calls refused (see the class docstring).
        self.ground_truth_refused = 0
        self._deterministic = deterministic
        # Never written, never exposed: see "Diagnostic" in the module docs.
        self._digest_key = None if deterministic else secrets.token_bytes(32)
        self._clock = clock
        self._start = clock()
        self._seq = 0
        self._pending: dict[str, _Pending] = {}
        self.records = 0
        self.failures = 0
        self.failure_types: list[str] = []
        self.stopped = False
        self.closed = False
        self._sink_failed = False
        self._close_timeout = close_timeout
        self._queue: queue.Queue[Any] | None = None
        self._writer: threading.Thread | None = None
        header: dict[str, Any] = {
            "type": "header",
            "format": FORMAT,
            "version": FORMAT_VERSION,
            "provenance": self.provenance.value,
            "session": str(session),
            "deterministic": deterministic,
            "redaction": self.redactor.policy.describe(),
            "config": self._config(config or {}),
        }
        if not deterministic:
            # Diagnostic: when the session began (UTC, to the second). The
            # ``t_ms`` offsets in ``diag`` count from here.
            header["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(wall_clock()))
        self._emit(header)
        if background:
            self._queue = queue.Queue(maxsize=queue_size)
            self._writer = threading.Thread(
                target=self._drain, name="stateguard-recorder", daemon=True
            )
            self._writer.start()

    def _config(self, config: Mapping[str, Any]) -> dict[str, Any]:
        """Host settings: flag-like values kept, anything else redacted like any value."""
        out: dict[str, Any] = {}
        for k, v in config.items():
            if self.redactor._sensitive(str(k)):
                self.redactor._put(out, k, self.redactor._scrub(v))
            elif isinstance(v, str):
                self.redactor._put(out, k, self.redactor.identifier(v))
            elif _is_number(v) and math.isfinite(v) and self.redactor.policy.mask_numbers:
                self.redactor._put(out, k, v)  # an operator setting, not traffic
            else:
                self.redactor._put(out, k, self.redactor.value(v))
        return out

    # -- the never-raising core -----------------------------------------------

    def _emit(self, record: dict[str, Any]) -> None:
        with self._lock:
            if self.stopped or self.closed:
                return
            try:
                if record["type"] != "header":
                    self._seq += 1
                    record["seq"] = self._seq
                else:
                    record["seq"] = 0
                self._sink.write(_dumps(record) + "\n")
                self._sink.flush()
                self.records += 1
            except Exception as exc:  # noqa: BLE001 -- a recorder never breaks its host
                self._sink_failed = True
                self._stop(exc)

    def _stop(self, exc: BaseException) -> None:
        # A leaf lock of its own: the host may record a failure while the
        # writer thread holds the main lock, blocked in the sink.
        with self._failure_lock:
            self.failures += 1
            self.failure_types.append(type(exc).__name__)
            self.stopped = True

    def _guard(self, build: Callable[[], dict[str, Any] | None]) -> None:
        if self.stopped or self.closed:
            return
        if self._queue is not None:
            try:
                self._queue.put_nowait(build)
            except queue.Full:
                if not self.stopped:
                    self._stop(RecorderOverflowError())
            return
        self._run(build)

    def _run(self, build: Callable[[], dict[str, Any] | None]) -> None:
        """Build (redact) and write one record, in order, under the lock."""
        with self._lock:
            if self.stopped or self.closed:
                return
            try:
                record = build()
            except Exception as exc:  # noqa: BLE001 -- e.g. a value redaction cannot handle
                self._stop(exc)
                return
            if record is not None:
                self._emit(record)

    def _drain(self) -> None:
        assert self._queue is not None
        while True:
            build = self._queue.get()
            if build is _CLOSE:
                self._write_end()
                return
            self._run(build)

    def _now(self) -> float | None:
        return None if self._deterministic else self._clock()

    def _diag(self, at: float | None, **fields: Any) -> dict[str, Any]:
        if at is not None:
            fields["t_ms"] = round((at - self._start) * 1000.0, 3)
        return fields

    # -- what the host feeds in -----------------------------------------------

    def message(self, direction: Direction | str, line: bytes, *, generation: int = 0) -> None:
        """One line as it crossed the wire (already written, or just read)."""
        at = self._now()  # when it crossed, not when a background writer got to it
        self._guard(lambda: self._message_record(Direction(direction), line, generation, at))

    def _message_record(
        self, direction: Direction, line: bytes, generation: int, at: float | None
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "type": "message",
            "dir": direction.value,
            "gen": int(generation),
            "diag": self._diag(at, bytes=len(line)),
        }
        if self._digest_key is not None:
            record["diag"]["digest"] = hmac.new(
                self._digest_key, line.rstrip(b"\r\n"), hashlib.sha256
            ).hexdigest()[:32]
        try:
            message = json.loads(line)
        except (ValueError, UnicodeDecodeError, RecursionError):
            record["kind"] = "unparsed"
            return record
        if not isinstance(message, dict):
            record["kind"] = "other"
            record["message"] = self.redactor.value(message)
            return record
        params = message.get("params")
        method = message.get("method")
        has_id = "id" in message and message["id"] is not None
        if isinstance(method, str):
            record["kind"] = "request" if has_id else "notification"
            tool = params.get("name") if isinstance(params, dict) else None
            tool = tool if isinstance(tool, str) and method == "tools/call" else None
            if has_id:
                self._pending[_canonical_id(message["id"])] = _Pending(method, tool)
                while len(self._pending) > _PENDING_LIMIT:
                    del self._pending[next(iter(self._pending))]
        elif "result" in message or "error" in message:
            record["kind"] = "error" if "error" in message else "response"
            pending = self._pending.get(_canonical_id(message.get("id")))
            method, tool = (pending.method, pending.tool) if pending else (None, None)
            result = message.get("result")
            if direction is Direction.SERVER_TO_RELAY and isinstance(result, dict):
                self.redactor.learn_tools(result.get("tools"))
        else:
            record["kind"] = "other"
            tool = None
        if "id" in message:
            record["id"] = self.redactor.request_id(message["id"])
        if isinstance(method, str):
            record["method"] = self.redactor.identifier(method)
        if tool is not None:
            record["tool"] = self.redactor.identifier(tool)
        record["message"] = self.redactor.message(message)
        return record

    def audit(self, event: Mapping[str, Any]) -> None:
        """A relay audit event (the relay's ``_log``)."""
        event = dict(event)
        self._guard(lambda: {"type": "audit", "event": self.redactor.audit(event)})

    def _refuse_ground_truth(self) -> bool:
        """True (and counted) when this provenance may carry no ground truth."""
        if self.provenance.admits_ground_truth:
            return False
        with self._failure_lock:
            self.ground_truth_refused += 1
        return True

    def fault(self, fault: str, **fields: Any) -> None:
        """A failure a harness injected. Ground truth for replay; refused on observed traffic."""
        if self._refuse_ground_truth():
            return
        self._guard(lambda: {"type": "fault", "fault": str(fault), **self.redactor.audit(fields)})

    def label(self, identifier: Any, *, tool: str | None = None, **fields: Any) -> None:
        """
        Ground truth for one request (e.g. ``intended=`` arguments). Argument
        values go through the same redaction as the request's own arguments,
        so they stay comparable. Refused on observed traffic.
        """
        if self._refuse_ground_truth():
            return

        def build() -> dict[str, Any]:
            record: dict[str, Any] = {
                "type": "label",
                "id": self.redactor.request_id(identifier),
            }
            if tool is not None:
                record["tool"] = self.redactor.identifier(tool)
            for k, v in fields.items():
                if k == "intended" and tool is not None and v is not None:
                    record[k] = self.redactor.arguments_for(tool, v)
                elif k in ("category", "injector", "note", "kind"):
                    record[k] = self.redactor.identifier(v)
                else:
                    record[self.redactor.key(k)] = self.redactor.value(v)
            return record

        self._guard(build)

    def close(self) -> None:
        """Write ``end``. A recorder that stopped early says so: ``complete: false``."""
        if self.closed:
            return
        if self._queue is None or self._writer is None:
            self._write_end()
            return
        # The writer thread writes ``end`` after everything queued before it;
        # the host waits at most ``close_timeout`` for that, never for the sink.
        with contextlib.suppress(queue.Full):
            self._queue.put(_CLOSE, timeout=self._close_timeout)
        self._writer.join(self._close_timeout)
        if self._writer.is_alive():
            # Blocked in the sink: give up without waiting. If it ever
            # finishes, its end record says the recording is incomplete.
            self._stop(RecorderCloseTimeoutError())
        self.closed = True

    def _write_end(self) -> None:
        end = {
            "type": "end",
            "records": self.records,
            "complete": not self.stopped,
            "failures": self.failures,
            "failure_types": list(self.failure_types),
            "values_redacted": self.redactor.redactions,
        }
        if self.ground_truth_refused:
            end["ground_truth_refused"] = self.ground_truth_refused
        with self._lock:
            # A sink that failed takes no more writes; one that did not still
            # gets the end record, which says recording stopped early.
            if not self._sink_failed:
                try:
                    self._seq += 1
                    end["seq"] = self._seq
                    self._sink.write(_dumps(end) + "\n")
                    self._sink.flush()
                except Exception as exc:  # noqa: BLE001
                    self._stop(exc)
            self.closed = True

    @property
    def healthy(self) -> bool:
        return not self.stopped


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


class RecordingError(ValueError):
    """A recording cannot be used. Messages name the line and the problem, never content."""


class MalformedRecordingError(RecordingError):
    pass


class UnsupportedRecordingVersionError(RecordingError):
    pass


@dataclass(frozen=True)
class Recording:
    header: dict[str, Any]
    records: tuple[dict[str, Any], ...]
    end: dict[str, Any] | None = None
    #: Where it was read from, for reports (never content).
    source: str = ""

    @property
    def provenance(self) -> Provenance:
        return Provenance(self.header["provenance"])

    @property
    def session(self) -> str:
        return str(self.header["session"])

    @property
    def complete(self) -> bool:
        """Closed cleanly, nothing lost: the only state replay trusts by default."""
        return self.end is not None and self.end.get("complete") is True

    def of_type(self, record_type: str) -> tuple[dict[str, Any], ...]:
        return tuple(r for r in self.records if r["type"] == record_type)

    def messages(self, direction: Direction | str | None = None) -> list[dict[str, Any]]:
        wanted = None if direction is None else Direction(direction).value
        return [
            r
            for r in self.records
            if r["type"] == "message" and (wanted is None or r["dir"] == wanted)
        ]


def _malformed(number: int, reason: str) -> MalformedRecordingError:
    return MalformedRecordingError(f"line {number}: {reason}")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_header(record: dict[str, Any], number: int) -> None:
    if record.get("type") != "header":
        raise _malformed(number, "the first record must be the header")
    if record.get("format") != FORMAT:
        raise _malformed(number, f"not a {FORMAT} file")
    version = record.get("version")
    if not _is_int(version):
        raise _malformed(number, "header version must be an integer")
    if version not in SUPPORTED_VERSIONS:
        raise UnsupportedRecordingVersionError(
            f"line {number}: format version {version} is not supported "
            f"(supported: {sorted(SUPPORTED_VERSIONS)})"
        )
    if record.get("provenance") not in {p.value for p in Provenance}:
        raise _malformed(number, "header provenance missing or unknown")
    if not isinstance(record.get("session"), str):
        raise _malformed(number, "header session must be a string")
    redaction = record.get("redaction")
    if not isinstance(redaction, dict) or redaction.get("mode") not in {
        m.value for m in RedactionMode
    }:
        raise _malformed(number, "header redaction policy missing or unknown")
    if record.get("seq") != 0:
        raise _malformed(number, "header seq must be 0")
    if "started_at" in record and not isinstance(record["started_at"], str):
        raise _malformed(number, "header started_at must be a string")
    if record["provenance"] == Provenance.REAL_CAPTURE.value and redaction["mode"] != (
        RedactionMode.MASK.value
    ):
        raise _malformed(number, "a real_capture recording must use mask redaction")
    if record["provenance"] == Provenance.REAL_CAPTURE.value and "mask_numbers" not in (
        redaction.get("rules") or []
    ):
        raise _malformed(number, "a real_capture recording must mask numbers")


def _check_record(record: dict[str, Any], number: int) -> None:
    kind = record.get("type")
    if kind == "message":
        if record.get("dir") not in {d.value for d in Direction}:
            raise _malformed(number, "message direction missing or unknown")
        if not _is_int(record.get("gen")) or record["gen"] < 0:
            raise _malformed(number, "message generation must be a non-negative integer")
        if record.get("kind") not in MESSAGE_KINDS:
            raise _malformed(number, "message kind missing or unknown")
        if "id" in record and not (
            record["id"] is None or isinstance(record["id"], str) or _is_int(record["id"])
        ):
            raise _malformed(number, "message id must be a string, an integer or null")
        if record["kind"] != "unparsed" and "message" not in record:
            raise _malformed(number, "message body missing")
        if record["kind"] in ("request", "notification", "response", "error") and not isinstance(
            record.get("message"), dict
        ):
            raise _malformed(number, "message body must be an object")
    elif kind == "audit":
        event = record.get("event")
        if not isinstance(event, dict) or not isinstance(event.get("event"), str):
            raise _malformed(number, "audit record without an event name")
    elif kind == "fault":
        if not isinstance(record.get("fault"), str):
            raise _malformed(number, "fault record without a fault name")
    elif kind == "label":
        if "id" not in record:
            raise _malformed(number, "label record without an id")
    elif kind == "end":
        if not _is_int(record.get("records")) or not isinstance(record.get("complete"), bool):
            raise _malformed(number, "end record malformed")
    elif kind == "header":
        raise _malformed(number, "a second header")
    else:
        raise _malformed(number, "unknown record type")
    if "diag" in record and not isinstance(record["diag"], dict):
        raise _malformed(number, "diag must be an object")


def parse_recording(lines: Iterable[str | bytes], *, source: str = "") -> Recording:
    """
    Parse and validate a recording. Raises ``RecordingError`` (malformed or
    unsupported version) and nothing else, whatever the input.
    """
    header: dict[str, Any] | None = None
    records: list[dict[str, Any]] = []
    end: dict[str, Any] | None = None
    last_seq = 0
    number = 0
    try:
        for number, raw in enumerate(lines, start=1):
            if isinstance(raw, bytes):
                if len(raw) > MAX_LINE_BYTES:
                    raise _malformed(number, "line too long")
                try:
                    raw = raw.decode("utf-8")
                except UnicodeDecodeError:
                    raise _malformed(number, "not UTF-8") from None
            if len(raw) > MAX_LINE_BYTES:
                raise _malformed(number, "line too long")
            if not raw.strip():
                continue
            try:
                record = json.loads(raw, parse_constant=_reject_constant)
            except (ValueError, RecursionError):
                raise _malformed(number, "not a JSON object") from None
            if not isinstance(record, dict):
                raise _malformed(number, "not a JSON object")
            if header is None:
                _check_header(record, number)
                header = record
                continue
            if end is not None:
                raise _malformed(number, "record after end")
            _check_record(record, number)
            if (
                record["type"] in ("fault", "label")
                and not Provenance(header["provenance"]).admits_ground_truth
            ):
                raise _malformed(
                    number,
                    f"a {record['type']} record in a {header['provenance']} recording: "
                    "observed and replayed traffic carries no ground truth",
                )
            seq = record.get("seq")
            if not isinstance(seq, int) or isinstance(seq, bool) or seq <= last_seq:
                raise _malformed(number, "seq must increase strictly")
            last_seq = seq
            if record["type"] == "end":
                end = record
            else:
                records.append(record)
    except RecordingError:
        raise
    except Exception as exc:  # noqa: BLE001 -- the contract: RecordingError only
        raise _malformed(number, f"unreadable ({type(exc).__name__})") from None
    if header is None:
        raise MalformedRecordingError("empty recording: no header")
    return Recording(header=header, records=tuple(records), end=end, source=source)


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-JSON constant {name}")


def read_recording(path: str | Path) -> Recording:
    """Read a recording file (see ``parse_recording``)."""
    target = Path(path)
    try:
        with target.open("rb") as handle:
            return parse_recording(handle, source=target.name)
    except OSError as exc:
        raise RecordingError(f"cannot read recording: {type(exc).__name__}") from None
