"""In-process scheduler for preview batches.

Design notes
------------
* All batches of one app process share one pool of ``settings.slots`` ffmpeg
  slots. A slot is held by a *live process*, not by a logical attempt status:
  an attempt that is being cancelled still occupies its slot until its ffmpeg
  process has actually exited.
* A segment starts with exactly one "first run" attempt. If that first run is
  still running after ``slow_after_seconds`` and a slot is free, one "backup"
  attempt is dispatched. Segments that have never started take priority over
  these backups.
* The first valid artifact observed for a segment wins. Every other attempt of
  that segment is cancelled; any success that still arrives later is recorded
  but its file is discarded and never replaces the selection.
* Once all three segments have a selection, composition starts immediately in
  a separate thread. It only reads the immutable selected artifacts, so it is
  allowed to run while cancelled processes are still exiting.
* A batch may carry an optional slot-second budget. Usage is wall-clock time
  during which one of the batch's ffmpeg processes holds a slot, summed over
  all of its attempts: two slots held for one second meter 2 slot-seconds.
  Backups and processes still exiting after a cancel keep metering; queued
  attempts and composition do not. Reaching the budget stops further dispatch
  for that batch and cancels its live attempts. A budgeted batch only becomes
  downloadable after every attempt of that batch has released its slot *and*
  the final usage is below the budget.
"""

from __future__ import annotations

import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import media
from .config import MAX_SLOT_BUDGET_SECONDS, MIN_SLOT_BUDGET_SECONDS, Settings

TERMINAL_ATTEMPT_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "superseded", "skipped"}
)
LIVE_ATTEMPT_STATUSES = frozenset({"queued", "running", "cancelling"})


# Tolerance for float accumulation when comparing metered usage to the budget.
SLOT_SECONDS_EPSILON = 1e-6


def pick_next_candidate(
    batches: list["Batch"], settings: Settings, now: float
) -> tuple["Segment", str] | None:
    """Choose the next dispatch.

    Returns ``(segment, "first")`` for a never-started first run, or
    ``(segment, "backup")`` for an overdue still-running first run. The caller
    owns ordering/locking; this function is pure for testability.

    Priority: every never-started segment of every running batch outranks all
    backups, so a later batch is not starved by an earlier batch's retry.

    Batches that already reached their slot-second budget are never candidates;
    they are cancelled by the scheduler instead of being dispatched further.
    """

    ordered = sorted(
        (b for b in batches if not b.budget_reached(now)),
        key=lambda b: b.created_at,
    )

    for batch in ordered:
        if batch.status != "running":
            continue
        for name in batch.clip_names:
            segment = batch.segments[name]
            if segment.selected_attempt_id is not None:
                continue
            if any(
                a.kind == "first" and a.status == "queued" for a in segment.attempts
            ):
                return segment, "first"

    candidates: list[tuple[float, Segment]] = []
    for batch in ordered:
        if batch.status != "running":
            continue
        for name in batch.clip_names:
            segment = batch.segments[name]
            if segment.selected_attempt_id is not None or segment.backup_launched:
                continue
            first = next((a for a in segment.attempts if a.kind == "first"), None)
            if (
                first is not None
                and first.status == "running"
                and first.started_at is not None
                and first.cancel_requested_at is None
                and now - first.started_at >= settings.slow_after_seconds
            ):
                candidates.append((first.started_at, segment))
    if candidates:
        candidates.sort(key=lambda item: item[0])
        return candidates[0][1], "backup"
    return None


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Attempt:
    id: str
    segment: str
    kind: str  # "first" | "backup"
    status: str = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    cancel_requested_at: float | None = None
    terminate_sent_at: float | None = None
    exit_code: int | None = None
    artifact_path: Path | None = None
    log_path: Path | None = None
    error: str | None = None
    cancel_reason: str | None = None
    slot_held: bool = False
    # Wall-clock seconds during which this attempt's live ffmpeg process held
    # a slot. Finalized on exit; while live it is a settled baseline that
    # ``slot_seconds_live`` extends with the current hold segment.
    slot_seconds: float = 0.0
    # Start of the not-yet-settled hold segment, if the process is live.
    slot_meter_started_at: float | None = None
    proc: Any = field(default=None, repr=False)
    log_file: Any = field(default=None, repr=False)

    def elapsed_ms(self, now: float | None = None) -> int | None:
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else (now or time.time())
        return max(0, int((end - self.started_at) * 1000))

    def slot_seconds_live(self, now: float | None = None) -> float:
        """Metered slot-seconds including the currently open hold segment."""

        total = self.slot_seconds
        if self.slot_held and self.slot_meter_started_at is not None:
            end = self.finished_at if self.finished_at is not None else (now or time.time())
            total += max(0.0, end - self.slot_meter_started_at)
        return total

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        return {
            "id": self.id,
            "segment": self.segment,
            "kind": self.kind,
            "status": self.status,
            "created_at": _iso(self.created_at),
            "started_at": _iso(self.started_at),
            "finished_at": _iso(self.finished_at),
            "elapsed_ms": self.elapsed_ms(now),
            "slot_seconds": round(self.slot_seconds_live(now), 3),
            "exit_code": self.exit_code,
            "error": self.error,
            "cancel_reason": self.cancel_reason,
            "slot_held": self.slot_held,
            "artifact_bytes": (
                self.artifact_path.stat().st_size
                if self.artifact_path is not None and self.artifact_path.is_file()
                else None
            ),
        }


