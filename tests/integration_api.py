"""Optional real-server smoke test; creates fixtures only beneath supplied roots."""
import argparse
import copy
import json
import time
import urllib.request
import uuid
from pathlib import Path

from PIL import Image


def request(server, route, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server + route, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as result:
        return json.load(result)


def run(server, workflow):
    run_id = uuid.uuid4().hex
    request(server, "/prompt", {"prompt": workflow, "extra_data": {"_batchledger_run_id": run_id}})
    deadline, idle = time.monotonic() + 60, 0
    while time.monotonic() < deadline:
        queue = request(server, "/queue")
        active = any(q[3].get("_batchledger_run_id") == run_id for q in queue["queue_running"] + queue["queue_pending"])
        idle = 0 if active else idle + 1
        if idle >= 3:
            history = request(server, "/history")
            jobs = [j for j in history.values() if j["prompt"][3].get("_batchledger_run_id") == run_id]
            if not jobs:
                raise AssertionError("No history for submitted run")
            reports = [json.loads(t) for j in jobs for t in j.get("outputs", {}).get("4", {}).get("text", [])]
            if not reports:
                raise AssertionError("No VerifiedSave report")
            return jobs, reports[-1]
        time.sleep(0.05)
    raise TimeoutError("Batch did not finish")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    folder = args.input_root / "batchledger_integration"
    folder.mkdir(exist_ok=True)
    for name, color in (("a", "red"), ("b", "blue")):
        Image.new("RGB", (16, 16), color).save(folder / f"{name}.png")
    workflow = json.loads((Path(__file__).parents[1] / "examples/workflow_api.json").read_text())
    output_folder = "batchledger_integration_" + uuid.uuid4().hex[:10]
    workflow["1"]["inputs"].update(folder=folder.name, output_folder=output_folder)
    workflow["5"] = {"class_type": "BatchLedgerReport", "inputs": {"plan": ["1", 0]}}
    jobs, report = run(args.server, workflow)
    assert report["counts"] == {"complete": 2}, report
    assert len(jobs) == 3, len(jobs)
    assert all("5" in job["outputs"] for job in jobs), jobs
    attempts = {r["logical_key"]: r["attempts"] for r in report["records"]}
    print("fresh batch + Report: 2 PNGs, 3 prompts, complete=2")

    jobs, resumed = run(args.server, workflow)
    assert len(jobs) == 1
    assert resumed["counts"] == {"complete": 2}
    assert attempts == {r["logical_key"]: r["attempts"] for r in resumed["records"]}
    print("resume: 1 lazy report prompt, zero new attempts")

    damaged = args.output_root / report["records"][0]["output_path"]
    damaged.write_bytes(b"nonempty truncated PNG")
    jobs, repaired = run(args.server, workflow)
    assert len(jobs) == 2
    assert repaired["counts"] == {"complete": 2}
    for record in repaired["records"]:
        assert record["attempts"] == attempts[record["logical_key"]] + (record["output_path"] == report["records"][0]["output_path"])
    print("corrupt output: only damaged record regenerated, complete=2")

    bad_folder = args.input_root / "batchledger_integration_bad"
    bad_folder.mkdir(exist_ok=True)
    (bad_folder / "a_bad.png").write_bytes(b"not a decodable image")
    Image.new("RGB", (16, 16), "green").save(bad_folder / "b_good.png")
    failure_workflow = copy.deepcopy(workflow)
    failure_workflow["1"]["inputs"].update(folder=bad_folder.name, output_folder=output_folder + "_failure")
    jobs, failed = run(args.server, failure_workflow)
    assert failed["counts"] == {"failed": 1, "complete": 1}, failed
    assert len(jobs) == 4
    assert sum(job["status"]["status_str"] == "error" for job in jobs) == 2
    bad = next(r for r in failed["records"] if r["status"] == "failed")
    assert bad["attempts"] == 2 and bad["run_attempts"] == 2
    print("upstream load failure: 2 limited attempts, next good image completed, final Report delivered")


if __name__ == "__main__":
    main()
