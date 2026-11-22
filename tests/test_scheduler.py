"""Scheduler behaviour is exercised against a deterministic fake ffmpeg."""

from __future__ import annotations

import time
from pathlib import Path

from app import media
from app.scheduler import pick_next_candidate


def _segment(batch: dict, name: str) -> dict:
    return next(s for s in batch["segments"] if s["name"] == name)


def _attempts(batch: dict, name: str, kind: str | None = None) -> list[dict]:
    items = _segment(batch, name)["attempts"]
    return [a for a in items if kind is None or a["kind"] == kind]


def test_happy_path_composes_all_three_segments(started):
    started.write_fake_config({"sleep": 0.02})
    manager = started.manager
    batch = manager.create_batch("A")

    final = started.wait_for(
        lambda: started.batch(batch["id"])
        if started.batch(batch["id"])["status"] == "ready"
        else None,
        timeout=10,
    )
    assert final["download_ready"] is True
    assert final["download_url"] == f"/batches/{batch['id']}/download"
    assert [s["status"] for s in final["segments"]] == ["succeeded"] * 3
    assert final["error"] is None

    art = started.manager.get_batch_file(batch["id"])
    assert art is not None and art.is_file() and art.stat().st_size > 0
    # Exactly one first run per segment, no backups needed.
    for name in ("A", "B", "C"):
        assert len(_attempts(batch, name)) == 1
        assert _segment(final, name)["selected_attempt_id"] == _attempts(final, name)[0]["id"]


def test_slow_first_run_gets_backup_and_first_artifact_wins(started):
    # First run of A is slow; its backup is fast, so the backup wins and the
    # slow run is cancelled while still running.
    started.write_fake_config(
        {
            "segments": {
                "A": {"slow": {"sleep": 5.0}, "normal": {"sleep": 0.02}},
            },
            "sleep": 0.02,
        }
    )
    manager = started.manager
    batch = manager.create_batch("A")

    def backup_visible():
        b = started.batch(batch["id"])
        attempts = _attempts(b, "A", "backup")
        if not attempts or attempts[0]["status"] not in ("running", "cancelling", "succeeded"):
            return None
        return b

    seen = started.wait_for(backup_visible, timeout=5)
    backup = _attempts(seen, "A", "backup")[0]
    assert backup["kind"] == "backup"
    # Backup was only dispatched after the 0.35s threshold.
    assert backup["created_at"] is not None

    final = started.wait_for(
        lambda: started.batch(batch["id"])
        if started.batch(batch["id"])["status"] == "ready"
        else None,
        timeout=10,
    )
    attempts = _attempts(final, "A")
    first_final = next(a for a in attempts if a["kind"] == "first")
    backup_final = next(a for a in attempts if a["kind"] == "backup")
    assert backup_final["status"] == "succeeded"
    assert _segment(final, "A")["selected_attempt_id"] == backup_final["id"]
    # The slow first run was cancelled and did not become the selection.
    assert first_final["status"] in ("cancelling", "cancelled")
    assert first_final["id"] != _segment(final, "A")["selected_attempt_id"]
    # Cancellation was requested because a sibling succeeded.
    assert first_final["cancel_reason"] == "同段已有成功产物"


