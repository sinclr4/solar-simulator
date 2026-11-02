"""Isolation tests for the per-user store, using an in-memory stand-in for Cosmos DB."""
import re

import pytest
from azure.cosmos import exceptions

from solarsim.store import Store


class FakeContainer:
    def __init__(self):
        self.items = {}

    def upsert_item(self, d):
        self.items[d["id"]] = dict(d)

    def delete_item(self, item_id, partition_key):
        if item_id not in self.items:
            raise exceptions.CosmosResourceNotFoundError(message="not found")
        self.items.pop(item_id)

    def query_items(self, sql, parameters=None, enable_cross_partition_query=False):
        p = {x["name"]: x["value"] for x in (parameters or [])}
        rows = list(self.items.values())
        if "c.userId = @u" in sql:
            rows = [r for r in rows if r.get("userId") == p["@u"]]
        if "NOT IS_DEFINED(c.userId)" in sql:
            rows = [r for r in rows if "userId" not in r]
        if "c.id = @id" in sql:
            rows = [r for r in rows if r["id"] == p["@id"]]
        if "c.date >= @s" in sql:
            rows = [r for r in rows if p["@s"] <= r["date"] < p["@e"]]
        return [r["date"] for r in rows] if "VALUE c.date" in sql else rows


class FakeDb:
    def __init__(self):
        self.containers = {}

    def get_container_client(self, name):
        return self.containers.setdefault(name, FakeContainer())


def test_users_cannot_see_each_others_data():
    db = FakeDb()
    alice, bob = Store(db, "alice-0001"), Store(db, "bob-00002")
    alice.upsert("consumption", [{"id": "2026-01-01", "date": "2026-01-01", "total_kwh": 10}])
    bob.upsert("consumption", [{"id": "2026-01-01", "date": "2026-01-01", "total_kwh": 99}])
    alice.upsert("settings", [{"id": "octopus", "api_key_enc": "secret"}])

    assert [d["total_kwh"] for d in alice.by_date("consumption", "2026-01-01", "2026-01-02")] == [10]
    assert [d["total_kwh"] for d in bob.by_date("consumption", "2026-01-01", "2026-01-02")] == [99]
    assert alice.dates("consumption") == ["2026-01-01"]
    assert bob.get("settings", "octopus") is None
    assert bob.list_all("settings") == []
    assert alice.get("settings", "octopus")["id"] == "octopus"


def test_delete_only_touches_own_documents():
    db = FakeDb()
    alice, bob = Store(db, "alice-0001"), Store(db, "bob-00002")
    alice.upsert("quotes", [{"id": "q1", "name": "A"}])
    bob.delete("quotes", "q1")
    assert [q["name"] for q in alice.list_all("quotes")] == ["A"]


def test_shared_solar_container_is_not_scoped():
    db = FakeDb()
    Store(db, "alice-0001").upsert("solar", [{"id": "2026-01-01", "date": "2026-01-01"}])
    assert len(Store(db, "bob-00002").by_date("solar", "2026-01-01", "2026-01-02")) == 1


def test_legacy_documents_are_adopted_once():
    db = FakeDb()
    db.get_container_client("quotes").upsert_item({"id": "q1", "name": "Old"})
    bob = Store(db, "bob-00002")
    assert bob.list_all("quotes") == []
    owner = Store(db, "owner-0001")
    assert owner.adopt_legacy() == 1
    assert [q["name"] for q in owner.list_all("quotes")] == ["Old"]
    assert bob.list_all("quotes") == []
    assert owner.adopt_legacy() == 0


@pytest.mark.parametrize("bad", ["", "a:b"])
def test_invalid_user_rejected(bad):
    with pytest.raises(ValueError):
        Store(FakeDb(), bad)


def test_credentials_round_trip_and_reject_bad_input(monkeypatch):
    from cryptography.fernet import Fernet
    from solarsim import credentials
    monkeypatch.setenv("CREDENTIALS_KEY", Fernet.generate_key().decode())
    token = credentials.encrypt("sk_live_abcdefghij1234")
    assert "sk_live" not in token and credentials.decrypt(token) == "sk_live_abcdefghij1234"
    monkeypatch.setenv("CREDENTIALS_KEY", Fernet.generate_key().decode())
    with pytest.raises(ValueError):
        credentials.decrypt(token)
    for bad in ("", "hello", "sk_live_short"):
        with pytest.raises(ValueError):
            credentials.clean_api_key(bad)
    with pytest.raises(ValueError):
        credentials.clean_account("../etc")
    assert credentials.clean_account(" a-1234abcd ") == "A-1234ABCD"
    assert re.match(r"^sk_", credentials.clean_api_key("sk_live_abcdefghij1234"))
