"""Conservative retention cleanup for terminal job artifacts.

Only paths derived from a validated job identifier under the configured jobs
and output roots can enter a cleanup plan.  The default execution mode is a
dry run, and source downloads under ``incoming_dir`` are deliberately outside
the scope of this module.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .state import JobNotFound, JobRecord, JobStatus, StateStore

_SAFE_JOB_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ArtifactCleanupError(RuntimeError):
    """The cleanup request violates a configured-root safety invariant."""


@dataclass(frozen=True, slots=True)
class CleanupAction:
    job_id: str
    status: str
    job_revision: int
    job_updated_at: str
    reason: str
    root: Path
    path: Path
    size_bytes: int

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["root"] = str(self.root)
        result["path"] = str(self.path)
        return result


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    generated_at: str
    retry_retention_days: int
    completed_retention_days: int
    actions: tuple[CleanupAction, ...]
    skipped: tuple[str, ...]

    @property
    def total_size_bytes(self) -> int:
        return sum(action.size_bytes for action in self.actions)

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": self.generated_at,
            "retry_retention_days": self.retry_retention_days,
            "completed_retention_days": self.completed_retention_days,
            "actions": [action.to_dict() for action in self.actions],
            "action_count": len(self.actions),
            "total_size_bytes": self.total_size_bytes,
            "skipped": list(self.skipped),
        }


@dataclass(frozen=True, slots=True)
class CleanupResult:
    dry_run: bool
    planned: int
    removed: tuple[CleanupAction, ...]
    missing: tuple[CleanupAction, ...]
    errors: tuple[str, ...]
    reclaimed_bytes: int

    def to_dict(self) -> dict[str, object]:
        return {
            "dry_run": self.dry_run,
            "planned": self.planned,
            "removed": [action.to_dict() for action in self.removed],
            "missing": [action.to_dict() for action in self.missing],
            "errors": list(self.errors),
            "reclaimed_bytes": self.reclaimed_bytes,
        }


def plan_artifact_cleanup(
    jobs: Iterable[JobRecord],
    *,
    jobs_root: Path,
    output_root: Path,
    retry_retention_days: int = 7,
    completed_retention_days: int = 30,
    now: datetime | None = None,
) -> CleanupPlan:
    """Plan cancelled and expired retryable-failure cleanup.

    Cancelled jobs are eligible immediately. Retryable failures retain their
    checkpoints for ``retry_retention_days``. Completed jobs, active jobs,
    non-retryable failures, and fresh retryable failures are never selected.
    """

    if retry_retention_days < 0 or completed_retention_days < 0:
        raise ValueError("Thời gian giữ artifact retry không được âm")
    jobs_base = _safe_root(jobs_root, label="jobs")
    output_base = _safe_root(output_root, label="output")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    else:
        current = current.astimezone(UTC)
    cutoff = current - timedelta(days=retry_retention_days)
    completed_cutoff = current - timedelta(days=completed_retention_days)
    actions: list[CleanupAction] = []
    skipped: list[str] = []
    seen_paths: set[Path] = set()

    for job in jobs:
        reason = _eligible_reason(
            job,
            cutoff=cutoff,
            completed_cutoff=completed_cutoff,
        )
        if reason is None:
            continue
        if not _SAFE_JOB_ID.fullmatch(job.id) or job.id in {".", ".."}:
            skipped.append(f"Bỏ qua job có ID không an toàn: {job.id!r}")
            continue
        status_value = JobStatus(job.status).value
        candidates = [
            (jobs_base, jobs_base / job.id),
            (output_base, output_base / f"{job.id}.mp4"),
            (output_base, output_base / f"{job.id}.vi.srt"),
            (output_base, output_base / f".{job.id}.part.mp4"),
        ]
        if output_base.exists():
            candidates.extend(
                (output_base, candidate)
                for candidate in output_base.glob(f".{job.id}.vi.srt.*.part")
            )
        for root, candidate in candidates:
            path = _lexically_beneath(candidate, root)
            if path in seen_paths or not _path_exists(path):
                continue
            seen_paths.add(path)
            actions.append(
                CleanupAction(
                    job_id=job.id,
                    status=status_value,
                    job_revision=job.revision,
                    job_updated_at=job.updated_at,
                    reason=reason,
                    root=root,
                    path=path,
                    size_bytes=_path_size(path),
                )
            )

    actions.sort(key=lambda item: (item.job_id, str(item.path)))
    return CleanupPlan(
        generated_at=current.isoformat(timespec="seconds"),
        retry_retention_days=retry_retention_days,
        completed_retention_days=completed_retention_days,
        actions=tuple(actions),
        skipped=tuple(skipped),
    )


def execute_cleanup(
    plan: CleanupPlan,
    *,
    apply: bool = False,
    state_store: StateStore | None = None,
) -> CleanupResult:
    """Execute a previously built plan; ``apply=False`` is the safe default."""

    if not apply:
        return CleanupResult(
            dry_run=True,
            planned=len(plan.actions),
            removed=(),
            missing=(),
            errors=(),
            reclaimed_bytes=0,
        )

    removed: list[CleanupAction] = []
    missing: list[CleanupAction] = []
    errors: list[str] = []
    reclaimed = 0
    actions_by_job: dict[str, list[CleanupAction]] = {}
    for action in plan.actions:
        actions_by_job.setdefault(action.job_id, []).append(action)
    cutoff = _parse_timestamp(plan.generated_at) - timedelta(
        days=plan.retry_retention_days
    )
    completed_cutoff = _parse_timestamp(plan.generated_at) - timedelta(
        days=plan.completed_retention_days
    )

    for job_id, actions in actions_by_job.items():
        if state_store is None:
            errors.append(
                f"Từ chối xóa artifact job {job_id}: thiếu khóa trạng thái hiện thời"
            )
            continue
        expected_revision = actions[0].job_revision

        def remove_if_still_eligible(
            current: JobRecord,
            *,
            actions: tuple[CleanupAction, ...] = tuple(actions),
            expected_revision: int = expected_revision,
        ) -> bool:
            nonlocal reclaimed
            if any(
                action.job_revision != expected_revision
                or action.job_updated_at != current.updated_at
                or action.status != current.status.value
                or action.reason
                != _eligible_reason(
                    current,
                    cutoff=cutoff,
                    completed_cutoff=completed_cutoff,
                )
                for action in actions
            ):
                return False
            for action in actions:
                try:
                    root = _safe_root(action.root, label="action")
                    path = _lexically_beneath(action.path, root)
                    if not _path_exists(path):
                        missing.append(action)
                        continue
                    _remove_without_following_root_escape(path, root)
                except OSError as exc:
                    errors.append(f"Không thể xóa {action.path}: {exc}")
                    continue
                except ArtifactCleanupError as exc:
                    errors.append(str(exc))
                    continue
                removed.append(action)
                reclaimed += action.size_bytes
            return True

        try:
            executed = state_store.run_if_job_revision(
                job_id,
                expected_revision,
                remove_if_still_eligible,
            )
        except JobNotFound:
            executed = False
        if not executed:
            errors.append(
                f"Bỏ qua cleanup job {job_id}: trạng thái đã thay đổi sau khi lập kế hoạch"
            )
    return CleanupResult(
        dry_run=False,
        planned=len(plan.actions),
        removed=tuple(removed),
        missing=tuple(missing),
        errors=tuple(errors),
        reclaimed_bytes=reclaimed,
    )


def _eligible_reason(
    job: JobRecord,
    *,
    cutoff: datetime,
    completed_cutoff: datetime,
) -> str | None:
    status = JobStatus(job.status)
    if status is JobStatus.COMPLETED:
        if job.pinned:
            return None
        updated = _parse_timestamp(job.updated_at)
        return (
            "completed-retention-expired"
            if updated <= completed_cutoff
            else None
        )
    if status is JobStatus.CANCELLED:
        return "cancelled"
    if status is not JobStatus.FAILED or not job.retryable:
        return None
    updated = _parse_timestamp(job.updated_at)
    return "retry-retention-expired" if updated <= cutoff else None


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArtifactCleanupError(
            f"Timestamp job không hợp lệ, từ chối cleanup: {value!r}"
        ) from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_root(path: Path, *, label: str) -> Path:
    root = Path(path).resolve(strict=False)
    if root == Path(root.anchor):
        raise ArtifactCleanupError(
            f"Từ chối dùng filesystem root làm {label} cleanup root: {root}"
        )
    return root


def _lexically_beneath(path: Path, root: Path) -> Path:
    candidate = Path(path).absolute()
    base = Path(root).absolute()
    if candidate == base or not candidate.is_relative_to(base):
        raise ArtifactCleanupError(
            f"Từ chối cleanup đường dẫn ngoài configured root: {candidate}"
        )
    return candidate


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _path_size(path: Path) -> int:
    try:
        status = path.lstat()
    except OSError:
        return 0
    if not stat.S_ISDIR(status.st_mode) or stat.S_ISLNK(status.st_mode):
        return status.st_size
    total = 0
    for directory, subdirectories, filenames in os.walk(path, followlinks=False):
        for name in [*subdirectories, *filenames]:
            candidate = Path(directory) / name
            try:
                candidate_status = candidate.lstat()
            except OSError:
                continue
            if not stat.S_ISDIR(candidate_status.st_mode) or stat.S_ISLNK(
                candidate_status.st_mode
            ):
                total += candidate_status.st_size
    return total


def _remove_without_following_root_escape(path: Path, root: Path) -> None:
    # A symlink itself may live under the configured root; unlinking it is safe
    # and must never recurse into its target.
    status = path.lstat()
    if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
        path.unlink()
        return
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise ArtifactCleanupError(
            f"Từ chối cleanup thư mục trỏ ra ngoài configured root: {path}"
        )
    shutil.rmtree(path)


def remove_terminal_job_artifacts(
    job_id: str,
    *,
    incoming_root: Path,
    jobs_root: Path,
    output_root: Path,
) -> int:
    """Remove deterministic job artifacts only after a no-symlink preflight."""

    if not _SAFE_JOB_ID.fullmatch(job_id) or job_id in {".", ".."}:
        raise ArtifactCleanupError(f"ID job không an toàn: {job_id!r}")
    incoming_base = _safe_root(incoming_root, label="incoming")
    jobs_base = _safe_root(jobs_root, label="jobs")
    output_base = _safe_root(output_root, label="output")
    candidates = (
        (incoming_base, incoming_base / job_id),
        (jobs_base, jobs_base / job_id),
        (output_base, output_base / f"{job_id}.mp4"),
        (output_base, output_base / f"{job_id}.vi.srt"),
        (output_base, output_base / f".{job_id}.part.mp4"),
    )
    present: list[tuple[Path, Path, int]] = []
    for root, raw_path in candidates:
        path = _lexically_beneath(raw_path, root)
        if not _path_exists(path):
            continue
        _verify_no_symlinks(path, root)
        present.append((root, path, _path_size(path)))
    reclaimed = 0
    for root, path, size in present:
        _verify_no_symlinks(path, root)
        status = path.lstat()
        if stat.S_ISDIR(status.st_mode):
            shutil.rmtree(path)
        else:
            path.unlink()
        reclaimed += size
    return reclaimed


def _verify_no_symlinks(path: Path, root: Path) -> None:
    if root.is_symlink():
        raise ArtifactCleanupError(f"Cleanup root không được là symlink: {root}")
    status = path.lstat()
    if stat.S_ISLNK(status.st_mode):
        raise ArtifactCleanupError(f"Artifact job không được là symlink: {path}")
    if not path.resolve(strict=True).is_relative_to(root.resolve(strict=True)):
        raise ArtifactCleanupError(f"Artifact job thoát configured root: {path}")
    if not stat.S_ISDIR(status.st_mode):
        return
    for directory, subdirectories, filenames in os.walk(path, followlinks=False):
        for name in [*subdirectories, *filenames]:
            candidate = Path(directory) / name
            if stat.S_ISLNK(candidate.lstat().st_mode):
                raise ArtifactCleanupError(
                    f"Artifact job chứa symlink, từ chối xóa: {candidate}"
                )
