# 畜禽与饲料周度现货阅读

本用途消费已经保管的 `industry-reading-v1` 包，接在原 `industry-read` 上。
它不请求网页或 Relay、不另建采集器、不写 records，也不把数据读通视为当前公司暴露。

## 入口与来源

```bash
python -m decision_kernel.interfaces.industry_cli --binding config/store-binding.json --ref packet-ref.json --livestock-weekly
```

原 `research-runtime.yml` 的 `industry-read` 使用 `view=livestock-weekly`，其余完整 SourceRef、
独立 `public_sources_custody_confirmed=true`、code_sha、只读作业及结果预算不变。
可选 subject_id 或 company_id，但不同时传；筛选不改变完整分母或 view_version。
未取得发行人证据的价格包允许 relations 为空；公司查询无结果不是零经济暴露。

原[产业读取合同](industry-reading.md)仍拥有来源元数据、取得/公布/整理/截止顺序、CSV 与角色。
本用途只解释 `observation_family=mara-livestock-weekly` 的 commodity Entity；每主体一条系列。
来源为农业农村部畜牧兽医局的集贸市场/采集点调查，或明确记录取得方式的同源转发摘录。
源页面、转发页面、原发布与转发日期必须各自记录；引用统计数据不等于获得全文再分发权。
现役 Relay 已登记的期货不代替这些现货；没有可用接口时不猜 API 或重复探测。

字段来自[农业农村部周报](https://xmsyj.moa.gov.cn/jcyj/)，例如
[2026年9月第3周的政府转发](https://xmsy.pingliang.gov.cn/scxq/art/2026/art_990dc7740cbe4bf6bed6b7f1ea055664.html)。
全国与主产省份统计范围分别建主体；雏鸡元/只不改成元/公斤；鸡肉不解释为活鸡棚前收购价。
真实资料资格与取得/保存/恢复的事实在 #11 原运行中验收，文档例子不是已取得证明。

## 原资料表示

每条观察仍选择原 COLUMNS 单行，不在包中复制价格。其 dataset 可附加四列：
`reported_previous,reported_year_ago,reported_wow_pct,reported_yoy_pct`。
它们属于同一行的公布前周、上年同期和公布同比/环比，不新造历史观察点或历史日期。
读取这些列时绑定同一 manifest/行，并再核原 COLUMNS 全部一致。

price basis 为 survey-price / CNY / CNY/kg 或 CNY/bird / survey / spot /
contract=not-applicable / weekly / price-level / collection-day。
period_start=end=实际采集日，不伪造整周平均区间；specification、region 与税费口径保留原声明。
原文没有拆税时可明确记 not-separately-disclosed，并在限制中说明；不能由此得到不含税合同价。
缺值保留空格；现价和比较基数要求正值。比较列未提供或不合格只限制该点的公布比较，
已经可读的现价和真实相邻观察仍保留。

## 两种比较与拒绝

同版公布比较按显示价格计算 `(本周/公布基数-1)*100`，以0.1个百分点、half-up做诊断。
输出原百分比、未舍入计算和舍入结果；不同仅标 rounding-difference，不能宣布发布者出错，
因为原发布可能使用未显示的高精度输入。上年同期不推定为采集日减365天。

跨版比较仅在同口径同来源、公布时间不倒退、两个采集日相隔恰好7天时解释为相邻周变化。
缺周、6/8/14天等不当一周；节假日移位必须留实际日期，未经来源解释不自动纠正。
同日的不同版本单列修订。保留公布前周值是否等于前版现价的诊断，不覆盖两者；
同一上游的两版和转发页面不是独立供需证据，不用公布前周值填补遗漏的采集日。
不构造猪粮比、蛋料比、利润弹性或受益股票；公司关系仍只来自原包的具名发行人证据。

## 维护与退出

复用原 read_industry、industry_source_scope、VersionedStore、SourceRef 和标准库 Decimal；
没有新库、网络客户端、权限、工作流、存储或 CI 政策。旧三视图输出保持。
专属 application/livestock_weekly.py、两处原产业入口接线、tests/test_livestock_weekly.py 与本说明一起维护。
退出视图时同步退出这些专属接线/测试/导航，不删除原资料和历史引用，既有原产业读取仍可恢复价格。
无源全文或最新期资格时如实标注；一次保管读取不证明自动采集、自然连续运行、500+或研究质量。
