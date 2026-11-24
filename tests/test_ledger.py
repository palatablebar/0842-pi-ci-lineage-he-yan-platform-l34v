import pytest

from app.errors import DomainIssue
from app.ledger.service import (
    correct_lot,
    create_doc,
    get_evidence,
    register_lot,
    seed_from_rows,
    sign_doc,
    workbench,
)
from app.ledger.store import LedgerStore


@pytest.fixture
def store(tmp_path):
    return LedgerStore(tmp_path / "ledger.sqlite3")


def make_split(store, doc_id="D1", lot_id="L1", qty=300):
    return create_doc(store, doc_id, "SPLIT", [{"lot_id": lot_id, "qty": qty}])


def test_register_and_workbench_baseline(store):
    register_lot(store, "L1", 1000)
    board = workbench(store)
    lot = board["lots"][0]
    assert lot["actual_qty"] == 1000
    assert lot["signed_qty"] == 0
    assert lot["available_qty"] == 1000
    assert lot["shortage"] == 0
    assert lot["in_shortage"] is False


def test_split_sign_consumes_parent_availability(store):
    register_lot(store, "L1", 1000)
    make_split(store, "D1", "L1", 600)
    result = sign_doc(store, "D1")
    assert result["outcome"] == "signed"
    lot = workbench(store)["lots"][0]
    assert lot["signed_qty"] == 600
    assert lot["available_qty"] == 400


def test_draft_does_not_consume_availability(store):
    register_lot(store, "L1", 100)
    make_split(store, "D1", "L1", 100)
    # 未签发的草稿不占量
    assert workbench(store)["lots"][0]["available_qty"] == 100


def test_sign_below_available_is_blocked_and_leaves_no_usage(store):
    register_lot(store, "L1", 100)
    make_split(store, "D1", "L1", 120)
    with pytest.raises(DomainIssue) as exc:
        sign_doc(store, "D1")
    assert exc.value.code == "INSUFFICIENT_AVAILABLE"
    assert exc.value.status == 409
    lot = workbench(store)["lots"][0]
    # 签发失败不留任何占用
    assert lot["signed_qty"] == 0
    assert lot["available_qty"] == 100
    doc = workbench(store)["docs"][0]
    assert doc["status"] == "DRAFT"
    assert doc["signable"] is False
    assert doc["blocked_by"][0]["missing"] == 20


def test_merge_is_all_or_nothing(store):
    register_lot(store, "L4", 200)
    register_lot(store, "L5", 30)  # 需要 40，余量不足
    create_doc(store, "M1", "MERGE", [{"lot_id": "L4", "qty": 200}, {"lot_id": "L5", "qty": 40}])
    with pytest.raises(DomainIssue):
        sign_doc(store, "M1")
    board = workbench(store)
    usage = {lot["lot_id"]: lot["signed_qty"] for lot in board["lots"]}
    # 余量充足的 L4 也不能留下占用
    assert usage == {"L4": 0, "L5": 0}
    assert workbench(store)["docs"][0]["status"] == "DRAFT"


def test_merge_succeeds_when_every_parent_covers(store):
    register_lot(store, "L4", 200)
    register_lot(store, "L5", 40)
    create_doc(store, "M1", "MERGE", [{"lot_id": "L4", "qty": 200}, {"lot_id": "L5", "qty": 40}])
    assert sign_doc(store, "M1")["outcome"] == "signed"
    board = workbench(store)
    assert {lot["lot_id"]: lot["signed_qty"] for lot in board["lots"]} == {"L4": 200, "L5": 40}


def test_retry_after_failure_does_not_double_reserve(store):
    register_lot(store, "L1", 100)
    make_split(store, "D1", "L1", 80)
    sign_doc(store, "D1")
    # 第二张单需要 30，只剩 20，先失败
    make_split(store, "D2", "L1", 30)
    for _ in range(3):
        with pytest.raises(DomainIssue):
            sign_doc(store, "D2")
    lot = workbench(store)["lots"][0]
    assert lot["signed_qty"] == 80  # 多次失败重试不会重复占量
    assert lot["available_qty"] == 20
    # 更正补量后重试同一单据应成功且只占一次
    correct_lot(store, "L1", 110)
    sign_doc(store, "D2")
    lot = workbench(store)["lots"][0]
    assert lot["signed_qty"] == 110
    assert lot["available_qty"] == 0


def test_signed_doc_replay_is_idempotent(store):
    register_lot(store, "L1", 100)
    make_split(store, "D1", "L1", 60)
    first = sign_doc(store, "D1")
    second = sign_doc(store, "D1")
    assert second["outcome"] == "already_signed"
    assert first["doc"]["evidence"]["artifact_sha256"] == second["doc"]["evidence"]["artifact_sha256"]
    assert workbench(store)["lots"][0]["signed_qty"] == 60


