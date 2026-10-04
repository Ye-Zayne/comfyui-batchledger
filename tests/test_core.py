import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image

from batchledger_testpkg.core import Ledger, load_records, within, workflow_fingerprint


def make_plan(ledger, records, run="run1", **kwargs):
    return ledger.plan(records, "workflow-v1", "models-sha256:test", run, **kwargs)


def test_stable_seed_and_ids_ignore_folder_sort_changes(workspace, ledger, records):
    first = make_plan(ledger, records)
    Image.new("RGB", (3, 3)).save(workspace[0] / "0-new.png")
    again = load_records(workspace[0], base_seed=42)
    old = next(r for r in again if r["path"] == "a.png")
    assert old["seed"] == records[0]["seed"]
    second = make_plan(ledger, again, "run2")
    assert first["item_ids"][0] in second["item_ids"]


def test_graph_ignores_layout_titles_ids_and_unrelated_nodes(graph):
    first = workflow_fingerprint(graph)
    changed = copy.deepcopy(graph)
    changed["3"]["_meta"] = {"title": "new name"}
    changed["3"]["pos"] = [100, 300]
    changed["100"] = {"class_type": "Other", "inputs": {"seed": 999}}
    changed["1"]["inputs"]["output_folder"] = "somewhere-else"
    assert workflow_fingerprint(changed) == first
    renamed = {"n" + k: v for k, v in copy.deepcopy(graph).items()}
    for node in renamed.values():
        for key, value in node["inputs"].items():
            if isinstance(value, list):
                node["inputs"][key] = ["n" + value[0], value[1]]
    assert workflow_fingerprint(renamed) == first
    changed["3"]["inputs"]["width"] = 100
    assert workflow_fingerprint(changed) != first


def test_png_save_verified_and_completed_skipped(ledger, records):
    plan = make_plan(ledger, records)
    item = ledger.claim_next(plan)
    path = ledger.save(item, Image.new("RGB", (8, 6)), 8, 6)
    assert path.is_file()
    assert ledger.report()["counts"] == {"complete": 1}
    same = make_plan(ledger, records, "run2")
    assert ledger.claim_next(same)["skip"]
    assert not list(ledger.directory.glob(".tmp-*"))


@pytest.mark.parametrize("change", ["content", "workflow", "model", "seed", "prompt"])
def test_changes_invalidate_item_versions(change, workspace, ledger, records):
    first = make_plan(ledger, records)
    item = ledger.claim_next(first)
    ledger.save(item, Image.new("RGB", (8, 6)))
    new_records = copy.deepcopy(records)
    wf, model = "workflow-v1", "models-sha256:test"
    if change == "content":
        Image.new("RGB", (8, 6), "red").save(workspace[0] / "a.png")
        new_records = load_records(workspace[0], base_seed=42)
    elif change == "workflow":
        wf = "workflow-v2"
    elif change == "model":
        model = "models-sha256:new"
    elif change == "seed":
        new_records[0]["seed"] += 1
    else:
        new_records[0]["prompt"] = "changed prompt"
    second = ledger.plan(new_records, wf, model, "run2", "invalid_only")
    assert first["item_ids"] != second["item_ids"]
    assert ledger.report()["counts"] == {"stale": 1, "invalid": 1}
    assert not ledger.claim_next(second)["skip"]


@pytest.mark.parametrize("damage", ["truncate", "replace", "delete"])
def test_output_damage_is_replanned(damage, ledger, records):
    item = ledger.claim_next(make_plan(ledger, records))
    path = ledger.save(item, Image.new("RGB", (8, 6)))
    if damage == "truncate":
        path.write_bytes(b"broken non-empty PNG")
    elif damage == "replace":
        Image.new("RGB", (8, 6), "blue").save(path)
    else:
        path.unlink()
    plan = make_plan(ledger, records, "run2", run_mode="invalid_only")
    assert plan["summary"]["counts"] == {"invalid": 1}
    assert not ledger.claim_next(plan)["skip"]


def test_simultaneous_claims_only_one_winner(ledger, records):
    plan = make_plan(ledger, records)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: ledger.claim_next(plan), range(2)))
    assert sum(not r["skip"] for r in results) == 1
    assert next(r for r in results if r["skip"])["reason"] == "busy"


def test_limited_retry_then_manual_failed_only_once(ledger, records):
    plan = make_plan(ledger, records, max_retries=1, auto_queue=True)
    first = ledger.claim_next(plan)
    ledger.fail(first, "upstream error")
    assert ledger.report()["counts"] == {"retry": 1}
    second = ledger.claim_next(plan)
    assert second["attempt"] == 2
    ledger.fail(second, "still fails")
    assert ledger.claim_next(plan)["skip"]
    retry = make_plan(ledger, records, "manual", run_mode="failed_only")
    third = ledger.claim_next(retry)
    assert third["attempt"] == 1
    ledger.fail(third, "manual failure")
    assert ledger.claim_next(retry)["skip"]


def test_cancel_never_retries_same_run_but_manual_resume_works(ledger, records):
    plan = make_plan(ledger, records, max_retries=3)
    item = ledger.claim_next(plan)
    ledger.cancel_run(plan["run_id"], item)
    assert ledger.claim_next(plan)["reason"] == "cancelled"
    assert ledger.report()["counts"] == {"cancelled": 1}
    resumed = make_plan(ledger, records, "new-run")
    assert not ledger.claim_next(resumed)["skip"]


