import asyncio
import copy
import sys
from types import SimpleNamespace

import pytest
import torch

from batchledger_testpkg import runtime
from batchledger_testpkg.core import canonical, workflow_fingerprint
from batchledger_testpkg.nodes import BatchLedgerNext, BatchLedgerPlan, BatchLedgerVerifiedSave


def prepare(ledger, records, graph, auto_queue=True):
    plan = ledger.plan(records, workflow_fingerprint(graph), "no-model:v1", "runtime-run", max_retries=1, auto_queue=auto_queue)
    context = runtime.register_plan({"prompt_id": "prompt-1", "run_id": plan["run_id"], "snapshot": (0, "prompt-1", copy.deepcopy(graph), {}, ["4"], {}), "node_id": "1"}, plan)
    item = ledger.claim_next(plan)
    runtime.attach_item(context, item)
    return plan, context, item


class FakeQueue:
    def __init__(self):
        self.items = []
    def put(self, item):
        self.items.append(item)


class FakeLoop:
    def __init__(self):
        self.callbacks = []
    def call_soon_threadsafe(self, callback):
        self.callbacks.append(callback)


def test_lazy_done_never_requests_image_branch(ledger, records):
    plan = ledger.plan(records, "workflow", "no-model:v1", "run")
    item = ledger.claim_next(plan)
    ledger.cancel_run(plan["run_id"], item)
    skipped = ledger.claim_next(plan)
    save = BatchLedgerVerifiedSave()
    assert save.check_lazy_status(skipped, None) == []
    assert save.save(skipped, None)["result"][0] == ""


def test_lazy_active_requests_image_branch(ledger, records):
    plan = ledger.plan(records, "workflow", "no-model:v1", "run")
    item = ledger.claim_next(plan)
    assert BatchLedgerVerifiedSave().check_lazy_status(item, None) == ["images"]


def test_next_decodes_image_and_mask(ledger, records):
    plan = ledger.plan(records, "workflow", "no-model:v1", "run")
    image, mask, seed, prompt, row, item = BatchLedgerNext().next(plan)
    assert tuple(image.shape) == (1, 6, 8, 3)
    assert tuple(mask.shape) == (1, 6, 8)
    assert seed == records[0]["seed"] and not item["skip"]


def test_custom_socket_values_are_acyclic(workspace, monkeypatch):
    import batchledger_testpkg.nodes as pack_nodes
    monkeypatch.setattr(pack_nodes, "roots", lambda: workspace)
    plan, _ = BatchLedgerPlan().build("", "", "", "batchledger", True, 42, "no-model:v1", "pending_and_invalid", 0, False)
    item = BatchLedgerNext().next(plan)[-1]
    # A circular context/item dictionary crashes ComfyUI's model tracker even
    # though these sockets contain no models. JSON rejects that same cycle.
    assert "context" not in plan and "context" not in item
    assert canonical(item)


def test_size_and_nan_validation_fail_without_output(ledger, records, graph):
    plan, context, item = prepare(ledger, records, graph)
    with pytest.raises(ValueError, match="width"):
        BatchLedgerVerifiedSave().save(item, torch.zeros((1, 6, 8, 3)), 9, 6)
    assert ledger.report()["counts"] == {"retry": 1}
    item = ledger.claim_next(plan)
    with pytest.raises(ValueError, match="NaN"):
        BatchLedgerVerifiedSave().save(item, torch.full((1, 6, 8, 3), float("nan")), 8, 6)
    assert ledger.report()["counts"] == {"failed": 1}


def test_error_is_logged_and_terminal_event_deduplicated(ledger, records, graph):
    plan, context, item = prepare(ledger, records, graph)
    server = SimpleNamespace(loop=FakeLoop())
    event = {"prompt_id": "prompt-1", "exception_type": "RuntimeError", "exception_message": "upstream failed"}
    runtime.handle_event(server, "execution_error", event)
    runtime.handle_event(server, "execution_error", event)
    assert ledger.report()["counts"] == {"retry": 1}
    assert len(server.loop.callbacks) == 1


