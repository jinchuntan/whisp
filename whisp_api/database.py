"""Postgres state gateway (Supabase PostgREST + rpc).

A ``Database`` Protocol describes every operation the routes need; tests inject
an in-memory ``FakeDatabase`` (see tests/conftest.py) implementing the same
surface, so no test ever contacts Supabase.

All rows are returned as plain dicts. Routes translate them into explicit
Pydantic responses, so DB columns are never leaked verbatim.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from supabase import Client


class Database(Protocol):
    async def get_active_event(self) -> dict[str, Any] | None: ...
    async def get_event(self, event_id: str) -> dict[str, Any] | None: ...
    async def get_open_round(self, event_id: str) -> dict[str, Any] | None: ...
    async def get_round(self, round_id: str) -> dict[str, Any] | None: ...
    async def list_rounds(self, event_id: str, limit: int = 20) -> list[dict[str, Any]]: ...
    async def touch_badge(self, badge_id: str, event_id: str | None) -> None: ...
    async def create_question(
        self,
        *,
        event_id: str | None,
        round_id: str | None,
        badge_id: str,
        audio_storage_path: str,
    ) -> dict[str, Any]: ...
    async def get_question(self, question_id: str) -> dict[str, Any] | None: ...
    async def get_cluster(self, cluster_id: str) -> dict[str, Any] | None: ...
    async def list_recent_questions(
        self, event_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]: ...
    async def list_clusters(self, round_id: str, limit: int = 50) -> list[dict[str, Any]]: ...
    async def get_badge_notifications(
        self, badge_id: str, round_id: str | None
    ) -> list[dict[str, Any]]: ...
    async def create_event(self, name: str, join_code: str) -> dict[str, Any]: ...
    async def deactivate_events_except(self, event_id: str) -> None: ...
    async def create_round(self, event_id: str, prompt: str | None) -> dict[str, Any]: ...
    async def close_open_rounds(self, event_id: str) -> None: ...
    async def close_round(self, round_id: str) -> dict[str, Any] | None: ...
    async def mark_question_answered(self, question_id: str) -> bool: ...
    async def mark_cluster_answered(self, cluster_id: str) -> bool: ...
    async def request_recluster(self, round_id: str) -> bool: ...
    async def list_worker_heartbeats(self) -> list[dict[str, Any]]: ...


def _first(rows: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    return rows[0] if rows else None


class SupabaseDatabase:
    """Database implementation backed by supabase-py v2 (sync client off-thread)."""

    def __init__(self, client: Client) -> None:
        self._c = client

    async def _run(self, fn: Any) -> Any:
        return await asyncio.to_thread(fn)

    # -- events -------------------------------------------------------------
    async def get_active_event(self) -> dict[str, Any] | None:
        def q() -> Any:
            return (
                self._c.table("events")
                .select("*")
                .eq("active", True)
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )

        return _first((await self._run(q)).data)

    async def get_event(self, event_id: str) -> dict[str, Any] | None:
        def q() -> Any:
            return self._c.table("events").select("*").eq("id", event_id).limit(1).execute()

        return _first((await self._run(q)).data)

    async def create_event(self, name: str, join_code: str) -> dict[str, Any]:
        def q() -> Any:
            return (
                self._c.table("events")
                .insert({"name": name, "join_code": join_code, "active": True})
                .execute()
            )

        return (await self._run(q)).data[0]

    async def deactivate_events_except(self, event_id: str) -> None:
        def q() -> Any:
            return self._c.table("events").update({"active": False}).neq("id", event_id).execute()

        await self._run(q)

    # -- rounds -------------------------------------------------------------
    async def get_open_round(self, event_id: str) -> dict[str, Any] | None:
        def q() -> Any:
            return (
                self._c.table("rounds")
                .select("*")
                .eq("event_id", event_id)
                .eq("status", "open")
                .order("opened_at", desc=True)
                .limit(1)
                .execute()
            )

        return _first((await self._run(q)).data)

    async def get_round(self, round_id: str) -> dict[str, Any] | None:
        def q() -> Any:
            return self._c.table("rounds").select("*").eq("id", round_id).limit(1).execute()

        return _first((await self._run(q)).data)

    async def list_rounds(self, event_id: str, limit: int = 20) -> list[dict[str, Any]]:
        def q() -> Any:
            return (
                self._c.table("rounds")
                .select("*")
                .eq("event_id", event_id)
                .order("opened_at", desc=True)
                .limit(limit)
                .execute()
            )

        return (await self._run(q)).data or []

    async def create_round(self, event_id: str, prompt: str | None) -> dict[str, Any]:
        def q() -> Any:
            return (
                self._c.table("rounds")
                .insert({"event_id": event_id, "prompt": prompt, "status": "open"})
                .execute()
            )

        return (await self._run(q)).data[0]

    async def close_open_rounds(self, event_id: str) -> None:
        def q() -> Any:
            return (
                self._c.table("rounds")
                .update({"status": "closed", "closed_at": "now()"})
                .eq("event_id", event_id)
                .eq("status", "open")
                .execute()
            )

        await self._run(q)

    async def close_round(self, round_id: str) -> dict[str, Any] | None:
        def q() -> Any:
            return (
                self._c.table("rounds")
                .update({"status": "closed", "closed_at": "now()"})
                .eq("id", round_id)
                .execute()
            )

        return _first((await self._run(q)).data)

    async def request_recluster(self, round_id: str) -> bool:
        def q() -> Any:
            return (
                self._c.table("rounds")
                .update({"recluster_requested_at": "now()"})
                .eq("id", round_id)
                .execute()
            )

        return bool((await self._run(q)).data)

    # -- badges -------------------------------------------------------------
    async def touch_badge(self, badge_id: str, event_id: str | None) -> None:
        def q() -> Any:
            return (
                self._c.table("badges")
                .upsert(
                    {"id": badge_id, "event_id": event_id, "last_seen_at": "now()"},
                    on_conflict="id",
                )
                .execute()
            )

        await self._run(q)

    # -- questions ----------------------------------------------------------
    async def create_question(
        self,
        *,
        event_id: str | None,
        round_id: str | None,
        badge_id: str,
        audio_storage_path: str,
    ) -> dict[str, Any]:
        def q() -> Any:
            return (
                self._c.table("questions")
                .insert(
                    {
                        "event_id": event_id,
                        "round_id": round_id,
                        "badge_id": badge_id,
                        "audio_storage_path": audio_storage_path,
                        "status": "queued",
                    }
                )
                .execute()
            )

        return (await self._run(q)).data[0]

    async def get_question(self, question_id: str) -> dict[str, Any] | None:
        def q() -> Any:
            return self._c.table("questions").select("*").eq("id", question_id).limit(1).execute()

        return _first((await self._run(q)).data)

    async def list_recent_questions(
        self, event_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        def q() -> Any:
            query = self._c.table("questions").select("*")
            if event_id:
                query = query.eq("event_id", event_id)
            return query.order("created_at", desc=True).limit(limit).execute()

        return (await self._run(q)).data or []

    async def get_badge_notifications(
        self, badge_id: str, round_id: str | None
    ) -> list[dict[str, Any]]:
        def q() -> Any:
            query = (
                self._c.table("questions")
                .select("id,cluster_id,round_id,status")
                .eq("badge_id", badge_id)
                .eq("status", "done")
                .not_.is_("cluster_id", "null")
            )
            if round_id:
                query = query.eq("round_id", round_id)
            return query.order("created_at", desc=True).limit(5).execute()

        rows = (await self._run(q)).data or []
        out: list[dict[str, Any]] = []
        for row in rows:
            cluster = await self.get_cluster(row["cluster_id"]) if row.get("cluster_id") else None
            count = int(cluster["question_count"]) if cluster else 1
            if count > 1:
                out.append(
                    {
                        "question_id": row["id"],
                        "cluster_id": row.get("cluster_id"),
                        "similar_count": count,
                        "canonical_question": cluster.get("canonical_question")
                        if cluster
                        else None,
                    }
                )
        return out

    async def mark_question_answered(self, question_id: str) -> bool:
        def q() -> Any:
            return (
                self._c.table("questions")
                .update({"answered_at": "now()"})
                .eq("id", question_id)
                .execute()
            )

        return bool((await self._run(q)).data)

    # -- clusters -----------------------------------------------------------
    async def get_cluster(self, cluster_id: str) -> dict[str, Any] | None:
        def q() -> Any:
            return self._c.table("clusters").select("*").eq("id", cluster_id).limit(1).execute()

        return _first((await self._run(q)).data)

    async def list_clusters(self, round_id: str, limit: int = 50) -> list[dict[str, Any]]:
        def q() -> Any:
            return (
                self._c.table("clusters")
                .select("*")
                .eq("round_id", round_id)
                .order("question_count", desc=True)
                .limit(limit)
                .execute()
            )

        return (await self._run(q)).data or []

    async def mark_cluster_answered(self, cluster_id: str) -> bool:
        def q() -> Any:
            return (
                self._c.table("clusters")
                .update({"status": "answered", "answered_at": "now()"})
                .eq("id", cluster_id)
                .execute()
            )

        return bool((await self._run(q)).data)

    # -- worker heartbeats --------------------------------------------------
    async def list_worker_heartbeats(self) -> list[dict[str, Any]]:
        def q() -> Any:
            return (
                self._c.table("worker_heartbeats")
                .select("*")
                .order("last_seen_at", desc=True)
                .limit(20)
                .execute()
            )

        return (await self._run(q)).data or []


def get_database() -> Database:
    """FastAPI dependency: real Supabase-backed database (overridden in tests)."""
    from whisp_api._client import get_supabase_client

    return SupabaseDatabase(get_supabase_client())