def test_source_change_before_commit_never_marks_complete(workspace, ledger, records):
    item = ledger.claim_next(make_plan(ledger, records))
    Image.new("RGB", (8, 6), "red").save(workspace[0] / "a.png")
    with pytest.raises(ValueError, match="Input changed"):
        ledger.save(item, Image.new("RGB", (8, 6)))
    assert ledger.report()["counts"] == {"stale": 1}
    assert not list(ledger.directory.glob("*.png"))


def test_parameter_change_while_claimed_blocks_obsolete_commit(ledger, records):
    item = ledger.claim_next(make_plan(ledger, records))
    ledger.plan(records, "workflow-v2", "models-sha256:test", "run2")
    with pytest.raises(ValueError, match="superseded"):
        ledger.save(item, Image.new("RGB", (8, 6)))
    assert not list(ledger.directory.glob("*.png"))
    assert ledger.report()["counts"] == {"stale": 1, "invalid": 1}


def test_encoding_failure_leaves_no_complete_or_partial(ledger, records, monkeypatch):
    item = ledger.claim_next(make_plan(ledger, records))
    def broken_save(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(Image.Image, "save", broken_save)
    with pytest.raises(OSError, match="disk full"):
        ledger.save(item, Image.new("RGB", (8, 6)))
    assert ledger.report()["counts"] == {"failed": 1}
    assert not list(ledger.directory.glob("*.png"))


def test_rehash_after_encoding_detects_mid_save_change(workspace, ledger, records, monkeypatch):
    item = ledger.claim_next(make_plan(ledger, records))
    original = Image.Image.save
    def changing_save(image, fp, *args, **kwargs):
        result = original(image, fp, *args, **kwargs)
        if str(fp).find(".tmp-") >= 0:
            original(Image.new("RGB", (8, 6), "yellow"), workspace[0] / "a.png")
        return result
    monkeypatch.setattr(Image.Image, "save", changing_save)
    with pytest.raises(ValueError, match="Input changed"):
        ledger.save(item, Image.new("RGB", (8, 6)))
    assert ledger.report()["counts"] == {"stale": 1}
    assert not list(ledger.directory.glob("*.png"))


def test_duplicate_stems_get_distinct_safe_output_names(workspace, ledger):
    for name in ("left", "right"):
        (workspace[0] / name).mkdir()
        Image.new("RGB", (2, 2)).save(workspace[0] / name / "same.png")
    records = load_records(workspace[0], base_seed=0)
    plan = make_plan(ledger, records)
    paths = [ledger.save(ledger.claim_next(plan), Image.new("RGB", (2, 2))) for _ in records]
    assert len(set(paths)) == len(records)


def test_manifest_json_csv_and_duplicate_ids(workspace):
    root = workspace[0]
    inline = json.dumps([{"path": "a.png", "id": "shot-1", "seed": 7, "prompt": "portrait"}, {"path": "a.png", "id": "shot-2", "seed": 8}])
    rows = load_records(root, manifest_json=inline)
    assert [r["seed"] for r in rows] == [7, 8]
    (root / "batch.csv").write_text("id,path,seed,prompt\nshot-1,a.png,7,portrait\n", encoding="utf-8")
    assert load_records(root, manifest="batch.csv")[0] == rows[0]
    with pytest.raises(ValueError, match="Duplicate"):
        load_records(root, manifest_json='["a.png", "a.png"]')


@pytest.mark.parametrize("bad_path", ["../escape", "/tmp/escape"])
def test_output_root_traversal_rejected(workspace, bad_path):
    with pytest.raises(ValueError, match="escapes"):
        Ledger(*workspace, output_folder=bad_path)


def test_manifest_input_traversal_and_symlink_escape(workspace, tmp_path):
    outside = tmp_path / "outside.png"
    Image.new("RGB", (2, 2)).save(outside)
    root = workspace[0]
    with pytest.raises(ValueError, match="escapes"):
        load_records(root, manifest_json='[{"path": "../outside.png"}]')
    (root / "escape.png").symlink_to(outside)
    with pytest.raises(ValueError, match="escapes"):
        load_records(root, manifest_json='[{"path": "escape.png"}]')


def test_target_symlink_escape_rejected(workspace, ledger, records, tmp_path):
    item = ledger.claim_next(make_plan(ledger, records))
    target = ledger.directory / f"a--{item['item_id']}.png"
    target.symlink_to(tmp_path / "outside.png")
    with pytest.raises(ValueError, match="escapes"):
        ledger.save(item, Image.new("RGB", (8, 6)))
    assert not (tmp_path / "outside.png").exists()


def test_abandoned_claim_recovered(ledger, records):
    item = ledger.claim_next(make_plan(ledger, records))
    with ledger.connect() as db:
        db.execute("UPDATE items SET owner_pid=? WHERE item_id=?", (99999999, item["item_id"]))
    plan = make_plan(ledger, records, "restarted")
    assert not ledger.claim_next(plan)["skip"]


def test_empty_model_identity_rejected(ledger, records):
    with pytest.raises(ValueError, match="explicit model"):
        ledger.plan(records, "workflow", "", "run")
