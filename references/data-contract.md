# 清单、消息和媒体证据

冻结的月范围查询上下文清单要求 `schema:2`、`scope:month`、`start/end/timezone/expected_nick/items`；时区为 `Asia/Shanghai`，范围 `[start,end)`。每项至少有 `query_user_nick/cid/user_id/lane/latest_seen_day`；发现证据及 coverage 原样保留。`expected_nick` 是登录身份，`query_user_nick` 是本条查询身份，不相互替代。生成路径见 [发现与月范围规划](discovery.md)。

上下文按完整查询三元组及区间标识。相同 CID 的不同查询账号或 user_id 仍是不同任务。`latest_seen_day` 只决定排序；所有上下文从统一 `end_ms-1` 倒序查询到起始边界。默认已验证请求为 `forward:false/count:100/needRecalledContent:true`，每页保留返回游标和终止原因。

官方历史页为 `https://market.m.taobao.com/app/qn/im-history-search/index.html`。`TimedBrowser` 只调用身份 `mtop.user.getUserSimple` 与消息 `mtop.taobao.wireless.amp2.im.paas.message.list`；不负责会话发现。页面使用现有 `lib.mtop.request({api,v:'1.0',ecode:0,data})`。CLI 返回成功不代表业务成功；身份成功也不能证明消息接口没有安全挑战。

列表 `mtop.taobao.wireless.amp2.paas.conversation.list` 的空账号筛选不保证全店覆盖，已知存在 500 条返回上限；未知 CID 和列表完整性仍需独立证据。不编造分页参数、客服名册或“全量已完成”。清单完整是输入项与分工完整，不等于全店枚举完整。

## 原始消息与复用

- `sendTime` 验证为毫秒，并核对 CID；保留原 `messageId`、发送者、正文、扩展字段及所有原始观测。
- 读取状态与顶层 bizUnique 的观测差异可保留为版本；正文和 ext 的合法 JSON 对象仅规范化键顺序比较，类型、数组顺序和真正内容差异仍严格。真实冲突记录为待复核，不能任选一个当事实。
- 缓存必须匹配完整 `{api,v,ecode,data}` SHA，其中 data 为精确七字段 `userNick/cid/userId/cursor/forward/count/needRecalledContent`；来源文件 bytes/SHA 在索引和使用时核验。命中响应继续接受实时分页相同的范围/游标/内容检查。
- 完整旧月回执经全部绑定核验后可采用。旧 day 回执只能贡献请求恰好相同的 raw 页，不能当完整月完成。未完成 checkpoint 也只能贡献已验证页；不读失败页冒充成功。
- 旧原页未记录 ecode 时，只能明确采用冻结代码固定 ecode=0 的契约证据，不能称原页实测。原件不改，派生页保留来源路径与哈希。

## 图片与 OCR

图片以解析后的 `content.type == 7` 判定；`content.data` 可为 JSON 字符串或对象。外层 contentType 不能替代。消息阶段仅写图片引用，不下载、不做 OCR。

共享下载以实际字节 SHA 标识图像、保持 URL 与每条消息引用。核验非空内容、文件哈希和解码尺寸；API 声明 size 可能为零或不符。URL 内容变化时保留证据并隔离，不以最近版本覆盖旧事实。

OCR 独立以图像 SHA 和完整 profile 标识任务，结果保持帧、文字、坐标和置信度；与客服原话分字段。已验证旧 OCR 保留实际来源及版本，不能伪称当前 profile 重新处理。相同图像可存在多份合法 OCR，现有版本不覆盖。

原件身份与识别内容身份分开。文件 SHA 用于字节完整性；`scripts/unified/ocr_content.py` 是内容分组的主版本，仅排除 `frames[].elapsed_seconds` 后计算完整识别对象的 SHA，其他字段、帧顺序和未知字段均保留。`MediaStore.ocr_versions()` 返回全部原件版本，`ocr_content_groups()` 返回经逐份校验的识别内容组及所有版本、来源引用；后者不运行 OCR，也不新增表或缓存。展示与知识整理使用内容组，审计使用原件及来源列表，不能把文件数或来源关系数当作 OCR 调用次数。

下游同步这份纯函数时记录主版本位置和逐字节 SHA；不要各自维护另一种纯文本去重规则。去重作用域是单条图片消息，同图在不同消息或上下文中的引用仍分别保留。进入模型前必须校验每项 OCR 的内容指纹及图片消息归属，并拒绝同一图片消息块内的等价重复；不能仅依赖页面隐藏重复项。

媒体来源索引只采用完整绑定或已验证消息原页支持的资产，查找后再验 SHA。恢复旧媒体应在发起新下载/OCR之前做有界验证，避免重复处理。媒体和消息的阶段完成互不冒充。
