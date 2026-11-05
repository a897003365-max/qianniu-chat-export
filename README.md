# qianniu-chat-export

一个可由 Codex、Kimi Code 或其他兼容 Agent 独立使用的 Windows Skill：通过千牛官方聊天历史页，按日期、客服和客户采集获准读取的消息，保存图片引用，并按需下载原图和运行本地 OCR。

## 能力

- 新建本次浏览器会话，不依赖旧 Session、既有任务或其他 Agent。
- 默认采一个会话的一页消息；支持明确扩大会话和消息范围。
- 保存原始响应、规范 JSONL、图片引用、分页断点和文件指纹。
- 消息、共享下载、OCR 分开运行；按图片内容共享缓存并保留引用和版本。
- 登录、验证、内容冲突和媒体错误有独立回执；抽样不冒充完整导出。
- 提供图文分类、Jev 与 Dify 的下游交接约束；模型与知识库适配不包含在采集器中。

已验证通道是 **官方历史页＋已登录 Chrome＋OpenCLI**。千牛客户端登录与 Chrome 登录分别核验；原生 `im.bizutil.ExportMsg`、`im.singlemsg.GetRemoteHisMsg` 调用桥尚未建立。

## 准备

需要 Windows、Python 3.11+、Node、已安装的 OpenCLI，以及与同一 Chrome 环境连接的 OpenCLI 扩展。先在 Chrome 登录有权查询的店铺账号。

仅采消息使用 Python 标准库。原图处理和离线测试需要 Pillow：

```powershell
python -m pip install -r requirements.txt
```

OCR 使用单独的 Python 环境，按需安装 `requirements-ocr.txt`。脚本不自动安装依赖、不复制浏览器登录凭据。

## 从零采一个会话

在仓库目录运行；输出放到仓库之外的短路径：

```powershell
python -X utf8 -B scripts/standalone.py doctor
python -X utf8 -B scripts/standalone.py collect --start 2026-11-03 --end 2026-11-04 --output C:/qn-data/sample
```

日期为示例，`--start` 包含、`--end` 排除，使用北京时间。默认最多一个会话、一页，页大小为 100；实际不足 100 条时如实保存。已有输出目录拒绝覆盖。

可选筛选：`--staff "店铺名:客服名"`、`--customer "完整客户昵称"`、`--expected-nick "当前登录账号"`、`--cid`。明确扩大范围使用 `--all-conversations --all-messages`，或设置 `--max-conversations`、`--max-pages`。

`doctor` 只检查依赖，`probe` 只检查登录身份；真实消息权限由一次有界 `collect` 验证。页面提示“Session 过期”时，使用回执中的淘宝登录入口在同一 Chrome 环境重新建立登录，再做一次有界核验。完整说明见 [独立采集](references/standalone.md)。

## 图片和 OCR

```powershell
python -X utf8 -B scripts/standalone.py media --output C:/qn-data/sample --stage downloads --workers 1
python -X utf8 -B scripts/standalone.py media --output C:/qn-data/sample --stage ocr --ocr-python C:/qn-ocr/Scripts/python.exe --workers 1
```

`run.json` 汇总状态；消息和图片引用位于 `unified/messages/<job_key>/`。完整区间与抽样分别生成 `message-receipt.json`、`sample-receipt.json`。原图和 OCR 使用共享媒体库。

## Agent 安装与同步

将本仓库作为一个完整 Skill 目录提供给 Agent，并读取 [SKILL.md](SKILL.md)。常见位置：

- Codex：`~/.codex/skills/qianniu-chat-export`
- Kimi Code：`~/.kimi-code/skills/qianniu-chat-export`
- 共享发现入口：`~/.agents/skills/qianniu-chat-export`

已有目录先核对差异并保留旧版本，不覆盖独立修改。同步只包含发布代码、说明和合成测试，并逐文件核对 SHA；聊天可能缓存旧说明，更新后重新加载 Skill。详见 [同步规则](references/kimi-sync.md)。

## 离线测试

```powershell
python -X utf8 -B scripts/run_tests.py
```

测试使用合成数据和模拟通道，不访问真实聊天、模型 API 或数据库。Node 用于生成的 JavaScript 离线验证，可用 `QIANNIU_TEST_NODE` 指定路径；测试入口使用短临时路径避免 Windows 路径长度干扰。

## 范围与发布边界

仅采集当前账号获准查询的内容。达到会话列表上限、来源延迟和缺失上下文时保留未知标记，不宣称全店完整。系统不发送业务消息，不提取 Cookie/令牌，不自动处理验证或无限重试。

仓库只发布代码、文档和合成 fixture。原始聊天、图片、OCR、模型响应、任务目录、运行登记和认证文件应保留在仓库之外。源码指纹清单明确排除 `.git`、缓存和本地任务数据。
