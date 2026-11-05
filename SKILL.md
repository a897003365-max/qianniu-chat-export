---
name: qianniu-chat-export
description: 在 Windows 独立采集千牛客服聊天记录：检查环境、新建官方历史页会话，按日期/客服/客户导出消息和图片引用，再按需下载原图与 OCR。支持单 Agent 抽样或批量，不依赖旧 Session、主 Agent 或已有月清单。
---

# 千牛聊天导出 v5

默认走独立入口：检查环境 → 新建本次会话并绑定实际登录账号 → 按日期及账号筛选 → 消息和图片引用 → 可选原图下载/OCR。一个 Agent 可以完成全部选定上下文，无需其他 Agent、旧任务、旧 session/tab、月清单或 OCR 环境。

先读 [独立采集](references/standalone.md)。用本次加载的 Skill 目录定位 `scripts/standalone.py`，不要跳到其他平台副本或以前的任务目录找运行参数。`doctor` 自动查找现有 Python、Node、OpenCLI；`collect` 自行创建独立 session/tab，默认一会话一页，返回原始页、规范消息和图片引用。

已验证的读取通道是千牛官方历史页的 MTOP 接口，经 OpenCLI 使用已登录 Chrome。**千牛客户端登录不等于 Chrome 已登录。** `im.bizutil.ExportMsg`、`im.singlemsg.GetRemoteHisMsg` 的原生调用桥尚未建立，不能把接口名当成可调用接口。用户限定只用客户端且当前进程未开放可用通道时，明确说明该条件；不要反复试 F12、扫缓存或要求复用已过期 Session。

1. 日期采用 `Asia/Shanghai`，start 包含、end 排除。用户指定店铺/登录账号时传 `--expected-nick`；否则只绑定当前浏览器返回的真实登录身份。客服与客户筛选使用用户给定的完整账号，不猜账号、不要求员工名册。
2. 需要证明“接口可读/验证已解除”时，使用一次有界 `collect`，检查真实消息响应。`doctor` 只证明本地依赖，`probe` 只证明登录身份，均不证明消息权限。
3. 达到页数/会话限额如实记为抽样；500 条会话上限、空结果、最多一天的来源延迟都不证明全店完整。不同账号上下文不能仅按 CID 删除。
4. 消息不等待下载/OCR；原件、图片引用、多个消息/图片/OCR 版本和失败证据保留。安全验证、账号错误或通道故障停止本轮，不自动重试、换账号或恢复旧任务。
5. 权限限于授权读取和本地保存，不提取 Cookie/Storage/凭证，不发送业务消息，不重启千牛或更改调试配置。暂停中的历史任务不受一次新独立采集影响。

只有需要冻结大批量清单、明确双 owner 分工或接续已有批次时，才读 [发现与月范围规划](references/discovery.md) 和 [批次运行](references/operation.md)。普通独立采集不经过这个入口。两种入口共用消息解析和共享媒体模块。

核查消息格式与版本来源读 [数据约束](references/data-contract.md)；安装或跨平台同步读 [同步与分工](references/kimi-sync.md)。任务保存代码指纹，升级先保留旧版本并核验副本差异，不边跑边覆盖、手改指纹跳过检查。报告实际验证的阶段和本地结果路径。

采集结果需要进入 Jev 分类、Dify 或其他知识库时，读 [图文判断与知识交接](references/knowledge-handoff.md)。下游判断独立于采集，结构化角色、原始证据和审核状态必须保留。