@pytest.mark.parametrize("has_client", [False, True])
def test_installed_adapter_observes_api_events_without_websocket(ledger, records, graph, monkeypatch, has_client):
    plan, context, item = prepare(ledger, records, graph)
    sent = []
    server = SimpleNamespace(loop=FakeLoop(), client_id="client" if has_client else None, send_sync=lambda *args: sent.append(args), routes=SimpleNamespace(post=lambda path: lambda function: function))
    class Executor:
        def __init__(self, host):
            self.server = host
        def add_message(self, event, data, broadcast):
            if self.server.client_id is not None or broadcast:
                self.server.send_sync(event, data, self.server.client_id)
    monkeypatch.setitem(sys.modules, "server", SimpleNamespace(PromptServer=SimpleNamespace(instance=server)))
    monkeypatch.setitem(sys.modules, "execution", SimpleNamespace(PromptExecutor=Executor))
    # Route registration is tested without pulling in a running web server.
    monkeypatch.setitem(sys.modules, "aiohttp", SimpleNamespace(web=SimpleNamespace()))
    runtime.install()
    Executor(server).add_message("execution_error", {"prompt_id": "prompt-1", "exception_message": "broken upstream"}, False)
    assert len(server.loop.callbacks) == 1
    assert ledger.report()["counts"] == {"retry": 1}
    assert bool(sent) == has_client


def test_interrupt_does_not_schedule_requeue(ledger, records, graph):
    plan, context, item = prepare(ledger, records, graph)
    server = SimpleNamespace(loop=FakeLoop())
    runtime.handle_event(server, "execution_interrupted", {"prompt_id": "prompt-1"})
    assert ledger.report()["counts"] == {"cancelled": 1}
    assert server.loop.callbacks == []
    assert ledger.claim_next(plan)["reason"] == "cancelled"


def test_success_only_requeues_after_verified_save(ledger, records, graph):
    plan, context, item = prepare(ledger, records, graph)
    server = SimpleNamespace(loop=FakeLoop())
    runtime.handle_event(server, "execution_success", {"prompt_id": "prompt-1"})
    assert server.loop.callbacks == []


def test_requeue_validates_fresh_prompt_and_preserves_sensitive_tuple(ledger, records, graph, monkeypatch):
    plan, context, item = prepare(ledger, records, graph)
    context["snapshot"][2]["2"]["is_changed"] = "obsolete"
    context["snapshot"][5]["private_test"] = "value"
    seen = []
    async def validate(prompt_id, prompt, partial):
        seen.append(prompt)
        return (True, None, ["4"], {})
    monkeypatch.setitem(sys.modules, "execution", SimpleNamespace(validate_prompt=validate))
    server = SimpleNamespace(number=5, prompt_queue=FakeQueue())
    asyncio.run(runtime.enqueue_next(server, context))
    assert "is_changed" not in seen[0]["2"]
    queued = server.prompt_queue.items[0]
    assert queued[0] == 5 and queued[3]["_batchledger_run_id"] == plan["run_id"]
    assert queued[5] == {"private_test": "value"}


def test_cancel_during_validation_prevents_submit(ledger, records, graph, monkeypatch):
    plan, context, item = prepare(ledger, records, graph)
    async def validate(prompt_id, prompt, partial):
        ledger.cancel_run(plan["run_id"], item)
        return (True, None, ["4"], {})
    monkeypatch.setitem(sys.modules, "execution", SimpleNamespace(validate_prompt=validate))
    server = SimpleNamespace(number=5, prompt_queue=FakeQueue())
    asyncio.run(runtime.enqueue_next(server, context))
    assert server.prompt_queue.items == []


def test_workflow_parameter_changed_during_execution_rejected(ledger, records, graph):
    plan, context, item = prepare(ledger, records, graph)
    graph["3"]["inputs"]["width"] = 99
    with pytest.raises(ValueError, match="Workflow changed"):
        BatchLedgerVerifiedSave().save(item, torch.zeros((1, 6, 8, 3)), 8, 6, graph, "4")
    assert ledger.report()["counts"] == {"stale": 1}
