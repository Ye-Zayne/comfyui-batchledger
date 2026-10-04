"""ComfyUI V1 node interface."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path

import numpy as np
import torch
from PIL import Image, ImageOps

from . import runtime
from .core import Ledger, canonical, digest, ledger_for, load_records, within, workflow_fingerprint


def roots():
    import folder_paths
    input_root = Path(os.environ.get("BATCHLEDGER_INPUT_ROOT", folder_paths.get_input_directory())).expanduser().resolve(strict=True)
    output_root = Path(os.environ.get("BATCHLEDGER_OUTPUT_ROOT", folder_paths.get_output_directory())).expanduser().resolve(strict=True)
    return input_root, output_root


class BatchLedgerPlan:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "folder": ("STRING", {"default": "batchledger_demo"}),
            "manifest": ("STRING", {"default": ""}),
            "manifest_json": ("STRING", {"default": "", "multiline": True}),
            "output_folder": ("STRING", {"default": "batchledger"}),
            "recursive": ("BOOLEAN", {"default": True}),
            "base_seed": ("INT", {"default": 0, "min": 0, "max": 2**64 - 1}),
            "model_fingerprint": ("STRING", {"default": "", "multiline": True}),
            "run_mode": (["pending_and_invalid", "failed_only", "invalid_only", "force_all"],),
            "max_retries": ("INT", {"default": 1, "min": 0, "max": 3}),
            "auto_queue": ("BOOLEAN", {"default": False}),
        }, "hidden": {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"}}

    RETURN_TYPES = ("BATCHLEDGER_PLAN", "STRING")
    RETURN_NAMES = ("plan", "report_json")
    FUNCTION = "build"
    CATEGORY = "BatchLedger"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        # A stable widget value must not hide changed source/output files.
        return float("nan")

    def build(self, folder, manifest, manifest_json, output_folder, recursive, base_seed, model_fingerprint, run_mode, max_retries, auto_queue, prompt=None, unique_id=None):
        prompt = prompt or {}
        saves = [k for k, n in prompt.items() if n.get("class_type") == "BatchLedgerVerifiedSave"]
        if prompt and len(saves) != 1:
            raise ValueError("Use exactly one BatchLedgerVerifiedSave per workflow")
        if prompt and any(sum(n.get("class_type") == name for n in prompt.values()) != 1 for name in ("BatchLedgerPlan", "BatchLedgerNext")):
            raise ValueError("Use exactly one BatchLedgerPlan and one BatchLedgerNext per workflow")
        if prompt:
            import nodes as comfy_nodes
            side_outputs = [k for k, n in prompt.items() if n.get("class_type") not in {"BatchLedgerVerifiedSave", "BatchLedgerReport"} and getattr(comfy_nodes.NODE_CLASS_MAPPINGS.get(n.get("class_type")), "OUTPUT_NODE", False)]
            if side_outputs:
                raise ValueError("Remove additional output nodes (SaveImage/PreviewImage); the lazy save must own the image branch")
        input_root, output_root = roots()
        context = runtime.context_for(prompt, unique_id)
        ledger = Ledger(input_root, output_root, output_folder)
        wf_hash = workflow_fingerprint(prompt, saves[0] if saves else None)
        config_digest = digest([str(input_root), str(output_root), folder, manifest, manifest_json, output_folder, recursive, base_seed, model_fingerprint, run_mode, max_retries, auto_queue, wf_hash])
        existing = runtime.existing_plan(context["run_id"])
        if existing:
            if existing.get("config_digest") != config_digest:
                raise ValueError("An automatic batch run cannot change its planning parameters")
            # Freeze the input manifest for this run. Each source is checked again
            # when loaded and committed; a manual queue creates a fresh plan.
            plan = existing
            plan["summary"] = ledger.report(plan["item_ids"])
        else:
            if not manifest and not manifest_json.strip():
                source_folder = within(input_root, folder or ".", must_exist=True)
                if ledger.directory.is_relative_to(source_folder):
                    raise ValueError("Output folder cannot be inside the folder being scanned")
            records = load_records(input_root, folder, manifest, manifest_json, recursive, base_seed)
            plan = ledger.plan(records, wf_hash, model_fingerprint, context["run_id"], run_mode, max_retries, auto_queue)
            plan["config_digest"] = config_digest
        # Custom socket values are traversed by ComfyUI's model tracker.
        # Keep runtime context (which owns the current item) out of those values.
        plan["prompt_id"] = context["prompt_id"]
        runtime.register_plan(context, plan)
        return (plan, canonical(dict(plan["summary"], run_id=plan["run_id"])))


class BatchLedgerNext:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"plan": ("BATCHLEDGER_PLAN",)}}

    RETURN_TYPES = ("IMAGE", "MASK", "INT", "STRING", "STRING", "BATCHLEDGER_ITEM")
    RETURN_NAMES = ("image", "mask", "seed", "prompt", "record_json", "item")
    FUNCTION = "next"
    CATEGORY = "BatchLedger"

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def next(self, plan):
        ledger = ledger_for(plan)
        item = ledger.claim_next(plan)
        context = runtime.get_context(plan.get("prompt_id"))
        if context:
            runtime.attach_item(context, item)
        if item["skip"]:
            if item["reason"] == "finished":
                ledger.verify_finished(item["plan"])
            return (torch.zeros((1, 1, 1, 3)), torch.zeros((1, 1, 1)), 0, "", "{}", item)
        try:
            p = within(ledger.input_root, item["spec"]["path"], must_exist=True)
            raw = p.read_bytes()
            if hashlib.sha256(raw).hexdigest() != item["spec"]["source_sha256"]:
                ledger.fail(item, "Input changed between planning and loading", stale=True)
                raise ValueError("Input changed between planning and loading")
            with Image.open(io.BytesIO(raw)) as loaded:
                if getattr(loaded, "n_frames", 1) != 1:
                    raise ValueError("BatchLedger v0.1 supports single-frame source images")
                image = ImageOps.exif_transpose(loaded)
                alpha = image.convert("RGBA").getchannel("A")
                rgb = np.asarray(image.convert("RGB"), dtype=np.float32) / 255.0
                mask = 1.0 - np.asarray(alpha, dtype=np.float32) / 255.0
            return (torch.from_numpy(rgb.copy()).unsqueeze(0), torch.from_numpy(mask.copy()).unsqueeze(0), item["spec"]["seed"], item["spec"]["prompt"], canonical(item["spec"]["fields"]), item)
        except Exception as exc:
            ledger.fail(item, f"Input loading failed: {exc}")
            raise


class BatchLedgerVerifiedSave:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "item": ("BATCHLEDGER_ITEM",),
            "images": ("IMAGE", {"lazy": True}),
            "expected_width": ("INT", {"default": 0, "min": 0, "max": 65536}),
            "expected_height": ("INT", {"default": 0, "min": 0, "max": 65536}),
        }, "hidden": {"prompt": "PROMPT", "unique_id": "UNIQUE_ID"}}

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("output_path", "report_json")
    FUNCTION = "save"
    CATEGORY = "BatchLedger"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def check_lazy_status(self, item, images=None, expected_width=0, expected_height=0, **kwargs):
        return ["images"] if not item.get("skip") and images is None else []

    def save(self, item, images=None, expected_width=0, expected_height=0, prompt=None, unique_id=None):
        ledger = ledger_for(item["plan"])
        if item.get("skip"):
            report = dict(ledger.report(item["plan"]["item_ids"]), run_id=item["plan"]["run_id"], reason=item["reason"])
            return {"ui": {"text": [canonical(report)]}, "result": ("", canonical(report))}
        try:
            if prompt and workflow_fingerprint(prompt, unique_id) != item["spec"]["workflow_sha256"]:
                ledger.fail(item, "Workflow changed during execution", stale=True)
                raise ValueError("Workflow changed during execution")
            if images is None or images.ndim != 4 or images.shape[0] != 1 or images.shape[-1] not in (3, 4):
                raise ValueError("VerifiedSave requires exactly one IMAGE per input record")
            pixels = images.detach().cpu().numpy()
            if not np.isfinite(pixels).all():
                raise ValueError("Output contains NaN or infinity")
            array = (np.clip(pixels[0, :, :, :3], 0, 1) * 255.0).round().astype(np.uint8)
            target = ledger.save(item, Image.fromarray(array), expected_width, expected_height)
            context = runtime.get_context(item["plan"].get("prompt_id"))
            if context:
                runtime.mark_saved(context)
            report = dict(ledger.report(item["plan"]["item_ids"]), run_id=item["plan"]["run_id"])
            import folder_paths
            native_output = Path(folder_paths.get_output_directory()).resolve()
            ui_images = []
            if target.is_relative_to(native_output):
                ui_images.append({"filename": target.name, "subfolder": target.parent.relative_to(native_output).as_posix(), "type": "output"})
            return {"ui": {"images": ui_images, "text": [canonical(report)]}, "result": (str(target), canonical(report))}
        except Exception as exc:
            ledger.fail(item, f"Output verification failed: {exc}")
            raise


class BatchLedgerReport:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"plan": ("BATCHLEDGER_PLAN",)}}

    RETURN_TYPES = ("STRING",)
    FUNCTION = "report"
    CATEGORY = "BatchLedger"
    OUTPUT_NODE = True

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def report(self, plan):
        report = dict(ledger_for(plan).report(plan["item_ids"]), run_id=plan["run_id"])
        text = canonical(report)
        return {"ui": {"text": [text]}, "result": (text,)}


NODE_CLASS_MAPPINGS = {cls.__name__: cls for cls in (BatchLedgerPlan, BatchLedgerNext, BatchLedgerVerifiedSave, BatchLedgerReport)}
NODE_DISPLAY_NAME_MAPPINGS = {"BatchLedgerPlan": "BatchLedger · Plan", "BatchLedgerNext": "BatchLedger · Next Image", "BatchLedgerVerifiedSave": "BatchLedger · Verified Save", "BatchLedgerReport": "BatchLedger · Report"}
