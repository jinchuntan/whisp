"""Entry point for the Whisp transcription worker.

Run from the ``worker/`` directory inside WSL2:

    python run_worker.py

Requires SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (see worker/.env.example).
Never runs on Vercel.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from whisp_worker.config import WorkerSettings, get_worker_settings
from whisp_worker.queue import JobQueue
from whisp_worker.worker import Worker


def build_client(settings: WorkerSettings):  # type: ignore[no-untyped-def]
    from supabase import create_client

    if not settings.supabase_configured:
        raise SystemExit(
            "Supabase is not configured. Set SUPABASE_URL and "
            "SUPABASE_SERVICE_ROLE_KEY in worker/.env (see worker/.env.example)."
        )
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


def main() -> None:
    settings = get_worker_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    client = build_client(settings)
    queue = JobQueue(client, settings.supabase_audio_bucket, settings.resolved_worker_id)
    worker = Worker(settings, queue)
    try:
        asyncio.run(worker.run_forever())
    except KeyboardInterrupt:
        print("\nworker stopped", file=sys.stderr)


if __name__ == "__main__":
    main()
