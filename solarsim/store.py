from __future__ import annotations

from azure.cosmos import CosmosClient, exceptions
from azure.identity import DefaultAzureCredential

# Containers holding one user's private data. "solar" is public weather data shared by everyone.
USER_CONTAINERS = ("consumption", "quotes", "simulations", "settings")


class Database:
    def __init__(self, endpoint: str, database: str = "solarsim"):
        client = CosmosClient(endpoint, credential=DefaultAzureCredential())
        self.db = client.get_database_client(database)

    def for_user(self, user_id: str) -> "Store":
        return Store(self.db, user_id)


class Store:
    """Every read and write on a private container is limited to one user.

    Documents are stamped with userId and stored under "<userId>:<id>", so two users can hold
    the same date or quote id without clashing and queries always filter on userId.
    """

    def __init__(self, db, user_id: str):
        if not user_id or ":" in user_id:
            raise ValueError("Invalid user")
        self.db = db
        self.user_id = user_id
        self._prefix = f"{user_id}:"

    def _private(self, container: str) -> bool:
        return container in USER_CONTAINERS

    def _query(self, container: str, where: str = "", select: str = "*", params: list | None = None) -> list:
        c = self.db.get_container_client(container)
        clauses, params = ([where] if where else []), list(params or [])
        if self._private(container):
            clauses.append("c.userId = @u")
            params.append({"name": "@u", "value": self.user_id})
        sql = f"SELECT {select} FROM c" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        return list(c.query_items(sql, parameters=params, enable_cross_partition_query=True))

    def _public_doc(self, container: str, doc: dict) -> dict:
        if self._private(container) and str(doc.get("id", "")).startswith(self._prefix):
            doc = {**doc, "id": doc["id"][len(self._prefix):]}
        return doc

    def upsert(self, container: str, docs: list[dict]) -> None:
        c = self.db.get_container_client(container)
        for d in docs:
            if self._private(container):
                d = {**d, "id": self._prefix + str(d["id"]), "userId": self.user_id}
            c.upsert_item(d)

    def by_date(self, container: str, start: str, end: str) -> list[dict]:
        rows = self._query(container, "c.date >= @s AND c.date < @e",
                           params=[{"name": "@s", "value": start}, {"name": "@e", "value": end}])
        return [self._public_doc(container, d) for d in rows]

    def dates(self, container: str) -> list[str]:
        return sorted(self._query(container, select="VALUE c.date"))

    def list_all(self, container: str) -> list[dict]:
        return [self._public_doc(container, d) for d in self._query(container)]

    def get(self, container: str, item_id: str) -> dict | None:
        rows = self._query(container, "c.id = @id", params=[{"name": "@id", "value": self._prefix + item_id}])
        return self._public_doc(container, rows[0]) if rows else None

    def delete(self, container: str, item_id: str) -> None:
        full_id = self._prefix + item_id if self._private(container) else item_id
        try:
            self.db.get_container_client(container).delete_item(full_id, partition_key=full_id)
        except exceptions.CosmosResourceNotFoundError:
            pass

    def adopt_legacy(self) -> int:
        """Claim documents saved before accounts existed (no userId, id = bare date or quote id)."""
        moved = 0
        for name in USER_CONTAINERS:
            c = self.db.get_container_client(name)
            for d in list(c.query_items("SELECT * FROM c WHERE NOT IS_DEFINED(c.userId)",
                                        enable_cross_partition_query=True)):
                legacy_id = d["id"]
                c.upsert_item({**d, "id": self._prefix + legacy_id, "userId": self.user_id})
                c.delete_item(legacy_id, partition_key=legacy_id)
                moved += 1
        return moved
