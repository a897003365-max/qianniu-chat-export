# 初始化、运行与验收

本节仅用于冻结清单批次。普通按日、按客服或抽样采集使用 [独立入口](standalone.md)，消息采集不需要预先具备 OCR 环境。

需要 Python 3.11+。初始化、队列和消息编排使用标准库；原图解码需要当前控制器 Python 的 Pillow，独立 OCR Python 需要实际可用的 RapidOCR/ONNX Runtime/Pillow。这些依赖由当前环境提供，不自动安装。Node/OpenCLI 指向当前已安装入口，OCR Python 显式提供；不复用旧任务浏览器标识。

执行包内浏览器适配器的离线 JavaScript 测试时，显式设置 `QIANNIU_TEST_NODE` 为现有 Node 可执行文件的绝对路径；测试只运行合成 DOM，不需要业务 runtime JSON 或已登录浏览器。

没有清单时，先按 [发现与月范围规划](discovery.md) 执行 `discover` → `plan-month`。发现目录与下面的 `$task` 必须分开，不能在已有发现目录上运行新任务初始化。

以下变量均由本次授权与环境确定：`$skill` 是已验收的包根；`$task` 是不存在的新数据目录；`$manifest` 是冻结的月范围查询上下文清单；`$python/$node/$opencli/$ocrPython` 是明确运行时路径。

```powershell
& $python -X utf8 -B "$skill/scripts/init_task.py" --task-root "$task" --manifest "$manifest" --node "$node" --opencli-script "$opencli" --ocr-python "$ocrPython"
```

`--manifest` 可重复；所有输入必须同区间/时区/登录身份，无重复查询上下文，合计 codex/kimi owner 数相差最多一。清单字节原样复制，owner 不重分配。初始化写 `skill-sync.json`、`codex-runtime.json`、`unified/state.sqlite3`，最后发布 `task-init.json`。输入清单和旧成果不改；既有目标目录拒绝覆盖。缺少最终回执的初始化不得当作成功，也不得自动清理旧目录重试。

任务的数据根和代码根分离；`skill-sync.master` 指向本次包根并列出相对文件 SHA。运行期间固定该代码版本。脚本变更或迁移路径后先重新核验来源，不手改哈希绕过差异。

有界阶段示例（`1` 是首轮观察规模，不代表最佳并发）：

```powershell
& $python -X utf8 -B "$skill/scripts/unified/pipeline.py" --task-root "$task" status
& $python -X utf8 -B "$skill/scripts/unified/pipeline.py" --task-root "$task" messages --owner codex --workers 1 --limit 1
& $python -X utf8 -B "$skill/scripts/unified/pipeline.py" --task-root "$task" messages --owner kimi --workers 1 --limit 1
& $python -X utf8 -B "$skill/scripts/unified/pipeline.py" --task-root "$task" downloads --workers 1 --limit 1
& $python -X utf8 -B "$skill/scripts/unified/pipeline.py" --task-root "$task" profile
& $python -X utf8 -B "$skill/scripts/unified/pipeline.py" --task-root "$task" ocr --workers 1 --limit 1
```

`profile` 只探测显式 OCR 运行时，记录 worker/模型及依赖指纹；不执行聊天 API。OCR 缺失 profile 时先修配置。普通消息运行不传 `--source-root`，该参数只用于显式限定单来源的有界验证；正常 claim 跨所有绑定来源按最新日期优先，没有旧批次门禁。`--limit` 限本次领取数，不是总任务覆盖声明。

验收分别读取队列、`unified/runs/<run>/completion.json` 与阶段证据：

- 消息：`unified/messages/<job_key>/` 的原始页、checkpoint、messages/image_refs JSONL、`message-receipt.json`。回执是 `messages_only`。
- 下载：统一媒体库的 URL→SHA、每消息引用、可解码原图、明确失败。相同 URL 不同 SHA 隔离并保留版本，不能覆盖选择一个。
- OCR：图像 SHA、profile、OCR 版本与每消息关联；无文字不等于图片无信息。下载与 OCR 错误不抹掉消息已完成事实。

认证、身份不符、滑块/验证码与系统性桥接错误需要人工处理时停止新 claim；单个内容冲突隔离后其他有效任务可继续。失败项、未结束 claim 不自动回收。恢复必须先确认原进程状态和平台验证已解除，再由本次主控明确处理；不提供盲目重跑命令。

已有成果迁移是单独操作，不由 `init_task.py` 自动执行。保留 `legacy_import.import_legacy`、`legacy_assets` 的只读校验原语；规划来源绑定与完整/部分回执口径后，在独立输出中采用。新建任务不复制旧 runtime、活动 target 或退休登记。不要用迁移工具替用户停止进程。

历史并发队列可能曾各用一份本地 OCR 缓存，启动时复制种子也不能共享其他队列之后的新结果。恢复与新增工作继续使用同一任务的共享媒体库，不启用已退休的分片 OCR 路径。迁移保留每份原件后，先检查识别内容组数，再交给知识消费者；运行耗时差异不代表新增业务证据。当前共享领取会跳过已有 OCR 结果的图片，缺少历史 profile 不触发自动重算。
