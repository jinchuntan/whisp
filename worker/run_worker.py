"""Entry point for the Persephone transcription worker.

Run from the ``worker/`` directory inside WSL2:

    python run_worker.py

Requires SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY (see worker/.env.example).
Never runs on Vercel.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from persephone_worker.assistant import AssistantProcessor
from persephone_worker.chatbot import build_chatbot_provider
from persephone_worker.config import WorkerSettings, get_worker_settings
from persephone_worker.queue import JobQueue
from persephone_worker.worker import Worker


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
    # The answer generator runs as a SEPARATE async task sharing the same queue,
    # so a slow LLM never blocks transcription. It is inert when chatbot is
    # disabled (provider is None -> no claims, no network).
    assistant = AssistantProcessor(settings, queue, build_chatbot_provider(settings))

    async def run_all() -> None:
        await asyncio.gather(worker.run_forever(), assistant.run_forever())

    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        print("\nworker stopped", file=sys.stderr)
    finally:
        worker.shutdown()  # release the Agora service once, if it was initialized


if __name__ == "__main__":
    main()
