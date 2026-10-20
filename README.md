# Decision OS 2.0

> 新项目从零建立业务状态，不从零假设技术能力。

这是 `auguspp/Decision-OS-2` 的代码与工程规范入口。产品帮助用户主动发现现实/产业变化、开展 Quick 与 Full Research、持续检验判断，并支持独立人工 Decision；不推断持仓、不自动交易。

## 日常阅读

打开 [每日阅读与行情更新](https://github.com/auguspp/Decision-OS-2/actions/workflows/daily-market.yml)，手动选择 `status` 并填写当前 `main` 的确切代码版本。现仓已按用户明确确认配置[公开运行授权](config/public-runtime.json)：通过原生准入后正常读取已存行情、研究、发现状态和最新已存 Brief，在本次运行的 Summary 看内容日期，下载产物中的 `reading-room.html` 阅读全文。新生成页面不等于今天有新行情或 Brief。

原六股周期仍为北京时间工作日18:35，每周期至多2个日历请求和12个价格请求；真实交易日、延迟补更、失败/hold及预算不变。正常 Public 更新、补更和定时采用已登记授权，不再停在仅元数据诊断。撤销授权时停止相应公开业务，保留明确原因；源码/CI公开不公开密钥，也不重开耗尽任务。现役原件/研究/Brief和备份仍走同一 records，具体见[公开运行合同](docs/engineering/public-runtime.md)、[统一阅读室](docs/delivery/reading-room.md)及[日常更新规则](docs/architecture/daily-market.md)。

### News Desk：打开已保管新闻

进入固定 [News Desk 工作流](https://github.com/auguspp/Decision-OS-2/actions/workflows/news-rolling.yml)，选择 `latest-desk`，保持 `save=false`，`code_sha`、`plan` 和两个写入字段留空。在原生 main/CI 与现役公开授权检查后，Summary 会显示**原捕获／窗口／发行截止**、有限原生运行状态和新闻清单；同包 `news-desk.html` 提供原链接、筛选、完整来源健康和确切历史请求。此入口不需要自己寻找 SourceRef 或 ArtifactRef，不采集、不写 records、不启动研究。

“较新已保管窗口”和“最后已保存发行版”分别显示；没有对应发行版时，只生成保持原窗口日期的未保存预览。10/16 的资料在今天打开仍是 10/16 的资料，今天的页面或绿色 Actions 不会把它更新为今天新闻。历史页先保留完整候选分母，翻页固定 records 快照；导出历史请求不等于已执行读取。完整操作与仍需真实新期／浏览器验收的范围见 [News Desk 合同](docs/delivery/news-desk.md#latest-entry)。当前交付仍是 Actions Summary 与自包含页面，没有新增常驻网站或调度。

## 从这里开始

| 你要做什么 | 唯一入口 |
|---|---|
| 新对话接手施工 | [AGENTS.md](AGENTS.md) → [当前阶段 #3](https://github.com/auguspp/Decision-OS-2/issues/3) → 具体任务/PR |
| 看完整产品与目标架构 | [最终目标设计 v1.2-final](docs/decision-os-2.0-design.md) |
| 看实施顺序、依赖和收口 | [实施基线 v0.2](docs/implementation-baseline.md) |
| 看雷达、资料、研究与交付的完整施工落点 | [36项能力地图](docs/implementation-baseline.md#coverage) · [工作组与依赖](docs/implementation-baseline.md#work-packages) |
| 查询Relay功能、接口参数说明和已有证据 | [共享功能登记与只读查询](docs/sources/relay-registry.md) |
| 持续保管、更新并读取沪深 A 股日数据 | [可恢复采集、增量集合与正常读取](docs/architecture/ashare-data-continuity.md) |
| 读取产业商品变化与公司双向经营角色 | [精确来源阅读合同与CLI](docs/sources/industry-reading.md) |
| 看统一施工与协作规则 | [WORKING-PROTOCOLS.md](WORKING-PROTOCOLS.md) |
| 看命名、文件归属和首批引用 | [仓库与接线合同](docs/architecture/repository-contracts.md) |
| 使用来源与研究版本保管 | [版本保管与合成CLI](docs/architecture/versioned-store.md) · [公开records分支绑定](docs/architecture/github-store.md#topology) |
| 恢复已保存的Quick/Full及其来源 | [只读研究接续与分页](docs/research/continuation.md) |
| 准备研究方法、逐题记录并提交成果修订 | [研究工作入口与权限边界](docs/research/workflow.md) |
| 通过已认证宿主保存研究、读取与对账 | [显式研究运行通路](docs/research/runtime.md) |
| 看完整 CI、验证继承和自举 | [CI 合同](docs/engineering/ci-contract.md) · [实现与运行说明](docs/engineering/ci-implementation.md) |
| 暂停、接管、换对话 | [交接格式与恢复步骤](docs/engineering/handoff.md) |

实时状态只在 #3 和其有限子事项，不在 README 维护第二份进度。历史目标归档见 [#1](https://github.com/auguspp/Decision-OS-2/issues/1) 与 [评审采纳记录](docs/reviews/v1.2-finalization.md)，不要重开已完成归档。

## 安装与工程入口

使用 Python 3.12 或更高版本，在仓库根目录执行：

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install .
decision-os --version
decision-os info --json
```

`decision-os` 与 `python -m decision_kernel` 的默认帮助、version/info提供安装信息；显式 `store` 子命令支持[本地合成版本保管](docs/architecture/versioned-store.md)，本地合成模式需要PATH中可用的Git；远端使用独立的GitHub绑定与运行凭据，不代表真实读写、研究或定时任务已启用。CLI不会启动采集、Quick/Full、通知或部署。当前运行包没有第三方Python依赖；工程依赖锁与测试方式见 [CI实现说明](docs/engineering/ci-implementation.md)。不保证旧 `decision-kernel` 命令或业务API兼容。

## 基础与边界

新会话使用同一主线规则、原生阶段标签和 Issue/PR 模板；记录认领分支、实际 head、验证和未知效果。模板与文档不是服务端权限或代码验收，GitHub/宿主未读取规则时也不会自动遵守。

首个代码责任是完整 CI 与可安装骨架；共同保管的必要合同可用后，直接 Full/Quick 与市场雷达、产业商品、披露资料及日常交付按依赖并行。首发可用不等于全部雷达或500+覆盖完成，未完成责任须明确接续。文档/规则已落地、CI已配置、产品已使用分别成立，实际结果读对应任务。

用户于2026-10-20将现仓切回 Public，沿[#164](https://github.com/auguspp/Decision-OS-2/issues/164)直接适配现仓；`main` 仍只提交工程内容与合成/获准样例。按[#6路线A](https://github.com/auguspp/Decision-OS-2/issues/6#issuecomment-6187561836)，同仓独立 `records` 分支保管原范围获准资料与研究，默认[只读绑定](config/store-binding.json)；保留原 repository_id、store_id、anchor 和历史公开建仓标记，运行时另核真实可见性。公开许可采用#164记录的用户确认；可见性变化和这项公开确认都不增加来源执行预算、新研究委托或人工 Decision 权限；同仓 records 与历史产物不会因代码检查重新隐藏，数据分支不是隐私或角色隔离。旧 Kernel/OS 业务状态和格式不兼容，技术/成功验收/失败反例按适用范围复用。
