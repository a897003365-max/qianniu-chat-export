# 从授权发现到统一任务

本节用于需要冻结清单、双 owner 分工的批次。一个 Agent 从零采集时，先用 [独立入口](standalone.md)，无需手工建立 session/tab 或先生成月清单。

无需用户先提供内部 manifest。已打包的 `qianniu_export.py discover` 负责初始按日发现，`plan-month` 将已观察三元组变成冻结的月范围查询上下文清单，再交 `init_task.py` 和三个独立阶段。这里的“月范围”不代表会话枚举完整。

## 变量与独立后台 context

由本次授权填写 `$start/$end`（`YYYY-MM-DD`，起始包含、结束排除，`Asia/Shanghai`）、`$expectedNick`（预期登录昵称）、`$discoveryRoot`（发现证据目录）和 `$task`（尚不存在的新统一任务目录）。两目录必须分开。`$skill/$python/$node/$opencli/$ocrPython` 为当前已验收包与实际运行时路径。规划命令必须显式传 start/end，没有内置日期默认值。

创建新的 session、唯一 URL 标记及后台 tab，不向已有采集 session 新建或导航标签页。以下只读访问已登录 Chrome，不获取认证状态值：

```powershell
if ([IO.Path]::GetFullPath($discoveryRoot) -eq [IO.Path]::GetFullPath($task)) { throw 'Discovery and new task roots must differ' }
$marker = [Guid]::NewGuid().ToString('N')
$session = "qn-discovery-$marker"
$url = "https://market.m.taobao.com/app/qn/im-history-search/index.html?qn_discovery=$marker"
$created = (& $node $opencli browser tab new $url --session $session --window background) | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $created.ok -ne $true) { throw 'New discovery context failed' }
$tab = $created.data.page
$listed = (& $node $opencli browser tab list --session $session --window background) | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $listed.ok -ne $true -or @($listed.data | Where-Object { $_.page -eq $tab }).Count -ne 1) { throw 'Discovery target is not uniquely registered to this session' }
$locationJS = "JSON.stringify({origin:location.origin,path:location.pathname,marker:new URL(location.href).searchParams.get('qn_discovery'),fragment:location.hash})"
$location = (& $node $opencli browser eval $locationJS --tab $tab --session $session --window background) | ConvertFrom-Json
if ($LASTEXITCODE -ne 0 -or $location.ok -ne $true) { throw 'Discovery route verification failed' }
$observed = $location.data | ConvertFrom-Json
if ($observed.origin -ne 'https://market.m.taobao.com' -or $observed.path -ne '/app/qn/im-history-search/index.html' -or $observed.marker -ne $marker -or $observed.fragment -notin @('', '#/')) { throw 'Unexpected discovery target route' }
```

仅检查上述公开路由字段和本次随机标记，不打印完整 location、Cookie 或 Storage。随后 `discover` 会先调用身份接口，与 `$expectedNick` 精确比较；账号不符、页面未就绪、认证/安全验证或 CLI 错误都停止，不自动重试。

## 初始发现 → 规划 → 初始化 → 三阶段

```powershell
& $python -X utf8 -B "$skill/scripts/qianniu_export.py" discover --start "$start" --end "$end" --expected-nick "$expectedNick" --output "$discoveryRoot" --node "$node" --opencli-script "$opencli" --session "$session" --tab "$tab"
if ($LASTEXITCODE -ne 0) { throw 'Discovery stopped; retain evidence and resolve the cause' }
$manifest = Join-Path $discoveryRoot 'month-manifest.json'
& $python -X utf8 -B "$skill/scripts/qianniu_export.py" plan-month --root "$discoveryRoot" --output "$manifest" --start "$start" --end "$end"
if ($LASTEXITCODE -ne 0) { throw 'Month-range planning stopped' }
& $python -X utf8 -B "$skill/scripts/init_task.py" --task-root "$task" --manifest "$manifest" --node "$node" --opencli-script "$opencli" --ocr-python "$ocrPython"
if ($LASTEXITCODE -ne 0) { throw 'Initialization stopped' }
& $python -X utf8 -B "$skill/scripts/unified/pipeline.py" --task-root "$task" messages --owner codex --workers 1 --limit 1
```

