# Relay 原始手册、实现对照与 Secret 配置

## 用户提供的原文

[客户调用手册 v0.5.61 原文](vendor/tushare-relay-v0.5.61.md)来自用户于2026-10-15上传的 `promax(1).txt`，经用户要求存入本仓。这里只将扩展名改为Markdown以便浏览，UTF-8原始字节不变：44358字节，SHA256 `402bbfdfa77e6035b5cd41bc9a5d66b6ed975db2f2a25748ab37b797c12c827f`，Git blob `3c89b6ed306f5232d417d1048597338130c64779`。

原文的章节、示例、257/298数字和表格排版原样保留，不将缺项或口径差异悄悄改写。它是客户说明的留存版本，不是实时capabilities响应、已执行取数、许可证或仓库工作指令；示例组合写入/删除不因归档被执行。此页是OS2接线说明，不是供应商手册的一部分。

## 当前实现与手册的关系

既有实现复用了旧Kernel登记和客户端，并参考同名接口字段合同；不能声称此前已经逐字使用了本次新上传的附件。现行 `relay_http.py` 和[八类财务批量入口](relay-financial-batch.md)与附件对照如下。

| 项目 | 手册 | OS2实际实现 |
|---|---|---|
| 服务与认证 | HTTPS `pcd.mobcvb.cn/tushare/pro/{api}`；`X-API-Key` | 相同主机与Header，密钥不放URL |
| 客户端变量名 | 示例 `TUSHARE_RELAY_KEY` | 延续旧Kernel的 `TUSHARE_PROXY_API_KEY`；只是变量名不同，不是另一把Key |
| 财务筛选与分页 | `ts_code`、`period`、`fields`、`limit`、`offset` | 八类财务入口使用这些参数，另按用途校验证券、报告期与核心字段 |
| 返回与失败 | HTTP及业务状态、`data.fields/items`、`count` | 保留结构、来源回执与错误；空结果不代签无披露，结构化表不是PDF |
| 完整目录 | `/tushare/capabilities`，也需Key | 当前手动财务入口不自动扫描全部目录，不代表手册列出的全部接口已接入 |
| 限流与重试 | 实际响应头为准，429遵循Retry-After，建议退避 | 现役客户端无自动重试；认证/限流拒绝停止剩余流量。完整退避策略并未实现，不能称逐项照搬手册 |

用户允许批量使用、不按费用或公司数限制；这不取消真实服务拒绝、超时、分页和凭据保密。尚未现场验证的接口/参数兼容性，以后续真实结果为准，不以本页或CI绿灯替代。

## 你需要配置的 Secret

在 **Decision-OS-2** 仓库打开 **Settings → Secrets and variables → Actions → Secrets → New repository secret**：

- **Name：`TUSHARE_PROXY_API_KEY`**。
- **Secret：你现有的 Tushare Relay 客户 Key**。不要填GitHub token或其他服务Key，不要将真实值发到聊天、Issue、代码或运行输入。

只需这个名称对应的一把Key，不必同时创建 `TUSHARE_RELAY_KEY`。旧仓库中同名的repository secret不因代码迁入自动成为本仓的secret；组织级共享secret须另有本仓访问范围。配置步骤及workflow显式注入规则见[GitHub官方说明](https://docs.github.com/zh/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets)。正常代码/CI访问权限不会自动给程序提供Relay客户Key；当前连接器未独立读取Secret值，不能据此断言它已配置或缺失。

工作流仅在采集步骤显式映射 `TUSHARE_PROXY_API_KEY: ${{ secrets.TUSHARE_PROXY_API_KEY }}`。源Key不提供给CI、安装或计划步骤；GitHub内置token仅有本仓contents:read，且checkout不持久保存凭据。此步骤隔离不等于仓库管理员或其他有权限修改workflow者无法访问仓库Secret。

## 仅手动运行，不自动上线

[工作流 relay-source-runtime.yml](../../.github/workflows/relay-source-runtime.yml)在Actions中名为 **decision-os-relay-source-runtime**。只支持本仓main的 `workflow_dispatch`，不随push/PR触发，也没有定时任务。

1. 选择main，填 `code_sha` 为已经核对的当前完整main提交号；与此次运行版本不符就拒绝。`codes` 和 `periods` 可用空格/逗号分隔，分别是明确证券及季末报告期。输入不得含Key或私人内容。
2. `operation=plan`（默认）只调用现有批量CLI生成计划，不读取源Key或访问Relay。`apis` 为空时选择已实现的八个财务API；它不是手册全部接口。
3. 明确要取数时选择 `operation=capture`，并确认 `public_rights_confirmed`：你有权查询且允许将这些资料保存在本公开仓的运行产物中。未确认则在任何源请求前拒绝；勾选是运行人的声明，不是来源自动授予版权。
4. `page_size/max_pages` 交给已有接口合同检查，不是商业用量额度。同一来源工作流串行排队，不取消已运行的采集。已有CLI保留部分结果及失败；失败不以绿色成功掩盖。
5. 运行产物 `dos2-relay-source-<run_id>-<attempt>`保留7天，含明确计划、运行说明及已取得的完整或部分capture。它不是私有资料库或长期备份；不打印原始财务正文到日志。运行被强制中止或上传失败仍可能没有完整产物，按实际原生结果处理。

本入口**不写main/records，不调用保存子命令，不执行Quick/Full、通知、PDF下载或投资动作**。产物后续如需进入内容池，另按原capture/意图/公开权利通过已有VersionedStore入口保存；不得重新取数冒充原保存恢复。单纯添加Secret不触发任何采集；工程交付也不代表密钥已设置、实际HTTP成功或资料已落库。

## 验证与维护

专属测试保护手动main范围、输入不进入shell、公开保管声明、非零失败、无记录写权限及原手册字节不变；模拟进程测试不证明真实Relay。原手册升级时保存新版本并按实际来源更新测试，不修改旧原件。此手动入口退役时同步退出wrapper、workflow和专属测试，保留仍有效的来源手册、现役批量CLI及历史引用。
