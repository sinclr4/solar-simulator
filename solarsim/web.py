from __future__ import annotations

import logging
import os
from dataclasses import asdict

from dotenv import load_dotenv
import base64
import json
import re
from urllib.parse import urlparse

import requests
from flask import Flask, abort, g, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from . import credentials, service
from .store import Database, Store

load_dotenv()
app = Flask(__name__)
_db: Database | None = None
_claimed: set[str] = set()

USER_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,128}$")
CONSUMER_TENANT = "9188040d-6c67-4c5b-b112-36a304b66dad"
TENANT_CLAIM = "http://schemas.microsoft.com/identity/claims/tenantid"


def _claims() -> dict:
    raw = request.headers.get("X-MS-CLIENT-PRINCIPAL")
    try:
        data = json.loads(base64.b64decode(raw)) if raw else {}
        return {c["typ"]: c["val"] for c in data.get("claims", [])}
    except (ValueError, KeyError, TypeError):
        return {}


@app.before_request
def identify_user():
    """App Service authentication (Easy Auth) sets these headers and strips any sent by the client."""
    user_id = request.headers.get("X-MS-CLIENT-PRINCIPAL-ID")
    if user_id:
        if not USER_ID_RE.match(user_id):
            abort(403)
        g.user_id, g.user_name = user_id, request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME", "")
    elif credentials.IN_AZURE:
        abort(401)
    else:
        g.user_id, g.user_name = service.local_user(), "Local development"
    if request.method in ("POST", "PUT", "DELETE") and request.headers.get("X-Solar-Client") != "1":
        abort(400)


@app.after_request
def harden(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def _is_legacy_owner() -> bool:
    """Data saved before accounts existed belongs to the original owner."""
    owner_id, owner_name = os.environ.get("LEGACY_DATA_OWNER_ID"), os.environ.get("LEGACY_DATA_OWNER", "").lower()
    if owner_id and g.user_id == owner_id:
        return True
    return bool(owner_name and g.user_name.lower() == owner_name and _claims().get(TENANT_CLAIM) == CONSUMER_TENANT)


def store() -> Store:
    global _db
    if _db is None:
        _db = Database(os.environ["COSMOS_ENDPOINT"], os.environ.get("COSMOS_DATABASE", "solarsim"))
    user_store = _db.for_user(g.user_id)
    if g.user_id not in _claimed and _is_legacy_owner():
        try:
            logging.info("Moved %s legacy documents to the owner", user_store.adopt_legacy())
            _claimed.add(g.user_id)
        except Exception:
            logging.exception("Could not move legacy documents")
    return user_store


@app.get("/")
def index():
    return render_template("index.html", defaults=asdict(service.Inputs()), orientations=service.ORIENTATIONS)


@app.get("/guide")
def guide():
    return render_template("guide.html")


@app.get("/api/me")
def me():
    return jsonify(name=g.user_name, local=g.user_id == service.local_user())


@app.get("/api/octopus")
def octopus_status():
    return jsonify(service.octopus_status(store()))


@app.post("/api/octopus")
def save_octopus():
    return jsonify(service.save_octopus(store(), request.get_json(silent=True) or {}))


@app.delete("/api/octopus")
def delete_octopus():
    service.delete_octopus(store())
    return jsonify(service.octopus_status(store()))


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


@app.post("/api/forecast")
def run_forecast():
    return jsonify(service.forecast(store(), request.get_json(silent=True) or {}))


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


@app.errorhandler(requests.RequestException)
def upstream_failed(e):
    logging.exception("Upstream request failed")
    resp = getattr(e, "response", None)
    host = urlparse(getattr(resp, "url", None) or getattr(getattr(e, "request", None), "url", None) or "").hostname
    outcome = f"returned {resp.status_code}" if resp is not None else "could not be reached"
    return jsonify(error=f"{host or 'An external service'} {outcome}"), 502


@app.errorhandler(Exception)
def server_error(e):
    if isinstance(e, HTTPException):
        return e
    logging.exception("Request failed")
    return jsonify(error=f"Something went wrong ({type(e).__name__}) - see the server log for details"), 500


def main() -> None:
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8000")), debug=False)


if __name__ == "__main__":
    main()
