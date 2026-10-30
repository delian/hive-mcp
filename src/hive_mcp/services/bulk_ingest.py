"""Resumable background ingestion of a whole folder, in steps of a few files (OQ-15).

One job per step (kind `ingest_folder`, payload run id + step number). A step walks the folder
after the previous step's cursor, ingests up to STEP_FILES files, stores the cumulative progress
under a key that only one worker can create (`put_if_absent`), and queues the next step. A crash,
a restart or a second replica therefore repeats at most one step, and every file is idempotent.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hive_mcp.domain.errors import ValidationError
from hive_mcp.domain.principal import Principal, Role
from hive_mcp.domain.scope import Scope, ScopeLevel
from hive_mcp.ingest.walk import walk_after
from hive_mcp.services.bulk_files import ingest_file
from hive_mcp.services.bulk_state import (
    CANCELLED,
    DONE,
    FAILED,
    INDEX_RUNS,
    MAX_ERRORS,
    NAMESPACE,
    RECORD_TTL_SECONDS,
    RUNNING,
    STEP_FILES,
    Progress,
    step_key,
)

if TYPE_CHECKING:
    from hive_mcp.domain.clock import Clock
    from hive_mcp.ports.jobs import Job, JobQueue
    from hive_mcp.ports.kv import KeyValueStore
    from hive_mcp.services.ingest import IngestService

INGEST_JOB = "ingest_folder"
LEASE_SECONDS = 120.0
log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunStatus:
    run_id: str
    state: str  # running, cancelling, done, cancelled, failed
    root: str
    user: str
    project: str | None
    started: str
    progress: Progress


class BulkIngest:
    def __init__(  # noqa: PLR0913 - collaborators and options
        self,
        ingest: IngestService,
        jobs: JobQueue,
        kv: KeyValueStore,
        clock: Clock,
        *,
        allowed_root: Path | None = None,
        step_files: int = STEP_FILES,
    ) -> None:
        self._ingest, self._jobs, self._kv, self._clock = ingest, jobs, kv, clock
        self._allowed, self._step_files = allowed_root, step_files

    def check_root(self, path: Path) -> Path:
        """The resolved folder: it must exist now and be below HIVE_INGEST_ROOT if that is set."""
        try:
            root = path.resolve(strict=True)
        except OSError as exc:
            raise ValidationError(f"folder not found on the server: {path}") from exc
        if not root.is_dir():
            raise ValidationError(f"not a directory: {root}")
        if self._allowed is not None and not root.is_relative_to(self._allowed.resolve()):
            raise ValidationError(f"folder is outside the allowed ingest root {self._allowed}")
        return root

    async def start(
        self,
        folder: Path,
        org: str,
        user: str,
        *,
        project: str | None = None,
        tags: tuple[str, ...] = (),
    ) -> str:
        root = self.check_root(folder)
        run_id = uuid.uuid4().hex[:16]
        header = {
            "run_id": run_id, "org": org, "user": user, "project": project, "tags": list(tags),
            "root": str(root), "started": self._clock.now().isoformat(),
        }  # fmt: skip
        await self._put(f"run:{run_id}", {**header, "progress": Progress().to_json()})
        await self._add_to_index(org, run_id)
        await self._enqueue(org, run_id, 0)
        return run_id

    async def cancel(self, run_id: str) -> bool:
        """Stop after the file in progress. False if the run is unknown or already finished."""
        status = await self.status(run_id)
        if status is None or status.state not in (RUNNING, "cancelling"):
            return False
        await self._put(f"cancel:{run_id}", {"at": self._clock.now().isoformat()})
        return True

    async def status(self, run_id: str) -> RunStatus | None:
        header = await self._kv.get(NAMESPACE, f"run:{run_id}")
        if header is None:
            return None
        progress = Progress.from_json(header.get("progress"))
        state = progress.state
        if state == RUNNING and await self._kv.get(NAMESPACE, f"cancel:{run_id}"):
            state = "cancelling"
        return RunStatus(
            run_id, state, header["root"], header["user"], header.get("project"),
            header["started"], progress,
        )  # fmt: skip

    async def list_runs(self, org: str) -> list[RunStatus]:
        index = await self._kv.get(NAMESPACE, f"index:{org}")
        found = [await self.status(r) for r in reversed((index or {}).get("ids", []))]
        return [s for s in found if s is not None]

    async def handle_step(self, job: Job) -> None:
        """Job handler. Safe to run twice for the same step (the second run changes nothing)."""
        run_id, step = job.payload["run_id"], int(job.payload["step"])
        header = await self._kv.get(NAMESPACE, f"run:{run_id}")
        if header is None:
            return  # records expired: nothing to continue
        if await self._kv.get(NAMESPACE, step_key(run_id, step)) is None:
            previous = Progress()
            if step:
                stored = await self._kv.get(NAMESPACE, step_key(run_id, step - 1))
                if stored is None:
                    return
                previous = Progress.from_json(stored)
            progress = await self._run_step(job, header, previous)
            if progress is None:
                return  # lease lost: the new holder repeats the step
            await self._kv.put_if_absent(
                NAMESPACE, step_key(run_id, step), progress.to_json(),
                ttl_seconds=RECORD_TTL_SECONDS,
            )  # fmt: skip
        await self._advance(header, step)

    async def _advance(self, header: dict[str, Any], step: int) -> None:
        """Publish the step's record in the run header and queue the next step (idempotent)."""
        run_id = header["run_id"]
        stored = await self._kv.get(NAMESPACE, step_key(run_id, step))
        if stored is None:
            return
        progress = Progress.from_json(stored)
        current = await self._kv.get(NAMESPACE, f"run:{run_id}") or header
        if current.get("step", -1) < step:  # a late repeat of an old step must not move it back
            await self._put(f"run:{run_id}", {**current, "progress": stored, "step": step})
        later = await self._kv.get(NAMESPACE, step_key(run_id, step + 1))
        if progress.state == RUNNING and later is None:
            await self._enqueue(header["org"], run_id, step + 1)

    async def _run_step(
        self, job: Job, header: dict[str, Any], progress: Progress
    ) -> Progress | None:
        run_id = header["run_id"]
        try:
            root = self.check_root(Path(header["root"]))
        except ValidationError as exc:
            progress.state = FAILED
            progress.errors = [*progress.errors, str(exc)][:MAX_ERRORS]
            return progress
        who = Principal(
            header["user"], header["org"], Role.MEMBER, frozenset(), frozenset(
                {header["project"]} if header["project"] else ()
            ),
        )  # fmt: skip
        scope = Scope(ScopeLevel.PROJECT, header["project"]) if header["project"] else None
        batch = await asyncio.to_thread(
            lambda: list(itertools.islice(walk_after(root, progress.cursor), self._step_files))
        )
        for rel, size in batch:
            if await self._kv.get(NAMESPACE, f"cancel:{run_id}"):
                progress.state = CANCELLED
                return progress
            if job.lease_owner and not await self._jobs.heartbeat(
                job.id, job.lease_owner, LEASE_SECONDS
            ):
                return None
            await ingest_file(
                self._ingest, who, scope, tuple(header["tags"]), (root, rel, size), progress
            )
            progress.cursor = rel
        if len(batch) < self._step_files:
            progress.state = DONE
        return progress

    async def _enqueue(self, org: str, run_id: str, step: int) -> None:
        await self._jobs.enqueue(
            org, INGEST_JOB, {"run_id": run_id, "step": str(step)},
            dedupe_key=f"bulk:{run_id}:{step}",
        )  # fmt: skip

    async def _add_to_index(self, org: str, run_id: str) -> None:
        stored = await self._kv.get(NAMESPACE, f"index:{org}")
        ids = [*(stored or {}).get("ids", []), run_id][-INDEX_RUNS:]
        await self._put(f"index:{org}", {"ids": ids})

    async def _put(self, key: str, value: dict[str, Any]) -> None:
        await self._kv.put(NAMESPACE, key, value, ttl_seconds=RECORD_TTL_SECONDS)
