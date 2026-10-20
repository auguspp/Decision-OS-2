# 原预测兑现与 T+5 / T+20 复核

责任：#147。该读取器对照指定已存研究中的原数字、确切实际来源，以及指定已存 MarketDiscovery 原事件之后的价格窗口。它不采集来源、不写 `records`、不改研究或状态、不发通知、不生成预测，也不替代现役相邻研究 `review`、Brief 采用或人工 Decision。

## 原件与输入

正常消费者是现役 `reading-room`：在原 Room 计划上选择可选 `outcome`，值为下述确切原件计划。原生宿主仍运行 `reading-room` 这个只读操作，统一保留报告、确切输入和离线 HTML。没有新增 runtime operation、workflow 或调度。

原主命令也可单独读取同一计划：

```bash
decision-os store --path /tmp/existing-synthetic.git \
  research-outcome-review --input /tmp/exact-outcome-plan.json \
  --market-registry docs/sources/relay-registry.json
```

主命令沿用原 `store --path` / `--binding` 定位方式，复用现役 `outcome_review_commands`；主 `research-room --input ... --out ...` 可同时生成条件面板。可选 `--market-registry` 是调用端可信本地登记文件，不能从来源材料中取执行配置。正常原生 Room 使用当前代码登记的 Relay registry。

底层入口是 `application.outcome_review.open_outcome_review(store, plan, registry=...)`。原独立 CLI 继续可在私有 binding 或已有本地合成 Store 上运行：

```bash
python -m decision_kernel.interfaces.outcome_review_cli \
  --binding /tmp/existing-private-binding.json \
  --input /tmp/exact-outcome-plan.json \
  --registry docs/sources/relay-registry.json
```

独立 CLI 把原生 binding 强制降为只读，使用同一凭证、Store 和 API 预算；原生私有资格在读取前后检查。它只输出完成所有范围退出检查后的 JSON，错误返回 2，不用旧输出补位。`--path` 与 `--binding` 二选一，均读取现有 Store。正常 Room 和主 CLI 也在现役只读 scope 退出后才返回结果，不增加原 API 或 Source 读取预算。

计划严格为以下四个顶层字段；至少声明一个经营预测或市场对照。截止时间必须带时区，不能晚于执行时钟。Outcome 的 `as_of` 是本项材料资格截止，面板单独展示；原 Room 的 `as_of` 仍是既有阅读时钟，不代替各材料的可用时刻或认证历史可见性。

| 字段 | 内容 |
| --- | --- |
| `schema` | `outcome-review-plan-v1` |
| `as_of` | 带时区的复核截止时间 |
| `economic` | 0—16 个明确原预测选择；数组原顺序和失败项保留 |
| `market` | `null`，或两个确切已存 ArtifactRef：`before`、`after`，可选 `offset`、`limit` |

每个 `economic` 项必须具有 `forecast_id`、`forecast`、`actual`、`missing_reason`。没有确切实际时用 `actual=null`，并说明缺什么；有实际则 `missing_reason=null`。

`forecast` 的字段为：

| 字段 | 原件约束 |
| --- | --- |
| `research` | 现役 Brief 的 `{reference,start,end}`：完整研究 BlobRef、从 1 开始且包含两端的正文行号，最多 80 行、8192 字节 |
| `quote` | 与上述原文片段完全一致，包括换行；不能把事后新预测粘贴为原预测 |
| `issued_on`、`issued_on_locator` | 原声明日期 `YYYY-MM-DD` 及其在所选原文中的精确文本位置 |
| `role` | `research-forecast`、`company-guidance`、`company-preannouncement` 或 `sell-side-forecast`；这是对原文本角色的显式选择，不是程序认证作者身份 |
| `basis` | 下述完整十字段口径 |
| `value_locator` | 原数字的 `{start,end}`；文本位置从 0 开始，包含 start、不含 end |
| `range` | 点预测为 `null`；区间预测为 `{lower_locator,upper_locator}`，此时 `value_locator=null` |
| `context_bindings` | 从口径字段名到原文位置的映射；缺任何必要字段时不计算兑现差异 |

十字段 `basis` 为 `subject`、`metric`、`period`、`period_start`、`period_end`、`period_kind`、`basis`、`currency`、`unit`、`share_basis`。期间起止使用 ISO 日期；期间种类只允许 `period-flow`、`ytd-flow`、`stock`、`per-share`、`ratio`。这些字段必须在两侧完全相同，并分别能在所选原文或同一原始数据行定位。未知不参与比较；币种、单位、母公司 / 合并、累计 / 当期、每股口径均不自动转换。即使“不适用”也要有原文依据，本入口不默补口径。

