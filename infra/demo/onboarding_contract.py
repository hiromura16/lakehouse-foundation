"""Build reviewable SQL source drafts from bounded metadata, without I/O.

This prototype never opens a database, accepts credentials, emits executable
SQL, registers a connector, or treats a browser checklist as approval. The
same pure engine serves Streamlit and FastAPI. Imported metadata remains an
unverified statement until a future authenticated discovery/runtime checks it.
"""

import hashlib
import json
import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

MAX_METADATA_BYTES = 262_144
MAX_CAPTURE_NAME_LENGTH = 100
Identifier = Annotated[
    str,
    Field(min_length=1, max_length=128, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"),
]
Slug = Annotated[
    str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_-]*$")
]
CaptureIdentifier = Annotated[
    str,
    Field(
        min_length=1,
        max_length=MAX_CAPTURE_NAME_LENGTH,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    ),
]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Classification = Literal[
    "unknown", "public", "internal", "sensitive", "excluded"
]
SQL_TYPE = re.compile(
    r"^[a-z][a-z0-9_]*(?:\((?:max|[0-9]{1,4})(?:,[0-9]{1,2})?\))?$"
)
CREDENTIAL_TEXT = re.compile(
    r"(?i)(?:\b(?:password|passwd|pwd|secret|token|credential"
    r"|api[_-]?key|aws_access_key_id|aws_secret_access_key)\s*[:=]"
    r"|\b(?:server|data source|initial catalog|user id|uid)\s*="
    r"|\b(?:bearer|basic)\s+\S+|[a-z][a-z0-9+.-]*://|-----BEGIN)"
)
SENSITIVE_COLUMN = re.compile(
    r"(?i)(?:password|passwd|secret|token|credential|api_?key|email"
    r"|phone|address|birth|salary|personal|original_values)"
)
SUPPORTED_TYPES = {
    "bigint",
    "bit",
    "char",
    "date",
    "datetime",
    "datetime2",
    "datetimeoffset",
    "decimal",
    "float",
    "int",
    "money",
    "nchar",
    "numeric",
    "nvarchar",
    "real",
    "smallint",
    "smallmoney",
    "time",
    "tinyint",
    "uniqueidentifier",
    "varchar",
}


class DraftModel(BaseModel):
    """Reject coercion and unrecognized fields, including secret settings."""

    model_config = ConfigDict(
        extra="forbid", strict=True, hide_input_in_errors=True
    )


def plain_text(value: str) -> str:
    """Bound annotations to one line and reject obvious credential payloads.

    This is input hygiene, not a claim of universal secret detection. The UI
    also tells operators to use metadata only and store exports privately.
    """
    if any(
        ord(character) < 32 or ord(character) == 127 for character in value
    ):
        raise ValueError("Only single-line annotations are accepted.")
    if CREDENTIAL_TEXT.search(value):
        raise ValueError("Credentials and connection URLs are not accepted.")
    return value.strip()


class Column(DraftModel):
    """Describe a physical column; no values, expressions or samples enter."""

    name: Identifier
    sql_type: str = Field(min_length=1, max_length=40)
    nullable: bool
    computed: bool = False

    @field_validator("sql_type")
    @classmethod
    def bounded_type(cls, value):
        """Allow a type description, never an arbitrary SQL fragment."""
        if not SQL_TYPE.fullmatch(value):
            raise ValueError("Use a lowercase bounded SQL type description.")
        return value


class DiscoveredTable(DraftModel):
    """Retain ordered physical columns and the declared composite key."""

    schema_name: Identifier
    table_name: Identifier
    columns: list[Column] = Field(min_length=1, max_length=128)
    primary_key: list[Identifier] = Field(default_factory=list, max_length=16)
    capture_instance: CaptureIdentifier | None = None
    cdc_enabled: bool = False

    @model_validator(mode="after")
    def unique_columns_and_keys(self):
        """Refuse ambiguous columns or key metadata before proposal work."""
        names = [column.name for column in self.columns]
        if len(set(names)) != len(names):
            raise ValueError("Duplicate metadata columns are not accepted.")
        if len(set(self.primary_key)) != len(self.primary_key):
            raise ValueError("Duplicate primary-key columns are not accepted.")
        if not set(self.primary_key).issubset(names):
            raise ValueError("Primary-key metadata must name known columns.")
        return self


