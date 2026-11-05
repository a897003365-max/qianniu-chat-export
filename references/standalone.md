# 一个 Agent 独立完成采集

`$skill` 是本次 Skill 的实际加载目录；`$out` 是用户任务下尚不存在的输出目录。使用本机 Python 3.11+，无需旧任务路径、session、tab、manifest、Codex/Kimi owner 或 OCR Python。路径有空格时保持引号。

## 最短调用

```powershell
python -X utf8 -B "$skill/scripts/standalone.py" doctor
python -X utf8 -B "$skill/scripts/standalone.py" collect --start 2026-11-03 --end 2026-11-04 --output "$out"
```

日期是示例，按用户要求填写。end 为排除边界，因此示例只查询 2026-11-03。若用户说“昨天”，按当前北京时间算日期，不照抄示例。默认查询当前已登录账号可见范围，选择返回列表中的一个上下文、最多一页（API count=100）；返回不足 100 条就如实报告，不凑数。没有命中会话时保留空结果，不宣称当天无记录。

若 `doctor` 找不到依赖，Agent 先检查当前 PATH、OpenCLI 安装位置；允许显式传 `--node`、`--opencli-script`，或设置 `QIANNIU_NODE`、`QIANNIU_OPENCLI_SCRIPT`。不自动安装或升级依赖。只采消息需要标准库；Pillow 和 RapidOCR 不作为消息前置条件。

## 指定客服、客户与范围

```powershell
python -X utf8 -B "$skill/scripts/standalone.py" collect --start 2026-11-03 --end 2026-11-04 --staff "店铺名:客服名" --customer "完整客户昵称" --expected-nick "当前登录账号" --output "$out"
```

三个账号参数都可按需省略；示例值必须替换。`--staff` 是完整客服账号，不是显示名；`--customer` 是完整客户昵称。脚本给查询字段加 `cntaobao` 前缀，并检查客服店铺前缀与实际登录身份一致。`--expected-nick` 必须精确匹配当前登录昵称，不能拿查询客服身份代替登录身份。

明确采集该日期范围全部已发现上下文与区间内消息：

```powershell
python -X utf8 -B "$skill/scripts/standalone.py" collect --start 2026-11-03 --end 2026-11-04 --all-conversations --all-messages --output "$out"
```

也可用 `--max-conversations 5 --max-pages 2` 有界采集；同组的 `--all-*` 与 `--max-*` 互斥。`--cid` 只从本次发现的合法上下文选择，不接受编造 userId。一个 Agent 执行全部选中工作，不固定分半。每个新调用使用新目录和新 session，拒绝覆盖现有目录。

## 登录、过期 Session 与客户端差异

支持的通道为 `official_history_h5_via_opencli`：OpenCLI 在已登录 Chrome 中创建本次官方页面上下文，并校验 session、target、官方路径和登录身份。旧 OpenCLI Session 过期时直接运行新命令，不重连旧标识、不继承旧 target。

官方页面：`https://market.m.taobao.com/app/qn/im-history-search/index.html`。Chrome 需要已连接可用的 OpenCLI 扩展。若只有千牛客户端登录，或聊天页提示“Session 过期”，先在同一 Chrome 环境打开淘宝登录入口 `https://login.taobao.com/member/login.jhtml`，完成授权账号登录；不复制客户端凭据。`auth_required` 回执的 `manual_entry_url` 已给出带官方历史页返回地址的完整登录链接。当前没有已验证的原生 `im.bizutil.ExportMsg` / `im.singlemsg.GetRemoteHisMsg` 调用桥，不能承诺任意千牛安装都能直接从 Python 调原生接口。用户只允许客户端时，报告通道条件缺失，不暗中换浏览器、重启或改调试参数。

需要代为打开登录入口时，用已连接 OpenCLI 的 `browser chrome describe windows.create` 核对能力，再通过 `browser chrome call windows.create` 创建普通、聚焦的新窗口，URL 使用回执给出的登录链接。该窗口不绑定临时采集 session，便于用户完成登录。不要在另一个浏览器 profile 登录后假定采集通道也恢复。

登录入口可能自动跳转到天猫商家中心。该跳转是新的登录状态信号，随后做一次新目录的有界 `collect`，以精确身份匹配和真实消息 API 成功确认恢复；不能仅凭跳转就判定通过。若仍 auth_required 就停止，等待用户处理账号登录。只刷新聊天查询页或反复建立 OpenCLI Session 不等于刷新淘宝登录。

```powershell
python -X utf8 -B "$skill/scripts/standalone.py" probe --output "$out"
```

`probe` 仅新建上下文并验证登录身份，不查询会话和消息。要检查验证码是否仍阻塞消息，用默认有界 `collect`；身份成功、页面看不到滑块、旧文件显示 complete 都不能替代真实消息 API 成功。

`run.json` 的 `error_code/action/manual_entry_url` 给出下一步。出现验证时停止本轮，在普通 Chrome 官方页面选择日期、查询并打开一个会话，按页面提示完成验证，再明确执行新的采集。不要不断重跑，没有弹窗时也不假设验证通过。

## 输出与图片

- `run.json`：本轮状态、筛选范围、计数、完成/抽样/失败及结果文件 SHA。
- `conversations.json`：本次查询范围的会话列表、上下文与 500 条触顶标记。
- `discovery/<day>.json`：查询参数与原始返回。
- `unified/messages/<job_key>/`：原始消息页、checkpoint、`messages.jsonl`、`image_refs.jsonl`。图片字段保留来源 URL、原始图片信息和消息关联，不把网页占位提示当图片 OCR。
- 完整区间回执为 `message-receipt.json`；页限抽样为 `sample-receipt.json`，不能混用完整性结论。

原图下载和 OCR 为独立命令，消息已保存后按用户需求执行：

```powershell
python -X utf8 -B "$skill/scripts/standalone.py" media --output "$out" --stage downloads --workers 1
python -X utf8 -B "$skill/scripts/standalone.py" media --output "$out" --stage ocr --ocr-python "$ocrPython" --workers 1
```

这里 `$out` 使用原采集目录。下载需要控制器 Python 的 Pillow；`$ocrPython` 指向已安装 RapidOCR/ONNX Runtime/Pillow 的 Python，OCR 不自动安装。共享缓存按图片 SHA 保留引用和版本；未知提交结果、未决领取及失败项不会自动重试。可用 `--limit 1` 验证媒体阶段。媒体入口只接受本工具的独立采集回执，不会恢复暂停中的旧批次。

## 实际接口契约

| 用途 | 接口与已验证字段 |
| --- | --- |
| 登录身份 | `mtop.user.getUserSimple`，`data.nick` |
| 会话列表 | `mtop.taobao.wireless.amp2.paas.conversation.list`；`selfWwNick/targetWwNick` 可为空，beginDate/endDate 为同日 YYYYMMDD；返回 `data.result`，每条 `cid.appCid/userID.appUid` |
| 消息分页 | `mtop.taobao.wireless.amp2.im.paas.message.list`；`userNick/cid/userId/cursor/forward=false/count=100/needRecalledContent=true`；返回 `userMessages/hasMore/nextCursor` |

该契约来自官方历史页前端及已验证响应。会话列表没有已建立的分页契约；不要把消息的 cursor/count 套到会话枚举上。前端显示最多三个月、数据最多延迟一天；超出可见范围或达到 500 条时单列限制，不能声称全店全量。
