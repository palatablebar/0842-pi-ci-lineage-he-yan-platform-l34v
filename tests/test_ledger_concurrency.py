import threading

import pytest

from app.errors import DomainIssue
from app.ledger.service import create_doc, register_lot, sign_doc, workbench
from app.ledger.store import LedgerStore


@pytest.fixture
def store(tmp_path):
    return LedgerStore(tmp_path / "ledger.sqlite3")


def _try_sign(store, doc_id, outcomes, index):
    try:
        sign_doc(store, doc_id)
        outcomes[index] = "ok"
    except DomainIssue as exc:
        outcomes[index] = exc.code
    except Exception as exc:  # pragma: no cover - 锁错误也算失败，需在断言中暴露
        outcomes[index] = f"ERROR:{type(exc).__name__}"


def test_concurrent_split_docs_never_overdraw_parent(store):
    register_lot(store, "L1", 100)
    # 两张各需 60 的拆批单：合计 120 > 100，必须恰好一张成功
    create_doc(store, "C1", "SPLIT", [{"lot_id": "L1", "qty": 60}])
    create_doc(store, "C2", "SPLIT", [{"lot_id": "L1", "qty": 60}])
    outcomes = [None, None]
    threads = [
        threading.Thread(target=_try_sign, args=(store, "C1", outcomes, 0)),
        threading.Thread(target=_try_sign, args=(store, "C2", outcomes, 1)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["INSUFFICIENT_AVAILABLE", "ok"]
    lot = workbench(store)["lots"][0]
    assert lot["signed_qty"] == 60
    assert lot["available_qty"] == 40


def test_concurrent_exact_fit_docs_either_combination_respects_limit(store):
    register_lot(store, "L1", 100)
    # 50+50=100 恰好可用，两张都应成功
    create_doc(store, "E1", "SPLIT", [{"lot_id": "L1", "qty": 50}])
    create_doc(store, "E2", "SPLIT", [{"lot_id": "L1", "qty": 50}])
    outcomes = [None, None]
    threads = [
        threading.Thread(target=_try_sign, args=(store, "E1", outcomes, 0)),
        threading.Thread(target=_try_sign, args=(store, "E2", outcomes, 1)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert outcomes == ["ok", "ok"]
    assert workbench(store)["lots"][0]["signed_qty"] == 100


def test_concurrent_merge_and_split_share_parent_without_overdraw(store):
    register_lot(store, "L1", 100)
    register_lot(store, "L2", 100)
    # 合批占用 L1:80 + L2:20；拆批占用 L1:30，L1 合计 110 > 100
    create_doc(store, "MERGE1", "MERGE", [{"lot_id": "L1", "qty": 80}, {"lot_id": "L2", "qty": 20}])
    create_doc(store, "SPLIT1", "SPLIT", [{"lot_id": "L1", "qty": 30}])
    outcomes = [None, None]
    threads = [
        threading.Thread(target=_try_sign, args=(store, "MERGE1", outcomes, 0)),
        threading.Thread(target=_try_sign, args=(store, "SPLIT1", outcomes, 1)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    board = workbench(store)
    l1 = next(lot for lot in board["lots"] if lot["lot_id"] == "L1")
    l2 = next(lot for lot in board["lots"] if lot["lot_id"] == "L2")
    # 若合批失败则两张父批都不占（原子性）；若合批成功则拆批必失败
    if outcomes[0] == "ok":
        assert outcomes[1] == "INSUFFICIENT_AVAILABLE"
        assert l1["signed_qty"] == 80
        assert l2["signed_qty"] == 20
    else:
        assert outcomes[0] == "INSUFFICIENT_AVAILABLE"
        assert outcomes[1] == "ok"
        assert l1["signed_qty"] == 30
        assert l2["signed_qty"] == 0  # 合批回滚，L2 不留占用
    # 总量始终不超父批实称量
    assert l1["signed_qty"] <= 100
    assert l2["signed_qty"] <= 100


def test_concurrent_retry_of_same_doc_reserves_once(store):
    register_lot(store, "L1", 100)
    create_doc(store, "R1", "SPLIT", [{"lot_id": "L1", "qty": 40}])
    outcomes = [None] * 6
    threads = [threading.Thread(target=_try_sign, args=(store, "R1", outcomes, i)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # 首个签发成功，其余全部走幂等 already_signed（服务层返回 ok），绝不重复占量
    assert workbench(store)["lots"][0]["signed_qty"] == 40
    assert all(item in {"ok"} for item in outcomes)