class Discovery(DraftModel):
    """Import a bounded metadata snapshot, not a connection specification."""

    schema_version: Literal[1] = 1
    source_ref: Slug = "configured-demo"
    origin: Literal["synthetic", "imported"]
    tables: list[DiscoveredTable] = Field(min_length=1, max_length=50)

    @field_validator("schema_version", mode="before")
    @classmethod
    def exact_version_type(cls, value):
        """Literal equality must not admit Python True or floating-point 1."""
        if type(value) is not int:
            raise ValueError("The schema version must be an integer.")
        return value

    @model_validator(mode="after")
    def distinct_bounded_tables(self):
        """Cap aggregate work as well as each nested collection."""
        identities = [
            (item.schema_name, item.table_name) for item in self.tables
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("Duplicate metadata tables are not accepted.")
        if sum(len(item.columns) for item in self.tables) > 512:
            raise ValueError("Metadata contains too many total columns.")
        return self


class Selection(DraftModel):
    """Represent choices made with widgets, with unresolved defaults."""

    schema_name: Identifier
    table_name: Identifier
    included_columns: list[Identifier] = Field(min_length=1, max_length=128)
    logical_name: str = Field(default="", max_length=128)
    business_purpose: str = Field(default="", max_length=512)
    freshness_minutes: int = Field(default=60, ge=1, le=10080)
    delete_handling: Literal["unreviewed", "retain", "propagate"] = (
        "unreviewed"
    )
    column_classifications: dict[Identifier, Classification] = Field(
        default_factory=dict, max_length=128
    )
    primary_key_confirmed: bool = False

    _annotations = field_validator("logical_name", "business_purpose")(
        plain_text
    )

    @model_validator(mode="after")
    def distinct_included_columns(self):
        """Keep multiplicity unambiguous without rewriting user intent."""
        if len(set(self.included_columns)) != len(self.included_columns):
            raise ValueError("Duplicate selected columns are not accepted.")
        return self


class DraftReview(DraftModel):
    """Collect self-declared review notes, never authenticated approvals."""

    business: bool = False
    technical: bool = False
    dba: bool = False
    security: bool = False
    contract_sha256: Digest | None = None


class ProposalRequest(DraftModel):
    """Bind all table choices and review notes to one metadata snapshot."""

    discovery: Discovery
    source_id: Slug = "configured-demo"
    owner_team: str = Field(default="", max_length=128)
    selections: list[Selection] = Field(min_length=1, max_length=50)
    review: DraftReview = Field(default_factory=DraftReview)

    _owner = field_validator("owner_team")(plain_text)

    @model_validator(mode="after")
    def distinct_selections(self):
        """Reject repeated tables so a contract cannot override itself."""
        identities = [
            (item.schema_name, item.table_name) for item in self.selections
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("Duplicate selected tables are not accepted.")
        return self


def canonical_hash(value) -> str:
    """Hash canonical JSON including order where order has source meaning."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def metadata_hash(discovery: Discovery | dict) -> str:
    """Return the hash of the complete validated metadata snapshot."""
    model = Discovery.model_validate(discovery)
    return canonical_hash(model.model_dump(mode="json"))


def parse_document(content: bytes | str):
    """Read a bounded UTF-8 JSON document and reject duplicate JSON keys.

    This does not accept paths and does not open a file. UI uploads and API
    transports must also enforce their own byte limits before buffering.
    """
    if not isinstance(content, (bytes, str)):
        raise ValueError("A UTF-8 metadata JSON document is required.")
    try:
        encoded = (
            content.encode("utf-8") if isinstance(content, str) else content
        )
    except UnicodeError as error:
        raise ValueError("Metadata JSON is invalid.") from error
    if len(encoded) > MAX_METADATA_BYTES:
        raise ValueError("Metadata JSON exceeds the size limit.")

    def unique_object(pairs):
        """Do not let last-key-wins parsing hide conflicting metadata."""
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON fields are not accepted.")
            result[key] = value
        return result

    def reject_constant(_value):
        """JSON metadata must not contain JavaScript NaN/Infinity values."""
        raise ValueError("Non-finite JSON values are not accepted.")

    try:
        value = json.loads(
            encoded.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("Metadata JSON is invalid.") from error
    return value


def parse_discovery(content: bytes | str) -> Discovery:
    """Validate an imported metadata snapshot without accepting file paths."""
    return Discovery.model_validate(parse_document(content))


def parse_proposal(content: bytes | str) -> ProposalRequest:
    """Validate the same bounded duplicate-free transport for proposal APIs."""
    return ProposalRequest.model_validate(parse_document(content))


def issue(code: str, message: str, table=None, column=None) -> dict:
    """Create bounded application-owned diagnostics without raw errors."""
    result = {"code": code, "message": message}
    if table is not None:
        result["table"] = table
    if column is not None:
        result["column"] = column
    return result


def supported_type(sql_type: str) -> bool:
    """Check basic ranges without claiming runtime connector compatibility."""
    base = sql_type.split("(", 1)[0]
    if base not in SUPPORTED_TYPES:
        return False
    match = re.fullmatch(r"(?:decimal|numeric)\((\d+),(\d+)\)", sql_type)
    if base in {"decimal", "numeric"}:
        return bool(
            match
            and 1 <= int(match[1]) <= 38
            and 0 <= int(match[2]) <= int(match[1])
        )
    if base in {"datetime2", "datetimeoffset", "time"} and "(" in sql_type:
        match = re.fullmatch(r"\w+\(([0-7])\)", sql_type)
        return match is not None
    if base in {"char", "nchar", "varchar", "nvarchar"}:
        match = re.fullmatch(r"\w+\((max|\d+)\)", sql_type)
        if match is None:
            return False
        if match[1] == "max":
            return base in {"varchar", "nvarchar"}
        return 1 <= int(match[1]) <= (4000 if base.startswith("n") else 8000)
    return "(" not in sql_type


def table_proposal(selection: Selection, table: DiscoveredTable):
    """Check one selected table and preserve its complete reviewable shape."""
    identity = table.schema_name + "." + table.table_name
    columns = {column.name: column for column in table.columns}
    blockers, warnings = [], []
    missing = set(selection.included_columns) - columns.keys()
    if missing:
        blockers.append(
            issue("UNKNOWN_COLUMN", "未検出の列が選択されています。", identity)
        )
    if set(selection.column_classifications) - columns.keys():
        blockers.append(
            issue(
                "UNKNOWN_CLASSIFICATION_COLUMN",
                "分類対象の列が未検出です。",
                identity,
            )
        )
    if not table.primary_key:
        blockers.append(
            issue(
                "MISSING_PRIMARY_KEY",
                "物理主キーを確認してください。",
                identity,
            )
        )
    if not set(table.primary_key).issubset(selection.included_columns):
        blockers.append(
            issue(
                "PRIMARY_KEY_EXCLUDED",
                "主キー列は取込対象に必要です。",
                identity,
            )
        )
    if not selection.primary_key_confirmed:
        blockers.append(
            issue(
                "PRIMARY_KEY_UNCONFIRMED",
                "業務上も主キーが安定するか確認してください。",
                identity,
            )
        )
    if not selection.logical_name or not selection.business_purpose:
        blockers.append(
            issue(
                "BUSINESS_CONTEXT_REQUIRED",
                "業務名と利用目的を入力してください。",
                identity,
            )
        )
    if selection.delete_handling == "unreviewed":
        blockers.append(
            issue(
                "DELETE_POLICY_UNREVIEWED",
                "削除の保持・伝播方針を確認してください。",
                identity,
            )
        )
    if table.cdc_enabled and table.capture_instance is None:
        blockers.append(
            issue(
                "CAPTURE_METADATA_INCOMPLETE",
                "CDC定義の追加調査が必要です。",
                identity,
            )
        )
    capture = table.capture_instance or (
        table.schema_name + "_" + table.table_name + "_v1"
    )
    if len(capture) > MAX_CAPTURE_NAME_LENGTH:
        blockers.append(
            issue(
                "CAPTURE_NAME_TOO_LONG",
                "capture名はDBAの別設計が必要です。",
                identity,
            )
        )
    selected_columns = []
    for name in selection.included_columns:
        if name not in columns:
            continue
        column = columns[name]
        classification = selection.column_classifications.get(name, "unknown")
        if classification in {"unknown", "excluded"}:
            blockers.append(
                issue(
                    "COLUMN_CLASSIFICATION_REQUIRED",
                    "取込列の機密区分を確認してください。",
                    identity,
                    name,
                )
            )
        if column.computed or not supported_type(column.sql_type):
            blockers.append(
                issue(
                    "COLUMN_COMPATIBILITY_REVIEW",
                    "この列は別途型・CDC互換性の確認が必要です。",
                    identity,
                    name,
                )
            )
        if name in table.primary_key and column.nullable:
            blockers.append(
                issue(
                    "NULLABLE_PRIMARY_KEY",
                    "NULL許可の主キー候補は採用できません。",
                    identity,
                    name,
                )
            )
        if SENSITIVE_COLUMN.search(name):
            warnings.append(
                issue(
                    "SENSITIVE_COLUMN_CANDIDATE",
                    "機密列候補です。名称だけで安全とは判定しません。",
                    identity,
                    name,
                )
            )
        selected_columns.append(
            {**column.model_dump(), "classification": classification}
        )
    if selection.delete_handling == "propagate":
        warnings.append(
            issue(
                "DELETE_PROPAGATION_REVIEW",
                "削除伝播は人の承認と別途受入試験が必要です。",
                identity,
            )
        )
    if not table.cdc_enabled:
        warnings.append(
            issue(
                "CDC_NOT_VERIFIED",
                "CDC準備は未確認です。DB変更は行いません。",
                identity,
            )
        )
    contract = {
        **selection.model_dump(),
        "primary_key": table.primary_key,
        "capture_instance": capture,
        "cdc_enabled": table.cdc_enabled,
        "columns": selected_columns,
    }
    return contract, blockers, warnings


def propose(request: ProposalRequest | dict) -> dict:
    """Generate a fresh draft and invalidate review notes on any change."""
    model = ProposalRequest.model_validate(request)
    blockers, warnings, tables = [], [], []
    metadata_sha256 = metadata_hash(model.discovery)
    catalog = {
        (table.schema_name, table.table_name): table
        for table in model.discovery.tables
    }
    if not model.owner_team:
        blockers.append(
            issue(
                "OWNER_TEAM_REQUIRED", "運用責任を持つ部門を入力してください。"
            )
        )
    names = [
        item.logical_name for item in model.selections if item.logical_name
    ]
    if len(names) != len(set(names)):
        blockers.append(
            issue(
                "DUPLICATE_LOGICAL_NAME",
                "業務名をテーブルごとに区別してください。",
            )
        )
    for selection in model.selections:
        identity = (selection.schema_name, selection.table_name)
        if identity not in catalog:
            blockers.append(
                issue("UNKNOWN_TABLE", "未検出のテーブルが選択されています。")
            )
            continue
        table, table_blocks, table_warnings = table_proposal(
            selection, catalog[identity]
        )
        tables.append(table)
        blockers.extend(table_blocks)
        warnings.extend(table_warnings)
    # Both discovered names and generated names must be distinct. Different
    # schema/table pairs can collapse when joined with underscores. This is
    # an exact-name draft check only, not a database-collation identity test.
    capture_names = [table["capture_instance"] for table in tables]
    if len(capture_names) != len(set(capture_names)):
        blockers.append(
            issue(
                "CAPTURE_NAME_COLLISION",
                "capture名が重複しています。DBAによる別設計が必要です。",
            )
        )
    contract = {
        "api_version": "lakehouse.source/v1alpha1",
        "kind": "SqlServerCdcSourceDraft",
        "source_id": model.source_id,
        "source_ref": model.discovery.source_ref,
        "owner_team": model.owner_team,
        "metadata_sha256": metadata_sha256,
        "requested_selections_sha256": canonical_hash(
            [
                selection.model_dump(mode="json")
                for selection in model.selections
            ]
        ),
        "tables": tables,
    }
    contract_sha256 = canonical_hash(contract)
    roles = ("business", "technical", "dba", "security")
    hash_matches = model.review.contract_sha256 == contract_sha256
    acknowledged = any(getattr(model.review, role) for role in roles)
    if acknowledged and not hash_matches:
        blockers.append(
            issue(
                "STALE_REVIEW",
                "構想が変更されました。レビュー項目を確認し直してください。",
            )
        )
    for role in roles:
        if not hash_matches or not getattr(model.review, role):
            blockers.append(
                issue(
                    "REVIEW_" + role.upper() + "_PENDING",
                    "担当者の確認メモが未完了です。",
                )
            )
    warnings.append(
        issue(
            "APPROVAL_NOT_VERIFIED",
            "画面の確認メモは正式な権限承認ではありません。",
        )
    )
    warnings.append(
        issue(
            "METADATA_UNVERIFIED",
            "metadataの実DBとの一致・権限・稼働は未検証です。",
        )
    )
    return {
        "schema_version": 1,
        "status": "proposed",
        "contract": contract,
        "metadata_sha256": metadata_sha256,
        "contract_sha256": contract_sha256,
        "warnings": warnings,
        "blockers": blockers,
        "review": {
            **{
                role: bool(hash_matches and getattr(model.review, role))
                for role in roles
            },
            "hash_matches": hash_matches,
            "verified": False,
        },
        "applied": False,
        "can_apply": False,
        "approvals_verified": False,
        "database_checked": False,
    }


def make_plan(request: ProposalRequest | dict) -> dict:
    """Recompute a private review plan; never accept an asserted proposal."""
    result = propose(request)
    result["status"] = "plan"
    result["steps"] = [
        {
            "id": "discovery",
            "status": "not_run",
            "description": "読取り専用Discoveryで実DB metadataを照合",
        },
        {
            "id": "review",
            "status": "pending",
            "description": "現場・技術・DBA・securityの独立承認",
        },
        {
            "id": "runtime",
            "status": "not_implemented",
            "description": "承認receiptを検証する共通runtime",
        },
        {
            "id": "acceptance",
            "status": "not_run",
            "description": "snapshot・I/U/D・再起動・欠落/重複の受入",
        },
    ]
    return result


def builtin_discovery() -> dict:
    """Return six synthetic fixed-demo tables, not live SQL observations.

    Column types/order/nullability mirror 002_demo_tables.sql. A regression
    independently compares this fixture with that public DDL. No source
    reader, DBA credentials, file paths or company endpoint is consulted.
    """
    definitions = [
        (
            "lh_demo_erp",
            "products",
            [
                ("product_id", "nvarchar(160)"),
                ("product_name", "nvarchar(256)"),
                ("product_family", "nvarchar(128)"),
                ("unit", "nvarchar(32)"),
                ("standard_cycle_seconds", "decimal(18,3)"),
            ],
        ),
        (
            "lh_demo_erp",
            "work_orders",
            [
                ("work_order_id", "nvarchar(160)"),
                ("factory_id", "nvarchar(64)"),
                ("product_id", "nvarchar(160)"),
                ("planned_date", "date"),
                ("planned_qty", "bigint"),
            ],
        ),
        (
            "lh_demo_erp",
            "sales_orders",
            [
                ("sales_order_id", "nvarchar(160)"),
                ("factory_id", "nvarchar(64)"),
                ("product_id", "nvarchar(160)"),
                ("order_date", "date"),
                ("promised_date", "date"),
                ("ordered_qty", "bigint"),
                ("unit_price", "decimal(18,2)"),
            ],
        ),
        (
            "lh_demo_erp",
            "shipments",
            [
                ("shipment_id", "nvarchar(160)"),
                ("sales_order_id", "nvarchar(160)"),
                ("shipped_at", "datetime2(7)"),
                ("shipped_qty", "bigint"),
            ],
        ),
        (
            "lh_demo_mes",
            "events",
            [
                ("event_id", "nvarchar(160)"),
                ("work_order_id", "nvarchar(160)"),
                ("event_time", "datetime2(7)"),
                ("good_qty", "bigint"),
                ("reject_qty", "bigint"),
            ],
        ),
        (
            "lh_demo_mes",
            "downtime",
            [
                ("downtime_id", "nvarchar(160)"),
                ("work_order_id", "nvarchar(160)"),
                ("start_time", "datetime2(7)"),
                ("end_time", "datetime2(7)"),
                ("reason_code", "nvarchar(64)"),
            ],
        ),
    ]
    tables = []
    for schema, table, columns in definitions:
        physical = [
            {"name": name, "sql_type": kind, "nullable": index != 0}
            for index, (name, kind) in enumerate(columns)
        ]
        physical.extend(
            {"name": name, "sql_type": kind, "nullable": False}
            for name, kind in [
                ("updated_at", "datetime2(7)"),
                ("created_at", "datetime2(7)"),
                ("run_id", "char(12)"),
                ("original_values", "nvarchar(max)"),
            ]
        )
        tables.append(
            {
                "schema_name": schema,
                "table_name": table,
                "columns": physical,
                "primary_key": [columns[0][0]],
            }
        )
    return Discovery.model_validate(
        {
            "origin": "synthetic",
            "tables": tables,
        }
    ).model_dump(mode="json")
