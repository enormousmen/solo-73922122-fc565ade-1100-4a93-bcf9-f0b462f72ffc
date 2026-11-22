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
