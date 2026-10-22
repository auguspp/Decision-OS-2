# 完整 CI 迁入与交付合同

范围：最终设计第22章的实施细化；执行owner为 [#5](https://github.com/auguspp/Decision-OS-2/issues/5)。本文不是已运行结果，实际配置、run、产物和main证据在#5/相关PR。用户已选完整优化体系，不按测试少/费用重开取舍。

<a id="scope"></a>
## 1. 一套完整骨架，四种明确资格

| scope | 需要成立的事实 | 不可冒充 |
|---|---|---|
| `content` | 本仓可信base、精确差异符合已审内容范围、真实内容检查 | 方法/规则/权限/schema的Markdown不自动免正式验证 |
| `draft_feedback` | 获准Draft的固定smoke及有界反馈 | 不代表全部受影响测试，不可用于正式合并 |
| `full` | 当前完整collection，独立小依赖合同组+剩余双级并行，最终集合/环境/产物核验 | 单组/最快分片/旧run不是全量 |
| `merge_reuse` | 本仓合格PR full、真实合并关系/tree/环境/策略相符，main必要smoke实际运行 | 是明确复用，不是main再跑全量 |

保留跨runner与片内worker能力，初始参考旧4×2；按实际集合校准参数，不删除双级能力，不制造业务测试填空。空分区必须来自已证明的集合划分且显式处置，不能把必需缺片/skipped混成空分区。

时长仅分配提示；新测试无时长照跑。研究小环境可少依赖，共享版本不得漂移。分片实际collection与结果相符，互斥并集覆盖完整声明集合；环境来自各实际runner，不复制prepare假装实测。

精确 base 没有合格最新 main 证据时，允许额外读取它的一个真实 Git 第一父提交的最新一次 main 合格产物，仅借其中时长并以 `timing_fallback` 记录来源。当前 base 的失败与资格不变；父提交也不合格或读取失败即保持无提示，不遍历祖先、不跳过同一 head 的最新失败，祖先时长不授予 content 或 merge_reuse 资格。

<a id="bootstrap"></a>
## 2. 自举：没有旧base也必须能建立首个可信full

#4只交付前代码文档与模板；#5的首次代码自举已由PR #15完成，实际证据留原事项。这不产生永久“文档免测”范围。

#5首个代码PR同时提交必要包/真实测试和完整CI工作流。首次没有可信base，content/merge_reuse自然不能用；由PR真实运行自己的完整工作流并记录head、tree、集合、环境和原产物。初次门禁信任/配置按原生能力明确，不先借旧仓绿灯、不把启用条件设成必须已有本仓成功full。

首次合并仍要main必要验证；没有合格复用关系/环境就新full。main实际成功后，才成为后继content/reuse候选。root gate精确名称以实际check-run为准；是否启用原生required-check强制按第5节用户决定，不在设置里猜一个永不出现的名字。

CI创建的检查工具依赖应在本仓锁定；取旧代码先核真实直接依赖和许可，不复制仅服务旧业务的历史codec。没有业务消费者时不预造Research模型，但可有真实安装/入口/CI合同测试，不用玩具测试填数量。

<a id="identity"></a>
## 3. 两种身份与失败语义

验证责任身份回答保护哪个现役能力/合同、由谁维护、何时退出；结果身份绑定实际repo、commit/tree、run/attempt、环境、collection、策略与产物。任务Issue只是定位，不是能力的永久ID。

root gate检查实际needs与scope；required job失败、取消、缺片、意外跳过不能被always归为成功。不用continue-on-error掩盖必要失败，不删旧失败/换attempt伪装首轮绿灯。同SHA从Draft转Ready需要正式验证。

Merge Commit本身不保证同tree；base移动或环境不同就按真实条件决定复用，不放宽资格。策略/依赖/未知变更不能自证免检。旧仓的原证据是实现依据，不跨仓取得本仓运行资格。

CI不持有业务源/私人研究凭据；读取Git元数据/产物的token不传给业务测试或来源文本，不执行下载包中的任意代码。正式失败保留原日志和原产物；若真实基础设施故障需重执行，明确原因与新attempt，不能仅为绿灯循环rerun。

测试collection保持quiet输出供确切node解析；正式执行使用pytest原生详细输出记录测试node与结果，xdist完成行同时标明worker，不承诺未完成node的完整worker时序。子进程超时后保留已经写入的原日志并尽力回显，仍抛出原`TimeoutExpired`，诊断读取或输出失败不能替换该失败。进度日志不替代collection、JUnit和result的完整验收；300秒子进程限时、worker、分片、环境和门禁条件不因诊断输出改变。

<a id="inheritance"></a>
## 4. 移植检查只围绕真实依赖

固定旧参考：`auguspp/decision-kernel@cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8`。读取 [CI-MAINLINE](https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/docs/CI-MAINLINE.md) 与 [ci-full-v2.yml](https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/.github/workflows/ci-full-v2.yml)，沿实际uses/import查所需prepare、合并证据、小环境和安装组件；不猜路径、也不全仓反复搜索。

每一搬入项短记：旧来源→新owner→直接消费者→保留的语义→新身份/依赖差异→退出内容。旧Python精确版本等先作已验起点，新运行版本须确切安装/记录，不用浮动patch导致分片漂移；升级按同责任正常处理。

不重跑旧4×2实验，不重建CI管理平台，不要求迁移所有旧产物解释器。需要的新反例放在实际责任owner：集合缺项/重复、旧head、环境漂移、无可信base、复用不适用和来源字符串无权限。

<a id="settings"></a>
## 5. 主线设置：按用户决定不启用保护

2026-10-15用户明确“主线保护不需要开”，[采纳与范围记录](https://github.com/auguspp/Decision-OS-2/issues/5#issuecomment-6185947014)。因此本公开工程仓不启用branch protection，也不通过ruleset或required-check设置变相启用同类强制。原主线保护与required-check配置义务退出，不再作为#5或后续施工阻塞。这是用户调整要求，不是配置成功，也不是降低完整CI目标。

正常工程协议不变：main仍经PR、使用普通merge，合并前检查确切head的适用完整证据；不进行无授权force/delete。`CI Gate`继续实际执行，但没有服务端保护替人阻止不合规合并，不能宣传主线具有强保护。私人运行目标、研究高影响写入、正式Decision的权限边界不因本决定解除。

原生自动关Issue禁用和合并选项设置不因这句话一并改变。没有实际管理入口时，继续普通merge及不含自动关闭词的Refs关联，按原事项保留设置状态，不能声称服务端已强制；这些配置缺口不阻断无关工程工作。已有设置先读后改，明确权限拒绝不绕过。

<a id="acceptance"></a>
## 6. #5收口材料

提供一组正常交付中的实际证据：包外安装/所需入口；Draft有限反馈及Ready full资格；当前collection和分片/合同并集；真实环境和完整产物；合并head/tree与main运行或严格复用；适用设置的实际状态和文件读回。第5节已由用户取消的原生主线保护不再要求配置验收。一次正常变更足够覆盖的，不另起同形pilot。

无法在首个main直接出现的content/reuse路径，用必要合同反例和下一次本就需要的正常变更完成采用；未实际使用的路径明确未自然采用，不靠空PR制造成功。首组完整体系代码和必要合同仍需实际交付，不能将这些路径简单留空/延期。

发布源代码、安装、浏览器、新数据服务、真实研究和Human接受分别有各自证明；本次CI不签投资研究正确。所有规则中的历史样例只按原范围继承。

## 7. 维护和退出

新增测试绑定能力；退役能力同步退专属workflow/fixture/配置/依赖，共享保护保留。优先消除已结束责任，再合并重复执行，最后提速；三种收益分开报告，不设测试数量配额。普通PR只读相关合同，不每次全文重验58项历史清单。
