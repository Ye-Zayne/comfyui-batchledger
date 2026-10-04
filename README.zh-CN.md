# ComfyUI BatchLedger

**v0.1.0：按结果是否仍有效，决定哪些图片需要重跑。**

四个 V1 节点，适用于单机、单图、串行工作流。SQLite 账本记录输入 SHA-256、实际图像上游工作流参数、显式模型版本和每条记录的 seed。输出必须能够解码、符合指定尺寸，并与账本中的 SHA-256 一致，才算完成。

[English](README.md) · [真实 API 工作流](examples/workflow_api.json)

## 安装与连接

把本仓库放入 `ComfyUI/custom_nodes/comfyui-batchledger`，重启后搜索 **BatchLedger**。需要 Python 3.10+，复用 ComfyUI 自带的 Pillow、NumPy、PyTorch 和 Python 标准库 SQLite，无须下载新模型。

连接方式：

```text
Plan.plan ──→ Next.plan
Next.image ──→ 现有图像处理链 ──→ Verified Save.images
Next.item  ──────────────────→ Verified Save.item
Next.seed  ──→ KSampler.seed（生成工作流）
Next.prompt ─→ 文本编码器（清单中含逐条提示词时）
```

| 节点 | 用途 |
| --- | --- |
| `BatchLedgerPlan` | 从文件夹或 JSON/CSV 清单规划；输出 plan 和报告 JSON |
| `BatchLedgerNext` | 领取一条记录；输出 image、mask、seed、prompt、record_json、item |
| `BatchLedgerVerifiedSave` | lazy 图像输入；临时 PNG 解码与尺寸检查后原子提交 |
| `BatchLedgerReport` | 查看状态、尝试次数、错误和输出位置 |

每个工作流只能使用一个 Plan、Next、Verified Save。删除额外的 SaveImage/PreviewImage 输出节点；它们会强制计算自己的上游，导致 lazy 跳过失效。Report 可以同时存在。v0.1 使用普通平面 API 图，不支持嵌套子图和动态扩展图。

`auto_queue=true` 会在整个 prompt 成功，或领取后的记录失败时，把下一项追加到 ComfyUI 队列；无需浏览器保持连接。使用正常队列优先级。最后会多执行一次只生成报告的 prompt，不请求昂贵的图像分支。`auto_queue=false` 则每次手动排队处理一条。

## 输入、路径和模型身份

默认输入路径限定在 ComfyUI 的 `input` 根下，输出限定在 `output` 根下。例如将图片放在 `input/batchledger_demo`，设置 `folder=batchledger_demo`、`output_folder=batchledger_demo`。支持 PNG/JPEG/WebP/BMP/TIFF 单帧图片；会处理 EXIF 方向和 alpha 蒙版。路径或符号链接越过配置根目录会被拒绝，输出目录不能位于扫描的输入文件夹中。

需要其他根目录时，由启动 ComfyUI 的宿主设置 `BATCHLEDGER_INPUT_ROOT`、`BATCHLEDGER_OUTPUT_ROOT` 环境变量；目录必须已经存在。工作流本身不能选择任意根路径。自定义输出位于原生 output 根之外时，返回路径和报告，但不提供原生图片预览。

`manifest` 可填写 input 根下的 JSON/CSV 文件，或在 `manifest_json` 中放入 JSON。不能同时设置两者。清单模式优先于 folder。

```json
[
  {"id":"shot-001","path":"batchledger_demo/a.png","seed":42,"prompt":"柔和日光"},
  {"id":"shot-002","path":"batchledger_demo/a.png","seed":43,"prompt":"暖色夕阳"}
]
```

必须有 `path`；同一输入用于多个镜头时，提供不同 `id`。CSV 列名相同，参考 [manifest.csv](examples/manifest.csv)。未指定 seed 时，由 `base_seed` 和记录身份确定，新增文件不会改变已有图片的 seed。其他清单字段通过 `record_json` 输出，并保守地计入指纹。

`model_fingerprint` 必填。例如 `checkpoint_sha256=…; vae_sha256=…; nodepack=1.2.0`。同名模型内容改变时，需要更新它；也建议写入影响结果的 ComfyUI/节点包版本。该包不会凭文件名猜测模型内容，也不会自动计算全部模型 hash。无模型示例使用 `no-model:ImageScale:v1`。

