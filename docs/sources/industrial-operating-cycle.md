# 工业经营周期阅读：利润、成本、库存与回款

Owner：#11。入口沿原 `industry_cli` 和 `industry-reading-v1` 阅读包；不新建来源、
保管格式、运行宿主或研究模型。原价格与公司双向阅读默认行为不变。

## 实际消费入口

```sh
python -m decision_kernel.interfaces.industry_cli \
  --binding config/store-binding.json --ref operating-packet-ref.json \
  --operating-cycle --subject-id nbs-industrial-total
```

本地合成仓用 `--path synthetic.git` 代替 `--binding`。引用文件必须是原保管入口返回的
完整 SourceRef，不是原数据 CSV、临时路径或 URL。此入口只读，不调用 Relay、不取得
来源 URL、不保存记录、不发送通知。远端仓库读取使用既有专用 Store 凭据，不能继承
工程 token；已有只读配置不会因为这条命令变成可写。不得将供应商公开许可未核实
的资料传入公开资料库，代码或标志不代签权利。

`--operating-cycle` 不接受 `--company-id`；公司角色仍用原双向读取命令。本输出保留
`retained` 中的完整原包和关系，但不根据工业总量推断公司受益。主体筛选只影响
`entries`，不改变原资料、整包分母、缺口和 `view_version`。退出 2 表示来源/指标部分
不可用或输入拒绝；退出 0 也不代表每种派生计算可用，仍检查逐项状态和原因。

## 继续用原始资料合同

使用 [原产业阅读包与确切 CSV 行列合同](industry-reading.md)。不允许在派生请求
中再提供一份可改写的金额。每个指标一个 `series`，每项观察仍是原 dataset 的单行
SourceRef；相同主体的指标按包中显式观察位置对齐，不排序、前向填充或补连缺期。

原包上限仍为 32 条序列、每序列 2–64 点、16 MiB 恢复结果；不是 500+ 覆盖承诺。
本层最多打开 32 个原生不可变提交的读取作用域，每个作用域由现役
`GitHubStore.source_read_scope` 在进入和退出时核实际可见性、绑定及可达性。
仅复用不可变字节，作用域关闭后不能读取；所有退出核验通过后才返回结果。
没有提升 512 请求预算、重置网络计数器、放松历史引用或写权限。

## 十二种指标，不混口径

所有主体为 `industry`；`currency=CNY`、`frequency=monthly`；`price_kind`、`market`、
`contract` 均为 `not-applicable`。规格须写实际统计总体，地区和会计口径明确；
`unknown/unspecified` 不可计算。所有 `period_end` 必须为真实月末。

| metric | unit | period_kind | assessment | period_start |
|---|---|---|---|---|
| revenue / cost / profit | CNY-100m | ytd-flow | cumulative | 当年1月1日 |
| revenue_yoy / cost_yoy / profit_yoy | percent | index-level | ytd-comparable-yoy | 当年1月1日 |
| receivables / inventory | CNY-100m | stock | period-end | 同 period_end |
| receivables_yoy / inventory_yoy | percent | index-level | period-end-comparable-yoy | 同 period_end |
| receivables_days / inventory_days | day | index-level | reported-average-days | 同 period_end |

`profit` 专指本用途的利润总额，不是经营利润、净利润或归母净利润；`inventory` 专指
产成品存货，不能悄悄混入全部存货。日期定位代表公布表按月末标记的期间，不能从
天数指标的月末定位推断其只使用月末余额。来源标签不是统计或发行人认证。

金额采用亿元；从原文万亿元换算时明确乘 10000，保留原单位、原数值、原定位和
舍入精度。同比百分数不转换为倍数，差为百分点。支持真实零、负利润和负同比；
收入、成本、余额和天数不接受负数。缺失与非法分别留下原点及不可用原因。

## 计算与可用性

同位置、同主体、同地区/总体/币种/会计口径/月末，且绑定**同一个完整来源版本**的
金额才能计算 `利润总额 / 营业收入 × 100` 和 `营业成本 / 营业收入 × 100`。
分母须大于零；128 位独立 Decimal 环境保留结果为字符串，不受调用者精度影响。
这是公布舍入值的派生，不认证与原统计内部未舍入计算完全相等。

应收账款加产成品存货只是两项选定余额之和，既不称完整净营运资金，也不称现金
流出。同比差只比较原发布的指标：利润/成本/库存/应收同比减收入同比；它们不是
因果归因，余额同比与累计收入同比还有存量/流量差别。只有相应差为正且输入合格
时返回明确待核问题，不给经济评分、交易动作或自动研究任务。

不同统计期的累计金额不相减成单月流量，原公布可比同比不拿上年旧发布金额重算。
同比/天数/余额的期间变化只接受相邻日历月；缺期不跨越。累计同比跨年重新起算时
拒绝直接接续。余额差不表示资金用途，零余额基数不返回相对增幅。
同一期间的不同值标 `same-period-revision`，不是新月增长；较早发布版本倒灌拒绝。
缺数据只限制有关点/计算；未提供的可选指标明确列出，不填零、不删除分母。

## 来源和复用依据

方法和表结构核于 2026-10-16：国家统计局
[2026年1–6月](https://www.stats.gov.cn/sj/zxfbhjd/202607/t20260727_1964194.html)、
[1–7月](https://www.stats.gov.cn/zwfwck/sjfb/202608/t20260827_1965126.html)、
[1–8月工业企业利润](https://www.stats.gov.cn/zwfwck/sjfb/202609/t20260928_1965425.html)。
表1提供累计金额及可比同比，表2和正文提供效益/余额；
[既有官方附注](https://www.stats.gov.cn/sj/zxfbhjd/202509/t20250927_1961400.html)
用于对照方法，不能当2026年数值。

复用原 `read_industry` 的 SourceRef/CSV/元数据核验及旧产业合同的存量/累计/修订
边界，计算只是原输入上的窄应用投影。标准库 Decimal 和原 Store 作用域已足够，
不增加 pandas、数据库、供应商 SDK 或通用派生公式执行器。保管仍由原
SourceInput/VersionedStore/research-runtime 唯一承担；数字资料的公开引用条件按
[国家统计局服务条款](https://www.stats.gov.cn/wzgl/202302/t20230217_1912857.html)
逐项核，不移作第三方供应商转授权。

## 验证、维护与剩余责任

`tests/test_industry_operations.py` 使用真实临时 Git、原 `GitAPI` 模拟线缆和真实
SourceRef，验证算术、单位、缺失、累计拒绝、版本混合、缺月、修订、过滤分母、
90点作用域读取、访问撤回以及原CLI新进程恢复。全部测试值均为明确合成数据；
真实来源和经营解释必须在#11实际运行回执中分别验收，不从测试数推断完成。

本组不替代 PMI、真实产销数量、汽车产批零出口、航线运价、存储报价和当期公司
角色，不改变旧历史数据。退出时同时退出两个专属应用模块、CLI标志、专属测试
及本说明；保留原默认产业命令、Store历史读取、共享保护和既有SourceRef。
