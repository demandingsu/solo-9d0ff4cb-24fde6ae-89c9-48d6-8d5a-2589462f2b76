# -*- coding: utf-8 -*-
"""End-to-end HTTP tests driven through the FastAPI test client."""

import csv
import io
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("WQ_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WQ_DB_PATH", str(tmp_path / "data" / "db.sqlite3"))
    monkeypatch.setenv("WQ_EXPORT_ROOT", str(tmp_path / "data" / "exports"))
    # import after env vars are set so the module-level paths pick them up
    import importlib

    import app.main as main

    importlib.reload(main)
    main._service = None
    with TestClient(main.app) as c:
        yield c, main


def _seed_demo(client):
    resp = client.post("/demo", follow_redirects=False)
    assert resp.status_code == 303
    return resp.headers["location"].rsplit("/", 1)[-1]


def test_full_isolate_and_restore_flow(client):
    c, _ = client
    dataset_id = _seed_demo(c)

    # invalid reason is rejected, no revision created
    page = c.get(f"/datasets/{dataset_id}")
    rev_before = page.text.count("当前")
    resp = c.post(
        f"/datasets/{dataset_id}/exclusions",
        data={"seq": "24", "reason": "   "},
    )
    assert resp.status_code == 400
    assert "请填写隔离原因" in resp.text

    # unknown seq rejected
    resp = c.post(
        f"/datasets/{dataset_id}/exclusions",
        data={"seq": "9999", "reason": "清洗"},
    )
    assert resp.status_code == 400
    assert "不在本批次" in resp.text

    # valid isolation
    resp = c.post(
        f"/datasets/{dataset_id}/exclusions",
        data={"seq": "24", "reason": "探头清洗，溶氧读数失真"},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    page_text = resp.text
    assert "已隔离序号 24" in page_text
    assert "探头清洗，溶氧读数失真" in page_text
    assert "excluded.csv" in page_text

    # the isolated seq is absent from every day file
    from app import main as main_mod  # noqa: F811

    service = main_mod.get_service()
    latest = service.store.latest_publication(int(dataset_id))
    for f in service.store.files(latest.id):
        if f["kind"] != "day":
            continue
        data = service.file_path(latest.id, "day", f["day"])[2].read_text()
        seqs = {int(r[0]) for r in csv.reader(io.StringIO(data)) if r and r[0] != "序号"}
        assert 24 not in seqs

    # quarantine download works and has the right header
    download = c.get(f"/p/{latest.id}/excluded.csv")
    assert download.status_code == 200
    rows = list(csv.reader(io.StringIO(download.text)))
    assert rows[0][0] == "序号"
    assert int(rows[1][0]) == 24
    assert rows[1][4] == "探头清洗，溶氧读数失真"

    # pending download must not contain seq 24
    pending = c.get(f"/p/{latest.id}/pending.csv")
    pending_seqs = {
        int(r[0]) for r in csv.reader(io.StringIO(pending.text))
        if r and r[0] != "序号"
    }
    assert 24 not in pending_seqs

    # double isolation rejected
    again = c.post(
        f"/datasets/{dataset_id}/exclusions",
        data={"seq": "24", "reason": "再隔离一次"},
    )
    assert again.status_code == 400
    assert "已处于隔离状态" in again.text

    # while seq 24 stays isolated, newly supplemented anchors on it are
    # accepted into the anchor table but disabled for correction
    from app import core, demo

    service = main_mod.get_service()
    service.add_anchors_and_publish(
        int(dataset_id),
        core.parse_anchors(demo.SUPPLEMENT_ANCHORS_TEXT.encode("utf-8")),
    )
    service.add_anchors_and_publish(
        int(dataset_id),
        core.parse_anchors(demo.MIGRATION_ANCHORS_TEXT.encode("utf-8")),
    )
    state_while_excluded = service.state(int(dataset_id))
    assert any(a.seq == 24 for a in state_while_excluded.anchors)
    assert {a.seq for a in state_while_excluded.result.disabled_anchors} == {24}
    assert 24 not in {r.seq for r in state_while_excluded.result.rows}

    # restore: the anchor reactivates and seq 24 is regenerated from raw data
    resp = c.post(
        f"/datasets/{dataset_id}/exclusions/24/restore",
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "已撤销序号 24" in resp.text
    assert "excluded.csv（隔离记录）" in resp.text
    # no excluded file registered for the new revision
    service = main_mod.get_service()
    new_latest = service.store.latest_publication(int(dataset_id))
    assert not [f for f in service.store.files(new_latest.id) if f["kind"] == "excluded"]
    # seq 24 is dated again, regenerated from the original data
    state = service.state(int(dataset_id))
    assert 24 in {r.seq for r in state.result.rows}
    assert state.result.excluded == []

    # downloading excluded.csv from the new revision is a clean 404
    missing = c.get(f"/p/{new_latest.id}/excluded.csv")
    assert missing.status_code == 404


def test_anchor_interleaving_keeps_one_coherent_revision(client):
    c, main_mod = client
    dataset_id = _seed_demo(c)
    service = main_mod.get_service()

    # supplement anchors
    from app import core, demo

    anchors = core.parse_anchors(demo.SUPPLEMENT_ANCHORS_TEXT.encode("utf-8"))
    pub2, _ = service.add_anchors_and_publish(int(dataset_id), anchors)

    # isolate a reading that carries an anchor (seq 13)
    resp = c.post(
        f"/datasets/{dataset_id}/exclusions",
        data={"seq": "13", "reason": "清洗失真"},
    )
    assert resp.status_code == 200
    latest = service.store.latest_publication(int(dataset_id))
    assert latest.revision == pub2.revision + 1

    # page links and correction state all come from the same revision
    page = c.get(f"/datasets/{dataset_id}")
    assert f"/p/{latest.id}/excluded.csv" in page.text
    state = service.state(int(dataset_id))
    assert state.latest.id == latest.id
    assert state.result.excluded_seqs == [13]
    assert {a.seq for a in state.result.disabled_anchors} == {13}
    # anchor row itself is still on record
    assert any(a.seq == 13 for a in state.anchors)
    # segment 2 now has a single usable anchor (36): only 36 stays dated
    seg2_rows = {r.seq for r in state.result.rows if r.segment == 1}
    assert seg2_rows == {36}


def test_old_links_keep_working_after_isolation(client):
    c, main_mod = client
    dataset_id = _seed_demo(c)
    service = main_mod.get_service()
    first = service.store.latest_publication(int(dataset_id))
    first_manifest = c.get(f"/p/{first.id}/manifest.csv").content

    c.post(
        f"/datasets/{dataset_id}/exclusions",
        data={"seq": "12", "reason": "清洗"},
    )
    # old manifest bytes unchanged and still served through the old link
    assert c.get(f"/p/{first.id}/manifest.csv").content == first_manifest


def test_healthz(client):
    c, _ = client
    assert c.get("/healthz").text == "ok"
