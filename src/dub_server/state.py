"""Durable SQLite job state and checkpoint storage.

The API and the network-isolated worker share this database through a local
Docker volume.  All writes append an event in the same transaction so SSE
clients never observe a state change without its matching event.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from .domain import TranscriptionResult
from .translation import TranslationBlock
from .translation_artifact import TranslationResult


class JobStatus(StrEnum):
    CREATED = "created"
    SEARCHING = "searching"
    AWAITING_RELEASE_SELECTION = "awaiting_release_selection"
    DOWNLOADING = "downloading"
    SUBTITLE_MATCHING = "subtitle_matching"
    READY_OFFLINE = "ready_offline"
    TRANSCRIBING = "transcribing"
    SUBTITLE_SELECTED = "subtitle_selected"
    READY_TRANSLATION = "ready_translation"
    TRANSLATING = "translating"
    READY_TTS = "ready_tts"
    SEPARATING = "separating"
    SYNTHESIZING = "synthesizing"
    TIMING = "timing"
    MIXING = "mixing"
    MUXING = "muxing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    PAUSED = "paused"
    FAILED = "failed"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    NEEDS_LANGUAGE = "needs_language"
    NEEDS_SUBTITLE_SELECTION = "needs_subtitle_selection"


class JobStage(StrEnum):
    ACQUISITION = "acquisition"
    SUBTITLE = "subtitle"
    ASR = "asr"
    TRANSLATION = "translation"
    SEPARATION = "separation"
    TTS = "tts"
    TIMING = "timing"
    MIX = "mix"
    EXPORT = "export"
    LIP_SYNC = "lip_sync"
    VERIFY = "verify"
    DONE = "done"


TERMINAL_STATUSES = frozenset(
    {JobStatus.COMPLETED, JobStatus.CANCELLED}
)

_SCHEMA_VERSION = 13

ACTIVE_JOB_STATUSES = (
    JobStatus.CREATED,
    JobStatus.SEARCHING,
    JobStatus.AWAITING_RELEASE_SELECTION,
    JobStatus.DOWNLOADING,
    JobStatus.SUBTITLE_MATCHING,
    JobStatus.READY_OFFLINE,
    JobStatus.TRANSCRIBING,
    JobStatus.SUBTITLE_SELECTED,
    JobStatus.READY_TRANSLATION,
    JobStatus.TRANSLATING,
    JobStatus.READY_TTS,
    JobStatus.SEPARATING,
    JobStatus.SYNTHESIZING,
    JobStatus.TIMING,
    JobStatus.MIXING,
    JobStatus.MUXING,
    JobStatus.VERIFYING,
    JobStatus.NEEDS_LANGUAGE,
    JobStatus.NEEDS_SUBTITLE_SELECTION,
    JobStatus.CANCELLING,
)

_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.CREATED: frozenset(
        {JobStatus.DOWNLOADING, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.SEARCHING: frozenset(
        {
            JobStatus.AWAITING_RELEASE_SELECTION,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.AWAITING_RELEASE_SELECTION: frozenset(
        {JobStatus.DOWNLOADING, JobStatus.FAILED, JobStatus.CANCELLED}
    ),
    JobStatus.DOWNLOADING: frozenset(
        {
            JobStatus.SUBTITLE_MATCHING,
            JobStatus.READY_OFFLINE,
            JobStatus.PAUSED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.SUBTITLE_MATCHING: frozenset(
        {
            JobStatus.READY_OFFLINE,
            JobStatus.NEEDS_SUBTITLE_SELECTION,
            JobStatus.PAUSED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.READY_OFFLINE: frozenset(
        {
            JobStatus.TRANSCRIBING,
            JobStatus.SUBTITLE_SELECTED,
            JobStatus.PAUSED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.TRANSCRIBING: frozenset(
        {
            JobStatus.READY_TRANSLATION,
            JobStatus.NEEDS_LANGUAGE,
            JobStatus.PAUSED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.SUBTITLE_SELECTED: frozenset(
        {
            JobStatus.READY_TRANSLATION,
            JobStatus.PAUSED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.NEEDS_LANGUAGE: frozenset(
        {JobStatus.TRANSCRIBING, JobStatus.PAUSED, JobStatus.CANCELLED}
    ),
    JobStatus.NEEDS_SUBTITLE_SELECTION: frozenset(
        {
            JobStatus.READY_OFFLINE,
            JobStatus.SUBTITLE_SELECTED,
            JobStatus.TRANSCRIBING,
            JobStatus.PAUSED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.READY_TRANSLATION: frozenset(
        {
            JobStatus.TRANSLATING,
            JobStatus.PAUSED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.TRANSLATING: frozenset(
        {
            JobStatus.READY_TTS,
            JobStatus.PAUSED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.READY_TTS: frozenset(
        {
            JobStatus.SEPARATING,
            JobStatus.SYNTHESIZING,
            JobStatus.PAUSED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.SEPARATING: frozenset(
        {
            JobStatus.SYNTHESIZING,
            JobStatus.PAUSED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.SYNTHESIZING: frozenset(
        {
            JobStatus.TIMING,
            JobStatus.PAUSED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.TIMING: frozenset(
        {
            JobStatus.MIXING,
            JobStatus.MUXING,
            JobStatus.PAUSED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.MIXING: frozenset(
        {
            JobStatus.MUXING,
            JobStatus.PAUSED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.MUXING: frozenset(
        {
            JobStatus.VERIFYING,
            JobStatus.PAUSED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.VERIFYING: frozenset(
        {
            JobStatus.COMPLETED,
            JobStatus.PAUSED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.PAUSED: frozenset({JobStatus.CANCELLED}),
    JobStatus.FAILED: frozenset({JobStatus.CANCELLED}),
    JobStatus.CANCELLING: frozenset({JobStatus.CANCELLED}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


class StateError(RuntimeError):
    pass


class JobNotFound(StateError):
    pass


class InvalidTransition(StateError):
    pass


class DuplicateJob(StateError):
    pass


class ActiveJobExists(StateError):
    pass


class StorageQuotaExceeded(StateError):
    pass


class JobDeletionConflict(StateError):
    pass


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    release_id: str
    status: JobStatus
    stage: JobStage
    progress_permille: int
    spec: dict[str, Any]
    details: dict[str, Any]
    result: dict[str, Any] | None
    error_code: str | None
    error_message: str | None
    retryable: bool
    cancel_requested: bool
    active_slot: bool
    pinned: bool
    previous_status: JobStatus | None
    revision: int
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "release_id": self.release_id,
            "status": self.status.value,
            "stage": self.stage.value,
            "progress_permille": self.progress_permille,
            "spec": self.spec,
            "details": self.details,
            "result": self.result,
            "error": (
                {
                    "code": self.error_code,
                    "message": self.error_message,
                    "retryable": self.retryable,
                }
                if self.error_code or self.error_message
                else None
            ),
            "cancel_requested": self.cancel_requested,
            "pinned": self.pinned,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class JobEvent:
    id: int
    job_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: str


@dataclass(frozen=True, slots=True)
class Checkpoint:
    job_id: str
    stage: JobStage
    payload: dict[str, Any]
    updated_at: str


@dataclass(frozen=True, slots=True)
class UploadStorageUsage:
    committed_bytes: int
    reserved_bytes: int

    @property
    def allocated_bytes(self) -> int:
        return self.committed_bytes + self.reserved_bytes


@dataclass(frozen=True, slots=True)
class StoredTranscriptSegment:
    """One durable source-language segment ready for Phase 3 translation."""

    job_id: str
    ordinal: int
    source: str
    language: str
    model_id: str | None
    start_us: int
    end_us: int
    text: str
    average_log_probability: float | None
    no_speech_probability: float | None


@dataclass(frozen=True, slots=True)
class StoredTranslationBlock:
    """One durable Phase 3 block, translated text may still be pending."""

    job_id: str
    ordinal: int
    start_us: int
    end_us: int
    source_text: str
    translated_text: str | None
    source_ordinals: tuple[int, ...]
    source_language: str
    target_language: str
    model_id: str
    plan_sha256: str
    source_token_count: int
    output_token_count: int | None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _encode(value: Mapping[str, Any] | None) -> str:
    return json.dumps(value or {}, ensure_ascii=False, separators=(",", ":"))


def _decode(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise StateError("Dữ liệu trạng thái trong cơ sở dữ liệu không hợp lệ")
    return parsed


class StateStore:
    """Small synchronous store safe for FastAPI thread-pool use."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self._initialize_lock = threading.Lock()
        self._initialized = False
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        with self._initialize_lock:
            if self._initialized:
                return
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = NORMAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS schema_metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    );
                    INSERT INTO schema_metadata(key, value)
                    VALUES ('schema_version', '1')
                    ON CONFLICT(key) DO NOTHING;

                    CREATE TABLE IF NOT EXISTS jobs (
                        id TEXT PRIMARY KEY,
                        release_id TEXT NOT NULL,
                        status TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        progress_permille INTEGER NOT NULL DEFAULT 0
                            CHECK(progress_permille BETWEEN 0 AND 1000),
                        spec_json TEXT NOT NULL,
                        details_json TEXT NOT NULL DEFAULT '{}',
                        result_json TEXT,
                        error_code TEXT,
                        error_message TEXT,
                        retryable INTEGER NOT NULL DEFAULT 0,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        active_slot INTEGER NOT NULL DEFAULT 0,
                        pinned INTEGER NOT NULL DEFAULT 0,
                        previous_status TEXT,
                        revision INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS jobs_status_updated_idx
                    ON jobs(status, updated_at);

                    CREATE TABLE IF NOT EXISTS checkpoints (
                        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        stage TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(job_id, stage)
                    );

                    CREATE TABLE IF NOT EXISTS transcript_segments (
                        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                        source TEXT NOT NULL CHECK(source IN ('asr', 'subtitle')),
                        language TEXT NOT NULL,
                        model_id TEXT,
                        start_us INTEGER NOT NULL CHECK(start_us >= 0),
                        end_us INTEGER NOT NULL CHECK(end_us > start_us),
                        text TEXT NOT NULL CHECK(length(text) > 0),
                        average_log_probability REAL,
                        no_speech_probability REAL,
                        PRIMARY KEY(job_id, ordinal)
                    );
                    CREATE INDEX IF NOT EXISTS transcript_segments_time_idx
                    ON transcript_segments(job_id, start_us, end_us);

                    CREATE TABLE IF NOT EXISTS translation_blocks (
                        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
                        start_us INTEGER NOT NULL CHECK(start_us >= 0),
                        end_us INTEGER NOT NULL CHECK(end_us > start_us),
                        source_text TEXT NOT NULL CHECK(length(source_text) > 0),
                        translated_text TEXT,
                        source_ordinals_json TEXT NOT NULL,
                        source_language TEXT NOT NULL,
                        target_language TEXT NOT NULL,
                        model_id TEXT NOT NULL,
                        plan_sha256 TEXT NOT NULL,
                        source_token_count INTEGER NOT NULL
                            CHECK(source_token_count > 0),
                        output_token_count INTEGER,
                        PRIMARY KEY(job_id, ordinal)
                    );
                    CREATE INDEX IF NOT EXISTS translation_blocks_time_idx
                    ON translation_blocks(job_id, start_us, end_us);

                    CREATE TABLE IF NOT EXISTS job_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                        event_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS job_events_job_id_id_idx
                    ON job_events(job_id, id);

                    CREATE TABLE IF NOT EXISTS upload_storage (
                        upload_id TEXT NOT NULL,
                        kind TEXT NOT NULL CHECK(kind IN ('media', 'subtitle')),
                        committed_bytes INTEGER NOT NULL DEFAULT 0
                            CHECK(committed_bytes >= 0),
                        reservation_id TEXT,
                        reserved_bytes INTEGER NOT NULL DEFAULT 0
                            CHECK(reserved_bytes >= 0),
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY(upload_id, kind),
                        CHECK(
                            (reservation_id IS NULL AND reserved_bytes = 0)
                            OR (reservation_id IS NOT NULL AND reserved_bytes > 0)
                        )
                    );
                    """
                )
                connection.execute("BEGIN IMMEDIATE")
                try:
                    version_row = connection.execute(
                        "SELECT value FROM schema_metadata "
                        "WHERE key = 'schema_version'"
                    ).fetchone()
                    schema_version = int(version_row[0]) if version_row else 1
                    if schema_version > _SCHEMA_VERSION:
                        raise StateError(
                            "Cơ sở dữ liệu có schema mới hơn ứng dụng: "
                            f"{schema_version} > {_SCHEMA_VERSION}"
                        )
                    columns = {
                        str(row["name"])
                        for row in connection.execute("PRAGMA table_info(jobs)")
                    }
                    active_values = ",".join(
                        f"'{status.value}'" for status in ACTIVE_JOB_STATUSES
                    )
                    if "active_slot" not in columns:
                        connection.execute(
                            "ALTER TABLE jobs ADD COLUMN active_slot "
                            "INTEGER NOT NULL DEFAULT 0"
                        )
                    if "pinned" not in columns:
                        connection.execute(
                            "ALTER TABLE jobs ADD COLUMN pinned "
                            "INTEGER NOT NULL DEFAULT 0"
                        )
                    if schema_version < 3:
                        # Recompute even when a prior process crashed after
                        # ALTER TABLE but before the backfill/version commit.
                        connection.execute(
                            "UPDATE jobs SET active_slot = CASE "
                            f"WHEN status IN ({active_values}) THEN 1 ELSE 0 END"
                        )
                    active_count = connection.execute(
                        "SELECT COUNT(*) FROM jobs WHERE active_slot = 1"
                    ).fetchone()[0]
                    if active_count > 1:
                        raise StateError(
                            "Cơ sở dữ liệu có nhiều job hoạt động; "
                            "không thể nâng cấp khóa GPU"
                        )
                    connection.execute("DROP INDEX IF EXISTS jobs_one_active_idx")
                    connection.execute(
                        "CREATE UNIQUE INDEX jobs_one_active_idx "
                        "ON jobs((1)) WHERE active_slot = 1"
                    )
                    if schema_version < _SCHEMA_VERSION:
                        now = _utc_now()
                        reclassified_errors: dict[str, str] = {}
                        if schema_version < 6:
                            reclassified_errors["output_track_layout_invalid"] = (
                                "mp4_auxiliary_track_fix"
                            )
                        if schema_version < 7:
                            reclassified_errors["output_duration_mismatch"] = (
                                "video_canonical_duration_fix"
                            )
                        if schema_version < 8:
                            reclassified_errors["output_track_layout_invalid"] = (
                                "mp4_cover_stream_selection_fix"
                            )
                        if schema_version < 9:
                            reclassified_errors["timing_rewrite_required"] = (
                                "timing_narration_auto_rewrite"
                            )
                        if schema_version < 10:
                            reclassified_errors["timing_rewrite_exhausted"] = (
                                "timing_narration_adaptive_rewrite"
                            )
                        if schema_version < 11:
                            reclassified_errors[
                                "timing_semantic_budget_impossible"
                            ] = "timing_narration_slack_group_fallback"
                        affected: list[sqlite3.Row] = []
                        if reclassified_errors:
                            placeholders = ",".join("?" for _ in reclassified_errors)
                            affected = connection.execute(
                                "SELECT id, error_code FROM jobs "
                                "WHERE status = ? AND retryable = 0 "
                                f"AND error_code IN ({placeholders})",
                                (
                                    JobStatus.FAILED.value,
                                    *reclassified_errors,
                                ),
                            ).fetchall()
                        for row in affected:
                            connection.execute(
                                "UPDATE jobs SET retryable = 1, "
                                "revision = revision + 1, updated_at = ? "
                                "WHERE id = ?",
                                (now, row["id"]),
                            )
                            self._insert_event(
                                connection,
                                str(row["id"]),
                                "job.error_reclassified",
                                {
                                    "code": str(row["error_code"]),
                                    "retryable": True,
                                    "reason": reclassified_errors[
                                        str(row["error_code"])
                                    ],
                                },
                                now,
                            )
                        if schema_version < 12:
                            single_block_failures = connection.execute(
                                "SELECT id, details_json FROM jobs "
                                "WHERE status = ? AND retryable = 0 "
                                "AND error_code = ?",
                                (
                                    JobStatus.FAILED.value,
                                    "timing_group_budget_impossible",
                                ),
                            ).fetchall()
                            for row in single_block_failures:
                                try:
                                    details = _decode(str(row["details_json"]))
                                except (json.JSONDecodeError, StateError, TypeError):
                                    # A malformed diagnostic must fail closed:
                                    # only a proven single-block failure is safe
                                    # to reopen for the new recovery policy.
                                    continue
                                failure = details.get("timing_failure")
                                if not isinstance(failure, Mapping):
                                    continue
                                ordinal = failure.get("ordinal")
                                failure_ordinal = failure.get("failure_ordinal")
                                group_start = failure.get(
                                    "critical_group_start_ordinal"
                                )
                                group_end = failure.get(
                                    "critical_group_end_ordinal"
                                )
                                deficit_us = failure.get("schedule_deficit_us")
                                rewrite_candidates = failure.get(
                                    "rewrite_candidates"
                                )
                                if (
                                    failure.get("failure_kind")
                                    not in {
                                        "single_window_capacity",
                                        "elastic_postvalidation",
                                    }
                                    or any(
                                        isinstance(value, bool)
                                        or not isinstance(value, int)
                                        for value in (
                                            ordinal,
                                            failure_ordinal,
                                            group_start,
                                            group_end,
                                        )
                                    )
                                    or failure_ordinal < 0
                                    or not ordinal
                                    == failure_ordinal
                                    == group_start
                                    == group_end
                                    or isinstance(deficit_us, bool)
                                    or not isinstance(deficit_us, int)
                                    or deficit_us <= 0
                                    or not isinstance(rewrite_candidates, list)
                                    or len(rewrite_candidates) != 1
                                ):
                                    continue
                                candidate = rewrite_candidates[0]
                                if not isinstance(candidate, Mapping):
                                    continue
                                candidate_ordinal = candidate.get("ordinal")
                                required_us = candidate.get(
                                    "required_duration_us"
                                )
                                target_us = candidate.get(
                                    "target_available_duration_us"
                                )
                                work_us = candidate.get("work_duration_us")
                                if (
                                    isinstance(candidate_ordinal, bool)
                                    or not isinstance(candidate_ordinal, int)
                                    or candidate_ordinal != failure_ordinal
                                    or isinstance(required_us, bool)
                                    or not isinstance(required_us, int)
                                    or required_us <= 0
                                    or isinstance(target_us, bool)
                                    or not isinstance(target_us, int)
                                    or target_us
                                    != max(1, required_us - deficit_us)
                                    or isinstance(work_us, bool)
                                    or not isinstance(work_us, int)
                                    or work_us <= 0
                                ):
                                    continue
                                connection.execute(
                                    "UPDATE jobs SET retryable = 1, "
                                    "revision = revision + 1, updated_at = ? "
                                    "WHERE id = ?",
                                    (now, row["id"]),
                                )
                                self._insert_event(
                                    connection,
                                    str(row["id"]),
                                    "job.error_reclassified",
                                    {
                                        "code": "timing_group_budget_impossible",
                                        "retryable": True,
                                        "reason": (
                                            "timing_narration_single_block_rescue"
                                        ),
                                    },
                                    now,
                                )
                        connection.execute(
                            "UPDATE schema_metadata SET value = ? "
                            "WHERE key = 'schema_version'",
                            (str(_SCHEMA_VERSION),),
                        )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
            finally:
                connection.close()
            self._initialized = True

    def journal_mode(self) -> str:
        connection = self._connect()
        try:
            row = connection.execute("PRAGMA journal_mode").fetchone()
            return str(row[0]).lower()
        finally:
            connection.close()

    def reserve_upload_bytes(
        self,
        upload_id: str,
        kind: str,
        *,
        reservation_id: str,
        target_bytes: int,
        safety_bytes: int,
        quota_bytes: int,
    ) -> UploadStorageUsage:
        """Atomically reserve aggregate incoming capacity across API processes."""

        if kind not in {"media", "subtitle"}:
            raise ValueError("Loại artifact tải lên không hợp lệ")
        values = (target_bytes, safety_bytes, quota_bytes)
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value < 0
            for value in values
        ):
            raise ValueError("Dung lượng reservation không hợp lệ")
        if not upload_id or not reservation_id or target_bytes + safety_bytes <= 0:
            raise ValueError("Định danh reservation không hợp lệ")
        connection = self._connect()
        now = _utc_now()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT committed_bytes, reservation_id FROM upload_storage "
                "WHERE upload_id = ? AND kind = ?",
                (upload_id, kind),
            ).fetchone()
            if row is not None and row["reservation_id"] not in {None, reservation_id}:
                raise StateError("Artifact tải lên đang có request khác ghi dữ liệu")
            committed = int(row["committed_bytes"]) if row is not None else 0
            reserved = target_bytes + safety_bytes
            allocated_elsewhere = int(
                connection.execute(
                    "SELECT COALESCE(SUM(committed_bytes + reserved_bytes), 0) "
                    "FROM upload_storage WHERE upload_id != ? OR kind != ?",
                    (upload_id, kind),
                ).fetchone()[0]
            )
            requested_total = allocated_elsewhere + committed + reserved
            if requested_total > quota_bytes:
                raise StorageQuotaExceeded(
                    f"Upload cần {requested_total} byte nhưng quota chỉ có {quota_bytes} byte"
                )
            connection.execute(
                """
                INSERT INTO upload_storage(
                    upload_id, kind, committed_bytes, reservation_id,
                    reserved_bytes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(upload_id, kind) DO UPDATE SET
                    reservation_id = excluded.reservation_id,
                    reserved_bytes = excluded.reserved_bytes,
                    updated_at = excluded.updated_at
                """,
                (upload_id, kind, committed, reservation_id, reserved, now),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.upload_storage_usage()

    def commit_upload_bytes(
        self,
        upload_id: str,
        kind: str,
        reservation_id: str,
        *,
        committed_bytes: int,
    ) -> UploadStorageUsage:
        if isinstance(committed_bytes, bool) or committed_bytes <= 0:
            raise ValueError("Dung lượng upload đã commit không hợp lệ")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE upload_storage SET committed_bytes = ?, reservation_id = NULL, "
                "reserved_bytes = 0, updated_at = ? "
                "WHERE upload_id = ? AND kind = ? AND reservation_id = ?",
                (committed_bytes, _utc_now(), upload_id, kind, reservation_id),
            ).rowcount
            if changed != 1:
                raise StateError("Reservation upload không còn hợp lệ")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.upload_storage_usage()

    def release_upload_reservation(
        self,
        upload_id: str,
        kind: str,
        reservation_id: str,
    ) -> UploadStorageUsage:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE upload_storage SET reservation_id = NULL, reserved_bytes = 0, "
                "updated_at = ? WHERE upload_id = ? AND kind = ? AND reservation_id = ?",
                (_utc_now(), upload_id, kind, reservation_id),
            )
            connection.execute(
                "DELETE FROM upload_storage WHERE upload_id = ? AND kind = ? "
                "AND committed_bytes = 0 AND reservation_id IS NULL",
                (upload_id, kind),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.upload_storage_usage()

    def reconcile_upload_storage(
        self,
        committed: Mapping[tuple[str, str], int],
    ) -> UploadStorageUsage:
        """Replace accounting with verified files and drop crash-stale reservations."""

        rows: list[tuple[str, str, int, str]] = []
        now = _utc_now()
        for (upload_id, kind), size in committed.items():
            if kind not in {"media", "subtitle"} or not upload_id:
                raise ValueError("Artifact upload reconciliation không hợp lệ")
            if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
                raise ValueError("Dung lượng upload reconciliation không hợp lệ")
            rows.append((upload_id, kind, size, now))
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM upload_storage")
            connection.executemany(
                "INSERT INTO upload_storage(upload_id, kind, committed_bytes, "
                "reservation_id, reserved_bytes, updated_at) VALUES (?, ?, ?, NULL, 0, ?)",
                rows,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.upload_storage_usage()

    def upload_storage_usage(self) -> UploadStorageUsage:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT COALESCE(SUM(committed_bytes), 0) AS committed, "
                "COALESCE(SUM(reserved_bytes), 0) AS reserved FROM upload_storage"
            ).fetchone()
        finally:
            connection.close()
        return UploadStorageUsage(
            committed_bytes=int(row["committed"]),
            reserved_bytes=int(row["reserved"]),
        )

    def create_job(
        self,
        release_id: str,
        spec: Mapping[str, Any],
        *,
        job_id: str | None = None,
    ) -> JobRecord:
        identifier = job_id or str(uuid.uuid4())
        now = _utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO jobs(
                    id, release_id, status, stage, progress_permille,
                    spec_json, details_json, active_slot, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 0, ?, '{}', 1, ?, ?)
                """,
                (
                    identifier,
                    release_id,
                    JobStatus.CREATED.value,
                    JobStage.ACQUISITION.value,
                    _encode(spec),
                    now,
                    now,
                ),
            )
            self._insert_event(
                connection,
                identifier,
                "job.created",
                {
                    "status": JobStatus.CREATED.value,
                    "stage": JobStage.ACQUISITION.value,
                },
                now,
            )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            if "jobs_one_active_idx" in str(exc):
                raise ActiveJobExists(
                    "Đã có một job nặng đang hoạt động trên GPU"
                ) from exc
            raise DuplicateJob(f"Job đã tồn tại: {identifier}") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_job(identifier)

    def create_ready_offline_job(
        self,
        release_id: str,
        spec: Mapping[str, Any],
        details: Mapping[str, Any],
        *,
        acquisition_checkpoint: Mapping[str, Any],
        subtitle_checkpoint: Mapping[str, Any],
        job_id: str | None = None,
    ) -> JobRecord:
        """Atomically publish a fully materialized local-upload job.

        A local upload has no network acquisition stage to resume.  Inserting
        the job, its artifact metadata, and both hand-off checkpoints in one
        transaction prevents a process death from exposing a transient
        ``CREATED`` job that the regular torrent resume path could mistake for
        an unfinished download.
        """

        identifier = job_id or str(uuid.uuid4())
        now = _utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO jobs(
                    id, release_id, status, stage, progress_permille,
                    spec_json, details_json, active_slot, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 250, ?, ?, 1, ?, ?)
                """,
                (
                    identifier,
                    release_id,
                    JobStatus.READY_OFFLINE.value,
                    JobStage.SUBTITLE.value,
                    _encode(spec),
                    _encode(details),
                    now,
                    now,
                ),
            )
            for stage, payload in (
                (JobStage.ACQUISITION, acquisition_checkpoint),
                (JobStage.SUBTITLE, subtitle_checkpoint),
            ):
                connection.execute(
                    """
                    INSERT INTO checkpoints(job_id, stage, payload_json, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (identifier, stage.value, _encode(payload), now),
                )
            self._insert_event(
                connection,
                identifier,
                "job.created",
                {
                    "status": JobStatus.READY_OFFLINE.value,
                    "stage": JobStage.SUBTITLE.value,
                    "source": "local_upload",
                },
                now,
            )
            for stage in (JobStage.ACQUISITION, JobStage.SUBTITLE):
                self._insert_event(
                    connection,
                    identifier,
                    "job.checkpoint",
                    {"stage": stage.value},
                    now,
                )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            if "jobs_one_active_idx" in str(exc):
                raise ActiveJobExists(
                    "Đã có một job nặng đang hoạt động trên GPU"
                ) from exc
            raise DuplicateJob(f"Job đã tồn tại: {identifier}") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_job(identifier)

    def get_job(self, job_id: str) -> JobRecord:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise JobNotFound(f"Không tìm thấy job: {job_id}")
        return self._row_to_job(row)

    def run_if_job_revision(
        self,
        job_id: str,
        expected_revision: int,
        operation: Callable[[JobRecord], bool],
    ) -> bool:
        """Run a short local operation while an unchanged job row is locked.

        ``BEGIN IMMEDIATE`` prevents resume/status writers from racing a
        filesystem cleanup between its final state check and deletion.  The
        callback must stay local and bounded; callers must never perform
        network I/O from it.
        """

        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValueError("Revision job không hợp lệ")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JobNotFound(f"Không tìm thấy job: {job_id}")
            current = self._row_to_job(row)
            if current.revision != expected_revision:
                connection.rollback()
                return False
            completed = bool(operation(current))
            connection.commit()
            return completed
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_jobs(
        self,
        statuses: Sequence[JobStatus] | None = None,
        *,
        limit: int = 100,
        newest_first: bool = False,
    ) -> list[JobRecord]:
        """List jobs in a deterministic order.

        Background coordinators keep the default oldest-first order for fair
        polling.  User-facing history can request newest-first without loading
        the entire database and reversing it in application memory.
        """

        bounded_limit = min(max(limit, 1), 1000)
        parameters: list[Any] = []
        where = ""
        if statuses:
            normalized = tuple(JobStatus(item).value for item in statuses)
            placeholders = ",".join("?" for _ in normalized)
            where = f"WHERE status IN ({placeholders})"
            parameters.extend(normalized)
        parameters.append(bounded_limit)
        order = "DESC" if newest_first else "ASC"
        connection = self._connect()
        try:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                {where}
                ORDER BY created_at {order}, id {order}
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        finally:
            connection.close()
        return [self._row_to_job(row) for row in rows]

    def set_job_pinned(self, job_id: str, pinned: bool) -> JobRecord:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JobNotFound(f"Không tìm thấy job: {job_id}")
            if JobStatus(row["status"]) is not JobStatus.COMPLETED:
                raise JobDeletionConflict("Chỉ kết quả completed mới có thể pinned")
            connection.execute(
                "UPDATE jobs SET pinned = ?, revision = revision + 1, updated_at = ? "
                "WHERE id = ?",
                (int(pinned), _utc_now(), job_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_job(job_id)

    def delete_terminal_job(
        self,
        job_id: str,
        expected_revision: int,
        operation: Callable[[JobRecord], bool] | None = None,
    ) -> bool:
        """Delete one unchanged terminal job and all cascaded rows atomically."""

        if isinstance(expected_revision, bool) or expected_revision < 0:
            raise ValueError("Revision job không hợp lệ")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JobNotFound(f"Không tìm thấy job: {job_id}")
            current = self._row_to_job(row)
            if current.revision != expected_revision:
                raise JobDeletionConflict("Job đã thay đổi sau khi kiểm tra")
            if current.retryable:
                raise JobDeletionConflict("Không xóa job retryable")
            if current.status not in {
                JobStatus.COMPLETED,
                JobStatus.CANCELLED,
                JobStatus.FAILED,
            }:
                raise JobDeletionConflict("Chỉ xóa job terminal")
            if current.pinned:
                raise JobDeletionConflict("Không xóa kết quả pinned")
            if operation is not None and not operation(current):
                connection.rollback()
                return False
            connection.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
            connection.commit()
            return True
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def list_active_jobs(self, *, limit: int = 100) -> list[JobRecord]:
        """List jobs that currently own the singleton execution slot."""

        bounded_limit = min(max(limit, 1), 1000)
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE active_slot = 1
                ORDER BY created_at ASC, id ASC
                LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
        finally:
            connection.close()
        return [self._row_to_job(row) for row in rows]

    def update_status(
        self,
        job_id: str,
        status: JobStatus,
        *,
        expected_status: JobStatus | None = None,
        stage: JobStage | None = None,
        progress_permille: int | None = None,
        details: Mapping[str, Any] | None = None,
        result: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        retryable: bool = False,
        cancel_requested: bool | None = None,
        force: bool = False,
    ) -> JobRecord:
        if progress_permille is not None and not 0 <= progress_permille <= 1000:
            raise ValueError("Tiến độ phải nằm trong khoảng 0 đến 1000")
        connection = self._connect()
        now = _utc_now()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JobNotFound(f"Không tìm thấy job: {job_id}")
            current = JobStatus(row["status"])
            if expected_status is not None and current is not expected_status:
                raise InvalidTransition(
                    "Trạng thái job đã thay đổi đồng thời từ "
                    f"{expected_status.value} sang {current.value}"
                )
            cancelling_allowed = (
                status is JobStatus.CANCELLING
                and current not in TERMINAL_STATUSES
            )
            if (
                status != current
                and not force
                and not cancelling_allowed
                and status not in _TRANSITIONS[current]
            ):
                raise InvalidTransition(
                    f"Không thể chuyển job từ {current.value} sang {status.value}"
                )

            previous_status: str | None = row["previous_status"]
            if status in {JobStatus.PAUSED, JobStatus.FAILED} and current != status:
                previous_status = current.value
            elif status not in {JobStatus.PAUSED, JobStatus.FAILED}:
                previous_status = None

            next_stage = stage.value if stage is not None else row["stage"]
            next_progress = (
                progress_permille
                if progress_permille is not None
                else row["progress_permille"]
            )
            next_details = (
                _encode(details) if details is not None else row["details_json"]
            )
            next_result = (
                _encode(result) if result is not None else row["result_json"]
            )
            next_cancel_requested = (
                int(cancel_requested)
                if cancel_requested is not None
                else row["cancel_requested"]
            )
            next_active_slot = (
                int(row["active_slot"])
                if status is JobStatus.CANCELLING
                else int(status in ACTIVE_JOB_STATUSES)
            )
            connection.execute(
                """
                UPDATE jobs SET
                    status = ?, stage = ?, progress_permille = ?,
                    details_json = ?, result_json = ?, error_code = ?,
                    error_message = ?, retryable = ?, cancel_requested = ?,
                    active_slot = ?, previous_status = ?,
                    revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    next_stage,
                    next_progress,
                    next_details,
                    next_result,
                    error_code,
                    error_message,
                    int(retryable),
                    next_cancel_requested,
                    next_active_slot,
                    previous_status,
                    now,
                    job_id,
                ),
            )
            self._insert_event(
                connection,
                job_id,
                "job.status",
                {
                    "status": status.value,
                    "stage": next_stage,
                    "progress_permille": next_progress,
                    "details": _decode(next_details),
                    "error": (
                        {
                            "code": error_code,
                            "message": error_message,
                            "retryable": retryable,
                        }
                        if error_code or error_message
                        else None
                    ),
                },
                now,
            )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            if "jobs_one_active_idx" in str(exc):
                raise ActiveJobExists(
                    "Đã có một job nặng đang hoạt động trên GPU"
                ) from exc
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_job(job_id)

    def update_progress(
        self,
        job_id: str,
        progress_permille: int,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> JobRecord:
        current = self.get_job(job_id)
        if (
            current.progress_permille == progress_permille
            and (details is None or dict(details) == current.details)
        ):
            return current
        return self.update_status(
            job_id,
            current.status,
            progress_permille=progress_permille,
            details=details,
        )

    def append_warning(
        self,
        job_id: str,
        code: str,
        message: str,
    ) -> JobRecord:
        """Persist a non-fatal warning without changing job status."""

        connection = self._connect()
        now = _utc_now()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT details_json FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JobNotFound(f"Không tìm thấy job: {job_id}")
            details = _decode(row["details_json"])
            raw_warnings = details.get("warnings", [])
            warnings = list(raw_warnings) if isinstance(raw_warnings, list) else []
            warning = {"code": code, "message": message}
            warnings.append(warning)
            details["warnings"] = warnings
            connection.execute(
                """
                UPDATE jobs SET details_json = ?, revision = revision + 1,
                    updated_at = ? WHERE id = ?
                """,
                (_encode(details), now, job_id),
            )
            self._insert_event(
                connection,
                job_id,
                "job.warning",
                warning,
                now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_job(job_id)

    def request_cancel(self, job_id: str) -> JobRecord:
        current = self.get_job(job_id)
        if current.status in TERMINAL_STATUSES:
            if current.status == JobStatus.CANCELLED:
                return current
            raise InvalidTransition("Job đã hoàn tất nên không thể hủy")
        if current.status is JobStatus.CANCELLING:
            return current
        return self.update_status(
            job_id,
            JobStatus.CANCELLING,
            cancel_requested=True,
        )

    def finalize_cancel(self, job_id: str) -> JobRecord:
        current = self.get_job(job_id)
        if current.status is JobStatus.CANCELLED:
            return current
        if current.status is not JobStatus.CANCELLING:
            raise InvalidTransition("Job chưa ở trạng thái đang hủy")
        return self.update_status(
            job_id,
            JobStatus.CANCELLED,
            cancel_requested=True,
        )

    def resume(self, job_id: str) -> JobRecord:
        current = self.get_job(job_id)
        if current.status not in {JobStatus.PAUSED, JobStatus.FAILED}:
            raise InvalidTransition("Chỉ có thể tiếp tục job đang tạm dừng hoặc lỗi")
        if current.status == JobStatus.FAILED and not current.retryable:
            raise InvalidTransition("Lỗi này không thể thử lại")
        target = current.previous_status or JobStatus.CREATED
        if target in TERMINAL_STATUSES or target in {
            JobStatus.PAUSED,
            JobStatus.FAILED,
        }:
            target = JobStatus.CREATED
        return self.update_status(
            job_id,
            target,
            cancel_requested=False,
            force=True,
            expected_status=current.status,
        )

    def save_checkpoint(
        self,
        job_id: str,
        stage: JobStage,
        payload: Mapping[str, Any],
    ) -> Checkpoint:
        self.get_job(job_id)
        now = _utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO checkpoints(job_id, stage, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id, stage) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (job_id, stage.value, _encode(payload), now),
            )
            self._insert_event(
                connection,
                job_id,
                "job.checkpoint",
                {"stage": stage.value},
                now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return Checkpoint(job_id, stage, dict(payload), now)

    def commit_transcript(
        self,
        job_id: str,
        result: TranscriptionResult,
        *,
        artifact_path: Path | str,
        artifact_sha256: str,
        expected_status: JobStatus,
        progress_permille: int = 450,
    ) -> JobRecord:
        """Atomically persist every segment, checkpoint and stage transition.

        The transcript artifact must already have been atomically published on
        disk. Repeating this method is safe: rows for the job are replaced in
        one SQLite transaction, so a retry cannot duplicate segments.
        """

        if expected_status not in {
            JobStatus.TRANSCRIBING,
            JobStatus.SUBTITLE_SELECTED,
        }:
            raise ValueError("Trạng thái nguồn transcript không hợp lệ")
        if not result.segments:
            raise ValueError("Transcript không có segment")
        if not 0 <= progress_permille <= 1000:
            raise ValueError("Tiến độ phải nằm trong khoảng 0 đến 1000")
        digest = artifact_sha256.strip().lower()
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ValueError("SHA-256 artifact transcript không hợp lệ")

        previous_end_us = 0
        for segment in result.segments:
            if segment.start_us < previous_end_us:
                raise ValueError("Timestamp transcript bị overlap hoặc không đơn điệu")
            if segment.end_us > result.duration_us:
                raise ValueError("Timestamp transcript vượt quá thời lượng media")
            previous_end_us = segment.end_us

        now = _utc_now()
        checkpoint_payload = {
            "schema_version": 1,
            "source": result.source,
            "language": result.language,
            "language_probability": result.language_probability,
            "duration_us": result.duration_us,
            "model_id": result.model_id,
            "segment_count": len(result.segments),
            "artifact_path": str(artifact_path),
            "artifact_sha256": digest,
            "completed": True,
        }
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JobNotFound(f"Không tìm thấy job: {job_id}")
            current = JobStatus(row["status"])
            if current is not expected_status:
                raise InvalidTransition(
                    "Trạng thái job đã thay đổi đồng thời từ "
                    f"{expected_status.value} sang {current.value}"
                )

            connection.execute(
                "DELETE FROM transcript_segments WHERE job_id = ?", (job_id,)
            )
            connection.executemany(
                """
                INSERT INTO transcript_segments(
                    job_id, ordinal, source, language, model_id,
                    start_us, end_us, text,
                    average_log_probability, no_speech_probability
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        job_id,
                        ordinal,
                        result.source,
                        result.language,
                        result.model_id,
                        segment.start_us,
                        segment.end_us,
                        segment.text,
                        segment.average_log_probability,
                        segment.no_speech_probability,
                    )
                    for ordinal, segment in enumerate(result.segments)
                ],
            )
            connection.execute(
                """
                INSERT INTO checkpoints(job_id, stage, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id, stage) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (job_id, JobStage.ASR.value, _encode(checkpoint_payload), now),
            )

            details = _decode(row["details_json"])
            details.update(
                {
                    "transcript_source": result.source,
                    "source_language_detected": result.language,
                    "source_language_probability": result.language_probability,
                    "source_transcript_path": str(artifact_path),
                    "source_transcript_sha256": digest,
                    "source_transcript_segment_count": len(result.segments),
                }
            )
            if result.model_id is not None:
                details["asr_model_id"] = result.model_id
            next_progress = max(int(row["progress_permille"]), progress_permille)
            connection.execute(
                """
                UPDATE jobs SET status = ?, stage = ?, progress_permille = ?,
                    details_json = ?, error_code = NULL, error_message = NULL,
                    retryable = 0, previous_status = NULL,
                    revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    JobStatus.READY_TRANSLATION.value,
                    JobStage.TRANSLATION.value,
                    next_progress,
                    _encode(details),
                    now,
                    job_id,
                ),
            )
            self._insert_event(
                connection,
                job_id,
                "job.checkpoint",
                {"stage": JobStage.ASR.value},
                now,
            )
            self._insert_event(
                connection,
                job_id,
                "job.status",
                {
                    "status": JobStatus.READY_TRANSLATION.value,
                    "stage": JobStage.TRANSLATION.value,
                    "progress_permille": next_progress,
                    "details": details,
                    "error": None,
                },
                now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_job(job_id)

    def list_transcript_segments(self, job_id: str) -> list[StoredTranscriptSegment]:
        """Return the durable transcript in deterministic timeline order."""

        self.get_job(job_id)
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM transcript_segments
                WHERE job_id = ? ORDER BY ordinal ASC
                """,
                (job_id,),
            ).fetchall()
        finally:
            connection.close()
        return [
            StoredTranscriptSegment(
                job_id=row["job_id"],
                ordinal=row["ordinal"],
                source=row["source"],
                language=row["language"],
                model_id=row["model_id"],
                start_us=row["start_us"],
                end_us=row["end_us"],
                text=row["text"],
                average_log_probability=row["average_log_probability"],
                no_speech_probability=row["no_speech_probability"],
            )
            for row in rows
        ]

    def initialize_translation_plan(
        self,
        job_id: str,
        blocks: Sequence[TranslationBlock],
        *,
        source_language: str,
        target_language: str,
        model_id: str,
        model_revision: str | None,
        model_tree_sha256: str,
        source_transcript_sha256: str,
        plan_sha256: str,
        prompt_template_id: str,
        source_token_counts: Sequence[int],
        progress_permille: int = 475,
    ) -> JobRecord:
        """Persist an immutable translation plan before the first inference call."""

        if not blocks or len(blocks) != len(source_token_counts):
            raise ValueError("Kế hoạch dịch không có block hoặc số token không khớp")
        digests = {
            "model_tree_sha256": model_tree_sha256,
            "source_transcript_sha256": source_transcript_sha256,
            "plan_sha256": plan_sha256,
        }
        for field, value in digests.items():
            normalized = value.strip().lower()
            if len(normalized) != 64 or any(
                character not in "0123456789abcdef" for character in normalized
            ):
                raise ValueError(f"{field} không phải SHA-256 hợp lệ")
            digests[field] = normalized
        if not source_language.strip() or not target_language.strip() or not model_id.strip():
            raise ValueError("Ngôn ngữ và model của kế hoạch dịch không hợp lệ")
        if not prompt_template_id.strip():
            raise ValueError("Prompt template của kế hoạch dịch không hợp lệ")
        if not 0 <= progress_permille <= 1000:
            raise ValueError("Tiến độ phải nằm trong khoảng 0 đến 1000")

        previous_end_us = 0
        for block, token_count in zip(blocks, source_token_counts, strict=True):
            if block.start_us < previous_end_us:
                raise ValueError("Timeline kế hoạch dịch bị overlap")
            if isinstance(token_count, bool) or token_count <= 0:
                raise ValueError("Số token nguồn không hợp lệ")
            previous_end_us = block.end_us

        now = _utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if row is None:
                raise JobNotFound(f"Không tìm thấy job: {job_id}")
            current = JobStatus(row["status"])
            existing_checkpoint = connection.execute(
                "SELECT payload_json FROM checkpoints WHERE job_id = ? AND stage = ?",
                (job_id, JobStage.TRANSLATION.value),
            ).fetchone()
            if current is JobStatus.TRANSLATING:
                payload = _decode(
                    existing_checkpoint["payload_json"]
                    if existing_checkpoint is not None
                    else None
                )
                existing_count = connection.execute(
                    "SELECT COUNT(*) FROM translation_blocks WHERE job_id = ?",
                    (job_id,),
                ).fetchone()[0]
                if (
                    payload.get("plan_sha256") == digests["plan_sha256"]
                    and payload.get("source_transcript_sha256")
                    == digests["source_transcript_sha256"]
                    and payload.get("model_id") == model_id
                    and existing_count == len(blocks)
                ):
                    connection.rollback()
                    return self.get_job(job_id)
                raise InvalidTransition(
                    "Job đang dịch nhưng kế hoạch checkpoint không còn khớp"
                )
            if current is not JobStatus.READY_TRANSLATION:
                raise InvalidTransition("Job không sẵn sàng khởi tạo kế hoạch dịch")

            connection.execute(
                "DELETE FROM translation_blocks WHERE job_id = ?", (job_id,)
            )
            connection.executemany(
                """
                INSERT INTO translation_blocks(
                    job_id, ordinal, start_us, end_us, source_text,
                    translated_text, source_ordinals_json, source_language,
                    target_language, model_id, plan_sha256,
                    source_token_count, output_token_count
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL)
                """,
                [
                    (
                        job_id,
                        ordinal,
                        block.start_us,
                        block.end_us,
                        block.source_text,
                        json.dumps(
                            list(block.source_ordinals),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        source_language,
                        target_language,
                        model_id,
                        digests["plan_sha256"],
                        token_count,
                    )
                    for ordinal, (block, token_count) in enumerate(
                        zip(blocks, source_token_counts, strict=True)
                    )
                ],
            )
            checkpoint_payload = {
                "schema_version": 1,
                "source_language": source_language,
                "target_language": target_language,
                "source_transcript_sha256": digests["source_transcript_sha256"],
                "model_id": model_id,
                "model_revision": model_revision,
                "model_tree_sha256": digests["model_tree_sha256"],
                "plan_sha256": digests["plan_sha256"],
                "prompt_template_id": prompt_template_id,
                "block_count": len(blocks),
                "completed_blocks": 0,
                "completed": False,
            }
            connection.execute(
                """
                INSERT INTO checkpoints(job_id, stage, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id, stage) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    job_id,
                    JobStage.TRANSLATION.value,
                    _encode(checkpoint_payload),
                    now,
                ),
            )
            details = _decode(row["details_json"])
            details.update(
                {
                    "translation_model_id": model_id,
                    "translation_plan_sha256": digests["plan_sha256"],
                    "translation_block_count": len(blocks),
                    "translation_completed_blocks": 0,
                }
            )
            next_progress = max(int(row["progress_permille"]), progress_permille)
            connection.execute(
                """
                UPDATE jobs SET status = ?, stage = ?, progress_permille = ?,
                    details_json = ?, error_code = NULL, error_message = NULL,
                    retryable = 0, previous_status = NULL,
                    revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    JobStatus.TRANSLATING.value,
                    JobStage.TRANSLATION.value,
                    next_progress,
                    _encode(details),
                    now,
                    job_id,
                ),
            )
            self._insert_event(
                connection,
                job_id,
                "translation.plan",
                {
                    "model_id": model_id,
                    "block_count": len(blocks),
                    "plan_sha256": digests["plan_sha256"],
                },
                now,
            )
            self._insert_event(
                connection,
                job_id,
                "job.status",
                {
                    "status": JobStatus.TRANSLATING.value,
                    "stage": JobStage.TRANSLATION.value,
                    "progress_permille": next_progress,
                    "details": details,
                    "error": None,
                },
                now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_job(job_id)

    def commit_translation_block(
        self,
        job_id: str,
        ordinal: int,
        translated_text: str,
        *,
        output_token_count: int,
    ) -> JobRecord:
        """Idempotently commit one completed model response and progress."""

        if isinstance(ordinal, bool) or ordinal < 0:
            raise ValueError("Chỉ số block dịch không hợp lệ")
        normalized_text = " ".join(translated_text.split())
        if not normalized_text:
            raise ValueError("Nội dung dịch không được để trống")
        if isinstance(output_token_count, bool) or output_token_count <= 0:
            raise ValueError("Số token đầu ra không hợp lệ")

        now = _utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            job_row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job_row is None:
                raise JobNotFound(f"Không tìm thấy job: {job_id}")
            if JobStatus(job_row["status"]) is not JobStatus.TRANSLATING:
                raise InvalidTransition("Job không ở trạng thái đang dịch")
            block_row = connection.execute(
                "SELECT * FROM translation_blocks WHERE job_id = ? AND ordinal = ?",
                (job_id, ordinal),
            ).fetchone()
            if block_row is None:
                raise StateError("Không tìm thấy block dịch trong kế hoạch")
            existing = block_row["translated_text"]
            if existing is not None:
                if existing != normalized_text:
                    raise StateError("Block dịch đã được commit với nội dung khác")
                connection.rollback()
                return self.get_job(job_id)
            connection.execute(
                """
                UPDATE translation_blocks
                SET translated_text = ?, output_token_count = ?
                WHERE job_id = ? AND ordinal = ? AND translated_text IS NULL
                """,
                (normalized_text, output_token_count, job_id, ordinal),
            )
            counts = connection.execute(
                """
                SELECT COUNT(*) AS total,
                    SUM(CASE WHEN translated_text IS NOT NULL THEN 1 ELSE 0 END)
                        AS completed
                FROM translation_blocks WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
            total = int(counts["total"])
            completed = int(counts["completed"] or 0)
            progress = max(
                int(job_row["progress_permille"]),
                475 + round(175 * completed / max(total, 1)),
            )
            checkpoint_row = connection.execute(
                "SELECT payload_json FROM checkpoints WHERE job_id = ? AND stage = ?",
                (job_id, JobStage.TRANSLATION.value),
            ).fetchone()
            checkpoint_payload = _decode(
                checkpoint_row["payload_json"] if checkpoint_row is not None else None
            )
            checkpoint_payload.update(
                {"block_count": total, "completed_blocks": completed}
            )
            connection.execute(
                """
                INSERT INTO checkpoints(job_id, stage, payload_json, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(job_id, stage) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (
                    job_id,
                    JobStage.TRANSLATION.value,
                    _encode(checkpoint_payload),
                    now,
                ),
            )
            details = _decode(job_row["details_json"])
            details["translation_completed_blocks"] = completed
            details["translation_block_count"] = total
            connection.execute(
                """
                UPDATE jobs SET progress_permille = ?, details_json = ?,
                    revision = revision + 1, updated_at = ? WHERE id = ?
                """,
                (progress, _encode(details), now, job_id),
            )
            self._insert_event(
                connection,
                job_id,
                "translation.block",
                {
                    "ordinal": ordinal,
                    "completed_blocks": completed,
                    "block_count": total,
                    "progress_permille": progress,
                },
                now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_job(job_id)

    def list_translation_blocks(self, job_id: str) -> list[StoredTranslationBlock]:
        self.get_job(job_id)
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM translation_blocks
                WHERE job_id = ? ORDER BY ordinal ASC
                """,
                (job_id,),
            ).fetchall()
        finally:
            connection.close()
        result: list[StoredTranslationBlock] = []
        for row in rows:
            try:
                source_ordinals_raw = json.loads(row["source_ordinals_json"])
                source_ordinals = tuple(int(value) for value in source_ordinals_raw)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise StateError("Danh sách segment nguồn của block dịch bị hỏng") from exc
            result.append(
                StoredTranslationBlock(
                    job_id=row["job_id"],
                    ordinal=row["ordinal"],
                    start_us=row["start_us"],
                    end_us=row["end_us"],
                    source_text=row["source_text"],
                    translated_text=row["translated_text"],
                    source_ordinals=source_ordinals,
                    source_language=row["source_language"],
                    target_language=row["target_language"],
                    model_id=row["model_id"],
                    plan_sha256=row["plan_sha256"],
                    source_token_count=row["source_token_count"],
                    output_token_count=row["output_token_count"],
                )
            )
        return result

    def commit_translation_artifact(
        self,
        job_id: str,
        result: TranslationResult,
        *,
        artifact_path: Path | str,
        artifact_sha256: str,
        progress_permille: int = 650,
    ) -> JobRecord:
        """Atomically seal Phase 3 and make the job ready for Vietnamese TTS."""

        digest = artifact_sha256.strip().lower()
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError("SHA-256 artifact dịch không hợp lệ")
        if not 0 <= progress_permille <= 1000:
            raise ValueError("Tiến độ phải nằm trong khoảng 0 đến 1000")

        now = _utc_now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            job_row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if job_row is None:
                raise JobNotFound(f"Không tìm thấy job: {job_id}")
            if JobStatus(job_row["status"]) is not JobStatus.TRANSLATING:
                raise InvalidTransition("Job không ở trạng thái đang dịch")
            rows = connection.execute(
                "SELECT * FROM translation_blocks WHERE job_id = ? ORDER BY ordinal",
                (job_id,),
            ).fetchall()
            if not rows or any(row["translated_text"] is None for row in rows):
                raise StateError("Chưa hoàn tất tất cả block dịch")
            if len(rows) != len(result.segments):
                raise StateError("Artifact dịch không khớp số block checkpoint")
            for row, segment in zip(rows, result.segments, strict=True):
                if (
                    row["start_us"] != segment.start_us
                    or row["end_us"] != segment.end_us
                    or row["source_text"] != segment.source_text
                    or row["translated_text"] != segment.translated_text
                    or row["model_id"] != result.model_id
                ):
                    raise StateError("Artifact dịch không khớp block checkpoint")
            checkpoint_row = connection.execute(
                "SELECT payload_json FROM checkpoints WHERE job_id = ? AND stage = ?",
                (job_id, JobStage.TRANSLATION.value),
            ).fetchone()
            checkpoint_payload = _decode(
                checkpoint_row["payload_json"] if checkpoint_row is not None else None
            )
            if (
                checkpoint_payload.get("source_transcript_sha256")
                != result.source_transcript_sha256
                or checkpoint_payload.get("model_id") != result.model_id
            ):
                raise StateError("Artifact dịch không thuộc checkpoint hiện tại")
            checkpoint_payload.update(
                {
                    "completed": True,
                    "completed_blocks": len(rows),
                    "artifact_path": str(artifact_path),
                    "artifact_sha256": digest,
                }
            )
            connection.execute(
                """
                UPDATE checkpoints SET payload_json = ?, updated_at = ?
                WHERE job_id = ? AND stage = ?
                """,
                (
                    _encode(checkpoint_payload),
                    now,
                    job_id,
                    JobStage.TRANSLATION.value,
                ),
            )
            details = _decode(job_row["details_json"])
            details.update(
                {
                    "translation_model_id": result.model_id,
                    "target_language": result.target_language,
                    "translated_transcript_path": str(artifact_path),
                    "translated_transcript_sha256": digest,
                    "translation_block_count": len(rows),
                    "translation_completed_blocks": len(rows),
                }
            )
            next_progress = max(int(job_row["progress_permille"]), progress_permille)
            connection.execute(
                """
                UPDATE jobs SET status = ?, stage = ?, progress_permille = ?,
                    details_json = ?, error_code = NULL, error_message = NULL,
                    retryable = 0, previous_status = NULL,
                    revision = revision + 1, updated_at = ? WHERE id = ?
                """,
                (
                    JobStatus.READY_TTS.value,
                    JobStage.TTS.value,
                    next_progress,
                    _encode(details),
                    now,
                    job_id,
                ),
            )
            self._insert_event(
                connection,
                job_id,
                "job.checkpoint",
                {"stage": JobStage.TRANSLATION.value},
                now,
            )
            self._insert_event(
                connection,
                job_id,
                "job.status",
                {
                    "status": JobStatus.READY_TTS.value,
                    "stage": JobStage.TTS.value,
                    "progress_permille": next_progress,
                    "details": details,
                    "error": None,
                },
                now,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_job(job_id)

    def get_checkpoint(self, job_id: str, stage: JobStage) -> Checkpoint | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM checkpoints WHERE job_id = ? AND stage = ?",
                (job_id, stage.value),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return Checkpoint(
            job_id=row["job_id"],
            stage=JobStage(row["stage"]),
            payload=_decode(row["payload_json"]),
            updated_at=row["updated_at"],
        )

    def list_events(
        self,
        job_id: str,
        *,
        after_id: int = 0,
        limit: int = 100,
    ) -> list[JobEvent]:
        self.get_job(job_id)
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM job_events
                WHERE job_id = ? AND id > ?
                ORDER BY id ASC LIMIT ?
                """,
                (job_id, max(after_id, 0), min(max(limit, 1), 1000)),
            ).fetchall()
        finally:
            connection.close()
        return [
            JobEvent(
                id=row["id"],
                job_id=row["job_id"],
                event_type=row["event_type"],
                payload=_decode(row["payload_json"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]

    def prune_terminal_events(self, before: str) -> int:
        """Delete old events only for non-retryable terminal jobs."""

        try:
            datetime.fromisoformat(before.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Timestamp event retention không hợp lệ") from exc
        connection = self._connect()
        terminal = (
            JobStatus.COMPLETED.value,
            JobStatus.CANCELLED.value,
            JobStatus.FAILED.value,
        )
        try:
            connection.execute("BEGIN IMMEDIATE")
            removed = connection.execute(
                "DELETE FROM job_events WHERE created_at < ? AND job_id IN ("
                "SELECT id FROM jobs WHERE status IN (?, ?, ?) AND retryable = 0)",
                (before, *terminal),
            ).rowcount
            connection.commit()
            return int(removed)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _insert_event(
        connection: sqlite3.Connection,
        job_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO job_events(job_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (job_id, event_type, _encode(payload), created_at),
        )

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=row["id"],
            release_id=row["release_id"],
            status=JobStatus(row["status"]),
            stage=JobStage(row["stage"]),
            progress_permille=row["progress_permille"],
            spec=_decode(row["spec_json"]),
            details=_decode(row["details_json"]),
            result=_decode(row["result_json"]) if row["result_json"] else None,
            error_code=row["error_code"],
            error_message=row["error_message"],
            retryable=bool(row["retryable"]),
            cancel_requested=bool(row["cancel_requested"]),
            active_slot=bool(row["active_slot"]),
            pinned=bool(row["pinned"]),
            previous_status=(
                JobStatus(row["previous_status"])
                if row["previous_status"]
                else None
            ),
            revision=row["revision"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
