"""The runtime formats this source actually reads and writes; stdlib only.

Build tooling loads this file directly, without importing the application.
Zero identifies today's unversioned protocol/pending formats, absent receipts,
and legacy task semantics. Receipt zero means absence only, never a serialized
v0 receipt decoder. Receipt v1 uses request_receipts; task v1 uses task_state's
emitter-aware reducer, which also preserves legacy history without rewriting it.
Schema migrations are separate from runtime readability: ordinary runtime
entrypoints require schema 12 even though explicit migration can upgrade older
databases. Add readable versions only alongside their implemented decoders.
"""

SQL_SCHEMA_VERSION = 12
SUPPORTED_SQL_SCHEMA_VERSIONS = frozenset({SQL_SCHEMA_VERSION})
PLANE_SCHEMA_VERSION = "1.0.0"
SUPPORTED_PLANE_SCHEMA_VERSIONS = frozenset({PLANE_SCHEMA_VERSION})
TRANSPORT_PROTOCOL_VERSION = 0
SUPPORTED_TRANSPORT_PROTOCOL_VERSIONS = frozenset({TRANSPORT_PROTOCOL_VERSION})
PENDING_FORMAT_VERSION = 0
SUPPORTED_PENDING_FORMAT_VERSIONS = frozenset({PENDING_FORMAT_VERSION})
RECEIPT_FORMAT_VERSION = 1
SUPPORTED_RECEIPT_FORMAT_VERSIONS = frozenset({0, RECEIPT_FORMAT_VERSION})
TASK_MODEL_VERSION = 1
SUPPORTED_TASK_MODEL_VERSIONS = frozenset({0, TASK_MODEL_VERSION})
CONFIG_PLAN_VERSION = 1
SUPPORTED_CONFIG_PLAN_VERSIONS = frozenset({CONFIG_PLAN_VERSION})


def runtime_declaration() -> dict:
    """Fresh machine-readable build declaration; all supported sets are exact."""
    return {
        "declaration_version": 1,
        **{name: {"read": sorted(readable), "write": target}
           for name, readable, target in (
               ("schema", SUPPORTED_SQL_SCHEMA_VERSIONS, SQL_SCHEMA_VERSION),
               ("envelope", SUPPORTED_PLANE_SCHEMA_VERSIONS, PLANE_SCHEMA_VERSION),
               ("protocol", SUPPORTED_TRANSPORT_PROTOCOL_VERSIONS, TRANSPORT_PROTOCOL_VERSION),
               ("pending_format", SUPPORTED_PENDING_FORMAT_VERSIONS, PENDING_FORMAT_VERSION),
               ("receipt_format", SUPPORTED_RECEIPT_FORMAT_VERSIONS, RECEIPT_FORMAT_VERSION),
               ("task_model", SUPPORTED_TASK_MODEL_VERSIONS, TASK_MODEL_VERSION),
               ("config_plan", SUPPORTED_CONFIG_PLAN_VERSIONS, CONFIG_PLAN_VERSION),
           )},
    }
