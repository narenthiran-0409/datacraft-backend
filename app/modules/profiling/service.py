import uuid
from datetime import datetime, timezone

from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import (
    DatasetNotActiveError,
    DatasetNotFoundError,
    FullScanExceedsLimitError,
    ProfilingAlreadyRunningError,
    ProfileRunNotFoundError,
    SampleSizeExceedsLimitError,
)
from app.db.models import Dataset, Job, ProfileRun, User
from app.modules.lineage.service import LineageService
from app.modules.profiling.run_options import set_include_top_values


class ProfilingService:
    def __init__(self, db: Session, redis_client: Redis) -> None:
        self._db = db
        self._redis = redis_client
        self._lineage = LineageService(db)

    def get_profile_run(self, profile_run_id: uuid.UUID) -> ProfileRun:
        profile_run = self._db.get(ProfileRun, profile_run_id)
        if profile_run is None:
            raise ProfileRunNotFoundError(f"Profile run {profile_run_id} not found")
        return profile_run

    def list_profile_runs(
        self, *, dataset_id: uuid.UUID | None, status: str | None
    ) -> list[ProfileRun]:
        stmt = select(ProfileRun)
        if dataset_id is not None:
            stmt = stmt.where(ProfileRun.dataset_id == dataset_id)
        if status is not None:
            stmt = stmt.where(ProfileRun.status == status)
        stmt = stmt.order_by(ProfileRun.created_at.desc())
        return list(self._db.execute(stmt).scalars())

    def get_latest_completed_run(self, dataset_id: uuid.UUID) -> ProfileRun | None:
        return self._db.execute(
            select(ProfileRun)
            .where(ProfileRun.dataset_id == dataset_id, ProfileRun.status == "COMPLETED")
            .order_by(ProfileRun.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

    def _get_active_dataset(self, dataset_id: uuid.UUID) -> Dataset:
        dataset = self._db.get(Dataset, dataset_id)
        if dataset is None:
            raise DatasetNotFoundError(f"Dataset {dataset_id} not found")
        if not dataset.is_active:
            raise DatasetNotActiveError(f"Dataset {dataset_id} is not active")
        return dataset

    def _resolve_sample_size(self, *, dataset: Dataset, sample_size: int | None, full_scan: bool) -> int | None:
        """Resolves sample_size/full_scan against datasets.row_count_estimate
        BEFORE anything is created. Both rejection paths are 422s that
        create nothing — never a silent downgrade to a smaller value than
        requested."""
        if full_scan:
            estimate = dataset.row_count_estimate or 0
            if estimate > settings.PROFILING_MAX_FULL_SCAN_ROWS:
                raise FullScanExceedsLimitError(
                    f"Full scan rejected: dataset row_count_estimate ({estimate}) exceeds "
                    f"PROFILING_MAX_FULL_SCAN_ROWS ({settings.PROFILING_MAX_FULL_SCAN_ROWS})"
                )
            return None  # resolved at execution time to the dataset's actual row count

        if sample_size is not None:
            if sample_size > settings.PROFILING_MAX_SAMPLE_SIZE:
                raise SampleSizeExceedsLimitError(
                    f"Requested sample_size ({sample_size}) exceeds "
                    f"PROFILING_MAX_SAMPLE_SIZE ({settings.PROFILING_MAX_SAMPLE_SIZE})"
                )
            return sample_size

        return settings.PROFILING_DEFAULT_SAMPLE_SIZE

    def start_profiling(
        self,
        *,
        actor: User,
        dataset_id: uuid.UUID,
        sample_size: int | None,
        full_scan: bool,
        include_top_values: bool,
    ) -> tuple[ProfileRun, Job]:
        dataset = self._get_active_dataset(dataset_id)

        existing = self._db.execute(
            select(Job).where(
                Job.job_type == "PROFILE_RUN",
                Job.entity_type == "DATASET",
                Job.entity_id == dataset_id,
                Job.status.in_(("QUEUED", "RUNNING")),
            )
        ).scalars().first()
        if existing is not None:
            raise ProfilingAlreadyRunningError(
                f"A profiling job ({existing.id}) is already {existing.status} for this dataset"
            )

        resolved_sample_size = self._resolve_sample_size(
            dataset=dataset, sample_size=sample_size, full_scan=full_scan
        )

        job = Job(
            job_type="PROFILE_RUN",
            entity_type="DATASET",
            entity_id=dataset.id,
            created_by=actor.id,
        )
        self._db.add(job)
        self._db.flush()

        profile_run = ProfileRun(
            dataset_id=dataset.id,
            job_id=job.id,
            status="QUEUED",
            sample_size=resolved_sample_size,
            triggered_by=actor.id,
        )
        self._db.add(profile_run)
        self._db.flush()

        # Phase 10 touch point 2 (additive-only): DATASET -> PROFILE_RUN,
        # written regardless of eventual outcome, in this same transaction.
        self._lineage.record_edge("DATASET", dataset.id, "PROFILE_RUN", profile_run.id, "PROFILED_BY")

        self._db.commit()
        self._db.refresh(profile_run)
        self._db.refresh(job)

        # include_top_values has no column on the frozen profile_runs table.
        # Stored in Redis (app.modules.profiling.run_options), keyed by
        # profile_run_id, rather than passed as a Celery task kwarg, so it's
        # reliably recoverable for the lifetime of the run regardless of how
        # (or how many times) run_profile ends up being invoked — see that
        # module's docstring for why a task kwarg alone isn't sufficient.
        # Written only after the DB commit succeeds, so we never leave a
        # Redis flag for a profile_run row that didn't actually persist.
        set_include_top_values(self._redis, profile_run.id, include_top_values)

        return profile_run, job
