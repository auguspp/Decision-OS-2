# RSS：固定源、精确前驱与获准保管

归属 #48 / #12 新闻线。已交 NewsNow/Relay 的24小时滚动仍由 [news-rolling](news-rolling.md) 维护；RSS 元数据已通过原生精确引用接入联合视图，来源成功与失败均由相同入口读取；不重新实现滚动，也不代签 #13 Brief/页面或 #7 Quick。

## 复用与依赖

沿旧 `auguspp/decision-kernel@cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8` 的 `adapters/nbs_feed.py` 与 `docs/native-rss-successor-v0.md`，提炼固定feed、feedparser字节解析、严格发布时间声称和请求前精确前驱原则；不迁旧研究schema、原运行状态或历史registry。现役 `VersionedStore` / `SourceInput` / `SourceRef` / `GitHubStore` 负责唯一保管、原字节验证、原生CAS和原意图重放。

`pip install '.[rss]'` 安装可选RSS能力。feedparser **6.0.14** 与依赖 feedparser-sgmllib **2.1.0** 锁定于 `requirements/rss.txt`；dev包含它，contracts/基础无依赖运行保持不变。专属RSS工作流使用现有dev安装组合（包含测试工具，尚未单列轻量RSS profile）；既有完整CI从实际runner读取全部distribution及所有requirements策略文件，不修改CI门禁。