def test_cancelled_attempt_holds_slot_until_process_exits(started, monkeypatch):
    # A's first run is long; its backup succeeds. The slow run keeps its slot
    # while "cancelling" and composition may finish first, but the slot must
    # only be released once the process really exits.
    started.write_fake_config(
        {
            "segments": {"A": {"slow": {"sleep": 5.0}, "normal": {"sleep": 0.02}}},
            "sleep": 0.02,
        }
    )
    manager = started.manager
    terminate_calls: list[float] = []

    def fake_terminate(proc):
        # Simulate a platform that cannot signal; the demo still must reap
        # naturally and never double-count a slot.
        terminate_calls.append(time.monotonic())

    monkeypatch.setattr(media, "terminate_process", fake_terminate)
    batch = manager.create_batch("A")

    def holding():
        b = started.batch(batch["id"])
        first = _attempts(b, "A", "first")[0]
        if first["status"] == "cancelling" and first["slot_held"]:
            return b
        return None

    seen = started.wait_for(holding, timeout=6)
    assert seen["slots_used"] <= seen["slots_total"]
    first = _attempts(seen, "A", "first")[0]
    assert first["slot_held"] is True
    assert first["finished_at"] is None
    assert terminate_calls

    # Composition is allowed to finish while the cancelled process still exits.
    final = started.wait_for(
        lambda: started.batch(batch["id"])
        if started.batch(batch["id"])["status"] == "ready"
        else None,
        timeout=10,
    )
    assert final["status"] == "ready"
    assert _attempts(final, "A", "first")[0]["slot_held"] is True
    # Then the slot is eventually released after the slow process exits.
    released = started.wait_for(
        lambda: (lambda b: b if _attempts(b, "A", "first")[0]["slot_held"] is False else None)(
            started.batch(batch["id"])
        ),
        timeout=10,
    )
    assert released["slots_used"] <= released["slots_total"]
    assert _attempts(released, "A", "first")[0]["status"] in ("cancelled", "superseded")


def test_late_artifact_never_overwrites_selection(started, tmp_path):
    # First run is slow, ignores SIGTERM and *does* produce an artifact after a
    # delay; the backup succeeds much earlier. The late success must be
    # discarded as "superseded" and must not replace the selected artifact.
    started.write_fake_config(
        {
            "segments": {
                "A": {
                    "slow": {"sleep": 1.2, "ignore_term": 0.4},
                    "normal": {"sleep": 0.02},
                }
            },
            "sleep": 0.02,
        }
    )
    manager = started.manager
    batch = manager.create_batch("A")
    final = started.wait_for(
        lambda: started.batch(batch["id"])
        if started.batch(batch["id"])["status"] == "ready"
        else None,
        timeout=10,
    )
    selected_before = _segment(final, "A")["selected_attempt_id"]
    assert _segment(final, "A")["status"] == "succeeded"

    def late_settled():
        b = started.batch(batch["id"])
        first = _attempts(b, "A", "first")[0]
        return b if first["status"] in ("superseded", "cancelled", "failed") else None

    settled = started.wait_for(late_settled, timeout=10)
    first = _attempts(settled, "A", "first")[0]
    assert first["status"] in ("superseded", "cancelled")
    assert _segment(settled, "A")["selected_attempt_id"] == selected_before
    # The artifact of the selected attempt is still on disk; the late one is gone.
    work = started.settings.work_dir / batch["id"]
    selected_file = work / f"A_{selected_before}.mp4"
    late_file = work / f"A_{first['id']}.mp4"
    assert selected_file.is_file()
    assert not late_file.exists()
    assert started.manager.get_batch_file(batch["id"]) is not None
    assert settled["status"] == "ready"


def test_failed_attempt_waits_for_still_running_sibling(started):
    # A's backup fails quickly while the slow first run is still alive; the
    # segment must keep waiting rather than declare failure.
    started.write_fake_config(
        {
            "segments": {
                "A": {
                    "slow": {"sleep": 0.9},
                    "normal": {"sleep": 0.02, "exit": 3},
                }
            },
            "sleep": 0.02,
        }
    )
    manager = started.manager
    batch = manager.create_batch("A")

    def backup_failed_while_first_running():
        b = started.batch(batch["id"])
        backups = _attempts(b, "A", "backup")
        if not backups:
            return None
        backup = backups[0]
        first = _attempts(b, "A", "first")[0]
        if backup["status"] == "failed" and first["status"] == "running":
            return b
        return None

    seen = started.wait_for(backup_failed_while_first_running, timeout=6)
    assert _segment(seen, "A")["status"] == "running"
    assert seen["status"] == "running"

    final = started.wait_for(
        lambda: started.batch(batch["id"])
        if started.batch(batch["id"])["status"] in ("ready", "failed")
        else None,
        timeout=10,
    )
    # The slow first run eventually succeeds, so the batch is still usable.
    assert final["status"] == "ready"
    first = _attempts(final, "A", "first")[0]
    assert first["status"] == "succeeded"


