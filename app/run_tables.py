"""Create and describe run-specific raw-event and metadata tables."""

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

import click
from flask import Flask, current_app
from sqlalchemy import (
    Column,
    DateTime,
    Float,
    Integer,
    JSON,
    MetaData,
    String,
    Table,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.engine import Connection


RUN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
RAW_TABLE_SCHEMA_VERSION = 1


def _table_names(run_id: str) -> tuple[str, str]:
    """Validate the run ID before using it as part of dynamic SQL table names."""
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise ValueError("Run ID must start with a lowercase letter and contain only lowercase letters, digits, or underscores (max 32 characters).")
    return f"raw_{run_id}", f"metadata_{run_id}"


def _run_tables(run_id: str) -> tuple[MetaData, Table, Table]:
    """Define one run's raw event table and its single-row metadata table."""
    raw_table_name, metadata_table_name = _table_names(run_id)
    metadata = MetaData()
    raw_table = Table(
        raw_table_name,
        metadata,
        Column("event_id", String(64), primary_key=True),
        Column("study_run_id", String(32), nullable=False, server_default=text(f"'{run_id}'")),
        Column("participant_id", String(64)),
        Column("room_id", String(64)),
        Column("room_code", String(16)),
        Column("event_type", String(64), nullable=False, index=True),
        Column("dialogue_number", Integer),
        Column("turn_number", Integer),
        Column("occurred_at", DateTime(timezone=True)),
        Column("started_at", DateTime(timezone=True)),
        Column("ended_at", DateTime(timezone=True)),
        Column("duration_seconds", Float),
        Column("event_data", JSON, nullable=False),
    )
    run_metadata = Table(
        metadata_table_name,
        metadata,
        Column("study_run_id", String(32), primary_key=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
        Column("schema_version", Integer, nullable=False),
        Column("protocol_version", String(64), nullable=False),
        Column("application_revision", String(64)),
        Column("configuration", JSON, nullable=False),
        Column("configuration_hash", String(64), nullable=False),
    )
    return metadata, raw_table, run_metadata


def _configuration_hash(configuration: dict[str, Any]) -> str:
    """Hash canonical JSON so run settings can be compared without leaking values."""
    import hashlib

    encoded = json.dumps(configuration, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def initialize_study_run(
    engine: Engine,
    run_id: str,
    configuration: dict[str, Any],
    protocol_version: str,
    application_revision: str | None = None,
) -> bool:
    """Create the run tables and immutable metadata; return True only on first creation."""
    if not isinstance(configuration, dict):
        raise ValueError("Run configuration must be a JSON object.")
    if not isinstance(protocol_version, str) or not protocol_version.strip():
        raise ValueError("Protocol version must be a non-empty string.")

    metadata, raw_table, run_metadata = _run_tables(run_id)
    expected_tables = {raw_table.name, run_metadata.name}
    existing_tables = set(inspect(engine).get_table_names()) & expected_tables
    configuration_hash = _configuration_hash(configuration)

    if existing_tables:
        if existing_tables != expected_tables:
            raise ValueError("Only one run table exists; refusing to modify a partial run initialization.")
        with engine.connect() as connection:
            saved = connection.execute(
                select(run_metadata).where(run_metadata.c.study_run_id == run_id)
            ).mappings().one_or_none()
        if saved is None:
            raise ValueError("Run tables exist without their metadata row; refusing to guess configuration.")
        if (
            saved["schema_version"] != RAW_TABLE_SCHEMA_VERSION
            or saved["protocol_version"] != protocol_version
            or saved["configuration_hash"] != configuration_hash
        ):
            raise ValueError("Run is already initialized with different immutable metadata.")
        return False

    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(run_metadata.insert().values(
            study_run_id=run_id,
            created_at=datetime.now(timezone.utc),
            schema_version=RAW_TABLE_SCHEMA_VERSION,
            protocol_version=protocol_version,
            application_revision=application_revision,
            configuration=configuration,
            configuration_hash=configuration_hash,
        ))
    return True


def insert_raw_events(connection: Connection, run_id: str, events: list[dict[str, Any]]) -> int:
    """Insert buffered events once, omitting study_run_id so the table default supplies it."""
    if not events:
        return 0

    # Define the dynamic table only after validating the run suffix used in its SQL name.
    _, raw_table, _ = _run_tables(run_id)
    event_ids = [event.get("event_id") for event in events]
    if any(not isinstance(event_id, str) or not event_id for event_id in event_ids):
        raise ValueError("Every raw event must have a non-empty event_id.")
    if len(set(event_ids)) != len(event_ids):
        raise ValueError("A raw event batch cannot contain duplicate event_id values.")

    # Find existing IDs so retries do not duplicate events and later interval closures can update their rows.
    existing_rows = {
        row["event_id"]: row
        for row in connection.execute(
            select(raw_table.c.event_id, raw_table.c.ended_at, raw_table.c.duration_seconds)
            .where(raw_table.c.event_id.in_(event_ids))
        ).mappings()
    }
    timestamp_columns = {"occurred_at", "started_at", "ended_at"}
    allowed_columns = {column.name for column in raw_table.columns} - {"study_run_id"}
    rows = []
    for event in events:
        if "study_run_id" in event:
            raise ValueError("Raw events must rely on the table-specific study_run_id default.")
        if not isinstance(event.get("event_type"), str) or not isinstance(event.get("event_data"), dict):
            raise ValueError("Raw events require an event_type and one event_data object.")
        existing = existing_rows.get(event["event_id"])
        if existing is not None:
            # Intervals are buffered open at feedback, then closed later by the next page or study-completion request.
            updates = {}
            if event.get("ended_at") is not None and existing["ended_at"] is None:
                updates["ended_at"] = event["ended_at"]
                if isinstance(updates["ended_at"], str):
                    updates["ended_at"] = datetime.fromisoformat(updates["ended_at"])
            if event.get("duration_seconds") is not None and existing["duration_seconds"] is None:
                updates["duration_seconds"] = event["duration_seconds"]
            if updates:
                connection.execute(
                    raw_table.update().where(raw_table.c.event_id == event["event_id"]).values(**updates)
                )
            continue

        # Convert server ISO timestamps to SQLAlchemy datetime values for the raw table's typed time columns.
        # Supply explicit NULLs for absent optional fields so mixed event types batch consistently.
        row = {key: event.get(key) for key in allowed_columns}
        for key in timestamp_columns:
            timestamp = row.get(key)
            if isinstance(timestamp, str):
                row[key] = datetime.fromisoformat(timestamp)
        rows.append(row)

    if rows:
        # Use the caller's transaction so raw events and the feedback archive commit or roll back together.
        connection.execute(raw_table.insert(), rows)
    return len(rows)


def persist_raw_events(engine: Engine, run_id: str, events: list[dict[str, Any]]) -> int:
    """Commit one buffered event batch atomically to the selected raw run table."""
    if not events:
        return 0
    with engine.begin() as connection:
        return insert_raw_events(connection, run_id, events)


def _load_configuration(path: Path) -> dict[str, Any]:
    """Load a JSON object while rejecting likely credentials from run metadata."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise click.ClickException(f"Could not read run configuration JSON: {error}") from error
    if not isinstance(value, dict):
        raise click.ClickException("Run configuration must be a JSON object.")

    def reject_secret_keys(item: Any, location: str = "configuration") -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                normalized_key = str(key).lower()
                if any(marker in normalized_key for marker in ("secret", "password", "token", "api_key", "private_key")):
                    raise click.ClickException(f"Sensitive-looking setting is not allowed in metadata: {location}.{key}")
                reject_secret_keys(child, f"{location}.{key}")
        elif isinstance(item, list):
            for index, child in enumerate(item):
                reject_secret_keys(child, f"{location}[{index}]")

    reject_secret_keys(value)

    for field in ("target_n_pairs", "target_n_overall", "target_n_rooms"):
        target = value.get(field)
        if target is not None and (not isinstance(target, int) or isinstance(target, bool) or target < 0):
            raise click.ClickException(f"{field} must be a non-negative integer or null.")

    participant_materials = value.get("participant_materials")
    if participant_materials is not None:
        if not isinstance(participant_materials, dict):
            raise click.ClickException("participant_materials must be a JSON object.")
        workspace_root = path.resolve().parent.parent
        snapshots = {}
        for material_name in ("consent", "general_instructions", "role_instructions", "debriefing"):
            material_path = participant_materials.get(material_name)
            if not isinstance(material_path, str) or not material_path.strip():
                raise click.ClickException(f"participant_materials.{material_name} must be a file path.")
            source_path = Path(material_path)
            if not source_path.is_absolute():
                source_path = workspace_root / source_path
            try:
                text_snapshot = source_path.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError) as error:
                raise click.ClickException(f"Could not read participant material {material_name}: {error}") from error
            if not text_snapshot:
                raise click.ClickException(f"Participant material {material_name} is empty.")
            snapshots[material_name] = {
                "source": str(source_path.relative_to(workspace_root)) if source_path.is_relative_to(workspace_root) else str(source_path),
                "text": text_snapshot,
            }
        value["participant_materials"] = snapshots
    return value


def register_run_table_commands(app: Flask) -> None:
    """Register the explicit command that initializes one run's tables."""
    @app.cli.command("init-study-run")
    @click.option("--run-id", required=True, help="Lowercase table suffix, for example test.")
    @click.option("--config", "config_path", type=click.Path(path_type=Path, exists=True, dir_okay=False), required=True)
    @click.option("--protocol-version", required=True)
    @click.option("--application-revision", default=None)
    def init_study_run_command(
        run_id: str,
        config_path: Path,
        protocol_version: str,
        application_revision: str | None,
    ) -> None:
        """Initialize raw_<run_id> and metadata_<run_id> without replacing existing data."""
        configuration = _load_configuration(config_path)
        # Refuse to initialize a participant run that declares mandatory verification while the app has it disabled.
        if configuration.get("human_verification_required") is True and current_app.config.get("TURNSTILE_ENABLED") is not True:
            raise click.ClickException("This study run requires human verification, but TURNSTILE_ENABLED is not true.")
        try:
            created = initialize_study_run(
                current_app.extensions["sqlalchemy"].engine,
                run_id,
                configuration,
                protocol_version,
                application_revision,
            )
        except ValueError as error:
            raise click.ClickException(str(error)) from error
        raw_table, metadata_table = _table_names(run_id)
        action = "Initialized" if created else "Already initialized"
        click.echo(f"{action} {raw_table} and {metadata_table}.")