@dataclass
class Segment:
    name: str
    index: int
    slow_first: bool
    attempts: list[Attempt] = field(default_factory=list)
    status: str = "pending"  # pending/running/succeeded/failed/aborted
    selected_attempt_id: str | None = None
    backup_launched: bool = False

    def selected_attempt(self) -> Attempt | None:
        if self.selected_attempt_id is None:
            return None
        for attempt in self.attempts:
            if attempt.id == self.selected_attempt_id:
                return attempt
        return None

    def live_attempts(self) -> list[Attempt]:
        return [a for a in self.attempts if a.status in LIVE_ATTEMPT_STATUSES]

    def to_dict(self, now: float | None = None, slow_after_seconds: float = 2.0) -> dict[str, Any]:
        return {
            "name": self.name,
            "index": self.index,
            "slow_first": self.slow_first,
            "status": self.status,
            "selected_attempt_id": self.selected_attempt_id,
            "backup_launched": self.backup_launched,
            "backup_eligible": self._backup_eligible(now, slow_after_seconds),
            "attempts": [a.to_dict(now) for a in self.attempts],
        }

    def _backup_eligible(self, now: float | None, slow_after_seconds: float) -> bool:
        """True once the first run has been running longer than the slow threshold."""

        if self.backup_launched or self.selected_attempt_id is not None:
            return False
        first = next((a for a in self.attempts if a.kind == "first"), None)
        if first is None or first.status != "running" or first.started_at is None:
            return False
        if first.cancel_requested_at is not None:
            return False
        return (now or time.time()) - first.started_at >= slow_after_seconds