消息首轮验收后，按 [统一任务运行](operation.md) 分别执行另一获准 owner、共享 `downloads`、`profile` 和独立 `ocr`。这些是独立阶段，不把连续命令当作遇错仍自动执行的脚本；先检查每个阶段退出码和回执。

`discover` 从最新日期倒序，写：

- `discovery-scope.json`：start/end/expected_nick/timezone。
- `discovery/<day>.json`：原始 API 响应；同名 `<day>.receipt.json` 记录 day 和文件 SHA。
- `manifest.unassigned.json`：schema 1 的初始日/会话结果，未分 owner。

不要手改或重命名 `manifest.unassigned.json` 伪造月清单。`plan-month` 实际读取 discovery-scope 与有哈希回执的逐日响应（不是消费 unassigned 中的 lane），按 `(query_user_nick,cid,user_id)` 归并，最新日期优先交替分配 codex/kimi，输出新 schema 2 文件。已有规划输出拒绝覆盖；新发现应另建清单并重新核验分工。初始响应缺日会记录 `missing_initial_days`，500 条及以上记录 `capped_source_indexes`，`enumeration_complete` 始终 false。

当前 `discover` 固定 `selfWwNick:null,targetWwNick:null`，每次 beginDate/endDate 都是同一日 `YYYYMMDD`。这仅是初始查询范围，不能称全店或所有客服。空列表不证明当天无聊天。安全验证需要人工时停止请求，保留既有成功回执，不尝试绕过或用重试撞开。

## 已授权/已观察账号筛选的补充证据

本包没有“批量员工发现”的 CLI 参数；不存在可直接使用的 `discover --staff` 命令，也不要求先做人员名册审计。只有在用户授权范围内，按已观察身份扩展有限查询；不能猜测账号名。消息阶段的 TimedBrowser 白名单不含会话发现接口，不将其绕作发现器。

已有验证的会话请求字段是：

```javascript
{ selfWwNick: "cntaobao" + observedNick, targetWwNick: null,
  beginDate: day.replaceAll("-", ""), endDate: day.replaceAll("-", "") }
```

若另行获准的有限采集器已经按这些字段保存成功响应，将补充目录 `$supplement` 按 `month_support.plan_month` 的实际输入契约组织；以下是字段结构，不是假装已经执行的采集命令：

```text
scope.json:
  { "start": start, "end": end, "expected_nick": expectedNick }
query-counts.json:
  [{ "day": day, "count": actual_result_length,
     "response_file": "relative-response.json" }]
relative-response.json:
  { "api": "mtop.taobao.wireless.amp2.paas.conversation.list", "v": "1.0",
    "request": { "selfWwNick": "cntaobao" + observedNick, "targetWwNick": null,
                 "beginDate": YYYYMMDD, "endDate": YYYYMMDD },
    "response": original_mtop_response }
```

`response` 必须是实际 MTOP 响应对象，不是 OpenCLI 的 `{ok,data}` 外壳；其成功 `ret` 和 `data.result`（或旧返回的顶层 result）经 `q.api_data` 读取。每行需要非空 `cid.appCid`、`userID.appUid`，displayName 如实保留。scope 须与初始发现区间和登录昵称一致，day 在授权区间，查询昵称与登录昵称的店铺前缀一致，index 的 count 等于实际 result 长度，response_file 不得重复或逃出补充根。规划时计算每份包装文件的 SHA 并写入 discovery_sources；补充目录不要求虚构初始发现式 receipt。

用新输出文件纳入补充来源：

```powershell
$expandedManifest = Join-Path $discoveryRoot 'month-manifest-expanded.json'
& $python -X utf8 -B "$skill/scripts/qianniu_export.py" plan-month --root "$discoveryRoot" --output "$expandedManifest" --start "$start" --end "$end" --supplemental-discovery "$supplement"
```

需要改初始响应位置时，可传真实已有的 `--initial-discovery` 目录；scope 仍从 `--root/discovery-scope.json` 读取。补充查询返回 hasMore=true 而未收齐时规划停止；达到 500 条仍保留截断标记，不能通过多账号筛选宣称未知 CID 覆盖已经解决。将验收后的新 manifest 传给新的初始化目录，或由主控单独规划已有任务的增量，不覆盖活动任务来源绑定。
