# ComfyUI BatchLedger

**v0.1.0 — rerun the inputs whose results are no longer valid.**

BatchLedger is a four-node package for single-image, sequential workflows. It records the input SHA-256, image-producing workflow parameters, explicit model identity, and per-record seed in a SQLite ledger. A nonempty output file alone never counts as success: the saved PNG must decode, match the requested dimensions, and match its recorded hash.

[中文说明](README.zh-CN.md) · [Runnable API workflow](examples/workflow_api.json)

## Install

Clone this repository into `ComfyUI/custom_nodes/comfyui-batchledger` and restart ComfyUI. Search the node menu for **BatchLedger**. Python 3.10+ is required; Pillow, NumPy and PyTorch come from ComfyUI. No model downloads or API credentials are needed.

The nodes use the V1 `NODE_CLASS_MAPPINGS` interface and lazy inputs. The lifecycle adapter targets ComfyUI's `PromptServer.send_sync`, `PromptExecutor.add_message`, prompt queue, and prompt validator. Its signatures were checked against ComfyUI 0.33.1; use a current ComfyUI build for automatic batching. The adapter observes and forwards the original server/history messages, and does not require a browser connection.

## Nodes and connections

| Node | Inputs / outputs | Purpose |
| --- | --- | --- |
| `BatchLedgerPlan` | folder or JSON/CSV manifest; model identity; run mode → plan, report JSON | Build a fresh batch and classify current records |
| `BatchLedgerNext` | plan → image, mask, seed, prompt, record JSON, item | Claim and decode one input record |
| `BatchLedgerVerifiedSave` | item and lazy images; expected dimensions → output path, report JSON | Verify and atomically commit exactly one PNG |
| `BatchLedgerReport` | plan → report JSON | Inspect status, attempts and output locations |

Connect `Plan.plan → Next.plan`, `Next.image → your existing processing chain → VerifiedSave.images`, and **`Next.item → VerifiedSave.item`**. In generative workflows, connect `Next.seed` to the sampler seed and `Next.prompt` to the text encoder. Other per-record fields are available through `record_json`.

Use exactly one Plan, Next and Verified Save. Verified Save must own the image output branch: remove additional SaveImage/PreviewImage output nodes. Otherwise those outputs would force expensive upstream nodes to run even when the ledger save's image input is lazy. Report may coexist. Nested subgraph wrappers and dynamically expanding branches are not supported in this release; use an ordinary flat API graph.

Set `auto_queue=true` to process the remaining records on the server. It appends the next prompt **after whole-prompt success**, or after a claimed record's failure, and uses normal queue priority. With `auto_queue=false`, queue manually once per record. The final automatic prompt produces a report without requesting the expensive image branch.

## Inputs and model identity

Paths are relative to ComfyUI's `input` root. Put images in `input/batchledger_demo/` and set `folder=batchledger_demo`. Recursive folder mode accepts PNG, JPEG, WebP, BMP and TIFF; animated/multipage inputs are rejected when decoded. EXIF orientation and alpha-derived masks are handled. Output folders are relative to ComfyUI's `output` root. Escaping those roots, including through symlinks, is rejected. A scanned input folder cannot contain the output folder.

For other locations, the host administrator can set **`BATCHLEDGER_INPUT_ROOT`** and/or **`BATCHLEDGER_OUTPUT_ROOT`** before launching ComfyUI. Those configured directories must already exist. The graph cannot choose arbitrary root directories. Outputs outside the native ComfyUI output directory return paths and reports but have no built-in image preview.

Use either `manifest` (a JSON/CSV filename beneath the input root) or `manifest_json` (inline JSON). Both take precedence over folder mode; do not set both. Example:

```json
[
  {"id": "shot-001", "path": "batchledger_demo/a.png", "seed": 42, "prompt": "soft daylight"},
  {"id": "shot-002", "path": "batchledger_demo/a.png", "seed": 43, "prompt": "warm sunset"}
]
```

`path` is required. Give repeated input paths distinct `id` values. A CSV uses the same column names; see [manifest.csv](examples/manifest.csv). Without an explicit seed, the seed is derived from `base_seed` and record identity, so adding another file does not change existing seeds. Extra manifest fields are conservatively included in each fingerprint because the graph can consume `record_json`.

