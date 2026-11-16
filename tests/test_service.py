# -*- coding: utf-8 -*-
"""Service / persistence tests: atomicity, exports and failure recovery."""

import csv
import io

import pytest

from app import core, demo, publish
from app.service import IsolationError, Service


READINGS_CSV = demo.readings_csv()


@pytest.fixture()
def service(tmp_path):
    svc = Service(tmp_path / "db.sqlite3", tmp_path / "exports")
    raw = READINGS_CSV.encode("utf-8")
    readings = core.parse_readings(raw)
    anchors = core.parse_anchors(demo.INITIAL_ANCHORS_CSV.encode("utf-8"))
    svc.ingest(raw, readings, anchors, "批次甲")
    return svc


def _csv_rows(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode("utf-8"))))


def _file_bytes(svc: Service, publication_id: int, kind: str, day=None) -> bytes:
    _, row, path = svc.file_path(publication_id, kind, day)
    return path.read_bytes()


def test_isolate_publishes_new_revision_and_hides_reading_everywhere(service):
    first = service.store.latest_publication(1)
    assert first.excluded_count == 0

    # seq 24 is pending in the initial demo state: isolation moves it out of
    # pending, dated count is unaffected
    publication = service.isolate_and_publish(1, 24, "探头清洗，溶氧失真")
    assert publication.revision == first.revision + 1
    assert publication.excluded_count == 1
    assert publication.dated_count == first.dated_count
    assert publication.pending_count == first.pending_count - 1

    # isolating a dated reading (seq 12, itself an anchor) removes it from
    # the day files and disables its anchor: segment 1 keeps only anchor 1,
    # which alone can date just itself
    pub2 = service.isolate_and_publish(1, 12, "清洗锚点读数")
    assert pub2.excluded_count == 2
    assert pub2.dated_count == 1

    state = service.state(1)
    assert state.result.excluded_seqs == [12, 24]
    assert {a.seq for a in state.result.disabled_anchors} == {12}
    # segment 1's only usable anchor is now seq 1, which alone dates itself
    assert {r.seq for r in state.result.rows if r.segment == 0} == {1}
    # not in day files
    for date in state.result.dates():
        seqs = {
            int(r[0])
            for r in _csv_rows(
                _file_bytes(service, pub2.id, "day", date)
            )[1:]
        }
        assert 24 not in seqs and 12 not in seqs
    # not in pending
    pending_seqs = {
        int(r[0])
        for r in _csv_rows(_file_bytes(service, pub2.id, "pending"))[1:]
    }
    assert 24 not in pending_seqs and 12 not in pending_seqs
    # quarantine file exists with the reasons and original values
    excluded_rows = _csv_rows(
        _file_bytes(service, pub2.id, "excluded")
    )
    assert excluded_rows[0] == core.EXCLUDED_HEADER
    assert len(excluded_rows) == 3
    reasons = {int(r[0]): r[4] for r in excluded_rows[1:]}
    assert reasons == {12: "清洗锚点读数", 24: "探头清洗，溶氧失真"}
    # manifest records the isolated count
    manifest = _file_bytes(service, pub2.id, "manifest").decode("utf-8")
    assert "隔离读数" in manifest and ",2" in manifest


def test_old_revision_link_still_serves_original_bytes(service):
    first = service.store.latest_publication(1)
    before = {
        f["relpath"]: publish.verify(
            publish.resolve(
                service.export_root, 1, first.revision, f["relpath"]
            ),
            f["sha256"],
        )
        for f in service.store.files(first.id)
    }
    assert all(before.values())

    service.isolate_and_publish(1, 12, "清洗")
    # old revision untouched, still resolvable with its recorded hashes
    for f in service.store.files(first.id):
        path = publish.resolve(
            service.export_root, 1, first.revision, f["relpath"]
        )
        assert publish.verify(path, f["sha256"])
    old_pending = _file_bytes(service, first.id, "pending").decode("utf-8")
    # revision 1 had no excluded file
    with pytest.raises(Exception):
        service.file_path(first.id, "excluded", None)
    assert "excluded.csv" not in old_pending


def test_anchor_on_isolated_reading_is_disabled_but_kept(service):
    # add an anchor on seq 24 first, then isolate seq 24
    service.add_anchors_and_publish(
        1,
        core.parse_anchors(demo.MIGRATION_ANCHORS_TEXT.encode("utf-8")),
    )
    state_before = service.state(1)
    assert any(a.seq == 24 for a in state_before.anchors)

    service.isolate_and_publish(1, 24, "清洗")
    state = service.state(1)
    # anchor row retained ...
    assert any(a.seq == 24 for a in state.anchors)
    # ... but reported disabled and absent from the segment anchor list
    assert {a.seq for a in state.result.disabled_anchors} == {24}
    for seg in state.result.segments:
        assert all(a.seq != 24 for a in seg.anchors)
    assert 24 not in {r.seq for r in state.result.rows}

    # lifting the isolation re-enables the anchor and regenerates from raw data
    restored = service.restore_and_publish(1, 24)
    state_after = service.state(1)
    assert state_after.result.excluded == []
    assert state_after.result.disabled_anchors == []
    assert 24 in {r.seq for r in state_after.result.rows}
    assert restored.excluded_count == 0
    excluded_links = [
        f for f in service.store.files(restored.id) if f["kind"] == "excluded"
    ]
    assert excluded_links == []


