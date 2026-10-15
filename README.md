# Decision OS 2.0

> 新项目从零建立业务状态，不从零假设技术能力。

这是 `auguspp/Decision-OS-2` 的代码与工程规范入口。产品帮助用户主动发现现实/产业变化、开展 Quick 与 Full Research、持续检验判断，并支持独立人工 Decision；不推断持仓、不自动交易。

## 从这里开始

| 你要做什么 | 唯一入口 |
|---|---|
| 新对话接手施工 | [AGENTS.md](AGENTS.md) → [当前阶段 #3](https://github.com/auguspp/Decision-OS-2/issues/3) → 具体任务/PR |
| 看完整产品与目标架构 | [最终目标设计 v1.2-final](docs/decision-os-2.0-design.md) |
| 看实施顺序、依赖和收口 | [实施基线 v0.2](docs/implementation-baseline.md) |
| 看雷达、资料、研究与交付的完整施工落点 | [36项能力地图](docs/implementation-baseline.md#coverage) · [工作组与依赖](docs/implementation-baseline.md#work-packages) |
| 看统一施工与协作规则 | [WORKING-PROTOCOLS.md](WORKING-PROTOCOLS.md) |
| 看命名、文件归属和首批引用 | [仓库与接线合同](docs/architecture/repository-contracts.md) |
| 看完整 CI、验证继承和自举 | [CI 合同](docs/engineering/ci-contract.md) |
| 暂停、接管、换对话 | [交接格式与恢复步骤](docs/engineering/handoff.md) |

实时状态只在 #3 和其有限子事项，不在 README 维护第二份进度。历史目标归档见 [#1](https://github.com/auguspp/Decision-OS-2/issues/1) 与 [评审采纳记录](docs/reviews/v1.2-finalization.md)，不要重开已完成归档。

## 基础与边界

新会话使用同一主线规则、原生阶段标签和 Issue/PR 模板；记录认领分支、实际 head、验证和未知效果。模板与文档不是服务端权限或代码验收，GitHub/宿主未读取规则时也不会自动遵守。

首个代码责任是完整 CI 与可安装骨架；共同保管的必要合同可用后，直接 Full/Quick 与市场雷达、产业商品、披露资料及日常交付按依赖并行。首发可用不等于全部雷达或500+覆盖完成，未完成责任须明确接续。文档/规则已落地、CI已配置、产品已使用分别成立，实际结果读对应任务。

本仓公开。仅提交获准工程内容和合成/脱敏样例；私人运行数据、研究、人工决定和密钥在独立获准位置保管。旧 Kernel/OS 业务状态和格式不兼容，技术/成功验收/失败反例按适用范围复用。