def test_all_attempts_fail_means_no_final_video(started):
    started.write_fake_config(
        {
            "segments": {"B": {"normal": {"sleep": 0.02, "exit": 2}}},
            "sleep": 0.02,
        }
    )
    manager = started.manager
    batch = manager.create_batch("A")

    final = started.wait_for(
        lambda: started.batch(batch["id"])
        if started.batch(batch["id"])["status"] == "failed"
        else None,
        timeout=10,
    )
    assert final["status"] == "failed"
    assert final["download_ready"] is False
    assert final["download_url"] is None
    assert "B" in final["error"]
    assert started.manager.get_batch_file(batch["id"]) is None
    assert _segment(final, "B")["status"] == "failed"
    # Every attempt of B failed; other segments are not offered as a partial cut.
    assert all(a["status"] == "failed" for a in _attempts(final, "B"))


def test_never_started_segments_are_dispatched_before_backups(started):
    started.write_fake_config(
        {
            "segments": {"A": {"slow": {"sleep": 6.0}}},
            "sleep": 0.05,
        }
    )
    manager = started.manager
    batch = manager.create_batch("A")

    def all_first_runs_started():
        b = started.batch(batch["id"])
        firsts = [_attempts(b, n, "first")[0] for n in ("A", "B", "C")]
        if all(f["started_at"] is not None for f in firsts):
            return b
        return None

    seen = started.wait_for(all_first_runs_started, timeout=5)
    assert _segment(seen, "A")["backup_launched"] is False
    # While all three first runs occupy the two slots in turn, no backup may
    # exist yet.
    assert _attempts(seen, "A", "backup") == []

    def backup_arrives():
        b = started.batch(batch["id"])
        return b if _attempts(b, "A", "backup") else None

    with_backup = started.wait_for(backup_arrives, timeout=6)
    assert _attempts(with_backup, "A", "backup")[0]["kind"] == "backup"


def test_two_batches_share_the_two_slots(started):
    started.write_fake_config({"sleep": 0.4})
    manager = started.manager
    first = manager.create_batch("A")
    second = manager.create_batch("B")

    def both_running():
        seen = []
        for bid in (first["id"], second["id"]):
            b = started.batch(bid)
            running = [a for a in b["attempts"] if a["status"] == "running"]
            seen.extend(running)
        if len(seen) == 2 and started.batch(first["id"])["slots_used"] == 2:
            return True
        return None

    started.wait_for(both_running, timeout=5)
    # The pool is global: never more live attempts than slots at any time.
    for _ in range(30):
        for bid in (first["id"], second["id"]):
            b = started.batch(bid)
            assert b["slots_used"] <= b["slots_total"] == 2
            live = [a for a in b["attempts"] if a["status"] in ("running", "cancelling")]
            assert len(live) <= 2
        time.sleep(0.02)

    for bid in (first["id"], second["id"]):
        started.wait_for(
            lambda bid=bid: started.batch(bid)["status"] == "ready", timeout=15
        )


def test_artifacts_are_composed_in_clip_order(started, monkeypatch):
    started.write_fake_config({"sleep": 0.02})
    recorded: list[list[Path]] = []
    original = media.concat_clips

    def spy(settings, artifacts, target):
        recorded.append([Path(p) for p in artifacts])
        return original(settings, artifacts, target)

    monkeypatch.setattr(media, "concat_clips", spy)
    manager = started.manager
    batch = manager.create_batch("C")
    started.wait_for(
        lambda: started.batch(batch["id"])["status"] == "ready", timeout=10
    )
    assert len(recorded) == 1
    names = [p.name[0] for p in recorded[0]]
    assert names == ["A", "B", "C"]


