"""Checkpointed hand-off from network acquisition to the offline worker."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from pathlib import Path, PurePosixPath
from typing import Any

from dub_server.domain import (
    AcquisitionError,
    AcquisitionErrorCode,
    DownloadedFile,
    DownloadState,
    MediaAsset,
    MediaKind,
    SubtitleCandidate,
)
from dub_server.media_probe import FfprobeMediaProbe, MediaProbe, MediaProbeError
from dub_server.state import JobRecord, JobStage, JobStatus, StateStore

from .service import AcquisitionService

VIDEO_EXTENSIONS = frozenset(
    {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi", ".ts", ".m2ts"}
)


class CoordinatorError(RuntimeError):
    def __init__(self, code: str, message_vi: str, *, retryable: bool) -> None:
        super().__init__(message_vi)
        self.code = code
        self.message_vi = message_vi
        self.retryable = retryable


class AcquisitionCoordinator:
    def __init__(
        self,
        service: AcquisitionService,
        store: StateStore,
        incoming_dir: Path,
        jobs_dir: Path,
        media_probe: MediaProbe | None = None,
    ) -> None:
        self._service = service
        self._store = store
        self._incoming_dir = incoming_dir.resolve(strict=False)
        self._jobs_dir = jobs_dir.resolve(strict=False)
        self._media_probe = media_probe or FfprobeMediaProbe()
        self._refresh_locks: dict[str, asyncio.Lock] = {}

    async def refresh(self, job_id: str) -> JobRecord:
        """Advance one acquisition job by one polling cycle."""

        lock = self._refresh_locks.setdefault(job_id, asyncio.Lock())
        async with lock:
            return await self._refresh_locked(job_id)

    async def select_subtitle(self, job_id: str, subtitle_id: str) -> JobRecord:
        """Materialize a candidate selected from a manual matching result."""

        lock = self._refresh_locks.setdefault(job_id, asyncio.Lock())
        async with lock:
            job = self._store.get_job(job_id)
            if job.status is not JobStatus.NEEDS_SUBTITLE_SELECTION:
                raise AcquisitionError(
                    AcquisitionErrorCode.SUBTITLE_INVALID,
                    "Job không đang chờ chọn phụ đề",
                    retryable=False,
                )
            advertised = job.details.get("subtitle_candidates")
            allowed_ids = (
                {
                    item.get("subtitle_id")
                    for item in advertised
                    if isinstance(item, dict)
                    and isinstance(item.get("subtitle_id"), str)
                }
                if isinstance(advertised, list)
                else set()
            )
            if subtitle_id not in allowed_ids:
                raise AcquisitionError(
                    AcquisitionErrorCode.SUBTITLE_INVALID,
                    "Phụ đề không thuộc kết quả đã tìm cho job này",
                    retryable=False,
                )

            media = self._media_from_details(job)
            candidates = await self._service.find_subtitles(media)
            current = self._store.get_job(job.id)
            if current.status in {JobStatus.CANCELLING, JobStatus.CANCELLED}:
                return current
            selected = next(
                (candidate for candidate in candidates if candidate.subtitle_id == subtitle_id),
                None,
            )
            if selected is None:
                raise AcquisitionError(
                    AcquisitionErrorCode.SUBTITLE_UNAVAILABLE,
                    "Phụ đề đã chọn không còn khả dụng",
                    retryable=True,
                )
            destination = (
                self._jobs_dir
                / job.id
                / f"source-subtitle.{selected.format.value}"
            )
            materialized = await self._service.materialize_subtitle(
                media, selected, destination
            )
            current = self._store.get_job(job.id)
            if current.status in {JobStatus.CANCELLING, JobStatus.CANCELLED}:
                return current
            subtitle_path = self._safe_materialized_subtitle(job.id, materialized)
            selected_public = _public_subtitle(selected)
            details = {
                **job.details,
                "selected_subtitle": selected_public,
                "source_subtitle_path": str(subtitle_path),
                "transcript_source": "subtitle",
            }
            self._store.save_checkpoint(
                job.id,
                JobStage.SUBTITLE,
                {
                    "mode": "manual",
                    "candidates": advertised,
                    "selected": selected_public,
                    "source_subtitle_path": str(subtitle_path),
                },
            )
            return self._store.update_status(
                job.id,
                JobStatus.READY_OFFLINE,
                stage=JobStage.SUBTITLE,
                progress_permille=250,
                details=details,
            )

    async def select_asr(self, job_id: str) -> JobRecord:
        """Continue a manual subtitle decision with local ASR instead."""

        lock = self._refresh_locks.setdefault(job_id, asyncio.Lock())
        async with lock:
            job = self._store.get_job(job_id)
            if job.status is not JobStatus.NEEDS_SUBTITLE_SELECTION:
                raise AcquisitionError(
                    AcquisitionErrorCode.SUBTITLE_INVALID,
                    "Job không đang chờ chọn phụ đề",
                    retryable=False,
                )
            advertised = job.details.get("subtitle_candidates")
            details = {
                **job.details,
                "selected_subtitle": None,
                "transcript_source": "asr",
                "subtitle_fallback_reason": "user_selected_asr",
            }
            self._store.save_checkpoint(
                job.id,
                JobStage.SUBTITLE,
                {
                    "mode": "manual",
                    "candidates": advertised if isinstance(advertised, list) else [],
                    "selected": None,
                    "transcript_source": "asr",
                },
            )
            return self._store.update_status(
                job.id,
                JobStatus.READY_OFFLINE,
                stage=JobStage.SUBTITLE,
                progress_permille=250,
                details=details,
            )

    async def _refresh_locked(self, job_id: str) -> JobRecord:
        job = self._store.get_job(job_id)
        try:
            if job.status is JobStatus.DOWNLOADING:
                return await self._refresh_download(job)
            if job.status is JobStatus.SUBTITLE_MATCHING:
                media = self._media_from_details(job)
                return await self._match_subtitles(job, media)
            return job
        except AcquisitionError as error:
            return self._fail(
                job_id,
                code=error.code.value,
                message_vi=error.message_vi,
                retryable=error.retryable,
            )
        except MediaProbeError as error:
            return self._fail(
                job_id,
                code=error.code,
                message_vi=error.message_vi,
                retryable=error.retryable,
            )
        except CoordinatorError as error:
            return self._fail(
                job_id,
                code=error.code,
                message_vi=error.message_vi,
                retryable=error.retryable,
            )
        except Exception:
            return self._fail(
                job_id,
                code="acquisition_refresh_failed",
                message_vi="Không thể cập nhật tiến trình tải nguồn",
                retryable=True,
            )

    async def _refresh_download(self, job: JobRecord) -> JobRecord:
        if job.details.get("backend_started") is False:
            # The API has persisted a paused qBittorrent task but has not yet
            # received the explicit start acknowledgement. Polling it as a
            # regular download here could incorrectly turn the job PAUSED.
            return self._store.get_job(job.id)
        task_id = job.details.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise CoordinatorError(
                "download_task_missing",
                "Job không có mã tác vụ tải nguồn",
                retryable=False,
            )
        status = await self._service.download_status(task_id)
        current = self._store.get_job(job.id)
        if current.status in {JobStatus.CANCELLING, JobStatus.CANCELLED}:
            return current
        details = {
            **job.details,
            "download_state": status.state.value,
            "downloaded_bytes": max(status.downloaded_bytes, 0),
            "total_bytes": max(status.total_bytes, 0),
            "speed_bytes_per_second": max(status.speed_bytes_per_second, 0),
            "eta_seconds": status.eta_seconds,
            "download_progress": min(1.0, max(0.0, status.progress)),
        }
        details["stage_progress_permille"] = round(details["download_progress"] * 1000)
        progress = round(details["download_progress"] * 200)
        checkpoint_payload = {
            "task_id": task_id,
            **_download_checkpoint(details),
        }
        existing_checkpoint = self._store.get_checkpoint(
            job.id,
            JobStage.ACQUISITION,
        )
        if (
            existing_checkpoint is None
            or existing_checkpoint.payload != checkpoint_payload
        ):
            self._store.save_checkpoint(
                job.id,
                JobStage.ACQUISITION,
                checkpoint_payload,
            )
        updated = self._store.update_progress(job.id, progress, details=details)

        if status.state is DownloadState.FAILED:
            raise CoordinatorError(
                "download_failed",
                status.error_message or "Trình tải nguồn báo lỗi",
                retryable=True,
            )
        if status.state is DownloadState.PAUSED:
            return self._store.update_status(
                updated.id,
                JobStatus.PAUSED,
                stage=JobStage.ACQUISITION,
                progress_permille=progress,
                details=details,
            )
        if status.state is not DownloadState.COMPLETED:
            return updated

        files = await self._service.download_files(task_id)
        downloaded_file, media_path = self._select_media(job.id, files)
        source_language = _required_text(job.spec.get("source_language"), default="auto")
        media_kind = _media_kind(job.spec.get("media_type"))
        media = await self._media_probe.probe(
            media_path,
            source_language=source_language,
            title=(
                _optional_text(job.spec.get("search_query"))
                or _optional_text(job.details.get("name"))
            ),
            media_kind=media_kind,
            year=_optional_year(job.spec.get("year")),
            require_h264_passthrough=True,
            allow_hevc_transcode=True,
        )
        current = self._store.get_job(job.id)
        if current.status in {JobStatus.CANCELLING, JobStatus.CANCELLED}:
            return current
        media_details = _media_details(media, downloaded_file.relative_path, downloaded_file.size_bytes)
        details.update(
            {
                "download_progress": 1.0,
                "selected_media": media_details,
                "source_media_path": str(media.path),
            }
        )
        self._store.save_checkpoint(
            job.id,
            JobStage.ACQUISITION,
            {
                "task_id": task_id,
                **_download_checkpoint(details),
                "selected_media": media_details,
                "source_media_path": str(media.path),
            },
        )
        matching = self._store.update_status(
            job.id,
            JobStatus.SUBTITLE_MATCHING,
            stage=JobStage.SUBTITLE,
            progress_permille=220,
            details=details,
        )
        return await self._match_subtitles(matching, media)

    async def _match_subtitles(self, job: JobRecord, media: MediaAsset) -> JobRecord:
        mode = _required_text(job.spec.get("subtitle_mode"), default="prefer")
        if mode not in {"prefer", "manual", "asr"}:
            raise CoordinatorError(
                "invalid_job_spec",
                "Chế độ phụ đề của job không hợp lệ",
                retryable=False,
            )
        details = dict(job.details)
        if mode == "asr":
            details.update(
                {
                    "subtitle_candidates": [],
                    "transcript_source": "asr",
                    "subtitle_fallback_reason": "asr_requested",
                }
            )
            return self._ready_offline(job, details)

        if _subtitle_language_unknown(media.source_language):
            details.update(
                {
                    "subtitle_candidates": [],
                    "transcript_source": "asr",
                    "subtitle_fallback_reason": "source_language_unknown",
                }
            )
            return self._ready_offline(job, details)

        try:
            candidates = await self._service.find_subtitles(media)
        except AcquisitionError as error:
            current = self._store.get_job(job.id)
            if current.status in {JobStatus.CANCELLING, JobStatus.CANCELLED}:
                return current
            if mode != "prefer":
                raise
            details.update(
                {
                    "subtitle_candidates": [],
                    "transcript_source": "asr",
                    "subtitle_fallback_reason": "subtitle_search_failed",
                    "subtitle_warnings": [
                        {
                            "code": error.code.value,
                            "message": error.message_vi,
                            "retryable": error.retryable,
                        }
                    ],
                }
            )
            return self._ready_offline(job, details)
        current = self._store.get_job(job.id)
        if current.status in {JobStatus.CANCELLING, JobStatus.CANCELLED}:
            return current
        public_candidates = [_public_subtitle(candidate) for candidate in candidates]
        details["subtitle_candidates"] = public_candidates
        self._store.save_checkpoint(
            job.id,
            JobStage.SUBTITLE,
            {"mode": mode, "candidates": public_candidates},
        )

        if mode == "manual" and candidates:
            details["transcript_source"] = "pending_manual_subtitle"
            return self._store.update_status(
                job.id,
                JobStatus.NEEDS_SUBTITLE_SELECTION,
                stage=JobStage.SUBTITLE,
                progress_permille=250,
                details=details,
            )

        high_confidence = [candidate for candidate in candidates if candidate.high_confidence]
        if mode == "prefer" and high_confidence:
            materialization_warnings: list[dict[str, Any]] = []
            ordered = sorted(
                high_confidence,
                key=lambda candidate: (candidate.score, candidate.subtitle_id),
                reverse=True,
            )
            for selected in ordered:
                destination = self._jobs_dir / job.id / f"source-subtitle.{selected.format.value}"
                try:
                    materialized = await self._service.materialize_subtitle(
                        media, selected, destination
                    )
                    current = self._store.get_job(job.id)
                    if current.status in {
                        JobStatus.CANCELLING,
                        JobStatus.CANCELLED,
                    }:
                        return current
                    subtitle_path = self._safe_materialized_subtitle(job.id, materialized)
                except (AcquisitionError, CoordinatorError) as error:
                    code = error.code.value if isinstance(error, AcquisitionError) else error.code
                    message = error.message_vi
                    materialization_warnings.append(
                        {
                            "code": code,
                            "message": message,
                            "retryable": error.retryable,
                            "subtitle_id": selected.subtitle_id,
                        }
                    )
                    continue
                selected_public = _public_subtitle(selected)
                details.update(
                    {
                        "selected_subtitle": selected_public,
                        "source_subtitle_path": str(subtitle_path),
                        "transcript_source": "subtitle",
                    }
                )
                if materialization_warnings:
                    details["subtitle_warnings"] = materialization_warnings
                self._store.save_checkpoint(
                    job.id,
                    JobStage.SUBTITLE,
                    {
                        "mode": mode,
                        "candidates": public_candidates,
                        "selected": selected_public,
                        "source_subtitle_path": str(subtitle_path),
                        "warnings": materialization_warnings,
                    },
                )
                return self._ready_offline(job, details)
            details["subtitle_warnings"] = materialization_warnings
            details["subtitle_fallback_reason"] = "subtitle_materialization_failed"

        details.update(
            {
                "transcript_source": "asr",
                "subtitle_fallback_reason": details.get(
                    "subtitle_fallback_reason", "no_high_confidence_subtitle"
                ),
            }
        )
        return self._ready_offline(job, details)

    def _ready_offline(self, job: JobRecord, details: dict[str, Any]) -> JobRecord:
        self._store.save_checkpoint(
            job.id,
            JobStage.SUBTITLE,
            {
                "mode": job.spec.get("subtitle_mode", "prefer"),
                "transcript_source": details["transcript_source"],
                "selected_subtitle": details.get("selected_subtitle"),
                "source_subtitle_path": details.get("source_subtitle_path"),
                "fallback_reason": details.get("subtitle_fallback_reason"),
                "candidates": details.get("subtitle_candidates", []),
            },
        )
        return self._store.update_status(
            job.id,
            JobStatus.READY_OFFLINE,
            stage=JobStage.SUBTITLE,
            progress_permille=250,
            details=details,
        )

    def _select_media(
        self,
        job_id: str,
        files: tuple[DownloadedFile, ...],
    ) -> tuple[DownloadedFile, Path]:
        root = (self._incoming_dir / job_id).resolve(strict=False)
        choices: list[tuple[DownloadedFile, Path]] = []
        for downloaded in files:
            relative = _safe_relative_path(downloaded.relative_path)
            if relative is None:
                raise CoordinatorError(
                    "invalid_download_path",
                    "Trình tải nguồn trả về đường dẫn file không an toàn",
                    retryable=False,
                )
            if relative.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            candidate = (root / relative).resolve(strict=False)
            if not candidate.is_relative_to(root):
                raise CoordinatorError(
                    "invalid_download_path",
                    "Trình tải nguồn trả về đường dẫn file không an toàn",
                    retryable=False,
                )
            if downloaded.progress < 0.999999 or not candidate.is_file():
                continue
            choices.append((downloaded, candidate))
        if not choices:
            raise CoordinatorError(
                "source_video_missing",
                "Nội dung tải xong không có file video hoàn chỉnh được hỗ trợ",
                retryable=True,
            )
        return max(
            choices,
            key=lambda item: (item[0].size_bytes, str(item[0].relative_path)),
        )

    def _safe_materialized_subtitle(self, job_id: str, path: Path) -> Path:
        root = (self._jobs_dir / job_id).resolve(strict=False)
        materialized = path.resolve(strict=False)
        if not materialized.is_relative_to(root) or not materialized.is_file():
            raise CoordinatorError(
                "subtitle_materialization_invalid",
                "File phụ đề đã tạo nằm ngoài thư mục job hoặc không tồn tại",
                retryable=False,
            )
        return materialized

    def _media_from_details(self, job: JobRecord) -> MediaAsset:
        raw = job.details.get("selected_media")
        source_path = job.details.get("source_media_path")
        if not isinstance(raw, dict) or not isinstance(source_path, str):
            raise CoordinatorError(
                "media_checkpoint_missing",
                "Thiếu checkpoint file video để tìm phụ đề",
                retryable=False,
            )
        try:
            resolved_source = Path(source_path).resolve(strict=False)
            expected_root = (self._incoming_dir / job.id).resolve(strict=False)
            if not resolved_source.is_relative_to(expected_root) or not resolved_source.is_file():
                raise ValueError("unsafe or missing source path")
            return MediaAsset(
                path=resolved_source,
                title=str(raw["title"]),
                duration_us=int(raw["duration_us"]),
                source_language=str(raw["source_language"]),
                media_kind=MediaKind(str(raw.get("media_kind", MediaKind.MOVIE.value))),
                year=int(raw["year"]) if raw.get("year") is not None else None,
                fps=float(raw["fps"]) if raw.get("fps") is not None else None,
                imdb_id=str(raw["imdb_id"]) if raw.get("imdb_id") else None,
                tmdb_id=int(raw["tmdb_id"]) if raw.get("tmdb_id") is not None else None,
            )
        except (KeyError, TypeError, ValueError) as error:
            raise CoordinatorError(
                "media_checkpoint_invalid",
                "Checkpoint file video không hợp lệ",
                retryable=False,
            ) from error

    def _fail(self, job_id: str, *, code: str, message_vi: str, retryable: bool) -> JobRecord:
        current = self._store.get_job(job_id)
        if current.status in {
            JobStatus.CANCELLING,
            JobStatus.CANCELLED,
            JobStatus.COMPLETED,
        }:
            return current
        previous_checkpoint = self._store.get_checkpoint(job_id, current.stage)
        self._store.save_checkpoint(
            job_id,
            current.stage,
            {
                **(previous_checkpoint.payload if previous_checkpoint is not None else {}),
                "failed": True,
                "error_code": code,
                "error_message": message_vi,
                "retryable": retryable,
                "details": current.details,
            },
        )
        return self._store.update_status(
            job_id,
            JobStatus.FAILED,
            stage=current.stage,
            details=current.details,
            error_code=code,
            error_message=message_vi,
            retryable=retryable,
        )


def _safe_relative_path(path: Path) -> Path | None:
    value = str(path).replace("\\", "/")
    pure = PurePosixPath(value)
    if (
        not value
        or value == "."
        or pure.is_absolute()
        or ".." in pure.parts
        or (pure.parts and ":" in pure.parts[0])
    ):
        return None
    return Path(*pure.parts)


def _download_checkpoint(details: dict[str, Any]) -> dict[str, Any]:
    return {
        key: details.get(key)
        for key in (
            "download_state",
            "downloaded_bytes",
            "total_bytes",
            "speed_bytes_per_second",
            "eta_seconds",
            "download_progress",
            "stage_progress_permille",
        )
    }


def _media_details(media: MediaAsset, relative_path: Path, size_bytes: int) -> dict[str, Any]:
    payload = asdict(media)
    payload.pop("path", None)
    payload["media_kind"] = media.media_kind.value
    payload["relative_path"] = relative_path.as_posix()
    payload["size_bytes"] = max(size_bytes, 0)
    return payload


def _public_subtitle(candidate: SubtitleCandidate) -> dict[str, Any]:
    return {
        "subtitle_id": candidate.subtitle_id,
        "source": candidate.source.value,
        "language": candidate.language,
        "format": candidate.format.value,
        "score": candidate.score,
        "high_confidence": candidate.high_confidence,
        "release_name": candidate.release_name,
        "fps": candidate.fps,
        "hearing_impaired": candidate.hearing_impaired,
        "forced": candidate.forced,
        "matched_by": candidate.matched_by,
    }


def _required_text(value: object, *, default: str) -> str:
    return value.strip() if isinstance(value, str) and value.strip() else default


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_year(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        year = int(value)
    except (TypeError, ValueError):
        return None
    return year if 1888 <= year <= 2200 else None


def _media_kind(value: object) -> MediaKind:
    try:
        return MediaKind(str(value)) if value is not None else MediaKind.MOVIE
    except ValueError:
        return MediaKind.MOVIE


def _subtitle_language_unknown(language: str) -> bool:
    normalized = language.strip().lower().replace("_", "-").split("-", 1)[0]
    return normalized in {"", "auto", "und", "unknown", "mul", "zxx"}


__all__ = ["VIDEO_EXTENSIONS", "AcquisitionCoordinator", "CoordinatorError"]
