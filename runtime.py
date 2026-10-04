"""Server-side lifecycle tracking. Works without a connected browser."""
from __future__ import annotations

import asyncio
import copy
import inspect
import logging
import threading
import uuid

from .core import ledger_for

LOG = logging.getLogger(__name__)
_lock = threading.RLock()
_contexts = {}
_runs = {}


def server_instance():
    try:
        from server import PromptServer
        return getattr(PromptServer, "instance", None)
    except ImportError:
        return None


def existing_plan(run_id):
    with _lock:
        plan = _runs.get(run_id)
        return {k: v for k, v in plan.items() if k != "context"} if plan else None


def get_context(prompt_id):
    with _lock:
        return _contexts.get(prompt_id)


def context_for(prompt, node_id):
    server = server_instance()
    snapshot = None
    if server is not None:
        try:
            from comfy_execution.utils import get_executing_context
            executing = get_executing_context()
            wanted = executing.prompt_id if executing else None
        except ImportError:
            wanted = None
        running, _ = server.prompt_queue.get_current_queue()
        for queued in running:
            if (wanted and queued[1] == wanted) or (wanted is None and str(node_id) in queued[2]):
                snapshot = copy.deepcopy(queued)
                break
    if snapshot is None:
        return {"prompt_id": None, "run_id": uuid.uuid4().hex, "snapshot": None, "node_id": str(node_id)}
    run_id = snapshot[3].get("_batchledger_run_id") or uuid.uuid4().hex
    return {"prompt_id": snapshot[1], "run_id": run_id, "snapshot": snapshot, "node_id": str(node_id)}


def register_plan(context, plan):
    context = dict(context, plan=plan, item=None, saved=False, handled=False)
    if context["prompt_id"]:
        with _lock:
            _contexts[context["prompt_id"]] = context
            _runs[plan["run_id"]] = {k: v for k, v in plan.items() if k != "context"}
    return context


def attach_item(context, item):
    with _lock:
        context["item"] = item


def mark_saved(context):
    with _lock:
        context["saved"] = True


def handle_event(server, event, data):
    if event not in {"execution_error", "execution_interrupted", "execution_success"} or not isinstance(data, dict):
        return
    prompt_id = data.get("prompt_id")
    with _lock:
        context = _contexts.pop(prompt_id, None)
        if context is None or context["handled"]:
            return
        context["handled"] = True
    plan, item = context["plan"], context["item"]
    ledger = ledger_for(plan)
    if event == "execution_interrupted":
        ledger.cancel_run(plan["run_id"], item)
        LOG.info("BatchLedger run %s stopped by user", plan["run_id"])
        return
    if event == "execution_error":
        if item and not item.get("skip"):
            ledger.fail(item, f"{data.get('exception_type', 'Error')}: {data.get('exception_message', '')}")
        else:
            # No record was claimed: do not retry configuration/plan errors.
            return
    elif not context["saved"]:
        # A completed/occupied/cancelled batch takes no lazy image branch.
        return
    if plan["auto_queue"] and context["snapshot"]:
        # Never submit from inside the Save node: whole-prompt success/interrupt
        # must be observed first, and duplicate terminal messages are ignored.
        server.loop.call_soon_threadsafe(lambda: asyncio.create_task(enqueue_next(server, context)))


async def enqueue_next(server, context):
    import execution
    plan = context["plan"]
    ledger = ledger_for(plan)
    with ledger.connect() as db:
        cancelled = db.execute("SELECT cancelled FROM runs WHERE run_id=?", (plan["run_id"],)).fetchone()
        if not cancelled or cancelled[0]:
            return
    old = context["snapshot"]
    prompt = copy.deepcopy(old[2])
    for node in prompt.values():
        node.pop("is_changed", None)
    prompt_id = str(uuid.uuid4())
    try:
        # Current ComfyUI takes prompt_id, prompt, partial_execution_list.
        # Support the earlier synchronous 2-argument validator as well.
        parameters = inspect.signature(execution.validate_prompt).parameters
        if len(parameters) >= 3:
            args = (prompt_id, prompt, None)
        elif list(parameters)[0] == "prompt_id":
            args = (prompt_id, prompt)
        elif len(parameters) == 2:
            args = (prompt, None)
        else:
            args = (prompt,)
        valid = execution.validate_prompt(*args)
        if inspect.isawaitable(valid):
            valid = await valid
        if not valid[0]:
            LOG.error("BatchLedger requeue validation failed: %s", valid[1])
            ledger.cancel_run(plan["run_id"])
            return
        extra = copy.deepcopy(old[3])
        extra["_batchledger_run_id"] = plan["run_id"]
        extra["create_time"] = __import__("time").time_ns() // 1_000_000
        # Check cancellation again after asynchronous validation.
        with ledger.connect() as db:
            cancelled = db.execute("SELECT cancelled FROM runs WHERE run_id=?", (plan["run_id"],)).fetchone()
            if not cancelled or cancelled[0]:
                return
        number = server.number
        server.number += 1
        queued = (number, prompt_id, prompt, extra, valid[2])
        if len(old) >= 6:
            queued += (copy.deepcopy(old[5]),)
        # Append normally; BatchLedger does not monopolize the front of queue.
        server.prompt_queue.put(queued)
    except Exception:
        ledger.cancel_run(plan["run_id"])
        LOG.exception("BatchLedger could not enqueue next item")


def install():
    server = server_instance()
    if server is None or getattr(server, "_batchledger_installed", False):
        return
    original = server.send_sync

    def observed_send(event, data, sid=None):
        result = original(event, data, sid)
        try:
            handle_event(server, event, data)
        except Exception:
            LOG.exception("BatchLedger lifecycle observer failed")
        return result

    server.send_sync = observed_send
    server._batchledger_installed = True
    # PromptExecutor does not send non-broadcast success/error events when an
    # API caller has no WebSocket client_id. Observe its history event method
    # as well, preserving the original behavior and deduplicating both paths.
    import execution
    executor_class = execution.PromptExecutor
    if not getattr(executor_class.add_message, "_batchledger_observer", False):
        original_add_message = executor_class.add_message

        def observed_add_message(executor, event, data, broadcast):
            result = original_add_message(executor, event, data, broadcast)
            try:
                handle_event(executor.server, event, data)
            except Exception:
                LOG.exception("BatchLedger executor lifecycle observer failed")
            return result

        observed_add_message._batchledger_observer = True
        executor_class.add_message = observed_add_message
    from aiohttp import web

    @server.routes.post("/batchledger/stop")
    async def stop(request):
        body = await request.json()
        run_id = body.get("run_id")
        with _lock:
            plan = _runs.get(run_id)
            active = [c for c in _contexts.values() if c["plan"]["run_id"] == run_id]
        if plan is None:
            return web.json_response({"error": "Unknown active run_id"}, status=404)
        ledger = ledger_for(plan)
        ledger.cancel_run(run_id)
        server.prompt_queue.delete_queue_item(lambda q: q[3].get("_batchledger_run_id") == run_id)
        for c in active:
            if hasattr(server.prompt_queue, "interrupt_if_running"):
                server.prompt_queue.interrupt_if_running(c["prompt_id"])
            else:
                import nodes
                nodes.interrupt_processing()
        return web.json_response({"stopped": True, "run_id": run_id})