def test_pick_next_candidate_prioritises_unstarted_segments():
    from app.scheduler import Attempt, Batch, Segment

    settings = type("S", (), {"slow_after_seconds": 2.0})()

    def make_batch(bid, created, segments):
        batch = Batch(
            id=bid,
            slow_clip="A",
            clip_names=("A", "B", "C"),
            work_dir=Path("/tmp"),
            created_at=created,
        )
        for name, segment in segments.items():
            batch.segments[name] = segment
        return batch

    def seg(name, *, first_status="queued", started_at=None, backup_launched=False,
            selected=False):
        s = Segment(name=name, index=ord(name) - 65, slow_first=(name == "A"))
        a = Attempt(id=f"{name}-1", segment=name, kind="first", status=first_status)
        a.started_at = started_at
        s.attempts.append(a)
        s.backup_launched = backup_launched
        if selected:
            s.selected_attempt_id = a.id
        return s

    now = 100.0
    # Batch 1: A is overdue for a backup; B/C have never started.
    batch1 = make_batch(
        "b1",
        1.0,
        {
            "A": seg("A", first_status="running", started_at=now - 5),
            "B": seg("B"),
            "C": seg("C"),
        },
    )
    segment, kind = pick_next_candidate([batch1], settings, now)
    assert (segment.name, kind) == ("B", "first")

    # With B selected and C started long ago, A's backup is the only option.
    batch1.segments["B"].selected_attempt_id = "B-1"
    batch1.segments["C"] = seg("C", first_status="running", started_at=now - 4)
    segment, kind = pick_next_candidate([batch1], settings, now)
    assert (segment.name, kind) == ("A", "backup")

    # A fresh segment in a newer batch still beats the old batch's backup.
    batch2 = make_batch(
        "b2",
        2.0,
        {
            "A": seg("A", started_at=now - 3, first_status="running"),
            "B": seg("B"),
            "C": seg("C"),
        },
    )
    segment, kind = pick_next_candidate([batch2, batch1], settings, now)
    assert (segment.name, kind) == ("B", "first")


# ---------------------------------------------------------------- slot budget


def _mine(batch: dict) -> list[dict]:
    return batch["attempts"]


def test_slot_budget_is_optional_and_blank_keeps_original_flow(started):
    started.write_fake_config({"sleep": 0.02})
    manager = started.manager
    batch = manager.create_batch("A")
    assert batch["slot_budget"] is None
    assert batch["slot_seconds_used"] == 0
    final = started.wait_for(
        lambda: started.batch(batch["id"])
        if started.batch(batch["id"])["status"] == "ready"
        else None,
        timeout=10,
    )
    assert final["download_ready"] is True
    # Usage is still reported for unbudgeted batches, and never blocks them.
    assert final["slot_seconds_used"] > 0
    assert final["budget_exceeded"] is False
    assert final["download_blocked_reason"] is None


def test_two_slots_for_one_second_meter_two_slot_seconds(started):
    # Two segments run concurrently for ~0.5s, then the third; total metered
    # usage should be roughly the sum over attempts, not the batch wall time.
    started.write_fake_config({"sleep": 0.5})
    manager = started.manager
    batch = manager.create_batch("A", slot_budget=120)

    def two_running():
        b = started.batch(batch["id"])
        live = [a for a in b["attempts"] if a["status"] == "running"]
        return b if len(live) == 2 else None

    seen = started.wait_for(two_running, timeout=5)
    assert seen["slots_used"] == 2
    # Live usage grows while both processes hold slots: ~2 slot-seconds per
    # second of wall time while two are live.
    before = seen["slot_seconds_used"]
    time.sleep(0.2)
    after = started.batch(batch["id"])["slot_seconds_used"]
    assert after - before >= 0.3  # ~0.4 expected; generous for CI jitter
    final = started.wait_for(
        lambda: started.batch(batch["id"])
        if started.batch(batch["id"])["status"] in ("ready", "failed", "exceeded")
        else None,
        timeout=10,
    )
    assert final["status"] == "ready"
    per_attempt = [a["slot_seconds"] for a in final["attempts"]]
    assert all(v > 0.3 for v in per_attempt)
    assert abs(final["slot_seconds_used"] - sum(per_attempt)) < 0.05