def test_correction_drives_new_issue_but_frozen_evidence_stays(store):
    register_lot(store, "L1", 1000)
    make_split(store, "OLD", "L1", 600)
    sign_doc(store, "OLD")
    old_evidence = get_evidence(store, "OLD")
    assert old_evidence["lot_snapshots"][0]["actual_qty"] == 1000
    assert old_evidence["lot_snapshots"][0]["version"] == 1

    # 实称量更正为 500：已签 600 > 500，出现缺口
    result = correct_lot(store, "L1", 500)
    assert result["lot"]["version"] == 2
    board = workbench(store)
    lot = next(item for item in board["lots"] if item["lot_id"] == "L1")
    assert lot["signed_qty"] == 600
    assert lot["available_qty"] == -100
    assert lot["shortage"] == 100
    assert lot["in_shortage"] is True
    assert board["shortage_lots"] == ["L1"]

    # 旧凭证仍可原样下载，内容与摘要都不变
    assert get_evidence(store, "OLD") == old_evidence

    # 涉及该父批的新单停签
    make_split(store, "NEW", "L1", 1)
    with pytest.raises(DomainIssue) as exc:
        sign_doc(store, "NEW")
    assert exc.value.code == "INSUFFICIENT_AVAILABLE"


def test_unrelated_lot_keeps_signing_during_shortage(store):
    register_lot(store, "L1", 1000)
    register_lot(store, "L2", 500)
    make_split(store, "OLD", "L1", 600)
    sign_doc(store, "OLD")
    correct_lot(store, "L1", 500)  # L1 缺口，但不影响 L2
    make_split(store, "OTHER", "L2", 300)
    assert sign_doc(store, "OTHER")["outcome"] == "signed"
    board = workbench(store)
    l2 = next(item for item in board["lots"] if item["lot_id"] == "L2")
    assert l2["available_qty"] == 200


def test_correction_history_is_recorded(store):
    register_lot(store, "L1", 1000)
    correct_lot(store, "L1", 500)
    correct_lot(store, "L1", 900)
    history = store.list_corrections("L1")
    assert [(row["old_qty"], row["new_qty"], row["old_version"], row["new_version"]) for row in history] == [
        (1000, 500, 1, 2),
        (500, 900, 2, 3),
    ]


def test_correct_unknown_lot_raises(store):
    with pytest.raises(DomainIssue) as exc:
        correct_lot(store, "NOPE", 10)
    assert exc.value.code == "LOT_NOT_FOUND"


def test_register_is_idempotent(store):
    first = register_lot(store, "L1", 1000)
    second = register_lot(store, "L1", 1000)
    assert first["created"] is True
    assert second["created"] is False
    assert second["lot"]["actual_qty"] == 1000


def test_create_doc_is_idempotent(store):
    register_lot(store, "L1", 1000)
    lines = [{"lot_id": "L1", "qty": 100}]
    first = create_doc(store, "D1", "SPLIT", lines)
    second = create_doc(store, "D1", "SPLIT", lines)
    assert first["created"] is True
    assert second["created"] is False


def test_split_requires_single_parent(store):
    with pytest.raises(DomainIssue):
        create_doc(store, "BAD", "SPLIT", [{"lot_id": "L1", "qty": 1}, {"lot_id": "L2", "qty": 1}])


def test_merge_requires_multiple_parents(store):
    with pytest.raises(DomainIssue):
        create_doc(store, "BAD", "MERGE", [{"lot_id": "L1", "qty": 1}])


def test_duplicate_parent_line_rejected(store):
    with pytest.raises(DomainIssue):
        create_doc(store, "BAD", "MERGE", [{"lot_id": "L1", "qty": 1}, {"lot_id": "L1", "qty": 2}])


def test_evidence_only_after_signing(store):
    register_lot(store, "L1", 100)
    make_split(store, "D1", "L1", 10)
    with pytest.raises(DomainIssue) as exc:
        get_evidence(store, "D1")
    assert exc.value.code == "NOT_SIGNED"


def test_evidence_snapshot_contains_frozen_quantities(store):
    register_lot(store, "L4", 200)
    register_lot(store, "L5", 40)
    create_doc(store, "M1", "MERGE", [{"lot_id": "L4", "qty": 200}, {"lot_id": "L5", "qty": 40}])
    sign_doc(store, "M1")
    evidence = get_evidence(store, "M1")
    assert evidence["schema"] == "ledger-evidence/v1"
    assert evidence["total_qty"] == 240
    assert {item["lot_id"]: item["actual_qty"] for item in evidence["lot_snapshots"]} == {"L4": 200, "L5": 40}
    assert len(evidence["artifact_sha256"]) == 64
    # 更正 L4 后证据摘要不变
    correct_lot(store, "L4", 150)
    assert get_evidence(store, "M1")["artifact_sha256"] == evidence["artifact_sha256"]


def test_seed_from_sample_rows(store):
    from app.intake.service import load
    rows = load("clean")["rows"]
    result = seed_from_rows(store, rows)
    board = workbench(store)
    ids = {lot["lot_id"] for lot in board["lots"]}
    # clean.csv 中作为父批出现且自身有产出行的编号
    assert {"L1", "L2", "L3", "L4", "L5"} <= ids
    assert "seeded" in result
    # 幂等：再跑一次不会新建
    again = seed_from_rows(store, rows)
    assert again["seeded"] == []
