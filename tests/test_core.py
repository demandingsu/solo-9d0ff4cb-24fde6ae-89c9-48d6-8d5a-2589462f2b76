# -*- coding: utf-8 -*-
"""Tests for the correction engine with quality-control isolation."""

from datetime import datetime, timedelta, timezone

import pytest

from app import core

UTC = timezone.utc


def _reading(seq: int, minute: int, do: float = 6.0) -> core.Reading:
    return core.Reading(
        seq,
        datetime(2026, 3, 1, 20, 0, tzinfo=UTC) + timedelta(minutes=minute),
        do,
    )


def _rollback_readings() -> list[core.Reading]:
    """Two segments: seq 1-5 increasing, seq 6 jumps back an hour."""
    rows = [_reading(i, 10 * (i - 1)) for i in range(1, 6)]
    rows += [
        core.Reading(
            i,
            datetime(2026, 3, 1, 19, 0, tzinfo=UTC)
            + timedelta(minutes=10 * (i - 6)),
            7.0,
        )
        for i in range(6, 10)
    ]
    return rows


def test_split_uses_full_sequence_even_when_middle_reading_isolated():
    readings = _rollback_readings()
    anchors = [
        core.AnchorInput(1, datetime(2026, 3, 1, 20, 0, tzinfo=UTC)),
        core.AnchorInput(5, datetime(2026, 3, 1, 20, 40, tzinfo=UTC)),
        core.AnchorInput(6, datetime(2026, 3, 1, 21, 0, tzinfo=UTC)),
        core.AnchorInput(9, datetime(2026, 3, 1, 21, 30, tzinfo=UTC)),
    ]
    # isolating seq 5 (the last of segment 1) must not merge the two segments
    result = core.correct(
        readings, anchors, [core.Exclusion(5, "cleaning")]
    )
    assert len(result.segments) == 2
    assert result.excluded_seqs == [5]
    # seq 5 is neither dated nor pending
    assert 5 not in {r.seq for r in result.rows}
    assert 5 not in result.pending_seqs
    # anchor on seq 5 is retained but disabled, so segment 1 has only anchor 1
    assert {a.seq for a in result.disabled_anchors} == {5}
    seg1 = result.segments[0]
    assert {a.seq for a in seg1.anchors} == {1}
    # no cross-segment interpolation: segment 2 still has its own anchors
    seg2 = result.segments[1]
    assert {a.seq for a in seg2.anchors} == {6, 9}
    seg2_dated = {r.seq: r for r in result.rows if r.segment == 1}
    assert set(seg2_dated) == {6, 7, 8, 9}


def test_excluded_reading_never_dated_or_pending_and_anchor_disabled():
    readings = [_reading(i, 10 * (i - 1)) for i in range(1, 6)]
    anchors = [
        core.AnchorInput(1, datetime(2026, 3, 1, 20, 0, tzinfo=UTC)),
        core.AnchorInput(3, datetime(2026, 3, 1, 20, 20, tzinfo=UTC)),
        core.AnchorInput(5, datetime(2026, 3, 1, 20, 40, tzinfo=UTC)),
    ]
    result = core.correct(readings, anchors, [core.Exclusion(3, "probe wash")])
    excluded = result.excluded
    assert len(excluded) == 1
    assert excluded[0].seq == 3
    assert excluded[0].reason == "probe wash"
    assert excluded[0].has_anchor is True
    assert {a.seq for a in result.disabled_anchors} == {3}
    dated_seqs = {r.seq for r in result.rows}
    assert 3 not in dated_seqs
    assert 3 not in result.pending_seqs
    # remaining readings corrected between the still-valid anchors 1 and 5
    assert dated_seqs == {1, 2, 4, 5}
    row2 = next(r for r in result.rows if r.seq == 2)
    row4 = next(r for r in result.rows if r.seq == 4)
    # linear interpolation across anchors 1 and 5 over 40 device minutes
    assert row2.utc == datetime(2026, 3, 1, 20, 10, tzinfo=UTC)
    assert row4.utc == datetime(2026, 3, 1, 20, 30, tzinfo=UTC)


def test_exclusion_without_anchors_leaves_segment_pending():
    readings = _rollback_readings()
    result = core.correct(readings, [], [core.Exclusion(7, "noise")])
    assert len(result.segments) == 2
    assert result.excluded_seqs == [7]
    assert result.rows == []
    assert result.pending_seqs == [1, 2, 3, 4, 5, 6, 8, 9]
    assert result.disabled_anchors == []


def test_restore_brings_reading_and_anchor_back():
    readings = [_reading(i, 10 * (i - 1)) for i in range(1, 4)]
    anchors = [
        core.AnchorInput(1, datetime(2026, 3, 1, 20, 0, tzinfo=UTC)),
        core.AnchorInput(3, datetime(2026, 3, 1, 20, 20, tzinfo=UTC)),
    ]
    excluded = core.correct(readings, anchors, [core.Exclusion(2, "wash")])
    assert excluded.excluded_seqs == [2]
    restored = core.correct(readings, anchors, [])
    assert restored.excluded == []
    assert restored.disabled_anchors == []
    assert {r.seq for r in restored.rows} == {1, 2, 3}
    row2 = next(r for r in restored.rows if r.seq == 2)
    assert row2.utc == datetime(2026, 3, 1, 20, 10, tzinfo=UTC)


def test_exclusion_unknown_seq_is_ignored():
    readings = [_reading(1, 0)]
    result = core.correct(readings, [], [core.Exclusion(99, "nope")])
    assert result.excluded == []


def test_normalize_reason_validation():
    assert core.normalize_reason("  清洗探头  ") == "清洗探头"
    with pytest.raises(core.InputError):
        core.normalize_reason("")
    with pytest.raises(core.InputError):
        core.normalize_reason("a\nb")
    with pytest.raises(core.InputError):
        core.normalize_reason("x" * (core.MAX_REASON_LEN + 1))


def test_render_excluded_csv_cells():
    item = core.ExcludedReading(
        seq=3,
        device_time=datetime(2026, 3, 1, 20, 20, tzinfo=UTC),
        dissolved_oxygen=5.25,
        reason="清洗",
        segment=0,
        has_anchor=True,
        isolated_at="2026-11-16T01:02:03.000000Z",
    )
    cells = core.render_excluded(item)
    assert cells[0] == "3"
    assert cells[3] == "1"
    assert cells[4] == "清洗"
    assert cells[6] == "锚点已停用"
    assert core.EXCLUDED_HEADER[0] == "序号"