def test_budget_exhausted_cancels_live_attempts_and_blocks_download(started):
    started.write_fake_config({"sleep": 5.0})
    manager = started.manager
    batch = manager.create_batch("A", slot_budget=1)

    final = started.wait_for(
        lambda: started.batch(batch["id"])
        if started.batch(batch["id"])["status"] == "exceeded"
        else None,
        timeout=6,
    )
    assert final["budget_exceeded"] is True
    assert final["download_ready"] is False
    assert final["download_url"] is None
    assert "预算" in final["error"]
    assert manager.get_batch_file(batch["id"]) is None
    # Live attempts were asked to stop.
    assert all(
        a["status"] in ("cancelling", "cancelled", "failed") for a in final["attempts"]
    )
    assert any(a["cancel_reason"] == "批次槽秒预算已用尽" for a in final["attempts"])

    # Cancelled processes keep metering until they exit, then release.
    drained = started.wait_for(
        lambda: (lambda b: b if not any(a["slot_held"] for a in b["attempts"]) else None)(
            started.batch(batch["id"])
        ),
        timeout=10,
    )
    assert drained["status"] == "exceeded"
    assert drained["slot_seconds_used"] >= 1
    assert manager.slots_used == 0
    # No composed file was ever offered.
    assert manager.get_batch_file(batch["id"]) is None


def test_budgeted_batch_waits_for_all_slots_before_download(started):
    # A's slow first run ignores cancellation long enough that composition is
    # done before the cancelled process exits. With a generous budget the
    # batch must stay "draining" (no download) until the slot is free.
    started.write_fake_config(
        {
            "sleep": 0.02,
            "segments": {"A": {"slow": {"sleep": 1.2, "ignore_term": 0.4}}},
        }
    )
    manager = started.manager
    batch = manager.create_batch("A", slot_budget=60)

    draining = started.wait_for(
        lambda: started.batch(batch["id"])
        if started.batch(batch["id"])["status"] == "draining"
        else None,
        timeout=6,
    )
    assert any(a["slot_held"] for a in draining["attempts"])
    assert draining["download_ready"] is False
    assert "槽" in draining["download_blocked_reason"]
    assert manager.get_batch_file(batch["id"]) is None

    final = started.wait_for(
        lambda: started.batch(batch["id"])
        if started.batch(batch["id"])["status"] in ("ready", "exceeded")
        else None,
        timeout=10,
    )
    assert final["status"] == "ready"
    assert final["slot_seconds_used"] < final["slot_budget"]
    assert not any(a["slot_held"] for a in final["attempts"])
    assert final["download_ready"] is True
    assert manager.get_batch_file(batch["id"]) is not None


def test_overshoot_before_drain_withholds_composed_file(started):
    # Budget 1 slot-second; a fast attempt already uses ~0.5s and a slow one
    # pushes past the budget. The composed file might exist on disk but the
    # final gate must refuse it because confirmed usage exceeded the budget.
    started.write_fake_config(
        {
            "sleep": 0.3,
            "segments": {"A": {"slow": {"sleep": 1.4, "ignore_term": 0.2}}},
        }
    )
    manager = started.manager
    batch = manager.create_batch("A", slot_budget=1)
    final = started.wait_for(
        lambda: started.batch(batch["id"])
        if started.batch(batch["id"])["status"] in ("ready", "failed", "exceeded")
        else None,
        timeout=10,
    )
    assert final["status"] == "exceeded"
    assert final["download_ready"] is False
    assert manager.get_batch_file(batch["id"]) is None


