# 新闻：24小时滚动与精确前驱

归属 #48；用户于2026-10-16明确选择 **24小时滚动**，替代原事项沿用的旧Kernel 18小时。此处改变观察时间窗，不改变采集频率，不启用定时任务。#46的来源资格/标题拒绝、#48 RSS、#13 Brief/Attention及#7实质Quick仍各自验收。

## 时间与身份

窗口为 `[as_of - 24小时, as_of]`，两端包含。`as_of`必须明确提供含时区的ISO时钟，计算统一UTC；新结果固定`window_hours=24`。跨午夜、东八区与UTC表达同一时刻时不重置窗口，不按自然日归零。

依据是实际观察时钟，不是媒体发布时间：每条`observed_at`原样保留；有回执时用其真实接收/失败结束时钟表达来源健康，旧回执缺失不重建。`published_claim`只继续展示原声称，不能从抓取时间补新闻发布时间或时区。旧#47的`observed_at`是开始时钟，不冒称精确收到时刻。没有时钟的旧失败只附属于被保留批次，明确`unlocated-within-selected-batch`；不捏造它发生在窗口内的具体时间。

窗口内同一`article_id/version_id`只显示一个版本，记`first_seen_in_window`、`last_seen_in_window`、`seen_batch_count_in_window`和首末确切SourceRef。次数按不同阅读摘要的批次计算，重复提交同一引用、或相同字节存到另一source_id均不增加次数。这个次数/首次是**当前保留窗口内**的，不是终身首次发现、媒体发布时间、独立媒体数或事件数。标题变化形成的不同版本仍分别可见；相同version却不可变字段冲突时拒绝构建。

## 单内容池与有界恢复

`application/news_rolling.py`用调用方现有`VersionedStore.read_source`读取完整SourceRef，核原manifest和表示字节，然后复用`news_windows.verify_reading`。只读已保管元数据，不跟随文章URL，不连接NewsNow/Relay，不复制旧研究schema或运行状态。

续作显式提供已保存滚动视图的SourceRef，先核其24小时合同与摘要，再按其中保留的原批次SourceRef重新读取。不是从“最新绿色artifact”猜前驱，也不靠前驱里的标题替代原来源字节。未提供前驱标明`not-supplied`；不可读、损坏、未来前驱标明`unavailable-or-rejected`和`PREDECESSOR_GAP`，仍允许本次合格新批次被阅读。缺口在24小时内继续随下一前驱保留，不因恢复到一个可读索引便消失。

单次最多12个新引用、保留最多256个批次、2000个不同版本，视图最多512KiB。只去掉已过24小时的已定位批次/观察；若预算截断，则明确`BATCH_LIMIT`、`OBSERVATION_LIMIT`、`BYTE_LIMIT`，不缩短为18小时或悄悄丢健康信息。超出健康/元数据预算且无法在这些界限内表示时整体明确失败，不丢失败记录求成功。损失计数是保留的裁剪操作计数，不是独立丢失文章数。当前索引过期/裁剪不删除内容池原版本。

`coverage`报告观察间隔、失败来源窗口与无时钟来源，不签连续完整覆盖。每路有限返回、离线/手动采集、缺失批次和截断均可能导致覆盖不完整；有24小时过滤器不等于已连续运行24小时，也不等于收全24小时所有新闻。

## 显式手动构建和保管

原生`news-rolling.yml`只接受workflow_dispatch。输入`code_sha`绑定已审main及独立main CI；`plan`最多60KiB，只包含：

```json
{
  "as_of": "2026-10-16T12:00:00+08:00",
  "batch_references": [],
  "predecessor": null
}
```

`batch_references`填既有完整SourceRef对象，不是文章URL、source_id或latest选择器；`predecessor`可省略，为前一滚动视图的完整SourceRef。空新列表允许只做已有前驱的自然过期；无前驱且无输入只得到明确无覆盖的空观察窗，不称没有新闻。

默认`save=false`，build job只有contents/actions读权限。要将本次派生新闻元数据保管到既有records，显式`save=true`、`public_metadata_custody_confirmed=true`，同时提供原稳定`submission_id`与实际`expected_head`。save job才有contents写权限；由原`research_runtime.run_command(put-source)`负责原输入留存、CAS、幂等与未知效果，source_id为`news-roll-<submission_id>`，仍是`SourceInput(kind=content,custody=provided)`。没有新存储schema、索引仓、写协议或源凭据。宽仓级token不宣传为服务端分支隔离。

输出`plan.json`、`view.json`、`reading.json`及适用的原writer回执。出现未知写按`result/inputs.json`的原输入调用现役`reconcile-source`，不换提交键或rerun写作业。写失败或payload压缩后仍超过60KiB时明确未保管；不得因为产生view文件就称已经入池。有限30天artifact不是永久来源；成功写入后的来源版本遵循既有records/bundle恢复合同。

保存后必须用**另一次**`research-runtime.yml/read-source`按返回完整SourceRef打开，将返回data与原`view.json`核字节一致。该读取不采集、不写入，也不执行研究。构建/保存/独立恢复/长期备份/正常Brief采用分别取证。

## 正常分页阅读

从确切读回数据取得view.json后，安装包外也可离线调用：

```sh
python -m decision_kernel.interfaces.news_rolling_cli read --window view.json --limit 40
python -m decision_kernel.interfaces.news_rolling_cli read --window view.json --source cls --query 价格 --limit 20
# 将上页next_cursor对象保存为cursor.json；源/标题过滤必须保持一致。
python -m decision_kernel.interfaces.news_rolling_cli read --window view.json --limit 40 --cursor cursor.json
```

游标绑定视图摘要和来源/标题筛选，不得跨视图、跨筛选跳页；无网络、无输入覆写。每页，包括筛选零条页，均返回整窗来源健康、缺口、前驱和覆盖边界，`source_outcomes_scope=whole-24h-window`；不把分页后的行数当全部新闻数量。

## 复用和维护

旧固定版`decision-kernel@cb3bbc87`的`src/decision_kernel/runtime/news_rolling.py`提供article/version、窗口观察、前驱/缺口/裁剪语义；该旧版硬编码18小时不是新产品裁决。新实现窄适配OS2已成立的SourceRef/新闻解析/条件写，没有复刻其current_state/原生旧队列。日期和有界去重用标准库，现有源解析与保管直接复用；无新增解析器、数据框架或依赖锁变更。

本模块、CLI、专属手动workflow、tests/test_news_rolling.py及本文随新闻滚动能力维护/退役；共享新闻源校验与Store保护由原owner保留。测试保护24小时边界/时区、重复/版本、失败/断链、裁剪、筛选游标、真实本地GitStore与包外CLI；不以测试数代签业务价值。真实新采集、RSS、日常连续运行、24小时完整覆盖、#13人类使用及#7取证均不能由本组代码或一次保存预签。
