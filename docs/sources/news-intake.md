# NewsNow + Relay 新闻采集、保管与读取

归属 [#46](https://github.com/auguspp/Decision-OS-2/issues/46)，父责任 #12。本入口交付有限新闻观察；[24小时滚动与精确前驱](news-rolling.md)由[#48](https://github.com/auguspp/Decision-OS-2/issues/48)承担，RSS及日常采用仍按#48/#13各自验收；不把一个代码PR当成完整旧新闻站点。

## 复用与边界

旧Kernel固定 `cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8` 的 `news_daily` / `external_radar_observations.news` 提供七路NewsNow、固定镜像、有限窗口、来源文章/版本/时间声称语义。原 `news_rolling`、NBS RSS/feedparser和News Workbench按具名后继提炼，不搬旧业务状态或自动启用十分钟调度。

本仓复用 `observations.news_windows` 的读取身份、`infrastructure.news_capture` 的窄采集，原 `RelayHTTP` 固定域/凭据与拒绝，原 `VersionedStore` / `SourceInput` / `research_runtime.run_command` 的来源提交、条件写和只读对账。`news_custody`只是原生新闻产物到既有来源提交的适配，不是另一个数据仓、写入协议或模型平台。

## 来源资格

NewsNow仍是 cls、wallstreetcn、fastbull、jin10、mktnews、gelonghui、thepaper，固定本地端点与原镜像digest，每源1MiB/30条的既有上限；只记录安全HTTPS原文链接，不跟随。固定镜像源码等价性未独立证明。

2026-10-16 06:51:29 +08:00的真实Relay目录中，`news`、`major_news`、`cctv_news`标启用并有已核字段合同；目录声明不等于每个实时请求、字段或原件保管已通过。`express_news`字段未核，`news_cctv`及当时disabled的Baidu新闻不经本入口调用。允许来源、日期与24小时以内窗口由现役新闻参数合同限制。

## 明确采集，失败只影响对应来源

```sh
python -m decision_kernel.interfaces.news_cli capture-newsnow --output news-window.json
# 只复核原失败来源；不能因为一个来源失败就重抓成功兄弟
python -m decision_kernel.interfaces.news_cli capture-newsnow \
  --sources wallstreetcn thepaper --output news-failed-source-check.json
python -m decision_kernel.interfaces.news_cli capture-relay --api news \
  --source cls --start '2026-10-15 09:00:00' --end '2026-10-15 10:00:00' \
  --output relay-news-window.json
python -m decision_kernel.interfaces.news_cli merge \
  --windows news-window.json relay-news-window.json --output combined-news.json
python -m decision_kernel.interfaces.news_cli read --window combined-news.json --limit 40
python -m decision_kernel.interfaces.news_cli export-locators \
  --window news-window.json --output news-locators.json
```

`news-intake.yml`仍仅workflow_dispatch；`newsnow_sources`可指定原七源的非空子集，默认全部七源。需当时main代码/独立CI及明确有限公开元数据保留许可。成功五路与失败两路可以并存；退出2和原失败运行不改绿。再次有限请求是新的观察时点，不是补写旧时点或完整原子快照。

新结果 `source_outcomes[].capture` 保留每源请求/接收（或失败结束）时刻、请求筛选、HTTP成功时的字节摘要与大小、安全错误码和结构计数。不存任意上游错误文本；不放宽标题/来源域/字段检查以求成功。`observed_at`现在来自实际响应之后；旧#47产物的该字段是调用开始时刻，历史字节不改写，也不伪装成精确收到时间。旧结果缺少这些回执不猜测补齐。跨源同标题只代表重复曝光候选，不代表独立经济事件、事实认证或投资观点。

公开观察只包含有限标题/URL/时间声称和派生身份，不包含供应商全文或原HTTP正文；原始响应hash不是可恢复原件。`export-locators`仍仅生成外部定位候选，无文章URL的Relay项只指聚合接口，不谎称逐篇原文。

`read`、`merge`的标准输出及`export-locators`的文件和标准输出都保留 `reading_sha256`、完整 `source_outcomes` 与 `complete_news_coverage`。`source_outcomes_scope=whole-input-reading`明确它们属于整份已验证输入：`--source`与`--limit`只筛选可见条目，不筛掉失败来源或改写批次健康。全失败零条和零命中过滤仍带原失败清单；读取/导出成功不等于来源成功。合并摘要的身份属于输出的完整合并视图。定位项仍是原SourceInput，不把健康字段塞进每篇来源。原输入摘要不符仍拒绝，不为展示而修补历史数据。

## 已取得窗口进入同一个内容池

手动 `news-custody.yml` 接收以下明确输入：

| 字段 | 含义 |
|---|---|
| code_sha | 本次已审main代码；运行时必须一致且其独立main CI已通过 |
| source_run / artifact_id / artifact_sha256 | 同仓既有news-intake运行及唯一确切ZIP，不选最新/搜到绿色即用 |
| expected_head | 写前实际records SHA，原提交/CAS仲裁 |
| public_metadata_custody_confirmed | 本次有限元数据可持久公开的确认，不扩展到新闻全文 |

宿主先核仓库及head_repository、workflow/main/attempt1、终态success或保留部分观察的failure，再核原生artifact所属run/head/名称、过期、大小与SHA。下载唯一已选ZIP后，只允许一个`news-window.json`、有限展开、CRC/reading摘要及原观察合同。没有完整身份或可读数据不调用写入。不能因为原运行失败就丢掉已取得的合格观察，也不能把原失败改成成功。

提交使用原 `SourceInput(kind=content, custody=provided)`，text保持原生成的新闻元数据JSON，source_id与submission_id都是 `news-window-<source_run>`。它是**已保管的观察表示，不是已保管的媒体原文**；外部发布时间/经济事实仍未验证。原run/artifact/hash/代码身份和旧时钟限制进入limitations，不新增schema或另一真源。整个窗口超出原source/传输预算时明确拒绝，不截断。

`news_custody`调用既有 `run_command(operation=put-source)`；原expected_head/幂等/CAS/未知效果和输入回执继续有效。分支已前进导致冲突时先核原提交，不换键重写；超时/丢回执用原 `reconcile-source`，不得rerun写作业求绿。`news-custody`作业有内容写权限但不称服务端分支隔离；它不访问新闻URL、不使用Relay Secret，实际写路径仍由原存储合同限制。

成功后，以返回完整SourceRef通过**另一次只读 `research-runtime.yml/read-source`**打开，比较`result.data`与最初窗口字节/摘要，连失败列表一起保留。原记录通过现有Git bundle恢复路径保管，不新增备份存储。产物里的`reading.md`列出全批新闻、原文定位和来源缺口供人直接阅读；该表不是自动Quick、人工选择或Brief采用证明。

## 维护与未完成范围

专属捕获回执/原生ZIP接收/安全呈现测试随本新闻能力维护，原SourceInput/条件提交/研究只读归原owner；不因本Issue关闭删除共享保护。实际来源/保管/独立读取、长期恢复、模型分析和用户接受分层验收，具体发生过什么看#46原生回执。

用户于2026-10-16明确采用[24小时滚动](news-rolling.md)，不沿用旧18小时参数；这不改变采集频率或自动补齐未采集时段。RSS前驱仍归#48；#13接新闻浏览、Attention、晨晚Brief及迟到采用；#7对线索查证、提出可反驳研究问题。没有自动启用旧十分钟cron、新GPT任务、通知、Sites或正式Decision。某个来源失败不应阻止已合格资料的保管和读取，也不能以有限新闻计数签完整覆盖。
