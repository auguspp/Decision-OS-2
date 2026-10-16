# RSS：固定源、精确前驱与获准保管

归属 #48 / #12 新闻线。已交 NewsNow/Relay 的24小时滚动仍由 [news-rolling](news-rolling.md) 维护；本组是 RSS 来源接线，不重新实现滚动、不宣称 RSS 已加入原 NewsNow/Relay 联合视图，也不代签 #13 Brief/页面或 #7 Quick。

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

`open_capture`使用现行Store核精确SourceRef及可用原件，再重建当前与直接前驱的一步转移，不递归重验整个历史；也不把该有限检查称为全部历史灾备。失败保留运行报告、原前驱不改变；失败诊断当前随30天原生产物保留，尚无持久失败索引，不冒称长期健康日志。

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

零命中仍返回整个所选feed的状态、前驱、原件可用性和delta。分页offset只用于同一输入和过滤条件；阅读并非用户选择/接受，也不是合并24小时新闻池或Brief。

未知写应下载原运行的 `source.json`（先按原生run/artifact身份与摘要核验），使用同原submission和可信只读binding执行：

```sh
python -m decision_kernel.interfaces.rss_cli reconcile --binding config/store-binding.json --input source.json --submission-id ORIGINAL_ID
```

只读凭据由DECISION_OS_STORE_TOKEN提供；同意图可重放，缺失意图不能提交，无RSS请求。可用时亦可把同source经既有research_payload传到research-runtime/reconcile-source；其60KiB入口仍可能容不下大原件，不因本功能放宽。现有records/bundle负责数据备份，30天artifact不是永久原件或灾备承诺。

## 验证与退出

专属测试执行真实feedparser、本地GitStore、原件往返/解压界限、元数据/原件前驱差别、请求前拒绝、同意图、改版/无时区/原件权限、来源失败、未知写保留输入、独立CLI及手动宿主约束。只替换HTTP和测试用云端入口，不伪装真实源或CI执行。

代码CI、真实RSS元数据采集与读回、原件公开保管、日常页面/研究采用分别验收。只有标题等元数据的运行不能签原件生产恢复完成。若退役RSS，同时退出四个专属模块、测试、workflow、可选依赖和rss锁接线；共享Store、新闻24h及CI保护保留，已存源版本不删除。
