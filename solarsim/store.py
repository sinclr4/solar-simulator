from __future__ import annotations

from azure.cosmos import CosmosClient, exceptions
from azure.identity import DefaultAzureCredential


class Store:
    def __init__(self, endpoint: str, database: str = "solarsim"):
        client = CosmosClient(endpoint, credential=DefaultAzureCredential())
        self.db = client.get_database_client(database)

    def upsert(self, container: str, docs: list[dict]) -> None:
        c = self.db.get_container_client(container)
        for d in docs:
            c.upsert_item(d)

    def by_date(self, container: str, start: str, end: str) -> list[dict]:
        c = self.db.get_container_client(container)
        return list(c.query_items(
            "SELECT * FROM c WHERE c.date >= @s AND c.date < @e",
            parameters=[{"name": "@s", "value": start}, {"name": "@e", "value": end}],
            enable_cross_partition_query=True))

    def dates(self, container: str) -> list[str]:
        c = self.db.get_container_client(container)
        return sorted(c.query_items("SELECT VALUE c.date FROM c", enable_cross_partition_query=True))

    def list_all(self, container: str) -> list[dict]:
        c = self.db.get_container_client(container)
        return list(c.query_items("SELECT * FROM c", enable_cross_partition_query=True))

    def delete(self, container: str, item_id: str) -> None:
        try:
            self.db.get_container_client(container).delete_item(item_id, partition_key=item_id)
        except exceptions.CosmosResourceNotFoundError:
            pass