`model_fingerprint` is required. Supply model SHA-256 values, release versions, and relevant node-pack/core versions yourself, for example `checkpoint_sha256=…; vae_sha256=…; my-nodepack=1.2.0`. If a model file changes while retaining its filename, update this value. BatchLedger does **not** guess model content from filenames. For the no-model demo, use `no-model:ImageScale:v1`.

The workflow fingerprint follows the actual image ancestors of Verified Save and includes node class names, input values and link outputs. It excludes editor IDs, UI titles/layout, disconnected branches and output filenames. Its per-record terminals add input hash, seed, prompt and manifest fields. These signatures protect result reuse; they do not guarantee cross-GPU bitwise determinism.

## Recovery and selective reruns

| Run mode | Selected records |
| --- | --- |
| `pending_and_invalid` | New, changed, damaged-output or previously cancelled records |
| `failed_only` | Records with a terminal failure |
| `invalid_only` | Changed fingerprints or outputs that failed integrity checks |
| `force_all` | All current records, once each in this run |

`max_retries=1` means one initial attempt plus at most one retry per selected record, per run; allowed values are 0–3. An exhausted record is recorded as failed and skipped for the rest of that run. This includes OOM and deterministic processing errors; no seed, dimensions or image-generation settings are silently changed. Use zero retries where another attempt is wasteful. Fix transient causes and choose `failed_only` for a new manual run. If the fix changes input bytes, graph parameters or model identity, it creates a new invalid version; select `pending_and_invalid` or `invalid_only` instead.

Native **Cancel** (`execution_interrupted`) marks the run cancelled and never automatically requeues it. A manual queue creates a fresh run and can resume cancelled records. API users can stop an active run and remove its queued continuation with:

```sh
curl -X POST http://127.0.0.1:8188/batchledger/stop \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"COPY_FROM_REPORT"}'
```

The input manifest is frozen within an automatic run; this avoids repeatedly hashing the entire batch for every image. Each selected input is checked again when decoded and immediately before output commit. Completed records are checked again once at batch end. A changed input, overwritten output or changed plan is reported rather than certified as complete. Queue a fresh run to incorporate newly added/changed files or manifest changes.

The SQLite ledger lives at `output/<output_folder>/.batchledger.sqlite3`; PNG filenames contain the source stem and full item digest, so duplicate stems do not collide. Transactions allow one live record claim per ledger. Live competing prompts return `busy`; run one batch per output folder at a time. After a process crash, a new plan recovers claims whose owning PID is no longer alive. Filesystem replacement and SQLite commit are separate operations: after a power loss between them, the PNG may exist without a completion record, and the record is safely regenerated after recovery. Existing valid outputs are never trusted without their recorded SHA-256.

## Try the actual ComfyUI API

The example uses core ImageScale and requires no checkpoint:

```sh
python examples/run_api.py --server http://127.0.0.1:8188 --make-demo /absolute/path/to/ComfyUI/input
```

This creates two small fixtures if absent, submits [workflow_api.json](examples/workflow_api.json) to `/prompt`, waits for the automatic queue chain and prints the ledger report. Run it again to verify completed records are skipped; change ImageScale width/height and the corresponding expected dimensions to invalidate those results. API JSON is intended for `/prompt`, not the canvas workflow loader.

## Tests

From this repository root:

```sh
python -m pytest --rootdir=tests --confcutdir=tests tests -q
```

Tests cover structural fingerprints, model/seed/content changes, duplicate stems, corrupted and replaced outputs, concurrent claims, limited retries, cancellation, source changes during save, superseded plans, crash recovery, root/symlink escapes, and lazy branch behavior. ComfyUI lifecycle and requeue logic also have isolated adapter tests. Real API execution requires a running ComfyUI server.

The v0.1.0 smoke test passed against ComfyUI 0.33.1 on CPU without a WebSocket: two fresh records completed across three prompts; resume added no attempts; only a corrupted output was regenerated; an undecodable input exhausted two attempts and processing continued to the next good image. BatchLedgerReport returned status in each prompt. Reproduce with:

```sh
python tests/integration_api.py --server http://127.0.0.1:8188 --input-root /absolute/ComfyUI/input --output-root /absolute/ComfyUI/output
```

## v0.1 boundaries

This release handles one static image per input record on a single machine. It has no distributed workers, video/audio batches, automatic model hashing, model/environment installation, sampler-step checkpoints, or automatic parameter fallback. Keeping a sampler's parameters and models fixed does not remove GPU/attention implementation nondeterminism. Normal ComfyUI cache management remains in use; BatchLedger does not patch executor caches.