2026-10-16核对 [官方PyPI元数据](https://pypi.org/pypi/feedparser/6.0.14/json)：feedparser需Python>=3.10、feedparser-sgmllib>=2,<3，BSD-2-Clause；[sgmllib依赖](https://pypi.org/project/feedparser-sgmllib/2.1.0/) 为PSF-2.0。本组适配自己包的`digest/RecordError`，不复制第三方解析器源码或另造XML解析器。

## 固定来源与权限

[国家统计局RSS订阅说明](https://www.stats.gov.cn/wzgl/rss/202302/t20230217_1912859.html)列出：
- `nbs-releases` → https://www.stats.gov.cn/sj/zxfb/rss.xml
- `nbs-interpretations` → https://www.stats.gov.cn/sj/sjjd/rss.xml

只允许上面两地址，一次调用一源、一个GET，无重试/重定向/代理/凭据，无文章链接访问。HTTP原字节上限8MiB，完整feed最多512项，超限或坏条目明确失败，不截取前N条伪称整个源合格。日期、原标题、链接、摘要hash形成有限版次；不保管或显示摘要正文，`content:encoded`、enclosures和链接文章不参加这个有限版本语义。原XML保存与条目解析是不同范围。

2026-10-16核对[服务条款](https://www.stats.gov.cn/wzgl/202302/t20230217_1912857.html)：转载需归属与网址，非本网版权、限制转载内容等有例外，署名文章另有条件。官方订阅不是整份feed原文公开许可；默认 **只保存获准元数据**，含“引自国家统计局网站 https://www.stats.gov.cn”的归属，不给下游再转载授权。

只有操作人已核该份整个feed公开权利时，才同时提供 `plan.retain_original=true`、非空 `plan.rights_basis` 和独立 `public_original_custody_confirmed=true`。标准库gzip/base64仅为已有SourceInput的可逆表示，不改变公开权限或绕过原2MiB来源预算。没有这些条件，`original.status=not-retained-rights-unconfirmed`，原字节只在当次解析内存中存在，不写artifact或records；raw SHA不能被说成原件可恢复。即使有权限也可能因压缩/来源预算明确拒绝，不能自动降级后声称存了原件。

## 版本与时间

捕获保存实际 `requested_at` / `received_at`。媒体原`published_raw` / `updated_raw`原样保留；只有完整明确时区且通过旧严格规则的字符串可表达instant，仍为未验证声明。不从国家、时区、HTTP头或采集时钟补发布时间；日期只有日期不编午夜。

每次明确初始化或选定精确SourceRef前驱。初始化记 `baseline-not-new-publications`，不会把feed中数百篇历史条目叫当日新增。后继先打开完整前驱、核版本/来源/非未来时钟并验证其选定的一步关系，再开始唯一源请求；前驱缺失或错误就不抓新源，不悄悄改选“最新成功”。已有原件会解压核字节并实际重跑feedparser；只有元数据时恢复状态为 `restored-metadata-only`，绝不冒充原件重建。

标题/摘要hash/发布时间声明改变形成新版本；不变观察保留首次观察并累计次数。次数只沿连续选定且仍保留该版本的捕获，不是终身首次、已读/未读、独立事件或媒体数量。移出feed只表明 `missing_from_current_window`，不是撤稿或删除；过期、断线和有限窗口不能证明无新闻。无自动当前指针、原件删除或源游标推进。

`open_capture`使用现行Store核精确SourceRef及可用原件，再重建当前与直接前驱的一步转移，不递归重验整个历史；也不把该有限检查称为全部历史灾备。失败回执通过同一个 SourceInput 保管，原成功前驱不改变。失败不是成功快照的一步转移，读取失败回执不再次追索其当时不可读的前驱，也不冒称前驱已恢复；后继采集仍须显式指定可用的成功前驱。

## 显式手动运行

`rss-intake.yml`只有workflow_dispatch。代码必须是已核main及独立main CI的确切SHA；读取/来源捕获分job，读取只有contents:read。捕获job有仓级contents:write，但不称服务端分支或角色隔离；实际写仍经原Store路径/CAS。来源请求不使用存储token，原HTTP适配不接受动态地址/Authorization。

捕获 `operation=capture` 的plan：

```json
{"feed_id":"nbs-releases","bootstrap":true,"retain_original":false}
```

需明确 `public_metadata_custody_confirmed=true`、原稳定submission_id、当时records expected_head。后继改 `bootstrap=false` 并加完整 `predecessor` SourceRef。没有bootstrap也没有前驱会在HTTP前拒绝；同submission已存在也不重抓。每次失败只影响当前明确源调用，不要求重抓其他成功源。

元数据或获准原件的snapshot通过 `VersionedStore.put_source` 保存到 `rss-<submission_id>` 既有source-views路径，不另建源库/写协议。窄宿主只增加planned-head准入，真正条件提交仍由现役GitHubStore负责。发布前保留 `source.json` 原意图与capture/reading，返回完整SourceRef后才能报published。错误/未知效果不发布假引用、不换键或重跑来源作业求绿。

独立读取 `operation=read`，plan为 `{"reference":<完整SourceRef>}`，submission/expected_head空；原生另一次只读运行返回capture.json及reading.json。应核捕获字节与独立读回字节相同，再分别陈述元数据或原件是否恢复。

离线阅读：

```sh
python -m decision_kernel.interfaces.rss_cli read-local --input capture.json --query 统计 --limit 40
```

零命中仍返回整个所选feed的状态、前驱、原件可用性和delta。分页offset只用于同一输入和过滤条件；阅读并非用户选择/接受或Brief采用；联合24小时入口按确切 SourceRef 消费同一成功快照或失败回执。

未知写应下载原运行的 `source.json`（先按原生run/artifact身份与摘要核验），使用同原submission和可信只读binding执行：

```sh
python -m decision_kernel.interfaces.rss_cli reconcile --binding config/store-binding.json --input source.json --submission-id ORIGINAL_ID
```

只读凭据由DECISION_OS_STORE_TOKEN提供；同意图可重放，缺失意图不能提交，无RSS请求。可用时亦可把同source经既有research_payload传到research-runtime/reconcile-source；其60KiB入口仍可能容不下大原件，不因本功能放宽。现有records/bundle负责数据备份，30天artifact不是永久原件或灾备承诺。

## 失败回执与保管状态分开

在合法计划、权利和存储预检通过后，现役 `rss-intake.yml / capture` 默认同时保管失败回执。它仍只执行这一次显式调用，没有新增定时、自动重试、另一writer或永久失败索引。直接库调用 `collect` 原有抛错语义保留；宿主使用 `retain_failure=True`，复用同一取数和解析路径。

失败采用独立 `dos2-rss-failure-v1` 表示，不改已有 `dos2-rss-capture-v1` 成功字节和版次。回执区分前驱恢复/校验、来源请求、解析及版本转换三个阶段，记录真实尝试开始、HTTP请求/接收（仅实际发生时）和结束时刻；已取得的有限响应只留大小/SHA256。错误仅公开固定已知代码，未知异常统一为 `RSS_ATTEMPT_FAILED`；不输出任意错误正文、XML、原标题或堆栈。最大回执16KiB，响应原8MiB上限和原保管预算不变。

`observations=null`、`source_outcome=unavailable`，没有伪造“0条成功”、增量或撤稿结论。前驱失败不会发送HTTP；来源/解析失败不会提高任何旧版本观察计数。失败回执不能用作下一次成功采集的前驱；系统不暗选最近成功记录。旧成功快照及未授权原XML的限制继续保留。

| 情况 | 宿主回执和返回值 |
|---|---|
| 采集成功并保管 | 原 `completed` / `published`（或幂等replayed），退出0 |
| 采集失败但回执已保管 | `source-failed-receipt-saved`，有确切SourceRef、`write=published`，**仍退出2** |
| 独立读取已存失败 | `completed`、`source_outcome=unavailable`、无写入，退出0；成功的是读取，不是来源 |
| 存储冲突或未知写 | 保留原source.json及采集失败信息；无确认引用不称保存成功，按原意图对账，不重新抓源 |

无效计划、缺权限、预检/安装/进程中断等未产生合格采集尝试的错误，仍是操作失败，不自动包装为源站失败。无法写入时保留已有Actions产物；这不是任何情况下都能永久归档的保证。旧30天报告不自动回填成新的真实采集时钟。

原 `read-local`、精确 `read`、`reconcile` 和 `news-rolling.yml` 都可消费该表示。联合窗口保持 `status=unavailable / observations=null`；即使来源或标题筛选零条，完整回执及来源健康仍显示。一般尝试起点在新闻回执中明确标记为 `ATTEMPT_START_NOT_HTTP_REQUEST`，真正HTTP请求钟另列；缺失前驱时不制造HTTP时刻。只读消费者不请求RSS，不推进游标/成功前驱，也不把系统故障记录作为媒体新闻或独立经济事件。

## 验证与退出

专属测试执行真实feedparser、本地GitStore、原件往返/解压界限、元数据/原件前驱差别、请求前拒绝、同意图、改版/无时区/原件权限、来源失败、未知写保留输入、独立CLI及手动宿主约束。只替换HTTP和测试用云端入口，不伪装真实源或CI执行。

代码CI、真实RSS元数据采集与读回、原件公开保管、日常页面/研究采用分别验收。只有标题等元数据的运行不能签原件生产恢复完成。若退役RSS，同步处理现役专属模块（含失败表示）、测试、workflow、可选依赖和rss锁接线；共享Store、新闻24h及CI保护保留，已存源版本不删除。
