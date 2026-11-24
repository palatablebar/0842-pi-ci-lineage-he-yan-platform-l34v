import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _now():
    return datetime.now(timezone.utc).isoformat()


class LedgerStore:
    """父批余量台账与签发单据的 SQLite 存储。

    所有写操作在单个连接上以 BEGIN IMMEDIATE 取得保留锁（SQLite 库级写锁），
    保证并发签发串行化，余量校验与占量写入处于同一事务，不会超用。
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init()

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def init(self):
        with self.connect() as db:
            db.executescript(
                """
            CREATE TABLE IF NOT EXISTS parent_lots(
              lot_id TEXT PRIMARY KEY,
              actual_qty INTEGER NOT NULL CHECK(actual_qty >= 0),
              version INTEGER NOT NULL DEFAULT 1,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS lot_corrections(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              lot_id TEXT NOT NULL,
              old_qty INTEGER NOT NULL,
              new_qty INTEGER NOT NULL,
              old_version INTEGER NOT NULL,
              new_version INTEGER NOT NULL,
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS issue_docs(
              doc_id TEXT PRIMARY KEY,
              kind TEXT NOT NULL CHECK(kind IN ('SPLIT', 'MERGE')),
              status TEXT NOT NULL DEFAULT 'DRAFT' CHECK(status IN ('DRAFT', 'SIGNED', 'HELD')),
              created_at TEXT NOT NULL,
              signed_at TEXT,
              evidence TEXT
            );
            CREATE TABLE IF NOT EXISTS reservations(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              doc_id TEXT NOT NULL,
              lot_id TEXT NOT NULL,
              qty INTEGER NOT NULL CHECK(qty > 0),
              UNIQUE(doc_id, lot_id),
              FOREIGN KEY(doc_id) REFERENCES issue_docs(doc_id),
              FOREIGN KEY(lot_id) REFERENCES parent_lots(lot_id)
            );
            """
            )

    # ---------- 父批 ----------
    def get_lot(self, db, lot_id):
        return db.execute("SELECT * FROM parent_lots WHERE lot_id=?", (lot_id,)).fetchone()

    def register_lot(self, lot_id, actual_qty):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = self.get_lot(db, lot_id)
            if existing:
                return dict(existing), False
            stamp = _now()
            db.execute(
                "INSERT INTO parent_lots(lot_id, actual_qty, version, created_at, updated_at)"
                " VALUES(?,?,1,?,?)",
                (lot_id, actual_qty, stamp, stamp),
            )
            return dict(self.get_lot(db, lot_id)), True

    def correct_lot(self, lot_id, new_qty):
        """更正实称量，只改当前台账，保留更正流水；已签发证据不受影响。"""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.get_lot(db, lot_id)
            if row is None:
                return None, "missing"
            if row["actual_qty"] == new_qty:
                return dict(row), "unchanged"
            stamp = _now()
            db.execute(
                "INSERT INTO lot_corrections(lot_id, old_qty, new_qty, old_version, new_version, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (lot_id, row["actual_qty"], new_qty, row["version"], row["version"] + 1, stamp),
            )
            db.execute(
                "UPDATE parent_lots SET actual_qty=?, version=version+1, updated_at=? WHERE lot_id=?",
                (new_qty, stamp, lot_id),
            )
            return dict(self.get_lot(db, lot_id)), "corrected"

    def list_lots(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM parent_lots ORDER BY lot_id").fetchall()
        return [dict(row) for row in rows]

    def list_corrections(self, lot_id=None):
        with self.connect() as db:
            if lot_id:
                rows = db.execute(
                    "SELECT * FROM lot_corrections WHERE lot_id=? ORDER BY id", (lot_id,)
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM lot_corrections ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    # ---------- 单据 ----------
    def create_doc(self, doc_id, kind, lines):
        """登记草稿单据并写入预定明细，DRAFT 阶段不占量。

        同 doc_id 重放为幂等：已存在则原样返回。
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM issue_docs WHERE doc_id=?", (doc_id,)).fetchone()
            if existing:
                return dict(existing), False
            stamp = _now()
            db.execute(
                "INSERT INTO issue_docs(doc_id, kind, status, created_at) VALUES(?,?,'DRAFT',?)",
                (doc_id, kind, stamp),
            )
            db.executemany(
                "INSERT INTO reservations(doc_id, lot_id, qty) VALUES(?,?,?)",
                [(doc_id, lot_id, qty) for lot_id, qty in lines],
            )
            return dict(db.execute("SELECT * FROM issue_docs WHERE doc_id=?", (doc_id,)).fetchone()), True

    def get_doc(self, doc_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM issue_docs WHERE doc_id=?", (doc_id,)).fetchone()
        return dict(row) if row else None

    def doc_lines(self, db, doc_id):
        rows = db.execute(
            "SELECT lot_id, qty FROM reservations WHERE doc_id=? ORDER BY id", (doc_id,)
        ).fetchall()
        return [(row["lot_id"], row["qty"]) for row in rows]

    def list_docs(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM issue_docs ORDER BY created_at, doc_id").fetchall()
        return [dict(row) for row in rows]

    # ---------- 占量视图 ----------
    def signed_usage(self, db, lot_id):
        """某父批当前已签发占量（仅 SIGNED 单据计入）。"""
        row = db.execute(
            "SELECT COALESCE(SUM(r.qty),0) AS total FROM reservations r"
            " JOIN issue_docs d ON d.doc_id=r.doc_id"
            " WHERE r.lot_id=? AND d.status='SIGNED'",
            (lot_id,),
        ).fetchone()
        return int(row["total"])

    def workbench(self):
        with self.connect() as db:
            lot_rows = [dict(row) for row in db.execute("SELECT * FROM parent_lots ORDER BY lot_id").fetchall()]
            docs = [dict(row) for row in db.execute("SELECT * FROM issue_docs ORDER BY created_at, doc_id").fetchall()]
            reservation_rows = db.execute("SELECT * FROM reservations ORDER BY id").fetchall()
        doc_lines = {}
        for row in reservation_rows:
            doc_lines.setdefault(row["doc_id"], []).append((row["lot_id"], row["qty"]))
        usage = {}
        for doc_id, pairs in doc_lines.items():
            if next(d for d in docs if d["doc_id"] == doc_id)["status"] == "SIGNED":
                for lot_id, qty in pairs:
                    usage[lot_id] = usage.get(lot_id, 0) + qty
        lot_view = []
        for lot in lot_rows:
            used = usage.get(lot["lot_id"], 0)
            available = lot["actual_qty"] - used
            lot_view.append({
                "lot_id": lot["lot_id"],
                "actual_qty": lot["actual_qty"],
                "version": lot["version"],
                "signed_qty": used,
                "available_qty": available,
                "shortage": max(0, used - lot["actual_qty"]),
                "in_shortage": used > lot["actual_qty"],
                "updated_at": lot["updated_at"],
            })
        lot_index = {item["lot_id"]: item for item in lot_view}
        doc_view = []
        for doc in docs:
            pairs = doc_lines.get(doc["doc_id"], [])
            blocked = [
                {"lot_id": lot_id, "need": qty,
                 "available_qty": lot_index.get(lot_id, {}).get("available_qty", 0),
                 "missing": max(0, qty - lot_index.get(lot_id, {}).get("available_qty", 0))}
                for lot_id, qty in pairs
                if lot_id not in lot_index or lot_index[lot_id]["available_qty"] < qty
            ]
            doc_view.append({
                "doc_id": doc["doc_id"],
                "kind": doc["kind"],
                "status": doc["status"],
                "lines": [{"lot_id": lot_id, "qty": qty} for lot_id, qty in pairs],
                "total_qty": sum(qty for _, qty in pairs),
                "signable": doc["status"] == "DRAFT" and not blocked,
                "blocked_by": blocked,
                "created_at": doc["created_at"],
                "signed_at": doc["signed_at"],
            })
        return {"lots": lot_view, "docs": doc_view,
                "shortage_lots": [item["lot_id"] for item in lot_view if item["in_shortage"]]}
