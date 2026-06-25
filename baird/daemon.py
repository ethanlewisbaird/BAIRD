"""Satellite-side daemon: watchdog + (Phase 3) executor.

Phase 1 vertical slice — what's implemented here:

- Reads `~/.baird/host.yaml` for volume map and watch config.
- Watches each `watch.roots` path recursively via `watchdog`.
- Applies `watch.deny` gitignore-style patterns through `ScopeFilter`.
- On every file create/modify event: computes the fast fingerprint and POSTs
  the record to the hub (which dedups by `(storage_volume, relative_path)`).
- Runs a background sha256 backfill worker that polls the hub for files with
  `sha256_status='pending'` on this host's volumes and fills in the full hash.

The Phase 3 executor (JSON-RPC endpoints for `read_file`, `run_command`, etc.)
lives in the same process and shares state with the watchdog so the two streams
can be reconciled — that work lands during Phase 3 implementation.
"""

from __future__ import annotations

import logging
import queue
import signal
import sys
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from .config import HostConfig, VolumeSpec, load_host_config
from .fingerprint import fingerprint, full_sha256
from .memory_client import HubClient
from .scope import ScopeFilter

log = logging.getLogger("baird.daemon")


# Time the worker waits between sha256 backfill polls when there's nothing to do.
SHA256_POLL_INTERVAL = 10.0
# Batch size for backfill — keeps any one cycle bounded.
SHA256_BATCH = 10


