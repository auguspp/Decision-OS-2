# 重要数值：精确来源 → 确定性计算 → 同源展示

这是 #7 的现役数值消费者，不是新的 ResearchSnapshot、Odds 引擎或独立资料库。沿用原 `SourceRef`、`VersionedStore` 和 `research.md`。研究者在原 Full/Quick 委托内使用它检查**显式声明的关键数值**；没有识别全文所有数字、自动理解财报或认证经济判断的保证。没有数字底稿返回 `not-declared`，不是 PASS。

## 为什么提炼而不是搬回旧系统

固定旧依据：`auguspp/decision-kernel@cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8:src/decision_kernel/claim_audit_contract_v1.py` 中 `calculate_claim_audit_v1_result` 及操作数必须绑定来源/明确假设的语义。保留 identity、sum、difference、product、ratio 五类 Decimal 80 位运算；去掉旧 DeepResearchPackage、UUID/EvidenceArtifact、Pydantic 与研究快照耦合。单位核对和展示用标准库完成，未引入另一个计算/单位框架或新增依赖。这里的顺序计算只处理有界静态操作数，不是任意表达式、脚本、DAG 调度或模型执行环境。

[Python 3.12 Decimal 官方说明](https://docs.python.org/3.12/library/decimal.html)支持明确精度/舍入及信号处理。每次计算建立独立 Context（80 位、ROUND_HALF_EVEN），不继承调用者全局精度、traps 或舍入；除不尽的比值标记实际 80 位舍入。输入必须是有限十进制字符串，拒绝 float、NaN、无穷、模糊千分位及超限指数。单位只覆盖当前金额、股份、项目数量与比率用途，币种不自动转换。

## 输入与来源资格

`research-numbers-v1` 包含 `schema/sources/inputs/calculations` 和可选 `template`；`research-numbers-v2` 在同一形状上增加必填的 `knowledge_cutoff/evaluated_at`，用于下述截止时点核验。分别最多 64 个来源/输入/计算，每项计算最多 16 个操作数，整个输入与附录均不超过原记录的 2 MiB。来源列表是真实已保管 SourceRef，不是 URL 或自造哈希。

每个输入必须声明 `id/value/unit/subject/period/scope`，另选其一：

- `source` 是 sources 的零起始下标，`locator` 指向返回表示内的确切数字。CSV 使用 `{"row":1,"column":"revenue"}`，行号从 **SourceRef 已选行集** 的第一行起算；文本使用零起始 Unicode 字符 `start`（含）/`end`（不含），不把单个数字从另一金额或负号中截出来。
- `assumption` 是不可为空的明确假设理由。它不会变成 Evidence；所有依赖结果继续标为 conditional。

源值与 `value` 确定性比较。可选 `context_bindings` 用同样定位器绑定 `unit/subject/period/scope`：CSV 必须与数字处于同一行，防止拿另一公司的身份替本行背书；文本只证明字面匹配，语义联系仍需审阅。未绑定字段明确为 `CONTEXT_DECLARED_NOT_SOURCE_CHECKED:*`，不能为了变成 checked 而伪造来源列。字段匹配也不证明所选字段就是正确业务口径。年累计与单季、时点与流量、集团与归母不能通过填相同标签掩盖差异。

单位代码：`CNY/CNY_10K/CNY_100M`、`USD/USD_1M`、`share/share_10K/share_100M`、`CNY/share`、`USD/share`、`project`、`CNY/project`、`ratio/percent`。还识别明确的“人民币元/人民币万元/人民币亿元、股/万股/亿股、人民币元/股、%”。不猜币种或汇率。来源原表示、日期、保管级别和限制原样保留，摘录不升级为 PDF 原件。

每项计算声明 `id/operation/operands/unit/subject/period/scope`。操作数引用已有输入或**更早的**计算 ID，不拿人工 claimed 值作为后继输入。输出单位必须与运算量纲一致；`places` 是 0—12 位显式显示精度（默认2），可选 `claimed` 必须与该精度下生成的显示值相等，没有统一百分比容差。

主体/期间/归属不一致默认拒绝相依计算。合法的累计差分、集团到股东桥或估值反推可以用 `context_bridge` 对每个改变字段说明理由，例如 `{"period":"同口径全年减上半年得到下半年；重述口径已由研究者核对"}`。这只是可审查声明，结果持续 conditional，程序不会认证其经济成立；币种和量纲错误不能用该字段绕过。

<a id="time-qualified-v2"></a>
## v2：截止时已经保管的数值表示

v2 只增加一个明确用途：**在所声明的研究截止时点，OS2 已经保存该数值所依赖的表示**。它沿用 v1 的数值定位、单位、主体、期间、归属和五种 Decimal 运算，不将时点通过扩展为来源真实性、原件完整性或合格 Odds 输入。

`knowledge_cutoff` 是本底稿采用的研究截止时点，`evaluated_at` 是调用者固定声明的评估时点，不是系统签认的实际执行时间。两者必须是带时区、可比较的确切瞬间，且前者不晚于后者；输入格式为 ISO 日期时间，显式含秒、可选 1—6 位小数，以及 `Z` 或 `±HH:MM` 时区。它们随附录保存，精确重算时不替换为当前时钟。只有日期、缺秒、缺时区或缺失信息时，不补零点、秒或默认时区来取得资格。

读取一个有来源的输入时，使用其确切 SourceRef 对应的元数据逐项核验：

| 时点 | 取值位置 | 证明范围 |
|---|---|---|
| `published_at` | 来源 `manifest.input` | 作者提交的发布时点声明，不是系统独立核实的发行时间 |
| `acquired_at` | 来源 `manifest.input` | 作者提交的取得时点声明，不是来源 API 的认证回执 |
| `stored_at` | 来源 `manifest` | OS2 原保管元数据中的时点；固定元数据不等于认证机器时钟，今天补存旧资料不会倒算成过去已经保管 |

三个来源时点均须为可比较瞬间，并满足 `published_at <= acquired_at <= stored_at <= knowledge_cutoff <= evaluated_at`。缺失、只有日期、格式不合格、先后矛盾或晚于适用边界时，该来源输入及依赖它的计算为 blocked；无关来源、明确假设和其他计算仍分别检查、保留结果。明确假设仍是 conditional，不通过省略来源把它变成已验证证据。报告期和输入的 `period` 继续按原口径核对，不能拿发布日替代财务期间。

v2 输出为 `research-numerical-audit-v2`，保留两项底稿时点，`evidence_time_scope=retained-by-declared-cutoff`。逐输入 `evidence_time` 报告时点原值、精度、出处及具体限制；显式假设的时间核验为 not-applicable。只有实际掌握这些时点时才使用 v2 的时间用途；不能证明时仍可保存部分研究，或沿 v1 做没有声明此时间资格的算术核验，不伪造时间来取得通过。

访问、保管和公开再分发仍须按原入口与来源权利判断；时间字段不提供这些授权，也不认证外部原件。股类、指标语义的进一步绑定、第二个真实来源的等价使用及 Odds 资格由各自后继验收，本版不据此宣布完整来源接入已完成。

### 有明确来源的合成输入示例

以下仅演示本地合成底稿形状。先由测试库实际保存一份 CSV 表示，将返回的完整 SourceRef 写入 `source-ref.json`；不手填生产哈希。示例 CSV 的首行数据为 `125,CNY_100M,synthetic-co,2025,consolidated`，对应列名为 `revenue,unit,subject,period,scope`。测试来源声明的发布时间、取得时间及系统生成的保管时间须真实满足上述顺序；生产调用填写自己的实际时点，不照抄示例。

```python
import json
from pathlib import Path

source_ref = json.loads(Path("source-ref.json").read_text(encoding="utf-8"))
payload = {
    "schema": "research-numbers-v2",
    "knowledge_cutoff": "2026-10-25T10:00:00+08:00",
    "evaluated_at": "2026-10-25T10:01:00+08:00",
    "sources": [source_ref],
    "inputs": [{
        "id": "reported_revenue", "value": "125", "unit": "CNY_100M",
        "subject": "synthetic-co", "period": "2025", "scope": "consolidated",
        "source": 0, "locator": {"row": 1, "column": "revenue"},
        "context_bindings": {
            field: {"row": 1, "column": field}
            for field in ("unit", "subject", "period", "scope")
        },
    }],
    "calculations": [{
        "id": "revenue", "operation": "identity", "operands": ["reported_revenue"],
        "unit": "CNY_100M", "subject": "synthetic-co", "period": "2025",
        "scope": "consolidated", "places": 2,
    }],
    "template": "声明范围内的收入：{{number:revenue}}。",
}
Path("numbers.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
```

随后按原 `audit-numbers --input numbers.json` 读取。若该来源在示例 cutoff 后才保管，数值可以匹配而时间用途仍 blocked；修改 cutoff 意味着形成新的底稿意图，不能拿新时间重放或覆盖旧已保存正文。

## 实际命令与保存

```bash
# 原绑定默认只读，核对源字段/计算/展示，不保存、不取新来源。
decision-os store --binding config/store-binding.json audit-numbers --input numbers.json

# 本地合成测试仍使用原 --path；不能把真实私人材料伪装成合成数据。
# 已保存研究按确切 BlobRef 重算，不追最新修订或最新来源。
decision-os store --binding config/store-binding.json audit-numbers --ref research-ref.json
```

宿主 `research-runtime.yml` 增加只读 `operation=audit-numbers`。payload 是上述数值输入，或 `{"reference":<确切研究BlobRef>}`。既有无损载荷工具仍可使用；原 code_sha、60 KiB 外层、2 MiB 解码、8 MiB 结果及权限边界不变。无需 submission_id/expected_head/写 token；来源 URL 不会被获取。公开运行的 inputs/产物也是公开发布，压缩不会改变这一点，未获准个案材料不要提交到公开 Actions。

输出有逐项结果、限制、`table_markdown`、从 `{{number:calculation_id}}` 占位符生成的 `rendered_excerpt`，以及可保管的 `appendix`。表格与正文片段来自同一已重算数值，不再手动缩放。没有标记的正文数字不在检查范围；不要把整个输出当作自动审计整份报告。

把完整 `appendix` 加在原 `ResearchProgress.body` 中，并将其中全部 SourceRef 保留在原 `progress.sources`，正常调用现有 `checkpoint-research`。**保存后会自动返回独立的 `numerical_audit` 摘要**，包括数值错误和未核口径；不会更改原保存意图或旧方法/正文哈希。数字错也可以保留部分研究和成功回执，但数值用途必须标为 blocked，不把“存好了”当“数字通过”。普通 `submit-revision` 仍是底层保管，不签数值质量；使用前按确切版本执行 audit-numbers。

后继 `audit-numbers --ref` 从同一报告附录恢复原数值输入、检查引用确被该研究保留、重算并核对**附录内生成的展示**。后人只把“万元”改成“亿元”、只改显示值而不改依据，将返回 `SAVED_NUMERICAL_DISPLAY_MISMATCH`。附录外全文不在此保证内，附录不存在、删去或格式损坏不能取得通过结果。资料不可读时表示 not-comparable/blocked，不把读取故障说成正文被篡改。

v2 使用独立的 `decision-os-numbers-v2` 附录标记；由原 checkpoint、[完整交付](delivery.md)和新只读 reader 按已存版本恢复。同一正文只接受一份匹配版本的数值附录，不能混放 v1/v2 来选择较宽结果。v1 合法历史附录仍按原语义读回，不补造时间字段，也不因后来增加 v2 而宣称旧版曾核验截止时点。开始保管 v2 前先升级其实际 reader/恢复消费者；保存后回退写入口仍须保留 v2 历史读取。

CLI 对 blocked/not-declared 返回非零，同时保留 JSON 诊断。宿主这类结果是 `numerical-not-cleared`、退出2，原输入与计算结果仍进入原产物；checked/conditional 仅是声明范围内的机械检查结果。来源真实、口径选取正确、经营解释充分、合格行情/Odds、用户接受和正式 Decision 仍各自证明。

## 维护与退出

责任为 #7 数值证据/展示，用例是 Full 的现金/资本/每股桥与关键主张复算。单源缺失、金额错配或坏计算只阻止其依赖，其他结果继续呈现；部分研究、已保存版本和未决问题不被销毁。保存失败仍由原幂等/CAS/对账合同处理。

专属测试包括货币/每股换算、错币种、负号/截数、单位/主体/期间/口径错配、明确假设与跨期桥、舍入、失效来源隔离、原版本读取和展示篡改；v2 另核时区比较、缺失/仅日期时点、先后矛盾、截止后补存、相依计算隔离和两版附录恢复。同一次 cold-wheel 消费者完成来源→数字→原研究checkpoint→精确重算，模拟宿主验证真实 Git 上只读无写。工程测试不代签新公司 Full 或数值真实性。旧数值附录版本一旦保管，读取/重建支持随实际保留责任维护；将来改变语义必须新版本，不原地把历史机械结果改成新方法。
