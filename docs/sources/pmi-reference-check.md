# Relay PMI 与已保管参考值核对

本用途承接 #11 的既有 PMI 读取及 #94 产业周期试点。不是重写 #88、重新保管官方
摘录，或把五个宏观指标计入商品覆盖。现役 `industry_pmi` 解释字段、月份及百分点；
本模块把它接到一个实际的参考值比较消费者，并在未确立供应商再分发权时避免发布源行。

## 原宿主，两类不同权限

仍使用 `.github/workflows/relay-source-runtime.yml`，只在明确手动 main 调用时执行。

| 用途 | 操作与声明 | 保留内容 |
|---|---|---|
| 既有财务、参与者、研究 | 原 family，`capture`，`public_rights_confirmed=true` | 原授权捕获格式，原许可门槛不变 |
| 本 PMI 参考核对 | `family=industry-pmi`、`operation=check`、`access_confirmed=true`，公开源行许可保持 false | 生成请求、参考 Ref、状态与比较计数、安全错误/尝试/等待；没有供应商值、投影、正文、响应 ID 或响应哈希 |
| 本用途离线准备 | 同 family，`operation=plan` | 校验计划并生成原 PMI 请求；不读 Store、不请求 Relay、无需 Secret |

访问授权仅允许本用途运行内使用，不等于第三方再分发权。此模式没有新的公开源行开关，
也没有假设 `capture_plan(public_rights_confirmed=true)`。它像已交市场核验用途一样，
直接复用 `capture_registered` 与原 `RetryingRelay` 在进程内消费；原公开 capture 门槛未变。

唯一新增 Store token 使用点与 PMI/check 步骤绑定，GitHub 作业仍只有 contents:read。
旧 capture 步骤不获得该 Store 环境变量。先校验显式操作、main SHA、参考及输入；
准备失败时不开始带凭据的步骤。不改共享 Secret、HTTP、Registry、Retry、存储或 CI 政策。

## 输入与限定

`source_plan` 在此独立 family 下是下列 JSON，不是通用研究计划，不支持任意 API、URL、
密钥、额外代码、容差或源字段。`code_sha` 是调用前实际读取的 main；不传原 typed
codes/periods/dates/apis，page_size/max_pages 保持原默认 100，不把它们用于本模式。

```json
{
  "schema": "industry-pmi-reference-check-plan-v1",
  "start_month": "202607",
  "end_month": "202609",
  "reference": {
    "manifest": {
      "repository": "auguspp/Decision-OS-2",
      "commit": "<实际已存40位commit>",
      "path": "<实际source-views/.../manifest.json>",
      "sha256": "<实际64位摘要>"
    },
    "selection": {"kind": "all", "start": null, "end": null, "columns": []}
  }
}
```

从已存固定 Ref 取参考，不能给“最新来源”或外仓/部分行引用。原 GitHub 绑定及来源版本
检查在读取前和退出时执行，撤权/绑定变化不会留下比较成功。参考必须是现役 NBS 人工转录
content 表示：`representation=official-web-table-manual-transcription-not-original-html-or-relay`，
`unit=index-percent`、`comparison_unit=percentage-point`、`scope` 为声明的全国制造业月范围，
`rows` 恰有 month 与五项已审字段。每个预期月份必须唯一、完整且有效；参考的发布/取得不得
在整理截止之后，末报告月须在二者时钟上结束。源 origin 与 NBS 主机的匹配仅检查元数据，
**不认证原始网页、作者或人工转录正确性**。

现役 cn_pmi 官方字段说明（2026-10-17复核）：
[Tushare 文档325](https://tushare.pro/document/2?doc_id=325)。原五字段及 start_m/end_m 保持不变，
不由字段映射升级为数值或方法认证。支持1–120个已结束日历月；本模式不把当前未结束月当作
已公布最终月值。用原 pmi_source_plan 生成一个限200行请求，最多两页，300秒运行边界；
不是用量配额。初次+三次、至少30秒及有效更长Retry-After仍由原重试策略承担。
参数/权限/未启用拒绝不重复；失败只回原安全分类和等待，不输出错误正文或改 host/key。

## 核对结果怎样使用

读取完整捕获后，用原 read_pmi_context 重新核目录快照、查询、分页/运输、字段与月份。
同月同字段的指数值逐格十进制精确比较；月差也由两个原读取结果逐相邻月比较。
没有宽松容差、舍入求一致、缺值补零、跳过缺月或选取重复月份。参考不完整先拒绝，
不能挑几个参考值把整个窗口签成通过。

`matched` 要求全部预期格相等；`different` 是完整但存在差异；`incomplete` 是实际缺月/值，
可保留其余有效格的比较计数。`source-incomplete/source-rejected` 与 `reference-unavailable`
分别表示取数/语义或参考/权限问题，comparison 可为 null。只有 matched 退出0，其他退出2。
分页结束不代表所选月份齐全，合法空表不会写成无变化或通过。

回执的 nonmatching_cells 仅有**原计划坐标与状态**，没有供应商数值或可反推差值。
回执不能重放源捕获、不能创建供应商 SourceRef、不能证明历史 PIT、未修订、最新全覆盖或
持续健康。相等仅针对本次 Relay 与该已存转录版本；二者可能共享统计上游，不能算独立
经济证据。缺少源原件和公开权利的限制保持。统计指数仍不等于实物产销、库存金额、公司
采购价或利润，不能替代 #11 同期间发行人经营角色，更不能签 Full/Brief 实质采用。

来源版本、原 source 输入和历史报告不变。本核对没有 records 写入、研究、通知、定时或
交易。原作业保留7天诊断，与永久参考 Ref 和独立研究/交付保管是不同对象。

## 复用、测试与退出

本组复用原 Registry/HTTP/capture/retry、PMI读取器、VersionedStore 与只读作用域，仅增加
比较和原宿主窄接线。无需数据框架、供应商SDK、新存储或新的共享重试机制；标准库已经
覆盖比较与JSON，原范围的组件评估继续适用。测试使用实际合成 Git/HTTP捕获/PMI消费者，
验证相同/差异/缺失、重复/越界、参考损坏、退出撤权、原等待/长Retry-After、错误正文隔离，
两类许可、原入口回归和真实离线子进程。它们不是现场源、独立人审或经济质量证据。

退出本用途时，同组撤回 pmi_reference_check、pmi_check_commands、family/check及专属凭据
接线、专属测试与本说明；原制造业 PMI、其他family的capture和所有历史SourceRef保留。
现场结果及未知效果只在原 #11/PR/#94 引用，不在本文件维护第二份实时状态表。
