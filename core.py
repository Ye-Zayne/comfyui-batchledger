"""Content addressed, single-image batch records; no ComfyUI imports."""
from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import time
import uuid
from pathlib import Path

from PIL import Image

INSTANCE_ID = uuid.uuid4().hex
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
LEDGER_CLASSES = {"BatchLedgerPlan", "BatchLedgerNext", "BatchLedgerVerifiedSave", "BatchLedgerReport"}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def within(root, value, *, must_exist=False):
    """Resolve symlinks before testing containment, including nonexisting leafs."""
    root = Path(root).expanduser().resolve(strict=True)
    supplied = Path(value)
    candidate = (supplied if supplied.is_absolute() else root / supplied).resolve(strict=must_exist)
    if not candidate.is_relative_to(root):
        raise ValueError("Path escapes the configured BatchLedger root")
    return candidate


def workflow_fingerprint(prompt, save_node_id=None):
    """Hash only the image ancestors. UI layout, titles and unrelated outputs are omitted.

    The structural representation uses node classes/links rather than editor IDs.
    Ledger sources are terminals: each record's content, seed, prompt and metadata
    are added separately to its fingerprint.
    """
    prompt = {str(k): v for k, v in (prompt or {}).items()}
    saves = [k for k, v in prompt.items() if v.get("class_type") == "BatchLedgerVerifiedSave"]
    if save_node_id is None and len(saves) == 1:
        save_node_id = saves[0]
    visiting = set()

    def expand(value):
        if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], int) and value[0] in prompt:
            return {"node": node(value[0]), "output": value[1]}
        if isinstance(value, list):
            return [expand(v) for v in value]
        if isinstance(value, dict):
            return {k: expand(v) for k, v in sorted(value.items())}
        return value

    def node(node_id):
        if node_id in visiting:
            raise ValueError("Cycle in workflow image ancestors")
        data = prompt[node_id]
        cls = data["class_type"]
        if cls in LEDGER_CLASSES:
            return {"class_type": cls}
        visiting.add(node_id)
        result = {"class_type": cls, "inputs": {k: expand(v) for k, v in sorted(data.get("inputs", {}).items())}}
        visiting.remove(node_id)
        return result

    if save_node_id is not None:
        save = prompt[str(save_node_id)]
        image_graph = expand(save.get("inputs", {}).get("images"))
        checks = {k: save.get("inputs", {}).get(k, 0) for k in ("expected_width", "expected_height")}
        return digest({"image_graph": image_graph, "checks": checks, "schema": 1})
    # Useful for offline planning; graph mode nodes require a single save node.
    return digest({k: node(k) for k in sorted(prompt) if prompt[k].get("class_type") not in LEDGER_CLASSES})


