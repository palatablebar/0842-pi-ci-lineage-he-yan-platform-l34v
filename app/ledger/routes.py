from flask import Blueprint, jsonify, request

from app.errors import DomainIssue
from app.intake.service import load
from app.ledger import service
from app.ledger.service import doc_detail


ledger_api = Blueprint("ledger_api", __name__)


def store():
    from flask import current_app
    return current_app.extensions["ledger"]


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise DomainIssue("BAD_REQUEST")
    return data


@ledger_api.get("/api/ledger/workbench")
def workbench():
    return jsonify(service.workbench(store()))


@ledger_api.post("/api/ledger/lots")
def register_lot():
    data = body()
    if "lot_id" not in data or "actual_qty" not in data:
        raise DomainIssue("BAD_REQUEST")
    return jsonify(service.register_lot(store(), data["lot_id"], data["actual_qty"]))


@ledger_api.post("/api/ledger/lots/<lot_id>/corrections")
def correct_lot(lot_id):
    data = body()
    if "new_qty" not in data:
        raise DomainIssue("BAD_REQUEST")
    return jsonify(service.correct_lot(store(), lot_id, data["new_qty"]))


@ledger_api.get("/api/ledger/lots/<lot_id>/corrections")
def list_corrections(lot_id):
    return jsonify({"corrections": store().list_corrections(lot_id)})


@ledger_api.post("/api/ledger/docs")
def create_doc():
    data = body()
    if "doc_id" not in data or "kind" not in data or "lines" not in data:
        raise DomainIssue("BAD_REQUEST")
    return jsonify(service.create_doc(store(), data["doc_id"], data["kind"], data["lines"]))


@ledger_api.get("/api/ledger/docs/<doc_id>")
def get_doc(doc_id):
    return jsonify(doc_detail(store(), doc_id))


@ledger_api.post("/api/ledger/docs/<doc_id>/sign")
def sign_doc(doc_id):
    return jsonify(service.sign_doc(store(), doc_id))


@ledger_api.get("/api/ledger/docs/<doc_id>/evidence")
def evidence(doc_id):
    return jsonify(service.get_evidence(store(), doc_id))


@ledger_api.post("/api/ledger/seed")
def seed():
    data = request.get_json(silent=True) or {}
    profile = data.get("profile", "clean")
    path = data.get("path")
    rows = load(profile, path)["rows"]
    return jsonify(service.seed_from_rows(store(), rows))