@dataclass
class Batch:
    id: str
    slow_clip: str
    clip_names: tuple[str, ...]
    work_dir: Path
    slot_budget: int | None = None
    created_at: float = field(default_factory=time.time)
    # running -> composing -> draining/ready, or exceeded/failed at any point.
    status: str = "running"
    segments: dict[str, Segment] = field(default_factory=dict)
    # Set when the composed file has been written; for budgeted batches this
    # may happen while cancelled attempts still hold slots ("draining").
    compose_done: bool = False
    budget_exceeded: bool = False
    compose_started_at: float | None = None
    finished_at: float | None = None
    final_path: Path | None = None
    error: str | None = None

    def attempts(self) -> list[Attempt]:
        return [a for seg in self.segments.values() for a in seg.attempts]

    def slot_seconds_used(self, now: float | None = None) -> float:
        """Total metered usage: sum of every attempt's slot-held time."""

        return sum(a.slot_seconds_live(now) for a in self.attempts())

    def live_slot_attempts(self) -> list[Attempt]:
        """Attempts whose ffmpeg process still occupies a slot."""

        return [a for a in self.attempts() if a.slot_held]

    def budget_reached(self, now: float | None = None) -> bool:
        if self.slot_budget is None:
            return False
        if self.budget_exceeded:
            return True
        return self.slot_seconds_used(now) >= self.slot_budget - SLOT_SECONDS_EPSILON

    def budget_exceeded_message(self, now: float | None = None) -> str:
        used = self.slot_seconds_used(now)
        return (
            f"槽秒预算 {self.slot_budget} 已用尽（累计 "
            f"{used:.2f} 槽秒），已停止派工并取消仍在运行的尝试，不提供成片"
        )

    def can_download(self, now: float | None = None) -> bool:
        """True when the composed file may be served.

        Budgeted batches additionally require every slot released and the
        confirmed total usage strictly below the budget.
        """

        if self.status != "ready" or self.final_path is None:
            return False
        if not self.final_path.is_file():
            return False
        if self.slot_budget is not None:
            if self.live_slot_attempts():
                return False
            if self.slot_seconds_used(now) >= self.slot_budget - SLOT_SECONDS_EPSILON:
                return False
        return True

    def download_blocked_reason(self, now: float | None = None) -> str | None:
        if self.can_download(now):
            return None
        if self.budget_exceeded:
            # Metering keeps running while cancelled processes exit, so report
            # the live total rather than the frozen snapshot.
            return self.budget_exceeded_message(now)
        if self.slot_budget is not None:
            if self.live_slot_attempts():
                return "等待本批转码槽全部释放后开放下载"
            if self.slot_seconds_used(now) >= self.slot_budget - SLOT_SECONDS_EPSILON:
                return self.budget_exceeded_message(now)
        if self.status == "draining":
            return "等待本批转码槽全部释放后开放下载"
        if self.status == "composing":
            return "成片合成中"
        return self.error or "成片尚未就绪"

    def to_dict(
        self,
        slots_used: int,
        slots_total: int,
        now: float | None = None,
        slow_after_seconds: float = 2.0,
    ) -> dict[str, Any]:
        now = now or time.time()
        used = self.slot_seconds_used(now)
        error = self.error
        if self.budget_exceeded:
            # Usage keeps growing while cancelled processes exit, so the card
            # must show the live total rather than the snapshot at the stop.
            error = self.budget_exceeded_message(now)
        return {
            "id": self.id,
            "slow_clip": self.slow_clip,
            "status": self.status,
            "created_at": _iso(self.created_at),
            "finished_at": _iso(self.finished_at),
            "elapsed_ms": max(0, int(((self.finished_at or now) - self.created_at) * 1000)),
            "error": error,
            "slots_used": slots_used,
            "slots_total": slots_total,
            "slot_budget": self.slot_budget,
            "slot_seconds_used": round(used, 3),
            "budget_exceeded": self.budget_exceeded,
            "budget_reached": self.budget_reached(now),
            "slow_after_seconds": slow_after_seconds,
            "segments": [
                self.segments[name].to_dict(now, slow_after_seconds)
                for name in self.clip_names
            ],
            "attempts": [a.to_dict(now) for a in self.attempts()],
            "download_ready": self.can_download(now),
            "download_url": f"/batches/{self.id}/download" if self.status == "ready" else None,
            "download_blocked_reason": self.download_blocked_reason(now),
        }


