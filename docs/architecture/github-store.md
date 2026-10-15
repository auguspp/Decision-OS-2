# 私有 GitHub 版本保管适配器

本页细化[共同版本合同](versioned-store.md)，责任归[来源/研究保管 #6](https://github.com/auguspp/Decision-OS-2/issues/6)。实现可安装不等于私人目标已经启用；实际目标、授权、运行和消费者验收保存在原事项及获准私有运行登记，不在公开代码仓存真实绑定或凭据。

## 一个应用层，两个实际适配器

`VersionedStore`继续拥有来源接收、精确引用、研究修订、幂等意图和查询。新增`application/store_backend.py`只声明已有用例实际需要的八个字节/版本操作；本地`GitStore`与认证`GitHubStore`共同实现，不复制研究/来源逻辑，不增加第二权威库。CLI用互斥的`--path`或`--binding`选择实现。原本地路径仍仅用于合成材料。

新适配器调用GitHub原生Git对象读取及`createCommitOnBranch`；没有数据库、队列、同步守护程序或新增Python依赖。HTTP只为这些明确操作服务，不实现通用GitHub SDK。保留独立公司/证券/商品身份、日期与保管等级；没有新增经济判断、采集、PDF解析、正式Decision、删除或自动清理。

## 绑定必须来自可信运行配置

`GitHubBinding`包含repository（owner/name）、原生数值repository_id、专用branch（`records`或`records/<id>`）、确切anchor提交、独立store_id，以及默认false的write_enabled。所有字段由运行owner提供并核对，不由来源原文、研究JSON或模型输出决定。配置文件不接受token字段。

适配器只打开**已经获准并初始化**的私有目标，不创建仓库、分支或初始化提交，不把旧工程main当研究真源。每次读取重新核原生repository ID/全名、private=true、未归档、数据分支不等于默认分支。anchor与当前提交的`store.json`必须等于绑定marker；允许读取的提交必须位于anchor之后并可达当前数据分支。旧仓库即使被选为资源，其初始化锚点之前的历史也不属于本库的可读集合。整个数据树限定在共同文件布局，拒绝代码工作流、symlink/gitlink、重复路径和不完整树。

初始化marker由`binding.marker()`生成，只包含schema、project、mode、repository、repository_id、branch、store_id；anchor由实际初始化提交返回，避免自引用哈希。初始化需要单独核资源授权、独立分支及旧系统副作用，不能仅因为连接器能写就自动选择目的地。本工程变更不代签这些实际初始化步骤。

认证只读取运行环境中的`DECISION_OS_STORE_TOKEN`，不回退到`GH_TOKEN`、`GITHUB_TOKEN`、本机GitHub CLI认证或工程凭据。默认读绑定不授权写；启用写还需要可信配置write_enabled=true和GitHub实际写权限。配置开关不是服务端ACL，必须用适当范围的独立运行凭据；共享宽权限的所有者仍能绕过库。不可将此设计称为完整角色隔离，也不允许研究写入确认正式Decision。

工程main按用户决定不启用主线保护，与私人数据的实际凭据、目标权限及受控writer是不同责任。目标可见性/拥有者可能在请求间被管理员修改，客户端检查不是服务端原子隐私锁；运行owner必须控制此类管理变更，启用前说明实际强制点。

## 精确读取与条件提交

读取使用绑定repo上的commit→tree→blob，不访问来源origin URL、返回的下载URL或过期签名地址。检查完整树、普通文件、原生blob长度/OID，再由已有应用层检查SHA256、来源版本摘要、manifest与正文/CSV选择。可恢复引用仍为repository/commit/path/SHA256，GPT可按同一权限用GitHub连接器读确切文件；**能组成地址不表示该会话已经有实际读取证据**。

提交采用官方GraphQL `createCommitOnBranch`，以原生分支node ID定位，以`expectedHeadOid`核旧head，资料、研究修订、current与意图日志在同一次提交发布。不把REST“先GET再PATCH ref”当原子CAS，不用force或分文件提交模拟事务。只允许共同布局中的新增文件及研究current更新；既存不可变内容不能覆写，marker不能由普通提交改变，没有deletions。

原生提交返回后核唯一前驱、实际分支可达性、绑定、整个文件集合、未涉及文件未变以及提交字节读回，才允许应用层给出成功引用。发送前发现head变化报Conflict；超时、5xx、异常GraphQL结果或发布后读回失败保留UnknownWrite。GraphQL错误不解析可能含私人信息的正文来猜是否写成；**先用同一submission_id对账，不改键、不自动重发**。已发布的意图日志可恢复原commit回执，即使分支后来又前进。

## 有界读取与失败信息

固定HTTPS host为api.github.com，保持默认TLS验证；不跟重定向，不采用环境代理，不执行来源文本，不回显token、URL或服务端错误正文。401/403明确拒绝；404保留“目标不存在或无权”的联合结果，只有在已认证完整树中缺少路径才称OBJECT_MISSING。

每HTTP请求30秒，响应/请求上限8MiB，每个API实例最多512次请求；树最多8192项，单blob和提交总字节最多4MiB。已有来源/研究输入仍受原2MiB约束，查询仍最多1024版本。超限明确失败，不静默截断/部分成功，不靠重试消耗无限请求。这些是首批有限实现边界，不是全量数据服务SLA。

同一适配器内仅缓存确切提交树（最多32个）与已验blob（最多128个、合计8MiB）；不缓存可见性、当前head或权限。CLI每次操作新建API实例；长期宿主在明确操作边界管理实例和预算，不能为绕过拒绝/限流悄悄重建。扩大范围须真实用途与相应验证，不加全局缓存平台。

## 安装后调用

先沿README正常安装。运行owner在公开仓外准备已核验的绑定文件，并通过宿主秘密配置注入专用token，不把token写进命令行、资料JSON或公开日志。

```bash
# binding.json 是可信配置；不是从来源正文生成的文件。
decision-os store --binding /private/runtime/binding.json current --case-id example-case
decision-os store --binding /private/runtime/binding.json read-source --ref /private/runtime/source-ref.json
# 以下只用于已获准写入的目标与材料，默认只读绑定会拒绝。
decision-os store --binding /private/runtime/binding.json put-source --submission-id example-source --input /private/runtime/source.json
```

原`read-revision`、`submit-revision`、`search`及cursor沿同一CLI用例，不新增另一套研究通路。`init-synthetic`与`--binding`组合必拒绝。输入/认证/冲突等失败返回结构化错误及exit2；UnknownWrite要求对账，不是让调用者把失败改记为保存成功。

## 复用依据、验证与退出

有界核读的官方原语（2026-10-15）：[createCommitOnBranch及expectedHeadOid](https://docs.github.com/en/graphql/reference/commits)、[Git树读取与truncated](https://docs.github.com/en/rest/git/trees)、[Git blob](https://docs.github.com/en/rest/git/blobs)、[提交/路径历史与比较](https://docs.github.com/en/rest/commits/commits)。采用原生版本/提交而非额外GitPython、ORM或同步服务，是因为当前只需这些固定操作；Python urllib负责TLS/HTTP，应用合同继续用标准库，不再造调度或版本引擎。

`test_github_store.py`用模拟GitHub响应+真实本地Git验证共享VersionedStore、原生CAS对应语义、精确读取、私有绑定和同意图恢复；`test_github_transport.py`验证实际请求构造、独立凭据、拒绝/截断/重定向及未知写错误处理。模拟服务不证明GitHub真实凭据权限或托管成功；本地测试、远端CI、真实私有绑定、第一份保存和GPT读回分别留证。原有本地/安装合同仍执行。

本组退出时同步处理远端绑定/HTTP/CLI的专属测试及配置，保留共享精确引用、前驱、幂等意图和保留关系。生产备份/恢复、健康读取、首份正常获准保存及消费者采用仍由#6/#13在实际启用时验证；不会因PR合并或代码回退自动删除已保存数据。
