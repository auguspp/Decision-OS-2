# 已授权的现仓 Public 运行

责任：#164。用户于2026-10-20先直接公开现仓，随后明确“视同已经许可，可以按全面公开改造”；[原生授权回执](https://github.com/auguspp/Decision-OS-2/issues/164#issuecomment-6282012674)是本次适配依据。PR171的metadata-only拒绝模式是缺少公开确认时的前序行为，不再作为本部署正常模式。

## 原架构与授权

仍使用 auguspp/Decision-OS-2，main 放工程，records 保管来源与研究。原repository_id/store_id/anchor/marker、旧精确引用与完整历史不改，无拆仓、迁移或清理。config/public-runtime.json 是受审部署配置，不是来源JSON、业务输入或凭据；严格绑定同一既有数据目标和具名范围。enabled=false撤销部署授权，缺失配置不自动授权，格式错误、目标错误、未知范围或符号链接拒绝。

这记录用户的公开确认，不是代码对版权/法律的独立鉴定。密钥、账号凭据、私人识别信息和正式人工Decision排除；已有401/403/429、source_hold、耗尽试验和单次维修终态仍遵守原停止条件。授权公开不等于授权新研究、扩股票池、交易、通知、增加来源预算或补造数据。

## 现役入口

| 入口 | 已授权Public行为 |
| --- | --- |
| daily-market status | 原生预检之后正常读取已存行情、发现状态、研究与最新已存Brief，输出完整HTML/JSON/Summary；不是仅元数据诊断。 |
| daily-market update / recover-late / schedule | 原6股、工作日18:35、每周期2日历+12价格与迟到24小时界限不变；先无来源凭据预检，再原采集、保管、独立读回、发现状态与阅读。 |
| ashare-data ftshare-status / ftshare-sync | 原可信有限配置及单股整段路线，明确access_and_custody_confirmed仍是本次采集/保管操作授权；固定部署公开确认不冒充新请求。历史public_rights_confirmed传输位保持原默认false，由部署配置提供公开授权；非法组合明确拒绝，不全skipped。 |
| market-custody / discovery | 正常原件保管和build/save/read接同一发布授权；原custody或rights操作确认、确切提交、原始分母/失败和CAS保持。 |
| announcement-discovery | 原目录元数据计划、日期窗口、幂等及来源暂停继续；采集前与附件发布前核实际目标和授权。 |
| research-runtime / Reading Room | 已公开的正常材料、研究、议程、Brief、Watch、经营准备/兑现可以读取和保管；仅补已知权限字段，原输入与有效载荷摘要分别保留。正式Decision仍不启用。 |
| store-runtime backup / health | 复用已支持Public的现役Git bundle、配套阅读代码、固定expected_head与空库恢复；不新增备份平台、调度或删除规则。 |

历史ftshare-check/continue/kernel、calendar one-shot、adopt及原repair不因这项授权重开；曾经成功的来源和研究不重跑。相依实际新日、源可用、读取新鲜度和经济质量仍各自验收。

## 原生验证与密钥边界

公开CI继续使用合成输入和原完整测试策略，无供应商Secret。日更元数据预检核原仓库ID/全名、main、真实run/attempt/代码/事件及前后可见性，只返回授权路由；Public不能被填成Private。缺授权时status回原诊断、更新明确blocked；原生信息错误不会放行来源。

正常业务作业仍核实际main/完整CI与原运行身份；只在原获准源步骤注入相应Secret，只读消费者没有源密钥。公开PR/fork不获得业务凭据。原后端可见性反转保护、读取作用域、来源/结果上限、唯一writer/CAS/未知写对账保留，错误日志不输出凭据或任意上游正文。

## 接续与退出

实施结果读#164和关联PR/原生运行，本文件不是验收回执。合并与CI不代表发生过正常status、产生新日行情或恢复了长期连续运行。若实际工作流派发能力缺失，保留具体运行待验，不靠重跑耗尽旧run、换触发器或伪造时钟代签。

停用时关闭固定部署授权，按实际在途动作对账；不删除已保管来源、不覆盖旧失败或撤销历史公开副本。代码回退不收回已经公开的Git/Issue/PR/日志/附件。本授权模块退役时同步处理专属预检、入口接线与测试，共享Store/CI及原引用继续维护。