class BatchManager:
    """Thread-safe, in-process batch scheduler.

    A single uvicorn worker is required; state is intentionally in memory.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._batches: dict[str, Batch] = {}
        self._slots_used = 0
        self._stopping = False
        self._loop_thread = threading.Thread(
            target=self._run_loop, name="preview-scheduler", daemon=True
        )
        self._loop_thread.start()

    # ------------------------------------------------------------------ public

    @property
    def slots_total(self) -> int:
        return self.settings.slots

    @property
    def slots_used(self) -> int:
        with self._lock:
            return self._slots_used

    def create_batch(
        self, slow_clip: str, slot_budget: int | None = None
    ) -> dict[str, Any]:
        slow_clip = (slow_clip or "").strip().upper()
        if slow_clip not in self.settings.clip_names:
            raise ValueError("slow_clip must be one of the built-in clips")
        if slot_budget is not None and (
            isinstance(slot_budget, bool)
            or not isinstance(slot_budget, int)
            or not MIN_SLOT_BUDGET_SECONDS <= slot_budget <= MAX_SLOT_BUDGET_SECONDS
        ):
            raise ValueError(
                f"slot_budget 必须是 {MIN_SLOT_BUDGET_SECONDS}~"
                f"{MAX_SLOT_BUDGET_SECONDS} 的整数"
            )
        batch_id = uuid.uuid4().hex
        work_dir = self.settings.work_dir / batch_id
        work_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            batch = Batch(
                id=batch_id,
                slow_clip=slow_clip,
                clip_names=tuple(self.settings.clip_names),
                work_dir=work_dir,
                slot_budget=slot_budget,
            )
            for index, name in enumerate(self.settings.clip_names):
                segment = Segment(
                    name=name,
                    index=index,
                    slow_first=(name == slow_clip),
                )
                first = Attempt(
                    id=uuid.uuid4().hex[:12],
                    segment=name,
                    kind="first",
                )
                segment.attempts.append(first)
                batch.segments[name] = segment
            self._batches[batch_id] = batch
            self._cond.notify_all()
            return batch.to_dict(
                self._slots_used, self.slots_total, slow_after_seconds=self.settings.slow_after_seconds
            )

    def get_batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._lock:
            batch = self._batches.get(batch_id)
            if batch is None:
                return None
            return batch.to_dict(
                self._slots_used, self.slots_total, slow_after_seconds=self.settings.slow_after_seconds
            )

    def list_batches(self) -> list[dict[str, Any]]:
        with self._lock:
            now = time.time()
            batches = sorted(self._batches.values(), key=lambda b: b.created_at, reverse=True)
            return [
                b.to_dict(
                    self._slots_used,
                    self.slots_total,
                    now,
                    slow_after_seconds=self.settings.slow_after_seconds,
                )
                for b in batches
            ]

    def get_batch_file(self, batch_id: str) -> Path | None:
        with self._lock:
            batch = self._batches.get(batch_id)
            if (
                batch is None
                or batch.status != "ready"
                or batch.final_path is None
                or not batch.final_path.is_file()
            ):
                return None
            # A budgeted batch is downloadable only after all of its slots are
            # released and the confirmed usage is below the budget.
            if batch.slot_budget is not None:
                if batch.live_slot_attempts():
                    return None
                if batch.slot_seconds_used() >= batch.slot_budget - SLOT_SECONDS_EPSILON:
                    return None
            return batch.final_path

    def shutdown(self) -> None:
        with self._cond:
            self._stopping = True
            for batch in self._batches.values():
                for attempt in batch.attempts():
                    if attempt.status in ("running", "cancelling"):
                        self._request_cancel(attempt, "应用正在停止")
                    elif attempt.status == "queued":
                        attempt.status = "skipped"
                        attempt.finished_at = time.time()
                        attempt.error = "应用正在停止"
            self._cond.notify_all()
        self._loop_thread.join(timeout=self.settings.shutdown_grace_seconds)
        # Last resort: make sure no ffmpeg outlives the process.
        with self._lock:
            for batch in self._batches.values():
                for attempt in batch.attempts():
                    if attempt.proc is not None and attempt.proc.poll() is None:
                        media.kill_process(attempt.proc)

    # ---------------------------------------------------------------- internals

    def _run_loop(self) -> None:
        while True:
            with self._cond:
                if self._stopping:
                    return
                try:
                    now = time.time()
                    self._reap(now)
                    self._enforce_budgets(now)
                    self._settle_batches(now)
                    self._dispatch()
                    self._reap(now)
                    self._enforce_budgets(now)
                    self._settle_batches(now)
                except Exception as exc:  # keep the scheduler alive; surface via batches
                    self._record_internal_error(exc)
                self._cond.wait(timeout=self.settings.monitor_interval)

    # -- slot-second budget ------------------------------------------------

    def _enforce_budgets(self, now: float) -> None:
        """Stop a batch at its budget: cancel live attempts, skip queued ones.

        Usage is metered continuously, so the check must run independently of
        process exits; this is called both before and after ``_dispatch``.
        Attempts in ``cancelling`` keep metering until their process exits.
        """

        for batch in self._batches.values():
            if batch.slot_budget is None or batch.budget_exceeded:
                continue
            if batch.status in ("ready", "failed"):
                continue
            if not batch.budget_reached(now):
                continue
            batch.budget_exceeded = True
            batch.status = "exceeded"
            batch.finished_at = now
            batch.error = batch.budget_exceeded_message(now)
            for segment in batch.segments.values():
                for attempt in segment.attempts:
                    if attempt.status in ("running", "cancelling"):
                        self._request_cancel(attempt, "批次槽秒预算已用尽")
                    elif attempt.status == "queued":
                        attempt.status = "cancelled"
                        attempt.finished_at = now
                        attempt.error = "批次槽秒预算已用尽"
                self._refresh_segment(segment)

    def _settle_batches(self, now: float) -> None:
        """Finish a budgeted batch once every slot is released below budget.

        Composition may already be done ("draining"): what is still missing is
        confirmation that cancelled attempts have exited and that the final
        usage did not overshoot the budget. An overshoot turns the batch into
        ``exceeded`` so the composed file is never offered.
        """

        for batch in self._batches.values():
            if batch.slot_budget is None:
                continue
            if batch.status not in ("draining", "composing"):
                continue
            if batch.live_slot_attempts():
                continue
            used = batch.slot_seconds_used(now)
            if used >= batch.slot_budget - SLOT_SECONDS_EPSILON:
                # Final usage is not below budget: withhold the composed file.
                batch.budget_exceeded = True
                batch.status = "exceeded"
                if not batch.error:
                    batch.error = batch.budget_exceeded_message(now)
                if batch.finished_at is None:
                    batch.finished_at = now
                continue
            if batch.status == "draining":
                if batch.compose_done and batch.final_path is not None:
                    batch.status = "ready"
                    batch.error = None
                    batch.finished_at = now
                else:
                    # Composed file vanished or composition never completed.
                    batch.status = "failed"
                    batch.error = batch.error or "成片文件缺失"
                    batch.finished_at = now

    def _record_internal_error(self, exc: Exception) -> None:
        message = f"调度器内部错误: {exc!r}"
        for batch in self._batches.values():
            if batch.status in ("running", "composing", "draining"):
                batch.status = "failed"
                batch.error = message
                batch.finished_at = time.time()
            for attempt in batch.attempts():
                if attempt.status in ("running", "cancelling"):
                    self._request_cancel(attempt, "调度器内部错误")
                elif attempt.status == "queued":
                    attempt.status = "skipped"
                    attempt.finished_at = time.time()

    # -- dispatch ----------------------------------------------------------

    def _dispatch(self) -> None:
        while self._slots_used < self.settings.slots:
            candidate = pick_next_candidate(
                list(self._batches.values()), self.settings, time.time()
            )
            if candidate is None:
                return
            segment, kind = candidate
            if kind == "backup":
                attempt = Attempt(
                    id=uuid.uuid4().hex[:12], segment=segment.name, kind="backup"
                )
                segment.attempts.append(attempt)
                segment.backup_launched = True
                self._launch(segment, attempt, slow=False)
            else:
                attempt = next(
                    a
                    for a in segment.attempts
                    if a.kind == "first" and a.status == "queued"
                )
                self._launch(
                    segment,
                    attempt,
                    slow=segment.slow_first and attempt.kind == "first",
                )
            if segment.status == "pending" and segment.selected_attempt_id is None:
                segment.status = "running"

    def _launch(self, segment: Segment, attempt: Attempt, *, slow: bool) -> None:
        batch = self._batch_for(segment)
        source = self.settings.input_dir / self._fixture_name(segment)
        attempt.artifact_path = batch.work_dir / f"{segment.name}_{attempt.id}.mp4"
        attempt.log_path = batch.work_dir / f"{segment.name}_{attempt.id}.log"
        cmd = media.transcode_command(
            self.settings, source, attempt.artifact_path, slow=slow
        )
        attempt.started_at = time.time()
        segment.status = "running" if segment.selected_attempt_id is None else segment.status
        try:
            log_file = open(attempt.log_path, "wb")
            attempt.log_file = log_file
            attempt.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log_file,
                close_fds=True,
            )
            attempt.status = "running"
            attempt.slot_held = True
            attempt.slot_meter_started_at = attempt.started_at
            self._slots_used += 1
        except OSError as exc:
            attempt.status = "failed"
            attempt.finished_at = time.time()
            attempt.error = f"无法启动 ffmpeg: {exc}"
            attempt.exit_code = None
            attempt.proc = None
            attempt.slot_held = False
            if attempt.log_file is not None:
                attempt.log_file.close()
                attempt.log_file = None
            self._refresh_segment(segment)
            self._evaluate_batch(batch, attempt.finished_at)

    def _fixture_name(self, segment: Segment) -> str:
        return f"clip_{segment.index + 1}_{segment.name}.mp4"

    def _batch_for(self, segment: Segment) -> Batch:
        for batch in self._batches.values():
            if batch.segments.get(segment.name) is segment:
                return batch
        raise RuntimeError("segment does not belong to a known batch")

    # -- slow threshold ----------------------------------------------------

    # Backups are launched by ``_dispatch``'s priority queue: an overdue first
    # run becomes a backup candidate there, while never-started segments keep
    # jumping ahead of it. Keeping one dispatch path avoids two competing
    # schedulers.

    # -- process lifecycle -------------------------------------------------

    def _reap(self, now: float) -> None:
        exited: list[tuple[int, float, Batch, Attempt]] = []
        for batch in list(self._batches.values()):
            for attempt in list(batch.attempts()):
                if attempt.proc is None or attempt.status not in ("running", "cancelling"):
                    continue
                returncode = attempt.proc.poll()
                if returncode is None:
                    if (
                        attempt.status == "cancelling"
                        and attempt.terminate_sent_at is not None
                        and now - attempt.terminate_sent_at >= self.settings.cancel_grace_seconds
                    ):
                        media.kill_process(attempt.proc)
                    continue
                # Order simultaneous exits by when their artifact hit disk, so
                # "first successful artifact" means earliest produced rather
                # than earliest dict insertion.
                order = self._artifact_mtime(attempt)
                exited.append((returncode, order, batch, attempt))

        exited.sort(key=lambda item: (item[1], item[3].created_at))
        for returncode, _order, batch, attempt in exited:
            if attempt.status not in ("running", "cancelling"):
                continue
            self._finalize_attempt(batch, attempt, returncode, now)

    @staticmethod
    def _artifact_mtime(attempt: Attempt) -> float:
        try:
            if attempt.artifact_path is not None and attempt.artifact_path.is_file():
                return attempt.artifact_path.stat().st_mtime
        except OSError:
            pass
        return float("inf")

    def _finalize_attempt(
        self, batch: Batch, attempt: Attempt, returncode: int, now: float
    ) -> None:
        attempt.exit_code = returncode
        attempt.finished_at = now
        attempt.proc = None
        if attempt.log_file is not None:
            attempt.log_file.close()
            attempt.log_file = None
        if attempt.slot_held:
            attempt.slot_held = False
            if attempt.slot_meter_started_at is not None:
                attempt.slot_seconds += max(0.0, now - attempt.slot_meter_started_at)
                attempt.slot_meter_started_at = None
            self._slots_used = max(0, self._slots_used - 1)

        segment = batch.segments[attempt.segment]
        selected = segment.selected_attempt_id is not None
        cancellable_done = attempt.cancel_requested_at is not None

        artifact_ok = returncode == 0 and media.artifact_looks_complete(
            self.settings.ffmpeg_binary,
            attempt.artifact_path,
            min_seconds=self.settings.clip_seconds,
        )

        if selected:
            # A late artifact must never replace the earlier selection.
            if artifact_ok:
                attempt.status = "superseded"
                attempt.error = "迟到的成功产物已丢弃，保留先成功的产物"
            elif cancellable_done:
                attempt.status = "cancelled"
            else:
                attempt.status = "failed"
                attempt.error = self._failure_detail(attempt, returncode)
            if attempt.artifact_path is not None:
                attempt.artifact_path.unlink(missing_ok=True)
        elif batch.status == "failed":
            attempt.status = "cancelled" if cancellable_done else "failed"
            if not cancellable_done:
                attempt.error = self._failure_detail(attempt, returncode)
            if attempt.artifact_path is not None:
                attempt.artifact_path.unlink(missing_ok=True)
        elif batch.budget_exceeded:
            # The batch was stopped at its budget; keep the reported reason
            # ("budget exhausted") instead of relabelling a late success.
            attempt.status = "cancelled"
            if not cancellable_done:
                attempt.error = "批次槽秒预算已用尽"
            if attempt.artifact_path is not None:
                attempt.artifact_path.unlink(missing_ok=True)
        elif artifact_ok:
            attempt.status = "succeeded"
            segment.selected_attempt_id = attempt.id
            segment.status = "succeeded"
            self._cancel_segment_siblings(batch, segment, keep=attempt)
        else:
            attempt.status = "failed"
            attempt.error = self._failure_detail(attempt, returncode)
            attempt.artifact_path.unlink(missing_ok=True)

        self._refresh_segment(segment)
        self._evaluate_batch(batch, now)

    def _failure_detail(self, attempt: Attempt, returncode: int) -> str:
        if returncode != 0:
            base = f"ffmpeg 退出码 {returncode}"
        else:
            base = "ffmpeg 成功退出但没有可用的产物"
        tail = self._read_log_tail(attempt.log_path)
        return f"{base}: {tail}" if tail else base

    @staticmethod
    def _read_log_tail(path: Path | None, limit: int = 500) -> str:
        if path is None or not path.is_file():
            return ""
        try:
            data = path.read_bytes()[-4096:]
        except OSError:
            return ""
        text = data.decode("utf-8", "replace").strip()
        return text[-limit:]

    def _cancel_segment_siblings(
        self, batch: Batch, segment: Segment, *, keep: Attempt
    ) -> None:
        for other in segment.attempts:
            if other.id == keep.id:
                continue
            if other.status in ("running", "cancelling"):
                self._request_cancel(other, "同段已有成功产物")
            elif other.status == "queued":
                other.status = "cancelled"
                other.finished_at = time.time()
                other.error = "同段已有成功产物"

    def _request_cancel(self, attempt: Attempt, reason: str) -> None:
        if attempt.status in TERMINAL_ATTEMPT_STATUSES:
            return
        now = time.time()
        attempt.cancel_requested_at = attempt.cancel_requested_at or now
        attempt.cancel_reason = attempt.cancel_reason or reason
        if attempt.status == "queued":
            attempt.status = "cancelled"
            attempt.finished_at = now
            attempt.error = reason
            return
        if attempt.status == "running":
            attempt.status = "cancelling"
            attempt.terminate_sent_at = now
            if attempt.proc is not None:
                media.terminate_process(attempt.proc)

    # -- state roll-ups ----------------------------------------------------

    def _refresh_segment(self, segment: Segment) -> None:
        if segment.selected_attempt_id is not None:
            segment.status = "succeeded"
            return
        batch = self._batch_for(segment)
        if batch.status in ("failed", "exceeded"):
            segment.status = "failed" if segment.status == "failed" else "aborted"
            return
        if any(a.status in ("running", "cancelling") for a in segment.attempts):
            segment.status = "running"
        elif any(a.status == "queued" for a in segment.attempts):
            segment.status = "pending"
        elif segment.status not in ("failed", "succeeded", "aborted"):
            segment.status = "failed"

    def _evaluate_batch(self, batch: Batch, now: float) -> None:
        if batch.status in ("ready", "failed", "exceeded"):
            return

        failed_segment = next(
            (
                batch.segments[name]
                for name in batch.clip_names
                if batch.segments[name].selected_attempt_id is None
                and not batch.segments[name].live_attempts()
                and batch.segments[name].status != "pending"
            ),
            None,
        )
        if failed_segment is not None:
            batch.status = "failed"
            batch.finished_at = now
            batch.error = f"片段 {failed_segment.name} 的所有尝试均失败，整批不提供成片"
            # No selection can complete this batch any more; release the slots
            # for other batches instead of letting the remaining work run on.
            for name in batch.clip_names:
                segment = batch.segments[name]
                if segment.selected_attempt_id is not None:
                    segment.status = "succeeded"
                    continue
                if segment is failed_segment:
                    segment.status = "failed"
                    continue
                segment.status = "aborted"
                for attempt in segment.attempts:
                    if attempt.status == "running":
                        self._request_cancel(attempt, "同批次其他片段已无可用产物")
                    elif attempt.status == "queued":
                        attempt.status = "skipped"
                        attempt.finished_at = now
                        attempt.error = "同批次其他片段已无可用产物"
                self._refresh_segment(segment)
            return

        if all(
            batch.segments[name].selected_attempt_id is not None
            for name in batch.clip_names
        ):
            self._start_composition(batch, now)

    def _start_composition(self, batch: Batch, now: float) -> None:
        if batch.status != "running":
            return
        batch.status = "composing"
        batch.compose_started_at = now
        paths = [
            batch.segments[name].selected_attempt().artifact_path
            for name in batch.clip_names
        ]
        target = self.settings.output_dir / f"preview_{batch.id[:8]}.mp4"
        thread = threading.Thread(
            target=self._compose,
            args=(batch, paths, target),
            name=f"compose-{batch.id[:8]}",
            daemon=True,
        )
        thread.start()

    def _compose(self, batch: Batch, paths: list[Path], target: Path) -> None:
        try:
            media.concat_clips(self.settings, paths, target)
        except Exception as exc:
            with self._cond:
                if batch.status == "composing":
                    batch.status = "failed"
                    batch.error = f"合成失败: {exc}"
                    batch.finished_at = time.time()
                self._cond.notify_all()
            return
        with self._cond:
            if batch.status == "composing":
                batch.compose_done = True
                batch.final_path = target
                if batch.slot_budget is not None:
                    # Budgeted batches must pass the final gate: every ffmpeg
                    # slot of this batch released *and* confirmed usage below
                    # budget. ``_settle_batches`` makes that decision, which
                    # also avoids a race where ``ready`` is set without the
                    # usage check.
                    batch.status = "draining"
                else:
                    batch.status = "ready"
                    batch.finished_at = time.time()
            self._cond.notify_all()
