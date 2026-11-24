import pytest


@pytest.fixture
def client(tmp_path):
    from app.main import create_app
    app = create_app(str(tmp_path / "workbench.sqlite3"))
    app.config.update(TESTING=True)
    return app.test_client()


def register(client, lot_id, qty):
    return client.post("/api/ledger/lots", json={"lot_id": lot_id, "actual_qty": qty})


def test_full_issue_lifecycle_over_http(client):
    assert register(client, "L1", 1000).status_code == 200
    created = client.post("/api/ledger/docs", json={
        "doc_id": "D1", "kind": "SPLIT", "lines": [{"lot_id": "L1", "qty": 600}]})
    assert created.status_code == 200
    assert created.get_json()["doc"]["status"] == "DRAFT"

    signed = client.post("/api/ledger/docs/D1/sign")
    assert signed.status_code == 200
    assert signed.get_json()["outcome"] == "signed"

    board = client.get("/api/ledger/workbench").get_json()
    lot = board["lots"][0]
    assert lot["signed_qty"] == 600
    assert lot["available_qty"] == 400


def test_shortage_blocks_related_doc_but_not_unrelated(client):
    register(client, "L1", 1000)
    register(client, "L2", 500)
    client.post("/api/ledger/docs", json={"doc_id": "OLD", "kind": "SPLIT",
                                           "lines": [{"lot_id": "L1", "qty": 600}]})
    client.post("/api/ledger/docs/OLD/sign")
    corrected = client.post("/api/ledger/lots/L1/corrections", json={"new_qty": 500})
    assert corrected.status_code == 200
    board = corrected.get_json()["workbench"]
    assert board["shortage_lots"] == ["L1"]

    # 涉及缺口父批的新单停签
    client.post("/api/ledger/docs", json={"doc_id": "NEW", "kind": "SPLIT",
                                          "lines": [{"lot_id": "L1", "qty": 10}]})
    blocked = client.post("/api/ledger/docs/NEW/sign")
    assert blocked.status_code == 409
    assert blocked.get_json()["code"] == "INSUFFICIENT_AVAILABLE"

    # 无关父批的单照常签发
    client.post("/api/ledger/docs", json={"doc_id": "OTHER", "kind": "SPLIT",
                                          "lines": [{"lot_id": "L2", "qty": 300}]})
    ok = client.post("/api/ledger/docs/OTHER/sign")
    assert ok.status_code == 200


def test_frozen_evidence_downloads_unchanged_after_correction(client):
    register(client, "L1", 1000)
    client.post("/api/ledger/docs", json={"doc_id": "OLD", "kind": "SPLIT",
                                          "lines": [{"lot_id": "L1", "qty": 600}]})
    client.post("/api/ledger/docs/OLD/sign")
    before = client.get("/api/ledger/docs/OLD/evidence").get_json()
    client.post("/api/ledger/lots/L1/corrections", json={"new_qty": 500})
    after = client.get("/api/ledger/docs/OLD/evidence").get_json()
    assert after == before
    assert after["lot_snapshots"][0]["actual_qty"] == 1000


def test_evidence_409_before_signing(client):
    register(client, "L1", 100)
    client.post("/api/ledger/docs", json={"doc_id": "D1", "kind": "SPLIT",
                                          "lines": [{"lot_id": "L1", "qty": 10}]})
    response = client.get("/api/ledger/docs/D1/evidence")
    assert response.status_code == 409
    assert response.get_json()["code"] == "NOT_SIGNED"


def test_merge_rolls_back_on_single_shortage(client):
    register(client, "L4", 200)
    register(client, "L5", 30)
    client.post("/api/ledger/docs", json={"doc_id": "M1", "kind": "MERGE",
                                          "lines": [{"lot_id": "L4", "qty": 200},
                                                    {"lot_id": "L5", "qty": 40}]})
    response = client.post("/api/ledger/docs/M1/sign")
    assert response.status_code == 409
    board = client.get("/api/ledger/workbench").get_json()
    assert all(lot["signed_qty"] == 0 for lot in board["lots"])


def test_retry_same_doc_is_idempotent_over_http(client):
    register(client, "L1", 100)
    client.post("/api/ledger/docs", json={"doc_id": "D1", "kind": "SPLIT",
                                          "lines": [{"lot_id": "L1", "qty": 60}]})
    first = client.post("/api/ledger/docs/D1/sign")
    second = client.post("/api/ledger/docs/D1/sign")
    assert first.status_code == second.status_code == 200
    assert second.get_json()["outcome"] == "already_signed"
    board = client.get("/api/ledger/workbench").get_json()
    assert board["lots"][0]["signed_qty"] == 60


def test_correction_history_endpoint(client):
    register(client, "L1", 1000)
    client.post("/api/ledger/lots/L1/corrections", json={"new_qty": 500})
    client.post("/api/ledger/lots/L1/corrections", json={"new_qty": 900})
    history = client.get("/api/ledger/lots/L1/corrections").get_json()["corrections"]
    assert [row["new_qty"] for row in history] == [500, 900]


def test_unknown_lot_and_doc_404(client):
    assert client.post("/api/ledger/lots/NOPE/corrections", json={"new_qty": 1}).status_code == 404
    assert client.post("/api/ledger/docs/NOPE/sign").status_code == 404


def test_bad_requests(client):
    assert client.post("/api/ledger/lots", json={"lot_id": "L1"}).status_code == 400
    register(client, "L1", 100)
    # 负数、非整数被拒
    assert client.post("/api/ledger/lots", json={"lot_id": "LX", "actual_qty": -1}).status_code == 400
    assert client.post("/api/ledger/lots", json={"lot_id": "LX", "actual_qty": 1.5}).status_code == 400
    # 拆批多父批被拒
    response = client.post("/api/ledger/docs", json={"doc_id": "BAD", "kind": "SPLIT",
                                                     "lines": [{"lot_id": "L1", "qty": 1},
                                                               {"lot_id": "L2", "qty": 1}]})
    assert response.status_code == 400


def test_seed_endpoint(client):
    response = client.post("/api/ledger/seed", json={"profile": "clean"})
    assert response.status_code == 200
    board = client.get("/api/ledger/workbench").get_json()
    assert {lot["lot_id"] for lot in board["lots"]} >= {"L1", "L2", "L3", "L4", "L5"}
    # 再次 seed 幂等
    again = client.post("/api/ledger/seed", json={"profile": "clean"}).get_json()
    assert again["seeded"] == []
