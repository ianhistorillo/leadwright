"""Durable checkpoints for contact enrich CSV runs (resume without re-billing)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def enrich_runs_root() -> Path:
    root = Path("data") / "output" / "enrich_runs"
    root.mkdir(parents=True, exist_ok=True)
    return root


class EnrichRunStore:
    """Filesystem checkpoint for an enrich run."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.dir = enrich_runs_root() / run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.checkpoint_path = self.dir / "checkpoint.csv"
        self.state_path = self.dir / "state.json"

    @classmethod
    def create(cls, run_id: str | None = None) -> EnrichRunStore:
        import uuid

        return cls(run_id or str(uuid.uuid4()))

    @classmethod
    def latest_resumable(cls) -> dict[str, Any] | None:
        root = enrich_runs_root()
        best: tuple[float, dict[str, Any]] | None = None
        for path in root.glob("*/state.json"):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                continue
            if state.get("status") not in {
                "running",
                "interrupted",
                "error",
                "stopped_early",
            }:
                continue
            if not (path.parent / "checkpoint.csv").exists():
                continue
            mtime = path.stat().st_mtime
            payload = {
                "run_id": state.get("run_id") or path.parent.name,
                "status": state.get("status"),
                "valid_emails_found": state.get("valid_emails_found", 0),
                "people_done": state.get("people_done", 0),
                "enrich_targets": state.get("enrich_targets", 0),
                "stop_after_valid_emails": state.get("stop_after_valid_emails", 0),
                "updated_at": state.get("updated_at"),
                "source_filename": state.get("source_filename"),
            }
            if best is None or mtime > best[0]:
                best = (mtime, payload)
        return best[1] if best else None

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {
                "run_id": self.run_id,
                "status": "new",
                "processed_keys": [],
                "valid_emails_found": 0,
                "people_done": 0,
                "credits_consumed": 0.0,
            }
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, state: dict[str, Any]) -> None:
        state = {
            **state,
            "run_id": self.run_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        self.state_path.write_text(
            json.dumps(state, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def save_checkpoint_csv(self, csv_bytes: bytes) -> None:
        self.checkpoint_path.write_bytes(csv_bytes)
        # Extra rolling backup so a mid-write crash is less likely to wipe data.
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = self.dir / f"checkpoint_{stamp}.csv"
        backup.write_bytes(csv_bytes)
        # Keep only the newest few timestamped backups.
        backups = sorted(self.dir.glob("checkpoint_*.csv"), reverse=True)
        for old in backups[5:]:
            try:
                old.unlink()
            except OSError:
                pass

    def save_batch_response(
        self,
        batch_index: int,
        payload: dict[str, Any] | list[Any] | None,
        *,
        meta: dict[str, Any] | None = None,
    ) -> Path:
        batches = self.dir / "batches"
        batches.mkdir(parents=True, exist_ok=True)
        path = batches / f"batch_{batch_index:05d}.json"
        envelope = {
            "batch_index": batch_index,
            "meta": meta or {},
            "response": payload,
        }
        path.write_text(
            json.dumps(envelope, indent=2, default=str),
            encoding="utf-8",
        )
        # Always keep a small sidecar of the latest response meta for support.
        meta_path = self.dir / "last_apollo_response_meta.json"
        meta_path.write_text(
            json.dumps(meta or {}, indent=2, default=str),
            encoding="utf-8",
        )
        return path

    def load_checkpoint_csv(self) -> bytes:
        if not self.checkpoint_path.exists():
            raise FileNotFoundError(f"No checkpoint for enrich run {self.run_id}")
        return self.checkpoint_path.read_bytes()

    def mark_interrupted(self) -> None:
        state = self.load_state()
        if state.get("status") == "completed":
            return
        state["status"] = "interrupted"
        self.save_state(state)
