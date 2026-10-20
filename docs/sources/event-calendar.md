# 事件日期与原研究接续

事件日历读取明确保管的公司与宏观事件来源，展示计划、确认、改期、取消、来源报告的实际发生，以及到期未取得实际证据。入口是现役 `reading-room` / `store_cli research-room` 的可选 `event_calendar` 面板；它没有独立数据库、当前指针、定时任务或通知。

## 复用与范围

复用现役 `relay_disclosure_date` 保存的 `relay-disclosure-date-v1`、原 VersionedStore / SourceRef、研究版本与底稿问题、现役不可变读取作用域、原私有阅读门及离线只读页面。旧 Kernel `cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8` 的 `docs/b2-disclosure-appointments.md` 只提供已评估的日期/预约语义：`ann_date` 不是实际实施日，缺值不是未改期，实际披露日期不等于发行人 PDF 已读。本组没有恢复旧固定股票名单、来源预算或任务。日期处理使用现役 Python `datetime` / `zoneinfo`；不需要增加日历服务或调度依赖。

## 精确输入

Room plan 加入 `event_calendar: {reference: SourceRef, as_of: aware-ISO-time, subject_id?: id, window?: {start: ISO-day, end: ISO-day}}`。`as_of` 是本次复核时钟，不是历史当时可得或已执行研究的证明。Root SourceRef 必须选择完整已存 content：

```json
{
  "schema": "event-calendar-v1",
  "scope_note": "明确已保管来源、主体及资料窗口的范围与限制",
  "inputs": [{"input_id": "one-release", "format": "event-facts", "reference": {"manifest": {}, "selection": {"kind": "all"}}}],
  "followups": []
}
```

示例中的空 manifest 只说明结构，不能运行；实际输入必须使用原 `put-source` 的完整四字段 manifest 引用。

每个 input 的 format 为 `relay-disclosure-date` 或 `event-facts`，后者指向 `{schema: "event-calendar-facts-v1", events: [...]}`。每条 event 必须有 `event_id/category/subject_id/subject_name/title/period/timezone/status/evidence`；category 为 `company` 或 `macro`，status 为 `planned/confirmed/cancelled/occurred/unknown`。可选 `announced_at/scheduled_for/actual_at` 独立保留：字段未提供、显式 null、确切日期或有时区时间分别展示。`occurred` 必须有实际日期；取消或计划状态不能同时填写实际发生日期。日期值合法与引用原文存在不认证人工提取的经济语义。

`evidence` 每项为 `{reference: SourceRef, quote: exact-selected-text}`。消费者实际读原来源，逐项核原文引文确实存在，并保留该确切原证据的取得时钟与时间资格问题；引用失效、缺正文或引文不符使该 input 不可用，原 sibling 仍按真实范围展示。来源文字是数据，不执行其中指令。来源日期解释明确标为原稿提取，不能从标题、采集时间或当前时钟自动生成事件日期。页面的原日期资料与原引文均可导出既有 `read-source` 请求，payload 直接是原 `SourceRef`，不套另一层 reference。

## 改期、缺口与版本

同一显式 event_id 的全部已读来源版本保留。主体、类别或期间冲突不能按同名合并。只有来源公布时间区间能确定先后，才生成有序变更；同日未给具体时间、冲突版本及缺公布时间保留先后未知，不按输入顺序、抓取时间、最大预约日或哈希选“最新”。来源只给一天时，表示其声明时区的一整天，不伪造午夜实际发生。夏令时由 IANA 时区解释。

预约日期经过返回 `due-no-actual-evidence`；窗口当天返回 `window-open`。取消只从明确取消状态取得，后版删除日期只是 `date-became-unknown`。实际日期在复核时钟或原证据取得时钟之后、源日期自带时间问题时不签已发生。日期只到天而取得时钟落在当天，或原证据未保留取得时钟，实际发生的时间资格继续未知；较晚的提取稿保管时间不能代替较早的原件取得时间。来源报告已发生仍明确 `actual-material-not-read-by-calendar`。Relay 的原 `modify_date` 文本可能是未解析修改记录，不把它猜成一个日期。合法空请求只代表所选来源无记录，不代表完整日历无事件。

最多16个输入、128条原事件记录、16条研究接续，来源与研究共享本次16MiB读取总量、结果2MiB；来源沿原最多32个不可变 commit 读取作用域，研究沿同一原Store及宿主请求预算。相同确切来源或研究引用在本次读取中复用一次已核结果，各条研究关系仍分别核引文与原问题。所有实际输入先读再过滤，主体/日期窗口不改完整scope、失败分母或view_version。最新版日期未知或版本先后不明时，即使某个旧版日期在窗口外，也继续显示该事件及缺口。只有明确已读事件数可计数，未读取输入中的事件数量保持未知。原私有/公开绑定变化、私有升级反转、Store访问失败、过期读取作用域和本次读取预算耗尽均终止整个日历读取及Room输出，不降级成一个缺项后继续读取兄弟项，不重置预算或换后端继续。

## 原研究接续与权限

可选 followups 每项是 `{event_id, reference: exact-Research-BlobRef, question_id: id-or-null, quote: exact-research-text}`。原研究引文与问题实际核读，保留确切版本、未决项及原 `prepare/resume` 读取请求。关系由提交者明确声明，存在引用不代表新资料已被原研究采用；日期到期不会生成研究版本、改变判断、解除反证、创建 Full 或通知。

私有 Room 继续原生核仓库可见性，在原件及页面输出前重新核验。公开输出同时需要现役研究与来源导出确认，私有保管不赋予公开再分发权。代码导入、页面打开和筛选都没有来源请求或写入；来源保存沿已授权的原 `put-source`，未知写按原键对账。没有新源调度、旧预算重放、正式 Decision 或执行权。

本能力维护来源版本、日期语义、历史次序、原问题接续、导出权限与安全的页面转义。退役时同步移除专属模块、Room 接线与专属测试，共享 Store、研究、阅读和 CI 保护继续由现役 owner 维护。合成测试、正式 CI、真实来源保管及正常消费者、人类交互与经济研究质量分别验收。
