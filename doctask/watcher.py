"""Explicit watched-directory adapter with stable-file and PostgreSQL idempotency."""

import argparse
import os
import time
from pathlib import Path

from doctask.durable import DurableProjectDeliveryService

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".md", ".txt"}


def process_stable_files(
    service: DurableProjectDeliveryService,
    *,
    watcher_id: str,
    corpus_id: str,
    directory: Path,
    stable_for_seconds: float = 2.0,
) -> list[dict[str, object]]:
    now = time.time()
    paths = []
    for path in sorted(directory.iterdir()):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        stat = path.stat()
        if now - stat.st_mtime < stable_for_seconds:
            continue
        paths.append(path)
    if not paths:
        return []
    try:
        return service.ingest_watched_batch(
            watcher_id=watcher_id, corpus_id=corpus_id, paths=paths
        )
    except (OSError, ValueError) as exc:
        # A transient/oversized pre-read must not terminate the long-running adapter.
        # Parsable siblings are retried on the next poll; parsed errors are persisted
        # by ingest_watched_batch and therefore do not repeat.
        return [{"status": "ERROR", "error": str(exc), "runId": None}]


def main() -> None:
    parser = argparse.ArgumentParser(description="Watch a directory for stable project files")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--corpus-id", required=True)
    parser.add_argument("--watcher-id", default="default")
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise SystemExit("DATABASE_URL is required")
    with DurableProjectDeliveryService.connect(database_url) as service:
        while True:
            process_stable_files(
                service,
                watcher_id=args.watcher_id,
                corpus_id=args.corpus_id,
                directory=args.directory,
                stable_for_seconds=args.interval,
            )
            time.sleep(args.interval)


if __name__ == "__main__":
    main()