def test_budget_does_not_dispatch_new_work_after_reaching_limit(started):
    # Two batches, the budgeted one fills its budget; the other keeps going.
    started.write_fake_config({"sleep": 0.4})
    manager = started.manager
    budgeted = manager.create_batch("A", slot_budget=1)
    unbudgeted = manager.create_batch("B", slot_budget=None)

    final = started.wait_for(
        lambda: started.batch(budgeted["id"])
        if started.batch(budgeted["id"])["status"] == "exceeded"
        else None,
        timeout=8,
    )
    # No queued attempt in the budgeted batch may remain after the stop.
    assert not any(a["status"] == "queued" for a in final["attempts"])
    # The unbudgeted batch still gets to finish.
    other = started.wait_for(
        lambda: started.batch(unbudgeted["id"])
        if started.batch(unbudgeted["id"])["status"] == "ready"
        else None,
        timeout=15,
    )
    assert other["download_ready"] is True


def test_pick_next_candidate_skips_budget_reached_batches():
    from app.scheduler import Batch, Segment

    settings = type("S", (), {"slow_after_seconds": 2.0})()
    batch = Batch(
        id="b1",
        slow_clip="A",
        clip_names=("A", "B", "C"),
        work_dir=Path("/tmp"),
        slot_budget=1,
    )
    for index, name in enumerate(("A", "B", "C")):
        batch.segments[name] = Segment(name=name, index=index, slow_first=(name == "A"))
    # Charge the budget via a finished attempt.
    from app.scheduler import Attempt

    done = Attempt(id="x", segment="A", kind="first", status="succeeded")
    done.slot_seconds = 1.0
    done.slot_held = False
    batch.segments["A"].attempts.append(done)

    assert pick_next_candidate([batch], settings, 100.0) is None
    # With the budget removed the never-started segments are dispatchable again.
    batch.slot_budget = None
    from app.scheduler import Attempt as _Attempt

    queued = _Attempt(id="q", segment="B", kind="first", status="queued")
    batch.segments["B"].attempts.append(queued)
    chosen = pick_next_candidate([batch], settings, 100.0)
    assert chosen is not None and chosen[0].name == "B"


def test_usage_equal_to_budget_is_exceeded_and_never_downloadable():
    from app.scheduler import Attempt, Batch, Segment

    batch = Batch(
        id="b1",
        slow_clip="A",
        clip_names=("A", "B", "C"),
        work_dir=Path("/tmp"),
        slot_budget=5,
    )
    seg = Segment(name="A", index=0, slow_first=True)
    done = Attempt(id="x", segment="A", kind="first", status="succeeded")
    done.slot_seconds = 4.999
    seg.attempts.append(done)
    batch.segments["A"] = seg
    # Just below the budget: not reached.
    assert batch.budget_reached(100.0) is False
    # Exactly at the budget counts as reached ("用量达到预算").
    done.slot_seconds = 5.0
    assert batch.budget_reached(100.0) is True
    # "Ready" alone is not enough for a budgeted batch at the limit.
    batch.status = "ready"
    batch.final_path = Path("/tmp/does-not-matter.mp4")
    batch.final_path.write_bytes(b"x")
    try:
        assert batch.can_download(100.0) is False
    finally:
        batch.final_path.unlink(missing_ok=True)


def test_live_metering_extends_open_hold_segment():
    from app.scheduler import Attempt

    a = Attempt(id="1", segment="A", kind="first", status="running")
    a.started_at = 100.0
    a.slot_held = True
    a.slot_meter_started_at = 100.0
    assert a.slot_seconds_live(101.5) == 1.5
    # Finishing settles the open segment exactly once.
    a.slot_seconds += 101.5 - a.slot_meter_started_at
    a.slot_meter_started_at = None
    a.slot_held = False
    a.finished_at = 101.5
    assert a.slot_seconds_live(102.0) == 1.5
    assert a.to_dict(102.0)["slot_seconds"] == 1.5
