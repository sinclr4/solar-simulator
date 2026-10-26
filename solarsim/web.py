from __future__ import annotations

import logging
import os
from dataclasses import asdict

from dotenv import load_dotenv
from flask import Flask, abort, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from . import service
from .store import Store

load_dotenv()
app = Flask(__name__)
_store: Store | None = None


@app.before_request
def restrict_to_configured_user():
    allowed_user = os.environ.get("ALLOWED_ENTRA_USER_ID")
    if allowed_user and request.headers.get("X-MS-CLIENT-PRINCIPAL-ID") != allowed_user:
        abort(403)


def store() -> Store:
    global _store
    if _store is None:
        _store = Store(os.environ["COSMOS_ENDPOINT"], os.environ.get("COSMOS_DATABASE", "solarsim"))
    return _store


@app.get("/")
def index():
    return render_template("index.html", defaults=asdict(service.Inputs()), orientations=service.ORIENTATIONS)


@app.get("/guide")
def guide():
    return render_template("guide.html")


@app.get("/api/status")
def status():
    return jsonify(service.data_status(store()))


@app.post("/api/usage")
def fetch_usage():
    days = int((request.get_json(silent=True) or {}).get("days", 365))
    if not 1 <= days <= 1095:
        raise ValueError("days must be between 1 and 1095")
    return jsonify(service.fetch_consumption(store(), days))


@app.post("/api/simulate")
def run_simulation():
    return jsonify(service.run(store(), request.get_json(silent=True) or {}))


@app.get("/api/quotes")
def list_quotes():
    return jsonify(service.list_quotes(store()))


@app.post("/api/quotes")
def save_quote():
    return jsonify(service.save_quote(store(), request.get_json(silent=True) or {}))


@app.delete("/api/quotes/<quote_id>")
def delete_quote(quote_id: str):
    service.delete_quote(store(), quote_id)
    return jsonify(deleted=quote_id)


@app.errorhandler(ValueError)
def bad_request(e):
    return jsonify(error=str(e)), 400


@app.errorhandler(Exception)
def server_error(e):
    if isinstance(e, HTTPException):
        return e
    logging.exception("Request failed")
    return jsonify(error="Something went wrong - see the server log for details"), 500


def main() -> None:
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8000")), debug=False)


if __name__ == "__main__":
    main()