class _Handler(FileSystemEventHandler):
    def __init__(self, daemon: "WatchdogDaemon") -> None:
        self._daemon = daemon

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._daemon._enqueue(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._daemon._enqueue(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        # Treat destination as a new write.
        if not event.is_directory:
            self._daemon._enqueue(event.dest_path)


class WatchdogDaemon:
    def __init__(self, cfg: HostConfig, hub: HubClient | None = None) -> None:
        self.cfg = cfg
        self.scope = ScopeFilter(cfg.watch.deny)
        self.hub = hub or HubClient(cfg.hub_url, cfg.auth_token)

        # Resolved mount paths for volume lookup, longest-prefix wins so that
        # nested volumes (e.g. cluster:/work and cluster:/work/scratch) resolve
        # to the most specific match.
        self._volumes: list[tuple[Path, VolumeSpec]] = [
            (Path(v.mount).expanduser().resolve(), v) for v in cfg.volumes
        ]
        self._volumes.sort(key=lambda pair: len(str(pair[0])), reverse=True)

        self._queue: queue.Queue[str] = queue.Queue()
        self._observers: list[Observer] = []
        self._stopping = threading.Event()
        self._threads: list[threading.Thread] = []

    # ---- Public lifecycle ----

    def start(self) -> None:
        roots = self.cfg.watch.roots
        if not roots:
            log.warning("no watch.roots configured; daemon will only run the sha256 backfill")

        for root in roots:
            root_path = Path(root).expanduser()
            if not root_path.exists():
                log.warning("watch root %s does not exist; skipping", root_path)
                continue
            obs = Observer()
            obs.schedule(_Handler(self), str(root_path), recursive=True)
            obs.start()
            self._observers.append(obs)
            log.info("watching %s (recursive)", root_path)

        t1 = threading.Thread(target=self._event_worker, name="event-worker", daemon=True)
        t2 = threading.Thread(target=self._sha256_worker, name="sha256-backfill", daemon=True)
        for t in (t1, t2):
            t.start()
        self._threads.extend([t1, t2])

        log.info("BAIRD daemon ready on host_id=%s", self.cfg.host_id)

    def stop(self) -> None:
        if self._stopping.is_set():
            return
        log.info("stopping BAIRD daemon")
        self._stopping.set()
        for obs in self._observers:
            obs.stop()
        for obs in self._observers:
            obs.join(timeout=2.0)
        for t in self._threads:
            t.join(timeout=2.0)
        self.hub.close()

    def run_forever(self) -> None:
        self.start()
        signal.signal(signal.SIGTERM, lambda *_: self._stopping.set())
        signal.signal(signal.SIGINT, lambda *_: self._stopping.set())
        try:
            while not self._stopping.is_set():
                time.sleep(0.5)
        finally:
            self.stop()

    # ---- Path → volume mapping ----

    def _volume_for(self, abs_path: Path) -> tuple[VolumeSpec, str] | None:
        """Return (volume, rel_path) for an absolute file path, or None if outside our volume map."""
        try:
            p = abs_path.resolve()
        except OSError:
            return None
        for mount, vol in self._volumes:
            try:
                rel = p.relative_to(mount)
            except ValueError:
                continue
            return vol, str(rel)
        return None

    # ---- Event processing ----

    def _enqueue(self, abs_path: str) -> None:
        self._queue.put(abs_path)

    def _event_worker(self) -> None:
        while not self._stopping.is_set():
            try:
                abs_path = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._process_one(abs_path)
            except FileNotFoundError:
                # Race: file was created+deleted before we got to it. Ignore.
                pass
            except Exception:
                log.exception("error processing %s", abs_path)

    def _process_one(self, abs_path: str) -> None:
        p = Path(abs_path)
        if not p.is_file():
            return  # symlink-to-nowhere, FIFO, etc.

        match = self._volume_for(p)
        if match is None:
            log.debug("path %s is outside the volume map; skipping", abs_path)
            return
        volume, rel_path = match

        if self.scope.is_denied(rel_path):
            return

        fp = fingerprint(p)
        self.hub.register_file(
            storage_volume=volume.id,
            relative_path=rel_path,
            size=fp.size,
            mtime_ns=fp.mtime_ns,
            head_hash=fp.head_hash,
            tail_hash=fp.tail_hash,
        )

    # ---- sha256 backfill ----

    def _sha256_worker(self) -> None:
        vol_ids = {v.id for _, v in self._volumes}
        if not vol_ids:
            return

        while not self._stopping.is_set():
            try:
                processed = self._sha256_one_cycle(vol_ids)
            except Exception:
                log.exception("sha256 backfill error")
                processed = 0

            # If we found nothing to do, back off; otherwise loop immediately.
            if processed == 0:
                self._stopping.wait(timeout=SHA256_POLL_INTERVAL)

    def _sha256_one_cycle(self, vol_ids: set[str]) -> int:
        # Fetch a batch per volume — keeps each volume making progress even
        # when one has a huge backlog.
        processed = 0
        for vol_id in vol_ids:
            if self._stopping.is_set():
                break
            pending = self.hub.list_files(
                sha256_status="pending",
                storage_volume=vol_id,
                limit=SHA256_BATCH,
            )
            for rec in pending:
                if self._stopping.is_set():
                    break
                self._backfill_one(rec)
                processed += 1
        return processed

    def _backfill_one(self, rec: dict) -> None:
        # Locate the file on local disk.
        v_id = rec["storage_volume"]
        rel = rec["relative_path"]
        mount = next((m for m, v in self._volumes if v.id == v_id), None)
        if mount is None:
            # File belongs to a volume this host doesn't have mounted; skip.
            return
        abs_path = mount / rel
        try:
            sha = full_sha256(abs_path)
        except FileNotFoundError:
            self.hub.patch_file(rec["id"], sha256_status="skipped")
            return
        except OSError:
            log.exception("could not read %s for sha256", abs_path)
            return
        self.hub.patch_file(rec["id"], sha256=sha, sha256_status="computed")


# ---- Entry point used by `baird daemon` ---------------------------------


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    cfg_path = Path("~/.baird/host.yaml").expanduser()
    if not cfg_path.exists():
        log.error(
            "host config not found at %s — create one (see configs/example-host.yaml)",
            cfg_path,
        )
        return 2

    try:
        cfg = load_host_config(cfg_path)
    except Exception as e:  # pydantic.ValidationError or yaml errors
        log.error("invalid host config at %s: %s", cfg_path, e)
        return 2

    daemon = WatchdogDaemon(cfg)
    daemon.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