def test_invalid_isolation_requests_change_nothing(service):
    before_rev = service.store.latest_publication(1).revision
    with pytest.raises(IsolationError):
        service.isolate_and_publish(1, 999, "不存在的序号")
    with pytest.raises(IsolationError):
        service.isolate_and_publish(1, 24, "first")
        service.isolate_and_publish(1, 24, "again")
    with pytest.raises(core.InputError):
        service.isolate_and_publish(1, 5, "  ")
    # one successful isolation happened in the middle, so revision is +1 only
    assert service.store.latest_publication(1).revision == before_rev + 1

    with pytest.raises(IsolationError):
        service.restore_and_publish(1, 25)
    assert service.store.latest_publication(1).revision == before_rev + 1


def test_publish_failure_keeps_old_version_and_rolls_back_exclusion(
    service, monkeypatch
):
    first = service.store.latest_publication(1)
    original_build = publish.build_staging

    def broken_build(root, dataset_id, revision, result, anchors):
        staged = original_build(root, dataset_id, revision, result, anchors)
        raise publish.PublishError("simulated disk failure")

    monkeypatch.setattr(publish, "build_staging", broken_build)
    with pytest.raises(publish.PublishError):
        service.isolate_and_publish(1, 24, "清洗")

    # transaction rolled back: no exclusion row, no new revision
    assert service.store.exclusions(1) == []
    assert service.store.latest_publication(1).id == first.id
    state = service.state(1)
    assert state.result.excluded == []
    # old files still verify
    for f in service.store.files(first.id):
        path = publish.resolve(
            service.export_root, 1, first.revision, f["relpath"]
        )
        assert publish.verify(path, f["sha256"])


def test_registration_failure_quarantines_directory_and_keeps_old(service, monkeypatch):
    first = service.store.latest_publication(1)

    def broken_insert(*args, **kwargs):
        raise RuntimeError("simulated db failure")

    monkeypatch.setattr(service.store, "insert_publication", broken_insert)
    with pytest.raises(RuntimeError):
        service.isolate_and_publish(1, 24, "清洗")

    assert service.store.exclusions(1) == []
    assert service.store.latest_publication(1).id == first.id
    # the renamed-but-unregistered directory was moved out of the namespace
    pub_dirs = [
        p.name for p in service.export_root.glob("ds-*-rev-*") if p.is_dir()
    ]
    assert pub_dirs == [publish.dir_name(1, first.revision)]
    orphans = list((service.export_root / ".staging").glob("orphan-*"))
    assert orphans


def test_supplement_anchor_and_isolation_interleave_in_one_chain(service):
    # supplement anchors -> revision 2; isolate -> revision 3; the page state
    # and every link must describe the same committed revision
    pub2, added = service.add_anchors_and_publish(
        1,
        core.parse_anchors(demo.SUPPLEMENT_ANCHORS_TEXT.encode("utf-8")),
    )
    assert len(added) == 2
    pub3 = service.isolate_and_publish(1, 24, "清洗")
    assert pub3.revision == pub2.revision + 1

    state = service.state(1)
    assert state.latest.id == pub3.id
    kinds = {f["kind"] for f in service.store.files(pub3.id)}
    assert {"day", "pending", "excluded", "manifest"} <= kinds
    # revision 2 never had an excluded file and stays byte-stable
    rev2_kinds = {f["kind"] for f in service.store.files(pub2.id)}
    assert "excluded" not in rev2_kinds


def test_raw_csv_is_never_modified(service):
    ds = service.dataset(1)
    raw_path = service.export_root / ds.raw_path
    before = raw_path.read_bytes()
    service.isolate_and_publish(1, 24, "清洗")
    service.restore_and_publish(1, 24)
    assert raw_path.read_bytes() == before
    assert service.store.readings(1) == core.parse_readings(before)


def test_republish_after_restart_reuses_committed_revisions(service, tmp_path):
    service.isolate_and_publish(1, 24, "清洗")
    revisions_before = [p.revision for p in service.store.publications(1)]

    reopened = Service(tmp_path / "db.sqlite3", tmp_path / "exports")
    assert reopened.recovered == 0
    assert [p.revision for p in reopened.store.publications(1)] == revisions_before
    state = reopened.state(1)
    assert state.result.excluded_seqs == [24]
