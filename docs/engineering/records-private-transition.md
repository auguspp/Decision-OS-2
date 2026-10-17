# 同仓 public → private 可见性切换预案

责任：#10 的 A 股采集接续，复用 #6 的统一 `records` Store。此文件是**准备方案**，不是当前仓库已经设为私有或数据已经取得的证明。原 #6 路线 A 在真实仓库仍公开时继续适用；用户决定未来可将整个 `auguspp/Decision-OS-2` 改为 Private，不另建代码或数据仓。

## 为什么不能把 store.json 里的 mode 直接改成 private

`records` 历史锚点及全部旧提交的 `store.json` 记录了**建仓时**的 `mode=public`，它是历史版本的不可变身份的一部分；改 marker 或重建分支会破坏已发布的 SourceRef 和老研究修订。现在可信 `config/store-binding.json` 保持 `visibility=public` 以精确匹配这些原历史字节，同时显式 `private_upgrade_allowed=true`，表示 **只允许这个已绑定仓库向 private 单向升级**。

`GitHubStore` 对每次真实操作都以认证的 GitHub API 校验同一 repository_id、full_name、default_branch、records ref、anchor 祖先、原始 marker 与 live private 状态；未经单向开关同意的公开→私有变化仍按原规则拒绝。新的 `live_visibility()` 必须来自真实 API，不采信来源、GitHub workflow 用户输入或旧缓存。对同一 reader/writer 已见到 private 又变回 public 的会话，明确拒绝并停止。**这不能逆转仓库一度公开带来的泄露，也不能强制 GitHub 禁止将来重新公开**；发生变更时须先人工停写、排查历史/分支/外部 fork 并重新授予相应权限，不能盲目打开公共写。

## 分阶段切换与核对

1. **当前仍 public：** 本 PR 仅安装双状态兼容及测试，不改仓库可见性、records head、binding anchor 或原件；运行 `status` 仍应得到真实数据状态，未入库时为 `ASHARE_COLLECTION_NOT_RETAINED`；无 `public_rights_confirmed` 的 sync 仍明确拒绝，来源零 HTTP。
2. **切换前：** 核 repo 的实际 `main` 全量 CI、`records` 精确 head、已有 SourceRef 的独立读回与受控完整 bundle 备份。确认 GitHub fork、协作者、Pages、Actions / Artifacts、第三方安装和后续代码分发的变化；公开过的文件和 fork 不因修改可见性自动消失。必要时用户先保存离线恢复包；这不是自动生产备份。
3. **用户明确执行 GitHub 设置：** 在 GitHub 仓库 Settings → General → Danger Zone 修改整个仓的 visibility 为 Private。当前连接器没有已验证的仓库可见性变更写入动作，本工程 PR **没有替用户执行此设置**。
4. **切换之后：** 使用权限仍有效的 GitHub connector 与 Actions 令牌核同一 repo ID/branch/anchor，重新运行只读 `status`、独立 SourceRef 和原生完整 backup/restore（含历史 public marker）及真实已存研究重开；token 权限、原生 private clone 和 CI 环境须重新真实验收。不通过时保持不采集并保留真实失败原件。当前 BSE、沪深真实覆盖率/时效仍按原 #10 未完成。
5. **私有后的入库：** 原来源访问确认与明确的合法个人保管范围不可省；`public_rights_confirmed` 可保持 false，工作流按事件路由，宿主再核 **实时 GitHub private** 后才允许源调用。额外商业用途/分享许可不因仓库私有自动取得。私有化后持续使用 private；若要改回 public，必须先审核全量 Git 历史及 fork/Artifacts 并明确拒绝不获公开权利的材料。

## 技术及维护边界

本组只增加原 `GitHubBinding` 的布尔单向兼容策略；不换原 marker schema、SourceRef 或数据布局。市场原生读用现有子进程范围内的 Basic extraheader（不通过命令行、Git config 文件、结果回执传送原 token）；备份在 private 经认证后复用该原安全 fetch，再做原独立 Git bundle 和逐引用审计。失效/拒绝时不给无凭据的 fallback 换仓或换源；保留 CAS、512 API 和原大小限额。退役这次私有化兼容时同时删除新策略开关、差异化工作流授权、私有备份复用及其新增测试，不能移除仍有效的 #6/#12 原保护。
