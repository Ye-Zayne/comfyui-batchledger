"""Submit the real API example and wait for the server-side batch chain."""
import argparse
import json
import time
import urllib.request
import uuid
from pathlib import Path


def request(server, route, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(server.rstrip("/") + route, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", default="http://127.0.0.1:8188")
    parser.add_argument("--make-demo", type=Path, help="ComfyUI input root; create two demo images below it")
    parser.add_argument("--workflow", type=Path, default=Path(__file__).with_name("workflow_api.json"))
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--run-mode", choices=["pending_and_invalid", "failed_only", "invalid_only", "force_all"])
    args = parser.parse_args()
    if args.make_demo:
        from PIL import Image
        folder = args.make_demo.resolve() / "batchledger_demo"
        folder.mkdir(parents=True, exist_ok=True)
        for name, color in (("a", "tomato"), ("b", "steelblue")):
            path = folder / f"{name}.png"
            if not path.exists():
                Image.new("RGB", (128, 128), color).save(path)
    workflow = json.loads(args.workflow.read_text(encoding="utf-8"))
    if args.run_mode:
        for node in workflow.values():
            if node.get("class_type") == "BatchLedgerPlan":
                node["inputs"]["run_mode"] = args.run_mode
    run_id = uuid.uuid4().hex
    response = request(args.server, "/prompt", {"prompt": workflow, "extra_data": {"_batchledger_run_id": run_id}})
    if "prompt_id" not in response:
        raise SystemExit(json.dumps(response, indent=2))
    print("Submitted", response["prompt_id"])
    deadline = time.monotonic() + args.timeout
    idle = 0
    while time.monotonic() < deadline:
        queue = request(args.server, "/queue")
        if not queue["queue_running"] and not queue["queue_pending"]:
            idle += 1
            if idle >= 3:
                history = request(args.server, "/history")
                reports = []
                for job in history.values():
                    for output in job.get("outputs", {}).values():
                        for text in output.get("text", []):
                            try:
                                parsed = json.loads(text)
                                if "counts" in parsed and parsed.get("run_id") == run_id:
                                    reports.append(parsed)
                            except (ValueError, TypeError):
                                pass
                if not reports:
                    raise SystemExit("Batch produced no report; inspect /history and server logs")
                print(json.dumps(reports[-1], ensure_ascii=False, indent=2))
                if reports[-1]["counts"].get("failed"):
                    raise SystemExit("Batch finished with failed records")
                plans = [n["inputs"] for n in workflow.values() if n.get("class_type") == "BatchLedgerPlan"]
                if plans and plans[0].get("auto_queue") and plans[0].get("run_mode") in {"pending_and_invalid", "force_all"}:
                    unfinished = {k: v for k, v in reports[-1]["counts"].items() if k != "complete" and v}
                    if unfinished:
                        raise SystemExit("Automatic batch left unfinished records; inspect the report")
                return
        else:
            idle = 0
        time.sleep(0.25)
    raise SystemExit("Timed out waiting for batch; inspect /queue and /history")


if __name__ == "__main__":
    main()
