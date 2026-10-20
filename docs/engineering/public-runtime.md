# 现仓 Public 运行适配

责任 [#164](https://github.com/auguspp/Decision-OS-2/issues/164)。用户2026-10-20选择直接公开原仓，再改代码；这取代原“另建干净代码仓”施工路线。这里不新建仓库、迁移 records、重写历史、修改来源许可或引入第二运行平台。

## 日常入口

仍使用原 `daily-market.yml`，手动选择 `status` 并填写当前 main 的确切代码 SHA。

| 原生仓库可见性及请求 | 本组实际行为 | 不能据此声称 |
|---|---|---|
| Public / status | `metadata-only`，成功返回仓库/运行元数据及限制；不读 records、不取源、不生成研究阅读页 | 行情可读、新鲜、研究已采用或数据保管安全 |
| Public / update、recover-late、schedule | `blocked / PUBLIC_DAILY_RUNTIME_NOT_AUTHORIZED`，exit 2；不注入来源 Secret、不进入原业务 job | 无新交易日、完成零变化或更新成功 |
| Private / 原有请求 | `private-runtime-pending`；路由至原 status/daily 作业，后者继续核实际代码资格、来源许可、预算、CAS、只读恢复及最终输出权限 | 预检本身完成了任何资料读写或验收 |

预检属于原工作流、共用原串行组，只有 `contents:read` 与 `actions:read`。它复用 GitHubAPI，先核当前仓库身份和真实 private 布尔值，核本次 run 的仓库、代码、分支、事件、workflow、attempt，再重新核可见性；只执行三个原生元数据 GET。旧绑定、SourceRef、股池、日历、调度时刻及来源额度不改变。

**不依赖 `github.event.repository.private` 路由。** schedule 载荷可能没有 repository 对象；伪造、缺失或陈旧的载荷私有标记也不能替代原生 API。失败或可见性变化时不输出可用路由，依赖作业不能在 `always()` 下获得业务凭据。通过预检不是原子隐私锁，后续原宿主/reader/writer/发布步骤的实时复核仍保留。

## 已有公开能力与仍受限能力

工程 `ci.yml`、安装和合成测试不改，不增加业务来源密钥。原研究/资料宿主中已有的合格公开手动入口继续按其原合同、内容选择及独立公开权利确认运行；本组不统一停用它们，也不替它们填写确认。

自动私有日更会读取组合研究/Brief及供应商来源，并可能生成完整 HTML/JSON。公开仓不等于这些资料已可再分发，因此不能通过删除 check_private、将确认常量改成 true、伪报事件为 private 或只隐藏日志来恢复该模式。本组没有实现公开整包阅读或公开供应商日更；对应来源许可和受控保管需在真实适用责任组落实，不因 CI 通过代签。

## 历史与恢复边界

records 分支、历史提交、旧 Issue/PR、日志与附件不会因本次代码改造重新隐藏。没有执行历史删除、force push、数据迁移、凭据轮换或可见性设置；也没有认证旧资料都具备公开权利。不要将此预检称为历史隐私修复或数据隔离。

原 public 建仓 marker、anchor 和引用保留；历史 private 成功只证明当时版本与目标。回退工程不能撤销已发生的公开、采集或写入。未来如另行授权迁移或处置历史，须保留真实旧引用、单 writer 与独立恢复，不默认启用旧拆仓路线。

## 维护与证据

`daily_visibility_cli` 与其专属回归保护元数据身份、无仓库载荷的 schedule、真假值、前后可见性变化、错误不泄露、Public 拒绝以及 Secret 前路由；退出该能力时同组退出预检 job、CLI、测试和本文。共享 CI、Store、最终发布门与历史引用继续保留。

本组工程测试不签全系统数据安全、公开来源权利、当天行情、自然连续运行或用户接受。原生 PR/main 检查与实际运行证据在 #164 记录。

参考：[GITHUB_TOKEN 的仓库权限范围](https://docs.github.com/en/actions/concepts/security/github_token)、[Secrets 的显式注入与脱敏限制](https://docs.github.com/en/actions/concepts/security/secrets)。令牌掩码不是资料公开许可，也不是私有数据存放边界。
