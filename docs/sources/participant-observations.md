# 公开参与者八面：确切来源、历史与双向读取

本组承接[实施基线 §5.3](../implementation-baseline.md#53-聪明钱与公开参与者八面都要有处置)与 #147 / #12，复用现役 `SourceInput`、`SourceRef`、`VersionedStore`、Relay 注册资料检查器和原三族参与者字段合同。读取器不含 HTTP 客户端调用、采集调度、研究生成或写入动作。原失败任务和额度不会因读取、换文件或保管分片恢复为可执行状态。

## 产品语义

| 观察面 | 本组读取入口及历史 | 明确保留的边界 |
|---|---|---|
| 游资/公开席位 | `hm_detail` 的供应商标签、营业部、证券、原日期和金额 | 标签未认证自然人/账户；不累加重叠窗口；原数值单位保持未知 |
| 机构席位 | `top_inst` 的机构标签行；普通营业部行保留拒绝计数和原件位置 | 匿名“机构专用”不获得可跨期归并的参与者身份；不是某基金全仓 |
| 北向 | `hsgt_top10` 成交排名、`hk_hold` 披露快照、`hkex_shareholding` 保留原期末日的明确投影 | 成交额不等于净买；季度快照不变成日线/当前仓位；HKEX非A股行保留原件及拒绝位置 |
| 股东/基金 | `top10_holders`、`top10_floatholders`、`fund_portfolio`，公司和来源标签双向读取 | 基金代码与被持证券分开；报告期与公告日分开；消失不推断清仓，不能拼出全仓 |
| 调研 | `stk_surv` 的证券、日期、机构/机构群体原标签 | 参与行不是活动数或买入；未提供披露日就保持未知 |
| 研报/预测修订 | 原 `report_rc` / `research_report` 字段；同源机构标签、同公司、同目标期的具名预测指标比较 | 只对原合同五项明确金额/EPS指标作 Decimal 差额；不同机构/目标期不混算，同日冲突不排序选真；目录未读PDF，预测不是实绩/共识 |
| 股东变动/回购 | `stk_holdertrade` / `repurchase` 的方向、公告日、起止期和原 `proc` | 计划与实施不互换；累计不再次相加；单位、动机、注销和实际结果未证明即未知 |
| 锁定资本 | `share_float` 及 `placement` 的明确规范投影，保留发行、上市、锁定/解禁字段 | 发行条件不证明现金到账；未来锁定结束不是已卖出；没有真实原件时本面仍 `not-retained` |

除沿[既存参与者字段合同](relay-participants.md)已经审过的 `report_rc` 五项数值单位外，原始数值一律不推定单位，不作持仓增减或资金流算术。`participant_id` 是 provider + API + 来源标签的检索键，**不是**真实自然人/基金认证；不同来源/接口不自动合并同名身份。匿名机构行保留事实，但不给它虚构可连续跟踪的身份。

历史列出原证券、来源标签、报告期、报告日期、原行摘要和原保管位置，重复行仍可见。历史不是净持仓重建。公司查询使用现役证券身份，如 `security-002384-sz`；参与者查询使用读取结果中的 `participant_id`。只能选一种查询方向。

## 一个有限来源包

`application.participant_reading.read_participants(reference, store, *, company_id=None, participant_id=None, registry=None)` 读取一个整个 `SourceRef`，返回八个固定观察面。包合同是 `participant-observations-v1`：

- `prepared_at` / `as_of` 是有时区的准备与截至时点；`coverage_note` 描述真正声明范围。
- `inputs` 各项含 `input_id`、`api`、`format`、`references`。`registered-relay` 引用一份完整原 `research_sources.capture_plan` 结果；`registered-relay-parts` 把同一原结果的全部原字节（包括空页失败与所有尝试）按摘要连续拼回后交给同一检查器。两者必须提供现役 `RelayRegistry`；它重新使用原 `inspect_result` 验证查询/页次/终态，保留原失败码、尝试数和完整原件引用。`legacy-participant` 读取原三族 excerpt；旧 excerpt 未保留的完整查询终页资格继续未知。`page-parts` 要求同一物理页全部、按序的保管部分。
- `families` 必须是上述八项，每项含 `family`、`title`、`scope_note`、`input_ids`、`missing` 和 `reopen_when`。无输入的面仍保留 `not-retained` 与明确接点；既存失败/缺失不能写成无行为或无参与。
- 先恢复所有声明输入并计算完整范围、失败与拒绝计数，再作公司/参与者过滤。过滤不会改变 `scope`、`input_outcomes`、各面声明和完整 `view_version`。每行通过 `input_id`、`page_index`、`source_row` 回到 `input_outcomes.references` 的确切原件。

输入上限为32个有限查询、96个确切来源引用、同次读取16 MiB、结果8 MiB；复用原32个commit来源读取scope。根包及保管部分各自生成的commit都计入原32 commit界限；96 references不是96 commit授权。大页可采用下述全页压缩保管，但仍须核完整包的实际commit数与读取/结果容量。压缩后仍超限的组合须显式减少当次有限输入，不能分次读后合并宣称一次完整消费。结果8 MiB门在公司/参与者过滤前检查，压缩率高或查询无匹配行都不豁免。原操作级授权/可见性/额度错误立即传播，scope退出成功后才返回；不换客户端、不重置预算、不在错误后继续独立读取以掩盖失败。

正常 Reading Room 通过 `application.reading_participant.open_participants` 传入可信注册表和 `participants` 计划。计划含确切包引用、单一查询方向、观察面和行分页；默认每页20行，最多50行。每页保留八面卡片、全包范围、输入终态及原件引用，仅分页展示行与相应历史；显示上限768 KiB，超限显式失败，不静默删除原失败。页面的后续面、公司、参与者与页次查询均导出既有 `reading-room` 请求，原件读取导出既有 `read-source` 请求。纯 `participant_commands` 适配不新增宿主 operation。

## 同物理页的保管分片

`observations.participant_observations` 为一个明确的 `participant-observation-page-v1` 结构化页提供两种现役 `SourceInput` 保管表示；两者都沿用包内 `format=page-parts`，由 `join_page` 按首片schema识别并恢复同一完整页：

| 生成入口 | 保管表示 | 连续性与完整性 |
|---|---|---|
| `page_sources` | 保持既有 `participant-observation-page-part-v1`，每部分结构化文本最多24 KiB | 原 `part_index`、`part_count`、`row_start`、同一页头及全页结构化摘要 |
| `gzip_page_sources` | 显式选择 `participant-observation-page-gzip-part-v1`；先将完整规范JSON页压成一个gzip成员，再将压缩字节按最多36 KiB连续分片并base64保管 | 部分序号、总数、`byte_start`、压缩前后各自字节数及SHA-256；整组最多64片、压缩字节最多2 MiB、解压完整页最多8 MiB |

gzip使用Python标准库；它减少顺序保管完整大页需要的独立commit，避免为容量另造Store批量写入或扩大来源读取scope。旧v1生成和读取继续兼容。保管与读取还保持以下约束：

- `physical_page` 始终保存同一原 `page_id`、offset、总行数、原声明总数、原查询完整性、失败与可用的原HTTP摘要。
- 每份完整 `asdict(SourceInput)` 的普通JSON字节都须通过原宿主60 KiB门；标题、来源元数据、base64及JSON转义一并核算。压缩字节本身小于门限不等于整个输入可提交。原capture字节分片继续按每段最多36 KiB以base64无损保管，不改变原捕获检查器。
- 部分编号及字节位置只表示保管位置，不写成新的来源offset或新的来源请求。gzip须在有界解压中完整结束，拒绝截断、额外尾字节、多个成员及不符合规范JSON的内容。
- 全部原行均保管，包括ETF/其他不合格公司行。读取先验证整组连续性、重复/缺片、全页摘要与原总数，再作资格筛选。损坏尾片使该页整体不可用，不能释放前片的部分行或把原失败改成成功。
- 原HTTP摘要、结构化摘要及gzip摘要分别保留。gzip编码的是规范结构化投影，不能替代HTTP原字节保管；摘要和压缩均不证明来源真实性、公开权利、研究质量或新增执行权限。
- 分片函数只返回待保管对象，不调用 Store。实际保存沿原获准 `put-source` 顺序和独立读回；未知写仍按原 submission 对账，不能重新分片换键躲避未知效果。

HKEX两份旧全表与东财定增原字节已由旧本地body及当时回执恢复，并逐行重提取核对，见[#12原件恢复检查点](https://github.com/auguspp/Decision-OS-2/issues/12#issuecomment-6284882357)。HKEX两页保留原2026-09-30期末日，完整2089/2320行中的268/188条非A股行继续保管，合格A股分别1821/2132行；百分比分母保持原SSE/SZSE上市交易证券数量语义。东财定增保留原67行。三份原GET恢复不借Relay产物证明，gzip仅保管其明确结构化投影；这些原件及完整包的永续records保管、独立读回和现役Room实际消费仍待各自验收，本组代码和合成验证不代签这些事实。

原 Relay [`39275529783/1`](https://github.com/auguspp/Decision-OS-2/actions/runs/39275529783/attempts/1) 的10查询捕获已由宿主原产物恢复，425行可读；完整10条普通JSON SourceInput最大约34 KiB，均在原60 KiB门内，无需为了本次捕获改物理分页；`top_inst` 四次原尝试仍为未完成，读取不会重发。该历史运行的9个完整查询也不自动表示全部八面实际可用。

## 验证和维护

`test_participant_observations.py` 覆盖同物理页丢片/改行/顺序、全页gzip完整性及有界解压、原capture字节分片复原及完整SourceInput宿主容量、非A股拒绝保留、匿名身份、基金与证券区别、日期语义、预测同日冲突/跨机构跨目标期反例及旧来源兼容。`test_participant_reading.py` 保留独立复读、完整声明范围、失败原件及空页API校验，并以顺序 `VersionedStore.put_source` 产生的不同真实Git commit验证大页组合：原未压缩表示超32 commit仍拒绝，压缩后完整恢复原行、拒绝位置与失败，坏尾片仅使整页不可用，过滤不能绕过8 MiB结果门。`test_reading_participant.py` 以现役GitHubAPI和合成Wire验证同一大页经正常Room读取到末行，保留全包计数/原件引用；尾片的首次原生403必须终止读取，不继续邻接输入或导出私有inputs/HTML。合成资料不包含真实第三方正文，也不证明现役records已保管或真实Room已运行。

正常 Room 复用参与者读取器的操作级失败分类，原样抛出授权、可见性及预算错误；不能把一次原生 GET 403/409 降成可导出页面的局部缺口，也不能靠之后成功的权限复查或新客户端覆盖失败。`test_room_consumer_boundaries.py` 用现役 GitHubAPI、合成 Wire 和真实临时 Git 保护这个组合入口：首次失败后不再读取，私有 inputs/HTML 不释放，原 512 请求耗尽时保留首次错误；已验证树内的普通缺件仍保留完整声明范围并显示局部缺口。此参与者消费者退出时同步移除对应回归，共享传输与来源保护继续保留。

局部测试、真实原件兼容、完整Ready CI、实际main、正常消费者和人工接受分别成立。后继接入新的来源投影应给出具体原字段/时期映射和原件证明；本组没有新增采集资格。能力退出时移除专属观察/阅读入口与测试，共享Store与旧SourceRef历史读取保持。