def load_records(input_root, folder="", manifest="", manifest_json="", recursive=True, base_seed=0):
    root = Path(input_root).resolve(strict=True)
    if manifest and manifest_json.strip():
        raise ValueError("Choose a manifest file or inline JSON, not both")
    if manifest_json.strip():
        rows = json.loads(manifest_json)
    elif manifest:
        p = within(root, manifest, must_exist=True)
        if p.suffix.lower() == ".csv":
            with p.open(encoding="utf-8-sig", newline="") as f:
                rows = list(csv.DictReader(f))
        elif p.suffix.lower() == ".json":
            rows = json.loads(p.read_text(encoding="utf-8-sig"))
        else:
            raise ValueError("Manifest must be .json or .csv")
    else:
        p = within(root, folder or ".", must_exist=True)
        if not p.is_dir():
            raise ValueError("Folder mode requires a directory")
        files = p.rglob("*") if recursive else p.glob("*")
        rows = [{"path": str(f.relative_to(root))} for f in sorted(files) if f.is_file() and f.suffix.lower() in IMAGE_SUFFIXES]
    if not isinstance(rows, list) or not rows:
        raise ValueError("Input must contain at least one image record")
    result, keys = [], set()
    for row in rows:
        if isinstance(row, str):
            row = {"path": row}
        if not isinstance(row, dict) or not row.get("path"):
            raise ValueError("Each record requires path (and id for duplicate input paths)")
        p = within(root, str(row["path"]), must_exist=True)
        if not p.is_file() or p.suffix.lower() not in IMAGE_SUFFIXES:
            raise ValueError("Record path must refer to a supported image")
        rel = p.relative_to(root).as_posix()
        logical_key = str(row.get("id") or rel)
        if logical_key in keys:
            raise ValueError("Duplicate record id/path; give repeated source records unique id values")
        keys.add(logical_key)
        supplied_seed = row.get("seed")
        seed = int(supplied_seed) if supplied_seed not in (None, "") else int(digest([int(base_seed), logical_key])[:16], 16)
        if not 0 <= seed <= 2**64 - 1:
            raise ValueError("Seed must be an unsigned 64-bit integer")
        fields = {k: v for k, v in row.items() if k not in {"path", "seed", "id"}}
        result.append({"logical_key": logical_key, "path": rel, "source_sha256": file_hash(p), "seed": seed, "prompt": str(row.get("prompt", "")), "fields": fields})
    return result


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Ledger:
    def __init__(self, input_root, output_root, output_folder="batchledger"):
        self.input_root = Path(input_root).resolve(strict=True)
        self.output_root = Path(output_root).resolve(strict=True)
        self.directory = within(self.output_root, output_folder)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.db_path = self.directory / ".batchledger.sqlite3"
        if self.db_path.is_symlink():
            raise ValueError("Ledger database cannot be a symlink")
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS items (
                    item_id TEXT PRIMARY KEY, logical_key TEXT NOT NULL,
                    spec_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, run_attempts INTEGER NOT NULL DEFAULT 0,
                    last_run TEXT, owner TEXT, owner_pid INTEGER, claim TEXT,
                    output_path TEXT, output_sha256 TEXT, width INTEGER, height INTEGER,
                    error TEXT, updated REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS current_versions (
                    logical_key TEXT PRIMARY KEY, item_id TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, cancelled INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS status_idx ON items(status);
            """)

    @contextlib.contextmanager
    def connect(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def plan(self, records, workflow_sha256, model_fingerprint, run_id, run_mode="pending_and_invalid", max_retries=0, auto_queue=False):
        if not str(model_fingerprint).strip():
            raise ValueError("Provide explicit model hashes/versions; for no-model workflows use no-model:v1")
        if run_mode not in {"pending_and_invalid", "failed_only", "invalid_only", "force_all"}:
            raise ValueError("Unknown run mode")
        if not 0 <= int(max_retries) <= 3:
            raise ValueError("max_retries must be between 0 and 3")
        ids = []
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO runs(run_id) VALUES (?)", (run_id,))
            for row in db.execute("SELECT item_id,owner,owner_pid FROM items WHERE status='running'").fetchall():
                if not pid_alive(row["owner_pid"]) or (row["owner_pid"] == os.getpid() and row["owner"] != INSTANCE_ID):
                    db.execute("UPDATE items SET status='pending',claim=NULL,error='Recovered abandoned process claim' WHERE item_id=?", (row["item_id"],))
            for record in records:
                spec = dict(record, workflow_sha256=workflow_sha256, model_fingerprint=str(model_fingerprint).strip())
                item_id = digest(spec)
                ids.append(item_id)
                old = db.execute("SELECT item_id FROM current_versions WHERE logical_key=?", (record["logical_key"],)).fetchone()
                initial = "invalid" if old and old[0] != item_id else "pending"
                if old and old[0] != item_id:
                    db.execute("UPDATE items SET status='stale',error='Superseded by changed content/parameters' WHERE item_id=? AND status!='running'", (old[0],))
                db.execute("INSERT OR IGNORE INTO items(item_id,logical_key,spec_json,status,updated) VALUES (?,?,?,?,?)", (item_id, record["logical_key"], canonical(spec), initial, time.time()))
                # A previously superseded version can become current again.
                db.execute("UPDATE items SET status='invalid' WHERE item_id=? AND status='stale'", (item_id,))
                db.execute("INSERT INTO current_versions VALUES (?,?) ON CONFLICT(logical_key) DO UPDATE SET item_id=excluded.item_id", (record["logical_key"], item_id))
                existing = db.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
                if existing["status"] == "complete" and not self.output_valid(existing):
                    db.execute("UPDATE items SET status='invalid',error='Output missing, changed or corrupt' WHERE item_id=?", (item_id,))
        plan = {"input_root": str(self.input_root), "output_root": str(self.output_root), "output_folder": self.directory.relative_to(self.output_root).as_posix(), "item_ids": ids, "run_id": run_id, "run_mode": run_mode, "max_retries": int(max_retries), "auto_queue": bool(auto_queue), "workflow_sha256": workflow_sha256, "model_fingerprint": str(model_fingerprint).strip()}
        plan["summary"] = self.report(ids)
        return plan

    def output_valid(self, row):
        if not row["output_path"] or not row["output_sha256"]:
            return False
        try:
            p = within(self.output_root, row["output_path"], must_exist=True)
            if file_hash(p) != row["output_sha256"]:
                return False
            with Image.open(p) as im:
                im.load()
                return im.size == (row["width"], row["height"]) and im.format == "PNG"
        except (OSError, ValueError):
            return False

    def claim_next(self, plan):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT cancelled FROM runs WHERE run_id=?", (plan["run_id"],)).fetchone()
            if not run or run[0]:
                return {"skip": True, "reason": "cancelled", "plan": plan}
            # One live claim per ledger, even for simultaneous prompts/processes.
            if db.execute("SELECT 1 FROM items WHERE status='running' LIMIT 1").fetchone():
                return {"skip": True, "reason": "busy", "plan": plan}
            allowed = {"pending_and_invalid": {"pending", "invalid", "cancelled"}, "failed_only": {"failed"}, "invalid_only": {"invalid"}, "force_all": {"pending", "invalid", "cancelled", "failed", "complete"}}[plan["run_mode"]]
            # One query rather than one SQLite lookup per completed record.
            states = sorted(allowed | {"retry"})
            placeholders = ",".join("?" for _ in states)
            available = {row["item_id"]: row for row in db.execute(f"SELECT * FROM items WHERE status IN ({placeholders})", states).fetchall()}
            for item_id in plan["item_ids"]:
                row = available.get(item_id)
                if row is None:
                    continue
                if row["status"] == "retry" and row["last_run"] == plan["run_id"]:
                    eligible = True
                else:
                    eligible = row["status"] in allowed and row["last_run"] != plan["run_id"]
                if not eligible:
                    continue
                current = db.execute("SELECT item_id FROM current_versions WHERE logical_key=?", (row["logical_key"],)).fetchone()
                if not current or current[0] != item_id:
                    continue
                token = uuid.uuid4().hex
                run_attempts = row["run_attempts"] + 1 if row["last_run"] == plan["run_id"] else 1
                db.execute("UPDATE items SET status='running',attempts=attempts+1,run_attempts=?,last_run=?,owner=?,owner_pid=?,claim=?,updated=? WHERE item_id=?", (run_attempts, plan["run_id"], INSTANCE_ID, os.getpid(), token, time.time(), item_id))
                return {"skip": False, "item_id": item_id, "claim": token, "spec": json.loads(row["spec_json"]), "plan": plan, "attempt": run_attempts}
            return {"skip": True, "reason": "finished", "plan": plan}

    def verify_finished(self, plan):
        """Verify once at batch end, rather than rehashing every output per item."""
        ids = set(plan["item_ids"])
        with self.connect() as db:
            rows = db.execute("SELECT * FROM items WHERE status='complete'").fetchall()
            for row in rows:
                if row["item_id"] not in ids:
                    continue
                spec = json.loads(row["spec_json"])
                try:
                    source_ok = file_hash(within(self.input_root, spec["path"], must_exist=True)) == spec["source_sha256"]
                except (OSError, ValueError):
                    source_ok = False
                if not source_ok or not self.output_valid(row):
                    db.execute("UPDATE items SET status='invalid',error='Input/output changed during the batch; queue a new plan' WHERE item_id=? AND status='complete'", (row["item_id"],))

    def fail(self, item, error, *, cancelled=False, stale=False):
        if item.get("skip"):
            return
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,claim,run_attempts FROM items WHERE item_id=?", (item["item_id"],)).fetchone()
            if not row or row["status"] != "running" or row["claim"] != item["claim"]:
                return
            status = "cancelled" if cancelled else "stale" if stale else "retry" if row["run_attempts"] <= item["plan"]["max_retries"] else "failed"
            db.execute("UPDATE items SET status=?,claim=NULL,error=?,updated=? WHERE item_id=?", (status, str(error)[:4000], time.time(), item["item_id"]))

    def cancel_run(self, run_id, item=None):
        with self.connect() as db:
            db.execute("UPDATE runs SET cancelled=1 WHERE run_id=?", (run_id,))
        if item:
            self.fail(item, "User cancelled; no automatic retry", cancelled=True)

    def save(self, item, image, expected_width=0, expected_height=0):
        if item.get("skip"):
            return None
        if not isinstance(image, Image.Image):
            raise TypeError("Expected a PIL image")
        if expected_width and image.width != int(expected_width):
            raise ValueError("Output width does not match expected_width")
        if expected_height and image.height != int(expected_height):
            raise ValueError("Output height does not match expected_height")
        source = within(self.input_root, item["spec"]["path"], must_exist=True)
        if file_hash(source) != item["spec"]["source_sha256"]:
            self.fail(item, "Input changed during execution", stale=True)
            raise ValueError("Input changed during execution; queue again to plan the new content")
        stem = re.sub(r"[^\w.-]", "_", Path(item["spec"]["path"]).stem, flags=re.UNICODE)[:60].strip(". ") or "image"
        filename = f"{stem}--{item['item_id']}.png"
        target = within(self.output_root, self.directory / filename)
        temp = within(self.output_root, self.directory / f".tmp-{uuid.uuid4().hex}.png")
        try:
            image.convert("RGB").save(temp, format="PNG")
            with temp.open("rb") as f:
                os.fsync(f.fileno())
            with Image.open(temp) as decoded:
                decoded.load()
                if decoded.size != image.size or decoded.format != "PNG":
                    raise ValueError("Temporary output failed decode/size verification")
            checksum = file_hash(temp)
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT status,claim,logical_key FROM items WHERE item_id=?", (item["item_id"],)).fetchone()
                current = db.execute("SELECT item_id FROM current_versions WHERE logical_key=?", (item["spec"]["logical_key"],)).fetchone()
                run = db.execute("SELECT cancelled FROM runs WHERE run_id=?", (item["plan"]["run_id"],)).fetchone()
                if not row or row["status"] != "running" or row["claim"] != item["claim"]:
                    raise ValueError("Stale or already completed claim")
                if not current or current[0] != item["item_id"] or not run or run[0]:
                    raise ValueError("Plan superseded or run cancelled before output commit")
                # Rehash after encoding, immediately before committing the output.
                if file_hash(source) != item["spec"]["source_sha256"]:
                    raise ValueError("Input changed before output commit")
                # Re-check parent/symlink containment just before the filesystem write.
                target = within(self.output_root, target)
                os.replace(temp, target)
                db.execute("UPDATE items SET status='complete',claim=NULL,output_path=?,output_sha256=?,width=?,height=?,error=NULL,updated=? WHERE item_id=?", (target.relative_to(self.output_root).as_posix(), checksum, image.width, image.height, time.time(), item["item_id"]))
            return target
        except Exception as exc:
            stale = any(text in str(exc) for text in ("Input changed", "superseded", "Stale"))
            self.fail(item, str(exc), stale=stale)
            raise
        finally:
            temp.unlink(missing_ok=True)

    def report(self, ids=None):
        with self.connect() as db:
            rows = db.execute("SELECT item_id,logical_key,status,attempts,run_attempts,output_path,error FROM items ORDER BY logical_key,updated").fetchall()
        selected = set(ids) if ids is not None else None
        rows = [dict(r) for r in rows if selected is None or r["item_id"] in selected]
        counts = {}
        for row in rows:
            counts[row["status"]] = counts.get(row["status"], 0) + 1
        return {"counts": counts, "records": rows}


def ledger_for(plan):
    return Ledger(plan["input_root"], plan["output_root"], plan["output_folder"])