工作流指纹只沿 Verified Save 的图像上游收集节点类、输入值和链接输出，忽略画布节点 ID、标题、布局、无关分支和输出文件名。再叠加每条记录的输入 hash、seed、提示词和清单字段。它用于判断结果能否复用，不保证不同 GPU 或注意力实现逐像素一致。

## 恢复和选择性重跑

| run_mode | 选择范围 |
| --- | --- |
| `pending_and_invalid` | 新输入、指纹变化、输出损坏及此前取消的记录 |
| `failed_only` | 已经达到重试上限的失败记录 |
| `invalid_only` | 指纹或输出完整性发生变化的记录 |
| `force_all` | 本次所有当前记录各处理一遍 |

`max_retries=1` 表示一次初始执行加最多一次重试，每条记录、每次 run 分别计数；范围 0–3。达到上限后记为 failed，当前 run 不再选中，继续后续记录。OOM 和确定性错误也受同一上限约束，不会偷偷改变 seed、分辨率或生成参数。对于重复尝试无用的错误可设为 0；修复临时原因后用 `failed_only` 发起新的手动 run。若修复改变输入字节、图参数或模型身份，会产生新的 invalid 版本，此时使用 `pending_and_invalid` 或 `invalid_only`。

原生 **Cancel** 对应 `execution_interrupted`：记为取消，并且不会自动重排。手动排队创建新 run，可恢复 cancelled 记录。API 用户可从报告读取 run_id，并停止活动 run 及其排队的续项：

```sh
curl -X POST http://127.0.0.1:8188/batchledger/stop \
  -H 'Content-Type: application/json' \
  -d '{"run_id":"报告中的RUN_ID"}'
```

自动 run 内冻结输入清单，避免每处理一张都重读整个批次。领取时和提交前重新核对当前输入，批次结束时再次核对全部已完成记录。运行中修改源文件、输出或规划参数时，不会把不符的结果认证为完成；请重新手动排队生成新计划，以纳入新增图片和清单修改。

账本路径为 `output/<output_folder>/.batchledger.sqlite3`。输出名包含源文件 stem 和完整 item digest，同名图片不会冲突。事务保证每个账本同时只有一个活动 claim，竞争的 prompt 返回 busy；每个 output_folder 同时只运行一个批次。进程崩溃后，新计划会恢复已退出 PID 留下的 claim。PNG 文件替换与数据库提交属于不同系统，断电可能留下文件但没有完成记录；这种记录会在恢复后重新生成，而不是仅靠文件存在就跳过。

## API 示例与测试

示例只使用核心 ImageScale，不需要 checkpoint：

```sh
python examples/run_api.py --server http://127.0.0.1:8188 --make-demo /绝对路径/ComfyUI/input
```

它创建两张不存在时才写入的示例图，将 [workflow_api.json](examples/workflow_api.json) 提交到 `/prompt`，等待自动批次并输出报告。再次运行会跳过完整结果；修改 ImageScale 的尺寸及 Verified Save 对应期望尺寸，会让原结果失效。API JSON 应提交到 `/prompt`，不是画布导入格式。

在仓库根目录运行：

```sh
python -m pytest --rootdir=tests --confcutdir=tests tests -q
```

测试覆盖内容/参数/模型/seed 变化、损坏输出、并发领取、有限重试、取消、保存中源文件变化、计划被替换、进程恢复、路径/符号链接越界和 lazy 分支，并隔离验证生命周期适配器。适配器接口已对照 ComfyUI 0.33.1 源码；自动批次请使用当前 ComfyUI 版本。

v0.1.0 已在 ComfyUI 0.33.1 CPU 模式下实跑，无需 WebSocket：2 张图片通过 3 个 prompt 自动完成；再次恢复不增加尝试次数；损坏一个 PNG 后仅重做该项；一个无法解码的输入尝试 2 次后记为 failed，继续完成后面的正常图。每个 prompt 的 BatchLedgerReport 均可读。可复验：

```sh
python tests/integration_api.py --server http://127.0.0.1:8188 --input-root /绝对路径/ComfyUI/input --output-root /绝对路径/ComfyUI/output
```

v0.1 每条记录只接受一张静态图，不处理分布式任务、视频/音频、多图批次、采样步骤断点、自动模型/环境安装或参数降级。仍使用 ComfyUI 原生缓存管理，不修改执行器缓存。