`actual` 的字段为 `reference`、`value_locator`、`basis`、`reported_on`、`context_bindings`。引用必须是原 Store 的完整 SourceRef，不能用 URL 或当前指针替代。数字和口径可以使用原文本 `{start,end}`，也可以使用数据读取器的 `{row,column}`；行号从 1 开始。在表格中，所有口径绑定必须和实际数字来自同一行，不能拼接另一对象的单位或报告期。SourceRef 自身的选择仍由现役 Source Reader 验证。数值定位复用现役 numerical-audit 的完整数字 token 检查，禁止从更长数字里截取一个看似合适的值。

## 何时可以比较

只有所有必要口径均与原文字面核对、两侧一致、数字可读取并且日期满足条件时，才返回 `compared-literal-basis`。它证明这次机械比较引用了指定文字和数字；来源真实性、经济含义和角色身份仍由原研究与人负责。调用者声明相同单位或指标不构成资格：缺绑定返回 `insufficient-basis`，原数字、原引用和未绑定字段仍可读，`comparison=null`。

期间结束日当天保持 `not-mature`，待截止日期晚于原结束日后再判断。实际公布日期晚于截止时返回 `not-published-at-cutoff`；实际原 Source 的保存、公布与取得时刻也必须不晚于截止，缺时刻不能补造。实际保存时刻另行输出，不能靠倒填较早公布 / 取得日期，把截止后才保管的材料当作截止时可用。Source 元数据若记录报告期，必须与所选口径一致。实际来源的公布日期还须匹配 `reported_on`。

原声明日期须早于实际公布日期，普通预测不得在原期间结束后才声明；公司业绩预告采用单独角色，要求原期间已经结束、原预告早于实际公布。仅有日期而没有精确时刻时，不伪造盘前、盘后或同日先后关系；同日声明与实际不作先后认证。日期边界使用本次显式截止时区。

研究的 `stored_at` 和原文声明日期分别输出。研究保管时刻必须不晚于本次截止，声明日期不能晚于该稿的实际保存日期。晚于实际披露才保管的原文本可以作为**事后读取原声明**的材料，但不能据其原文日期认证事前已经保存或已经发出预测。MarketDiscovery 原 owner 未保留 Artifact 保存时钟时继续返回 `null`，不拿逻辑 `as_of` 代填保存时刻。

点预测计算实际减预测、绝对差，以及差额除以原预测绝对值。原预测为零时比例为 `null`，不用 epsilon，也不填零；负预测的相对差使用绝对值分母。区间保留两端，以原区间中点计算上述差异，位置包含两端，并保留实际减上限。解析和 80 位隔离 Decimal 上下文复用现役 numerical-audit 原语，计算精度不提高原来源经济精度。本读取器不合并 MAPE、Brier、胜率、置信度或方法优劣分数。

## 原事件之后的固定交易窗口

市场输入必须是已经保存的 MarketDiscovery 两个完整确切版本。读取器复用现役 MarketDiscovery 的 Artifact 解码、原 proof 恢复、原来源资格和直接状态边重建；不是把显示表格或调用者提供的价格重新当合格来源。股票沿现役 FTShare 原始 / 调整口径、参考资料、完整市场日历与上市状态；行业和概念沿现役 Relay / THS 根包、目录、查询及日历资格。原 owner 的失败、隔离、来源终态和预算继续生效。

T 从 `before` 中每个原状态的原进入 / 首次观察事件取。首次观察基线不认证当时已及时发现。T+5 / T+20 分别取 T **之后**完整交易日历的第 5 / 20 个 session，不使用自然日、工作日估计或第 N 条可用报价。日历来自原 qualified source，不因价格缺失删掉交易日；窗口任意交易日缺报价时，保留固定目标日与缺日，状态为 `source-needed`，不移动终点来凑齐 bars。

保存日历尚不足 5 / 20 个后续 session 时，返回 `not-mature-in-retained-window`，目标日和变动为 `null`。这只说明保存窗口尚不足；超过保存日历的当前成熟情况保持未知，需已有更晚日历材料才能确定。后续对象缺失、来源 hold、合约 / 分类范围变化、重叠原价格改写或日历修订均明确保留，不能用新历史覆盖原事件。已有完整窗口且同资格的对象仍可以逐项比较，不把另一个对象的缺口传播成所有对象不可用。

原 owner 为诊断保留数值，不代表来源合格；例如价格捕获晚于声明的原观察截止，仍须阻断兑现计算。现役路径策略自己的历史长度、滚动窗口或持续性缺口与来源资格错误分别处理：目标窗口之后出现的缺日可以使最新状态未知，却不撤销已经完整且同资格的早期 T+5 / T+20 窗口。

原值到目标值的差额和百分比是已存 close 的机械变动。原调整口径与两侧 SourceRef 随结果保留。未经公司行动调整的原价不能认证总回报，调整价也不自动获得公司行动完整性认证；`total_return`、`execution_return`、基准超额、方法有效性和独立样本数保持未建立。

