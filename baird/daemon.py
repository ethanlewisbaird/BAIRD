"""Satellite-side daemon: watchdog + executor.

One process per satellite. Two responsibilities (per the Phase 1 + Phase 3 design):

1. **Watchdog**: watches each declared `volume.mount` under `watch.roots`,
   computes the fast fingerprint on every new/modified file, POSTs bare
   provenance rows to the hub's registry. Skips paths matching `watch.deny`.

2. **Executor**: exposes a JSON-RPC interface so the hub orchestrator can
   `read_file`, `write_file`, `run_command`, `apply_diff`, `attach_tmux`, etc.
   Co-located with the watchdog so the two streams can be reconciled in-process
   (an executor-driven write knows not to re-emit a bare watchdog row).

Currently a skeleton — the watchdog + executor logic gets filled in during
Phase 1 + Phase 3 implementation.
"""

from __future__ import annotations

import logging
import sys

from .config import load_host_config

log = logging.getLogger("baird.daemon")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    cfg = load_host_config()

    log.info("BAIRD daemon starting on host_id=%s", cfg.host_id or "<unset>")
    log.info("hub_url=%s", cfg.hub_url)
    log.info("volumes=%s", [v.id for v in cfg.volumes])
    log.info("session_multiplexer=%s", cfg.session_multiplexer)

    if not cfg.host_id:
        log.error("host_id is required in ~/.baird/host.yaml")
        return 2

    # TODO Phase 1: spawn watchdog observers for each volume root
    # TODO Phase 3: start JSON-RPC executor server
    log.warning("daemon scaffolded but not yet implemented; exiting")
    return 0


if __name__ == "__main__":
    sys.exit(main())
