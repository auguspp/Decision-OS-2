# GitHub 版本保管：显式可见性与独立数据分支

本页细化[共同版本合同](versioned-store.md)，责任归[来源/研究保管 #6](https://github.com/auguspp/Decision-OS-2/issues/6)。适配器可安装、目标初始化、实际保存、GPT读取和运行保障分别留证，不因其中一项成立而代签其他项。

<a id="topology"></a>
## 首版路线 A，保留未来切换 B 的边界

按2026-10-15用户[正式裁决](https://github.com/auguspp/Decision-OS-2/issues/6#issuecomment-6187561836)，首版采用现有公开仓库 `auguspp/Decision-OS-2`：main保管代码、CI、方法和工程文档；独立非默认 `records` 分支保管有公开权利的来源版本和原创研究。数据分支当前树不包含工程代码或workflow；普通研究保存不向main提PR，也不触发主线代码CI。独立分支不是隐私隔离，不启用主线保护或替代ruleset。

本仓[只读绑定](../../config/store-binding.json)给出原生仓库ID、records、实际初始化anchor和store_id，不含凭据。anchor对应的store.json已经初始化并精确读回，初始树只有marker；初始化记录在#6。这证明目标位置成立，不证明运行token、GraphQL条件写或研究任务已经启用。main与数据head各自前进，不能把数据commit写成软件版本。

未来按需迁往独立数据仓库B时，代码仓可以继续公开；消费者仍使用SourceRef/研究修订合同，不把本仓ID写进实体本体ID。仓库、分支和可见性由运行配置选择。现有BlobRef里的repository/commit/path是确切的读取位置，不是假定永不移动的领域身份。迁移时必须保留旧位置可读或增加有界、显式的旧归档解析，并只保留一个当前写入真源；当前适配器仍拒绝foreign repository，单改配置不代表跨仓迁移已经完成。本版不建立B、双写、自动迁移或通用解析平台。

公开许可只覆盖有公开权利的成果与材料。密钥、账号、个人识别信息、第三方隐私以及没有再分发许可的付费研报/PDF/原文不得公开；旧私有资料不自动迁出。公开内容被复制后不能保证收回。数据可见性许可不自动启动Quick、Full、来源定时、通知、部署或正式Decision。

## 一个应用层，两个实际适配器

VersionedStore继续拥有来源接收、精确引用、研究修订、幂等意图和查询。application/store_backend.py只声明现有用例使用的八个字节/版本操作；本地GitStore与认证GitHubStore共同实现，不复制研究/来源逻辑，不新增第二权威库。CLI用互斥的--path或--binding选择实现；本地路径仍仅用于合成材料。

适配器调用原生Git对象及createCommitOnBranch，不增加数据库、队列、同步守护程序或Python依赖。独立公司/证券/商品身份、日期与provided/excerpt/external保管等级不变；不新增经济判断、采集、PDF解析、Decision确认、删除或自动清理。

## 可信配置、可见性与凭据

GitHubBinding包含repository（owner/name）、原生数值repository_id、专用branch（records或records/<id>）、确切anchor、独立store_id、默认false的write_enabled，以及默认private的visibility。public必须显式选择，不能根据仓库返回值自动跟随；未知值、真假值或大小写变体均拒绝。配置不接受token字段，也不由来源正文或研究JSON决定写入授权。

每次读取重新核仓库ID/全名、实际private布尔值与配置相符、未归档、数据分支不等于默认分支；提交发送前再次核查。anchor及当前提交的store.json必须等于binding.marker()，marker的mode与visibility一致。private默认下的既有marker字节不变；把配置改为public不能绕过原private marker。管理员在请求间改可见性仍可能产生竞态，客户端检查不是服务端原子隐私锁。

适配器只打开已获准并初始化的目标，不自动创建仓库、分支、anchor，也不把旧main当数据真源。读取commit须在anchor之后、可达当前数据分支；初始化之前的工程历史不属于本库读取集合。完整数据树只接受共同布局的普通文件，拒绝workflow、代码、symlink/gitlink、重复路径或不完整树。普通写入不能改marker或旧不可变记录。

认证只取DECISION_OS_STORE_TOKEN，不回退到工程GH_TOKEN、GITHUB_TOKEN、本机gh认证或Git凭据。public也不把公开可读解释为任何人可写。开启写入需要可信配置write_enabled=true、相应委托和实际GitHub权限；仓内分发的绑定保持只读，运行writer使用经过核验的独立配置及适当范围凭据。配置开关不是服务端ACL；共享宽凭据持有者可以绕过库，不能声称完整角色隔离。工程main不保护，不取消这些数据写入边界。

## 精确读取与条件提交

读取走绑定repo的commit→tree→blob，不跟随origin、download_url或签名地址。检查完整树、普通文件、blob长度/OID；应用层继续检查SHA256、manifest与版本摘要、正文行或CSV逻辑行列。GPT可用连接器按相同权限读取repository/commit/path；能组成地址不等于该宿主已取得读取证据。拒绝、丢失、损坏、不完整不能静默换最新版本。

写入使用官方createCommitOnBranch、原生分支node ID及expectedHeadOid，把资料/研究修订/current/意图日志一次条件发布。不将REST先GET再PATCH ref冒充原生CAS，不用force、分文件提交或单纯计数检查模拟事务。仅允许共同布局内新增文件和research current更新，没有deletions。

返回后核唯一前驱、分支可达性、绑定、完整文件集合、未涉及文件未变以及真实字节读回，才给成功引用。发送前head变化报Conflict；超时、5xx、异常GraphQL结果、提交后读回失败保留UnknownWrite。错误正文可能含敏感信息，不靠解析其文字猜成功；先用同一submission_id对账，不改键或自动重发。已发布日志可恢复原commit，即使分支后来前进。

连接器的初始化/文件读取、独立运行token调用本适配器、工程CI是不同通路；一个通路的成功不能代签另一个。没有可调用的GraphQL写入口或独立运行凭据时，明确保留相应验收，不把普通连接器提交包装成适配器原生条件写已验。

## 有界读取与失败处理

HTTP固定HTTPS host api.github.com、正常TLS校验，不跟重定向、不用环境代理、不执行来源文本、不回显token/URL/服务端错误正文。401/403明确拒绝；404保留不存在或无权的联合语义；只有完整已认证树中缺少路径才称OBJECT_MISSING。无自动重试。

每请求30秒，响应/请求上限8MiB；每API实例最多512次请求；树最多8192项，单blob及提交总字节最多4MiB。来源/研究输入仍受2MiB约束，查询最多1024版本。超限明确失败，不截断为成功，不为绕过限制重建实例。

每个GitHubStore实例缓存确切提交树（32个）、已验blob（128个、合计8MiB），以及最多128个已完整验证成功的祖先关系 `(baseSHA, headSHA)`。两个确切提交间的Git拓扑关系不会因分支移动而改变；不缓存否定、错误或未验证的compare响应，容量满后继续原请求。每次读取仍重新核仓库元数据、实时分支ref/node和marker，再以实际取得的head验证引用可达性；新head使用不同SHA对，旧证明不能代替它。可见性、权限和当前head不缓存。

该有限复用服务于研究交付保存后新reader核验同一清单时的重复compare开销，不增加读取范围或第二存储入口。fresh-reader仍新建独立GitHubStore及全部缓存，复用同一API的累计512次预算；不能为绕过限额重建API。CLI每个显式操作新建API实例，长期宿主在明确操作边界管理预算。Git对象/祖先关系缓存随实例退出，不跨操作持久化。

## 安装后调用与GPT读取

沿README安装后，运行owner通过宿主秘密配置提供专用token，不能把token放在命令行、输入JSON、公开仓或CI。以下仓内配置明确只读：

```bash
decision-os store --binding config/store-binding.json search --query 铜价
decision-os store --binding config/store-binding.json current --case-id example-case
# 读取先前返回的确切引用；ref文件属于实际获准运行记录。
decision-os store --binding config/store-binding.json read-source --ref /runtime/source-ref.json
```

获准writer在受信运行位置使用write_enabled=true的配置，再调用同一put-source或submit-revision，不修改仓内默认配置给所有消费者开写。read-revision、search、cursor和current复用原用例；init-synthetic与--binding组合拒绝。错误返回exit2及结构化代码；UnknownWrite要求对账，不是成功。

交互GPT先读绑定并核records当前head与store.json，再按保存回执的确切commit/path读取manifest和其representation；显示未读完及未认证原件的限制。普通会话读取可单独成立，不自动证明定时Quick或其他宿主具有相同权限、token或写入能力。

## 验证、复用与退出

沿PR #19核读的官方原语：[createCommitOnBranch](https://docs.github.com/en/graphql/reference/commits)、[Git树及truncated](https://docs.github.com/en/rest/git/trees)、[blob](https://docs.github.com/en/rest/git/blobs)、[提交历史/比较](https://docs.github.com/en/rest/commits/commits)。祖先证明复用依据[Git提交对象和提交图](https://git-scm.com/docs/gitglossary)的确切对象关系，仍使用现役compare验证，不替换版本、传输或事务机制。

test_github_store.py的真实Git/模拟GitHub与test_github_transport.py继续保护原合同，包括暖祖先缓存后的live权限/分支变化、错误不缓存、容量满回退及同API硬预算；test_github_visibility.py复用相同模型，保护private默认、public显式选择、marker不匹配、元数据漂移、只读默认、发送前复核、公开模式幂等和并发CAS。test_research_delivery_integration.py以真实512限制的传输和原生Git保护多commit原件交付后的独立读回。模拟响应不证明真实token或托管写入成功；远端完整CI与真实数据验收分别记录。

退役时同步处理适配器/绑定/CLI/HTTP的专属配置与测试，保留共享精确引用、前驱、意图和保留语义。生产备份/恢复/健康、第一份正常成果保存和真实消费者采用由#6/#13按启用范围验收；代码回退不自动删除数据，不把备份job成功当恢复成功。