## 原分母、失败和范围

经营分母是本次**明确声明的原预测选择**，不是扫描全部研究得出的预测全集。重复改名不能重复计算同一研究版本、原行段、口径及数字定位。原声明事件键不含指标，因此同一版本、对象、期间、角色、声明日期下的多个指标不会各算一个独立事件；原件失败也不减少已声明事件数量。所有项的缺失、不可比、未成熟和读取失败都保留。不用结果反选研究、对象、指标或原始判断。

市场分母是 `before` 每一类的完整原身份范围，含原 states、unknown 和 gaps。每一期限的所有状态计数之和必须等于已知的原 scope_count。后来的对象池不替换原池，原缺口不变成零回报或消失。原材料无法恢复时分母为未知；不假定零样本。每页最多 64 项，`offset` 最多 30000；分页仅改变输出行，整范围计数和 `whole_rows_sha256` 不变。

计划最多 128KiB，经营来源视图累计最多 8MiB，单次完整输出最多 1MiB。底层原 Artifact / Source / proof 和原生 API 上限不增加，不重置凭证或请求额度。普通局部缺材料会保留对应失败和其他已合格结果；实际原生访问撤回、请求 / 读取预算终态以及最后 mutable scope 检查失败必须终止整个导出，没有部分发布或自动重试。新增入口不写研究、反馈、Brief、Decision 或业务状态。

## 原 Room 和公共宿主适配

可选 `reading_outcome.open_outcome` 返回 `{plan,status,partial,view,next_plan}`，其中 `status=readable` 表示读取结果可展示，不表示每项都可以比较。`partial` 来自经营全部声明项和市场**全部原范围**计数；即使本页都是可比较对象，分页外仍有缺项也保持部分状态。未声明的市场类别不当成缺失。原结果过大时省略完整 Outcome 面板并保留读取计划，不截断原分母；访问和来源 / API 预算终态继续终止整个 Room 导出。

现役 Room 可选 `outcome` 使用同一原计划；`reading_outcome_page.CSS/JS` 使用现役离线 DOM，面板容器为 `outcome-content`。没有选择 Outcome 的旧 Room 不增加面板或导航；脚本遇到无容器直接返回。Watch、相邻 Review 和其他既有部分继续沿用现役接线。原文只经 `textContent`；按钮导出原件读取请求，不在页面自动请求网络。原读取结果整包下载与原全分母保持一致。

正常宿主 payload 为 `{plan: {as_of: ..., outcome: ...}, ...}`。公开 Room 必须同时声明 `public_research_custody_confirmed=true` 和 `public_sources_custody_confirmed=true`，并通过原 Room 其他已选部分的各自许可。public preflight 在材料读取和输入产物生成前完成；绑定允许私有升级时，先检查实际原生可见性，不能把配置开关当成私有证明。private 模式仍验证完整输入形状，但不借用公共声明。原生私有读取沿用同一已检查 adapter、单向私有 latch 和 API；最终退出及可见性检查成功后才释放内容结果、HTML 和确切输入。

这些权利布尔值仅为调用者声明，不认证研究正确、人工身份或 Decision。页面导出的相同原件和下一页请求都是原 `reading-room`；实际来源请求是 `{operation: "read-source", payload: 原SourceRef}`，`payload` 直接为完整 SourceRef，没有再包一层 `reference`。读取请求不自动继承授权布尔值，执行前由现役宿主重新查权限。

## 沿用与验证

数值比较有界参考旧 Kernel `cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8` 的 `runtime/operating_outcomes.py::compare_pair`：同口径、零分母、范围端点和不汇总评分。新实现直接使用当前 `research_numbers` / `numerical_audit`、Brief 原引文、Source Reader、MarketDiscovery proof 与原 reading scopes，不恢复旧运行时或另建来源保管。原件身份、模型、预算和相邻研究 Review 责任保持在现役 owner。

专属回归覆盖实际临时 Git 的独立 CLI 重开、主 `decision-os store` 命令、既有唯一 cold-wheel 安装的外部消费者、原生 private Room、无写入、原未解问题、声明 / 保存时钟、未来期限、完整数字定位、口径重标攻击、区间端点、零和负分母、调用者 Decimal 上下文、原 saved-generation replay、FTShare 原参考隔离、完整 session 缺日、历史修订、原全分母与分页、公共入口两项许可、同一 API 预算及最后访问撤回。原生测试用真实 Git 保管合成原件，经现役 GitHubAPI 预算和 source-only snapshot 恢复；只模拟网络响应 / Git 传输，不模拟已算好的 Outcome。离线脚本在既有 Node 环境执行，所有导出请求经真实解析器或原 Source 读取验证，并保留不可信文字为数据。合成工程测试不替代真实原研究兑现、原来源原件认证或人工接受；现场读取须另核确切输入与输出原件。
