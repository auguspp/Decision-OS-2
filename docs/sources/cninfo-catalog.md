# CNINFO 公告目录：首批来源接收与精确引用（#12）

本责任组从旧 Kernel 固定版本 `cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8` **窄复用**以下已验语义，而非复制旧 `runtime/` 任务状态：

- `src/decision_kernel/adapters/cninfo.py`：确切证券代码 → 官方 orgId（不得算法猜测）、公告 ID/标题、原始毫秒发布时间、官方 PDF 静态主机定位。
- `src/decision_kernel/runtime/cninfo_http.py`：一只证券、明确日期窗口、请求表单、跨页合计/重复/空页拒绝；后继预约和单PDF的历史合同在 `docs/b2-disclosure-appointments.md`。
- `src/decision_kernel/runtime/disclosure_radar.py`：公告目录不代替重要性/研究优先级；#7 决定经济分析而不是 #12 自动研判。

OS2 保留这些直接消费者所需的规则，以 Python 标准库纯转换 + 固定HTTPS有限请求 + **已存在的** `VersionedStore` 表达，不引回旧 `EvidenceArtifact`、旧 GitHub research_runs、付费API、多供应商fallback、重复数据库或定时平台。

## 具体能力和未完成边界

本组只负责一只六位证券、一个 0—31 日窗口的**正式公告目录**。组织 ID 必须来自来源查询，页大小30且最多4页；全页总数、每页长度、重复公告ID、证券/组织身份和发布时间都须一致，否则整批不合格，不保存任何条目。目录中保留公告原始 ID、类型、来源发布时间（上海时区的完整时间）、官方静态 PDF HTTPS locator，以及实际查询范围与获取时间。

目录返回的 `adjunctUrl` 只是PDF**定位**；首批绝不下载或保管 PDF、声称已读财务附注或判断报告真实内容。不擅自解析目录标题中的事件实施日、报告期、经济影响、券商预期或正式Decision。`SourceInput(custody="external", text=None)` 将公告目录中的每条原文 locator 按唯一来源身份保存为 `SourceRef`，manifest 会说明来源验证限制。后继 PDF 逐件、报告正文和预约另按 #12 原范围交付；不因本PR移除研报/聪明钱八面和其他来源。

本仓运行数据使用 #6 已有独立公开 `records` 分支；这**不是私有存储**。默认仓内 `config/store-binding.json` 是只读，不能修改成通用 writer。真实写入需运行 owner 明确授权、提供受信 `write_enabled=true` 的**独立绑定**和专用 `DECISION_OS_STORE_TOKEN`，原生CAS和来源引用由既有存储处理；不得把元数据或来源原文变成授权。此入口不会初始化仓库、修改 #7 研究、执行 Quick/Full、部署、通知或定时任务。

## 显式命令（安装后可用，不改共享 CLI）

保存或读取的输出 JSON 都标明是目录、是否外部来源实际取得、是否有PDF正文，以及精确引用。默认仅检查，不会写来源/数据分支。

```bash
# 离线回放来自已有授权读取得到的JSON页（非来源真实性证明）。
python -m decision_kernel.interfaces.cninfo_catalog_cli \
  --stock 600036 --start 2026-10-01 --end 2026-10-07 --pages /approved/cninfo-pages.json

# 明确请求官网固定HTTPS端点；403/429/超限/分页缺损都是失败，不尝试HTTP/代理回退。
python -m decision_kernel.interfaces.cninfo_catalog_cli \
  --stock 600036 --start 2026-10-01 --end 2026-10-07 --live

# 已初始化的合成仓：先验证整个目录，再逐条保存external-only定位，读回精确SourceRef。
python -m decision_kernel.interfaces.cninfo_catalog_cli \
  --stock 600036 --start 2026-10-01 --end 2026-10-07 \
  --pages /approved/cninfo-pages.json --save \
  --batch-id accepted-catalog-one --local-synthetic-store /tmp/previously-initialized.git
```

离线 JSON 文件形状为 `{"captured_at":"2026-10-15T09:00:00+00:00","org_rows":[{"code":"600036","orgId":"official-id"}],"pages":[{"totalAnnouncement":0,"announcements":[]}]}`。非零公告页使用CNINFO原字段 `announcementId / announcementTitle / announcementTime / adjunctUrl / secCode / orgId / announcementTypeName`。**离线输入只能证明内部形状完整，不能证明来源网页真实返回了这些内容**；保存的 manifest 明示 `offline-unverified`。

需要远端写时用 `--save --batch-id ... --binding /trusted/approved-writer.json`，不修改仓内只读配置；查询 `--live` 只针对用户显式窗口，不自动持续采集。生产连接结果尚需真实宿主/权限/来源回执，不能用mock HTTP、原Kernel历史曾成功或当前代码CI代签。CNINFO历史中存在HTTP 403，当前新适配器**只允许HTTPS，不转HTTP绕过**。官方PDF的获取和再分发须另行核许可证。

## 错误、保留、维护

- 原始页全部验证通过后才开始保存；源错误、非全覆盖、跨证券或重复ID均拒绝整批，不把故障/空权限冒充“无公告”。
- 一批可能跨多次**独立原子版本写入**；任一保存失败即返回此前真实 SourceRef 和本次失败意图，报告 `complete=false` 和非零退出；未知写先按原 submission ID 对账，不能换键重试。
- 数据分支/引用身份、来源 revision/hash、幂等/前驱、重读与退役保护继续由 #6 维护。#12 仅维护官方目录来源的这一适配器、消费者命令及专属测试，未来替换同步退出。
- 未完成：独立真实HTTPS生产捕获与合格外部使用证据、财报报告期与预约对接、研报目录、八类参与者、新闻/RSS及事件。消费者可使用当前已保存 locator，但不得代替已读原件或投资研究。
