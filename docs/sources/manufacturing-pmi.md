# 制造业月度景气读取

本模块是产业读取的一个有限来源族，不是新的采集器、价格雷达或公司暴露真源。
它读取现役 `research_sources.capture_plan` 的单个原始结果，复用真实目录资格、
`inspect_result` 和既有 `source_inputs` 导出；不改 Relay、保管、研究或市场共享合同。
命令行消费者是 `python -m decision_kernel.interfaces.industry_pmi_cli`。

## 字段与经济口径

字段合同核对于 2026-10-16：
[Tushare cn_pmi 官方说明](https://tushare.pro/document/2?doc_id=325)。
范围用 `start_m/end_m`；输出月份列是 `month`。不把输出列名当成单月筛选参数。

| 字段 | 本次解释范围 |
|---|---|
| `pmi010000` | 全国制造业 PMI 合成指数 |
| `pmi010400` | 生产景气 |
| `pmi010500` | 新订单景气 |
| `pmi010700` | 原材料库存景气 |
| `pmi011400` | 产成品库存景气 |

解释依据：[国家统计局指数编制方法](https://www.stats.gov.cn/zs/tjws/zytjzbqs/cgzlzs/202501/t20250121_1958396.html)、
[PMI 指标说明](https://www.stats.gov.cn/zs/tjws/tjzb/202301/t20230101_1903972.html)，
以及[官方发布中的单位、月差与季调附注](https://www.stats.gov.cn/sj/zxfb/202511/t20251130_1961945.html)。
这些是方法/字段依据，不是本次已取得或核验 2026 年业务数据的证明。
旧 Kernel 对 cn_pmi 仅登记为待评估补充来源，见
[`relay-source-catalog-review-2026-10-14.md`](https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/docs/readings/relay-source-catalog-review-2026-10-14.md)。
本次复用 OS2 已合入的注册式取材，而非重新移植旧来源入口。

输出 `level` 保持原指数百分数数值，`change_pp` 是相邻日历月份的百分点差，
绝不做价格收益率或实物量增速。`regime` 单独标记高于、等于或低于 50，
`direction` 只表示指数相对上月的升降。因此，“低于 50 但回升”不能被输出成库存增加。
零值是合法端点；布尔值、数字字符串、超界值不强转。数值缺失不填零。
合成指数与分类扩散指数分别标记，不重算合成指数或推断企业份额。

官方发布有季调说明，但 Relay 投影本身没有足够证据独立核准具体采集版本的季调、
原发布和历史修订；输出保留 `capture_checked_against_official_release=false`，
不能凭接口名把这一验收签成完成。

## 确定性与失败边界

单次范围为 1–120 个日历月，恰好六列，`limit` 为 1–2000，初始 offset 必须为零。
禁止混入地区、企业规模、非制造业或其他筛选后仍贴“全国制造业”标签。
缺月、空值和有限异常值分别显式表示；存在任一缺口就不能得到完整读取状态。
月份非法、越界、重复（即使数值相同）及不完整/损坏的采集结果整体拒绝，不能挑一条好值继续。
通用入口已经拒绝 NaN/Infinity 或复杂嵌套单元格，此处不绕过其保护。

`as_of` 必须是明确带时区的读取截点，拒绝截点之后的采集；月份按北京时间判断。
采集时间不冒充发布时间，`published_at` 仍为 null。月末前是否已发布、是否后修订、
历史时点是否可知仍须取得官方版本证据，不因通过时钟校验而升级为 PIT 认证。
单次采集的分页结束与所选月份/单元格覆盖分别报告，空响应不代表经济“无变化”。

每个非缺月点保留查询键、原分页 offset、页内 1-based 行、字段和投影哈希；
有效月差同时绑定前后两点，缺月不跨越比较。投影哈希直接对应现役
`source_inputs` 导出中的 `page`（包括已验证表与运输回执，不带任意执行诊断）。
`capture_view_sha256`、`view_version` 只标识本次读取输入/派生版本，不是 SourceRef、
来源真实性、公开许可或跨采集修订检测器。新采集不会覆盖旧版本。

## 使用

在按仓库既有流程安装项目的环境、仓库根目录执行；也可在子命令之前显式传
`--registry /path/to/relay-registry.json`，并保留其相对路径下的原手册与快照。
以下仅是命令示例，不是已执行的真实来源请求。

```sh
python -m decision_kernel.interfaces.industry_pmi_cli plan \
  --case-id manufacturing-context --start-month 202601 --end-month 202603 \
  --output pmi-plan.json
```

生成的是原始 `research-source-plan-v1`，能直接交给现役捕获命令；不是
`validate_plan` 的扩展诊断对象。取得适用取材/资料保管授权后，仍沿已有入口执行，
本模块不提供新的 `--live`、密钥、替代地址或任务开关：

```sh
python -m decision_kernel.interfaces.relay_research_cli capture \
  --plan pmi-plan.json --output pmi-capture.jsonl --live --public-rights-confirmed

python -m decision_kernel.interfaces.industry_pmi_cli read \
  --capture pmi-capture.jsonl --start-month 202601 --end-month 202603 \
  --as-of 2026-10-16T22:00:00+08:00 --output pmi-reading.json
```

`read` 接受上面单请求计划产生的一条原始 JSONL 结果，也接受同内容 JSON。
多结果文件必须先按明确查询键选择一条，不能默认取第一条或拼接不同来源版本。
本地解析成功与业务覆盖分开：退出 0 可对应 `read/partial/empty/unavailable`，
消费者必须检查状态和 `coverage`；输入、回执或输出路径拒绝退出 2。
输出文件只创建、不覆盖；失败只打印固定代码，不回显输入原文。

需要保管时，对原 `pmi-capture.jsonl` 使用既有 `relay_research_cli export` 与
版本保管流程，并独立读回真实 SourceRef；`pmi-reading.json` 不自动成为来源对象。
这里没有新增公开 records 写入，也未接通“仅凭 SourceRef 重建完整采集”的新入口。

## 验证与剩余工作

专属测试：`tests/test_industry_pmi.py`、`tests/test_industry_pmi_cli.py`。
复用 `test_research_sources` 的合成 HTTP fixture；用真实 RelayRegistry、RelayHTTP、
`capture_plan/inspect_result/source_inputs` 验证调用合同和页/行对应，并阻断离线 CLI 建立网络客户端。
全部业务值为明确的合成输入，不能充当当期 PMI 资料、独立审阅或用户验收。

本组不关闭整个产业任务。下一层实际工作仍包括：授权范围内取得真实响应，
与官方同月原发布逐项对照，沿既有保管获得 SourceRef 后独立读回，并在实际研究中采用/挑战。
真实产销数量、库存金额与资金占用、利润、汽车细分、运价航线、公司暴露及 500+ 扩展
均未被这五项宏观景气指标替代；未开定时任务、通知或交易动作。
