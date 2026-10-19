# A 股原价与复权进入同一阅读室

责任：#13；来源和市场资格仍由 #12/#10 维护，持续集合的端点比较由 #10 的原市场读取器提供。`reading-room` 的可选 `market` 计划经 `reading_market.read_market_reference` 进入原市场根或持续集合的读取器，复用原 Registry 和 VersionedStore，不建行情库、采集器或新的写入口；页面只展示读取器给出的数值，不另算收益。

## 输入与显示

```json
{"as_of":"2026-10-17T12:00:00+08:00","catalogue":{"limit":1},"market":{"reference":"替换为已保存整根的完整 SourceRef 对象","offset":0,"limit":20,"entity_id":null}}
```

`reference` 必须是以下现役完整整根 SourceRef 之一：

- `market-reading-custody-v1`，其 reading 为 `relay-security-stock-reading-v1`，沿原 `read_stored_market` 读取。
- `market-collection-custody-v1` 或 `market-collection-custody-v2`，沿 `read_collection_version` 恢复原计划、日历、主表和按日分区；GitHub 后端使用同一绑定及请求预算的只读 `GitMarketReader` 固定原生快照。v2 只增加[一次有限维修](../architecture/ashare-data-continuity.md#terminal-repair)的不可变 grant 和独立检查点/查询引用，不更换行情 schema 或收益算法。
- `ftshare-collection-custody-v1`，沿 `read_ftshare_collection_version` 独立恢复同 Store 的 FTShare 整段日线及原主表/日历前驱。该根可包含原批量计划、[已经验证并保管的 Kernel v1 输入及其有限后继更新](../sources/ftshare-daily.md)；读取不执行行情请求，单股原始正文沿真实 v1 身份恢复。

根、query、页、Registry 和选择由各自原保管层核验；根路径只选择核验器，不是内容已合格的证明。其他家族仍拒绝。计数检查回执、仅有缺失日期的 factor-audit、研究中的一个价格、裸 SourceInput 和调用者手填收益均不能替代该根。没有合法保管源不启动补抓，也不将私有供应商数据转成公开输出。

对 v2 根，只有完整并独立恢复合格的维修 query 才能按原槽进入正常读取。原失败 unit 和终态仍保留，维修来源与有效计数另行显示；原 `current-complete` / `current-partial` 服务状态及缺口含义继续适用。维修中的成功页、尚未完整的 capture 或一份 grant 都不构成行情输入，读取不会发布 grant 或消耗来源额度。

每页1—50证券（默认20），offset沿原读取器；可指定六位代码加 `.SH` 或 `.SZ`。下一页从本次原 `page.next_offset` 导出，固定同一根而不选择最新；原价/复权必须同页。未知证券的空查询不改变完整主表分母；证券代码不是永久公司身份。目录分页与市场分页各自固定，不把同页研究当市场发现或公司匹配。

`result.market.view` 是对应读取器的完整结果，页面不修改其中数值或字段。原价和复权在“完整逐日窗口”中分别显示原窗口状态、基准/截止日及原 `change_pct`，缺失显示未取得，不填零、不沿用因子。主表资格/隔离、原停复牌解释、因子缺口、来源版本和完整范围计数保留。

持续集合读取器增加并列的 `endpoint_comparisons`，页面在“端点区间比较”单独显示它的 5/20/60 交易日日期对、两端值、价格比率、资格与计数。基准由同一交易所日历定位；原价只依两端价格，Relay qfq 依两端及原截止日锚定因子，并复用原因子比值算法；FTShare forward 使用同一供应商整段请求的前复权价格，显示其独立语义和因子/PIT 未认证边界。中间缺日可以限制完整逐日窗口而不限制端点比较；任一所需端点、适用因子、主表身份或日历未合格时仍明确显示相应缺口，不挑选更近价格凑窗。端点比较不是完整路径、波动、事件反应或总回报认证；无该字段的旧单证券 reading 根继续按原结果显示。

两种用途各自保留完整声明分母和逐窗计数；主表隔离项不从分母消失，未知完整主表仍显示未取得。原价/复权的端点行沿用同一页和精确证券查询；下一页仍固定原根，`view_version` 覆盖所有证券及用途，不随展示页改变。端点详情中的日期与输入状态定位原整市场日查询，完整来源引用仍以正常读取结果为准，不创建按证券的来源身份。

Kernel v1 原文采用只提供本次实际取得的三个价格输入：深市原价、沪市原价、深市 forward；重复原价作为证据，不增加价格 owner。阅读室继续显示完整主表分母与 raw/qfq 各自缺口，三个已存输入齐全不能把全范围状态或 freshness 提升为完整。采用、正常 CLI 与 room 均从原精确引用恢复，新增行情请求为 0；临时 Actions artifact 到期不影响已经保管的原件读取。

v1 后继根继承原资格和各价格请求，有限更新只替换实际选定的股票 / 复权类型。完整 `single_queries` 保留原字节、原请求和引用，`kernel_sync` 标出精确 base、继承槽、新槽及本代预算；预留次数与消费者的来源请求 0 分别表达。新日历包含原请求未覆盖的开市日时，继承成功也不能提高全范围完成或 freshness。旧 forward 不填进新 forward 的缺失日期，旧根继续按自身日期和引用恢复；分页与实体筛选仍不改变 5,226 项分母。

持续集合保留当前尝试、实际选中的根、`current-partial` / `previous-complete` 等服务状态和原 freshness；端点结果也属于实际选中根，不能借最新尝试的日期为旧根计算新端点。旧完整版本可读不代表最新请求已经成功。市场 freshness 使用受信宿主的读取时钟，不由来源文本或议程 `as_of` 提升当期资格。端点可用不改变原严格窗口、集合完成、CLI 服务状态或阅读室 partial 判断；查询表示恢复成功也不等于声明窗口、全区间因子、当前健康、总回报或投资判断全部通过。页面不重新计算，不建立相对收益基准，不自动采用进 Brief。

## 宿主与权限

仍用现役 research-runtime 的 `reading-room`，无新工作流选项。payload 的 `plan` 为上述计划。受信宿主先用原绑定和 GitHub 原生元数据核当前可见性；当前同仓 private 时，按既有获准阅读范围进入私有输出路径，无需声明并不存在的公开再分发权。原历史 `visibility=public`、单向兼容开关、GitHub 事件或 payload 都不能自行证明当前私有。宿主保留同一后端，并在输出 exact 输入引用、结果正文和 HTML 前再次检查；已观测 private 后反转 public 则失败，不输出上述私有材料。原生核验不成功也不降级放行。

若实际为 public，继续按原权利分别确认 `public_research_custody_confirmed=true` 和 **`public_sources_custody_confirmed=true`**，在访问 records 内容及生成公共输入产物前完成预检。材料/反馈确认不能替代来源确认；布尔值是已有权利的调用声明，不认证法律权利。私有读取适配仅作用于 `reading-room`，不改变研究或来源写入、其他宿主及正式 Decision 的门禁。实际代码使用刚核过的 main/code_sha，submission_id/expected_head 留空，只读作业不匹配 writer。

原宿主使用checkout中 `docs/sources/relay-registry.json` 及其依赖；payload不能指定 Registry 路径或网络地址。本地包外读取可在原命令传可信 `--market-registry`，与原市场CLI相同，不从来源文字取得配置：

```bash
decision-os store --path /tmp/existing-synthetic.git research-room \
  --input /tmp/room-plan.json --out /tmp/new-room.html \
  --market-registry /trusted/relay-registry.json
```

Registry的原手册/目录/回执相对依赖也须可读，身份改变时沿原保管合同拒绝。无market计划不加载Registry，不改变旧入口模型。页面只导出原根read-source或同一reading-room请求，不自动提交/预填许可；下载JSON是读取结果，不是供应商原始响应文件。

## 故障、预算和退出

根损坏/缺失、Registry不适用、关键上下文失败只限制市场面板；缺单个因子 query 由原 owner 降级相依视图，原价好分支可保留。来源恢复、窗口缺口和未提供分别表达；原研究、议程、Brief 权限不变。市场部分上限 2MiB，超限整项省略、保留精确根和请求，不截断原结果。旧 reading 根继续受原 32-query / 原页限额约束；持续 collection 根按[原持续数据合同](../architecture/ashare-data-continuity.md)逐日恢复，不套旧 reading 根的 32-query 总量上限，也不改变每份原 query 的页与字节边界。原 512 请求、60KiB 宿主输入、6MiB 完整模型、8MiB 输出及末次访问核验不提高或重置；总预算或最终权限核验失败不能冒称局部成功。

测试使用合成资料的真实临时 Git、原宿主和原唯一安装消费链；包外两进程在捕获输入删除后独立恢复。分页、未知代码、计数伪根、来源缺失/损坏、容量、实时可见性及撤权分别验证。合成成功不代签真实行情资料、合法保管/公开表示或市场研究采用；真实业务验收须原 owner 给出获准根及声明窗口，不重抓成功样本制造验收。日历、主表分页前缀或一份旧备份能恢复，也不能代签当前完整分母与 raw/qfq 5/20/60 可用。

维护者同步维护 `application/reading_market.py`、原阅读室/命令/页面和 `tests/test_market_room.py`。退出时普通PR移除可选计划、确认接线、页面和专属消费者；原市场Reader、Registry、Store、历史SourceRef及其保管义务保留。BSE/其他market家族、自然运行、Brief采用、用户接受和长期备份不由本组代签。
