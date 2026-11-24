import json

from app.errors import DomainIssue
from app.evidence.manifest import stable_digest


def _non_negative_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DomainIssue("BAD_REQUEST", detail=f"{field} 必须是非负整数")
    return value


def _positive_int(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise DomainIssue("BAD_REQUEST", detail=f"{field} 必须是正整数")
    return value


def _parse_lines(lines):
    if not isinstance(lines, list) or not lines:
        raise DomainIssue("BAD_REQUEST", detail="lines 必须是非空数组")
    parsed, seen = [], set()
    for entry in lines:
        if not isinstance(entry, dict) or "lot_id" not in entry or "qty" not in entry:
            raise DomainIssue("BAD_REQUEST", detail="每行需要 lot_id 与 qty")
        lot_id = str(entry["lot_id"]).strip()
        if not lot_id:
            raise DomainIssue("BAD_REQUEST", detail="lot_id 不能为空")
        if lot_id in seen:
            raise DomainIssue("BAD_REQUEST", detail=f"{lot_id} 在同一张单中重复")
        seen.add(lot_id)
        parsed.append((lot_id, _positive_int(entry["qty"], "qty")))
    return parsed


def register_lot(store, lot_id, actual_qty):
    lot_id = str(lot_id or "").strip()
    if not lot_id:
        raise DomainIssue("BAD_REQUEST", detail="lot_id 不能为空")
    _non_negative_int(actual_qty, "actual_qty")
    row, created = store.register_lot(lot_id, actual_qty)
    return {"lot": row, "created": created}


def correct_lot(store, lot_id, new_qty):
    lot_id = str(lot_id or "").strip()
    if not lot_id:
        raise DomainIssue("BAD_REQUEST", detail="lot_id 不能为空")
    _non_negative_int(new_qty, "new_qty")
    row, outcome = store.correct_lot(lot_id, new_qty)
    if outcome == "missing":
        raise DomainIssue("LOT_NOT_FOUND", 404)
    return {"lot": row, "outcome": outcome, "workbench": workbench(store)}


def create_doc(store, doc_id, kind, lines):
    doc_id = str(doc_id or "").strip()
    if not doc_id:
        raise DomainIssue("BAD_REQUEST", detail="doc_id 不能为空")
    if kind not in {"SPLIT", "MERGE"}:
        raise DomainIssue("BAD_REQUEST", detail="kind 必须是 SPLIT 或 MERGE")
    parsed = _parse_lines(lines)
    if kind == "SPLIT" and len(parsed) != 1:
        raise DomainIssue("BAD_REQUEST", detail="拆批单只能占用一个父批")
    if kind == "MERGE" and len(parsed) < 2:
        raise DomainIssue("BAD_REQUEST", detail="合批单至少占用两个父批")
    doc, created = store.create_doc(doc_id, kind, parsed)
    return {"doc": _doc_detail(store, doc_id), "created": created}


def _detail_from_row(doc, lines):
    detail = {k: doc[k] for k in ("doc_id", "kind", "status", "created_at", "signed_at")}
    detail["lines"] = [{"lot_id": lot_id, "qty": qty} for lot_id, qty in lines]
    detail["total_qty"] = sum(item["qty"] for item in detail["lines"])
    if doc["evidence"]:
        detail["evidence"] = json.loads(doc["evidence"])
    return detail


def _doc_detail(store, doc_id):
    doc = store.get_doc(doc_id)
    if doc is None:
        raise DomainIssue("DOC_NOT_FOUND", 404)
    with store.connect() as db:
        return _detail_from_row(doc, store.doc_lines(db, doc_id))


def doc_detail(store, doc_id):
    return _doc_detail(store, doc_id)


def sign_doc(store, doc_id):
    """签发单据：BEGIN IMMEDIATE 下校验全部父批余量后一次性占量。

    - 合批任一父批余量不足：整体回滚，其他父批不留下任何占用。
    - 已 SIGNED 的单重放：直接返回冻结证据，绝不二次占量（幂等重试）。
    - HELD 单需先转回 DRAFT 才能签，这里明确拒绝。
    """
    doc = store.get_doc(doc_id)
    if doc is None:
        raise DomainIssue("DOC_NOT_FOUND", 404)
    if doc["status"] == "HELD":
        raise DomainIssue("DOC_HELD", 409)
    if doc["status"] == "SIGNED":
        return {"doc": _doc_detail(store, doc_id), "outcome": "already_signed"}

    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        # 重新加锁读取，确保拿到最新台账
        locked = db.execute("SELECT * FROM issue_docs WHERE doc_id=?", (doc_id,)).fetchone()
        if locked["status"] == "SIGNED":
            # 并发下已被其他线程签发，复用同事务数据，不二次占量
            lines = store.doc_lines(db, doc_id)
            return {"doc": _detail_from_row(dict(locked), lines), "outcome": "already_signed"}
        lines = store.doc_lines(db, doc_id)
        shortages = []
        snapshots = []
        for lot_id, qty in lines:
            lot = store.get_lot(db, lot_id)
            if lot is None:
                shortages.append({"lot_id": lot_id, "need": qty, "available_qty": 0,
                                  "reason": "LOT_NOT_FOUND"})
                continue
            used = store.signed_usage(db, lot_id)
            available = lot["actual_qty"] - used
            if available < qty:
                shortages.append({"lot_id": lot_id, "need": qty, "available_qty": available,
                                  "reason": "INSUFFICIENT_AVAILABLE"})
            snapshots.append({"lot_id": lot_id, "qty": qty,
                              "actual_qty": lot["actual_qty"], "version": lot["version"],
                              "available_before": available})
        if shortages:
            # 主动回滚，本事务此前没有任何占量写入
            db.rollback()
            raise DomainIssue("INSUFFICIENT_AVAILABLE", 409, detail=json.dumps(
                {"shortages": shortages}, ensure_ascii=False))
        evidence = _build_evidence(locked, lines, snapshots)
        db.execute(
            "UPDATE issue_docs SET status='SIGNED', signed_at=?, evidence=? WHERE doc_id=?",
            (evidence["signed_at"], json.dumps(evidence, ensure_ascii=False), doc_id),
        )
        signed_row = dict(db.execute("SELECT * FROM issue_docs WHERE doc_id=?", (doc_id,)).fetchone())
        detail = _detail_from_row(signed_row, lines)
    return {"doc": detail, "outcome": "signed"}


def _build_evidence(doc, lines, snapshots):
    evidence = {
        "schema": "ledger-evidence/v1",
        "doc_id": doc["doc_id"],
        "kind": doc["kind"],
        "lines": [{"lot_id": lot_id, "qty": qty} for lot_id, qty in lines],
        "total_qty": sum(qty for _, qty in lines),
        # 冻结签发瞬间各父批的实称量与版本；后续更正不改变本快照，保证旧凭证原样可下载
        "lot_snapshots": snapshots,
        "signed_at": _now_iso(),
    }
    evidence["artifact_sha256"] = stable_digest(evidence)
    return evidence


def _now_iso():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def get_evidence(store, doc_id):
    doc = store.get_doc(doc_id)
    if doc is None:
        raise DomainIssue("DOC_NOT_FOUND", 404)
    if doc["status"] != "SIGNED" or not doc["evidence"]:
        raise DomainIssue("NOT_SIGNED", 409)
    return json.loads(doc["evidence"])


def workbench(store):
    return store.workbench()


def seed_from_rows(store, rows):
    """把采信行里的父批（parent_lot 中出现的编号）登记进台账，便于演示与前端初始化。

    实称量取该父批自身行的 qty_out（可对外供应的产出量）；纯父批无自身行时跳过。
    已存在的父批保持不动，接口幂等。
    """
    qty_out = {}
    parent_ids = set()
    for row in rows:
        lot_id = row.get("lot_id")
        if lot_id is not None:
            qty_out[str(lot_id)] = row.get("qty_out")
        spec = row.get("parent_lot") or ""
        for edge in spec.split(";"):
            parent = edge.split(":", 1)[0].strip()
            if parent:
                parent_ids.add(parent)
    seeded, skipped = [], []
    for lot_id in sorted(parent_ids):
        if lot_id not in qty_out or qty_out[lot_id] is None:
            skipped.append(lot_id)
            continue
        result = register_lot(store, lot_id, int(qty_out[lot_id]))
        if result["created"]:
            seeded.append(lot_id)
    return {"seeded": seeded, "already_present": sorted(parent_ids - set(seeded) - set(skipped)),
            "skipped_without_lot_row": skipped}
