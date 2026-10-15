# 版本保管：来源、研究修订与精确读取

范围与前提见[仓库接线合同](repository-contracts.md)；本实现属于[共同保管 #6](https://github.com/auguspp/Decision-OS-2/issues/6)。实际提交、CI、私人目标及产品验收状态保存在原事项，不在本文维护进度表。

## 当前可调用范围与真实消费者

`VersionedStore` 提供来源接收、精确读取、研究修订、当前修订读取、来源查询/分页。`decision-os store` 是安装后的真实消费者；它通过显式配置选择本地 `GitStore` 或认证 `GitHubStore`，共用同一应用层。远端绑定、权限、限额及调用见[私有GitHub适配器](github-store.md)。后续研究、雷达和交付复用同一版本/引用语义，不分别建立资料库。

**本地适配器只支持专用 bare Git 中的合成材料；远端适配器要求独立的已核私有绑定。** `init-synthetic` 不复用已有目录、不联系远端、不读取工程凭据、不绑定私人运行目标。`repository=synthetic-git:<id>` 是本地逻辑身份，不是可供远端GPT读取的GitHub地址。合成模式是使用边界，不是自动识别敏感材料的分类器；不得拿真实私人材料冒充合成输入。

本保管层没有采集器、解析PDF、模型执行、定时、部署、通知、清理或正式Decision；远端适配器是显式读写，不是自动同步入口。保存kind=quick/full的合成记录不执行一次Quick/Full研究，也不代表研究质量已接受。真正私有G通路仍需实际获准目标、受控writer、相同权限的确切远端读取与首份正常使用证据。

## 责任与复用决定

| 责任 | 实现/消费者 | 复用与新增边界 |
|---|---|---|
| 输入、版本引用、用途范围 | `domain/records.py` | 标准库dataclass/JSON/日期/摘要；不引回旧ResearchSnapshot或旧研究ID |
| 接收、修订、重复意图、查询 | `application/versioned_store.py` | 沿既定来源先保管、精确引用、修订保留语义，补新文件合同与实际消费者 |
| Git字节、提交与条件更新 | `infrastructure/git_store.py` | 原生Git对象/tree/commit/ref，不自建版本数据库或全局锁服务 |
| 明确CLI接线 | `interfaces/store_cli.py` | 延伸现有CLI；安装测试在同一wheel消费者中扩展，不新增同形安装pilot |

选择原生Git而不是SQLite/ORM，是因为本批消费者需要版本化文件与commit/path读取；数据库会额外引入一套权威状态及导出责任。没有批量全文索引需求，不加入向量库、GitPython、通用插件/DAG或存储管理平台。远端消费者复用GitHub原生条件提交和同一文件合同，不新增第二主库。每次提交的 `submissions/<id>.json` 仅用于同一保存意图恢复，不是第二研究账本。

原范围参考为旧固定版本[研究成果合同](https://github.com/auguspp/decision-kernel/blob/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8/docs/research-outcome-contract-v1.md)的部分成果/来源资格/修订边界；不继承其旧任务、schema、费用或兼容义务。实际底层采用官方[Git update-ref](https://git-scm.com/docs/git-update-ref)、[commit-tree](https://git-scm.com/docs/git-commit-tree)与[Python subprocess](https://docs.python.org/3.12/library/subprocess.html)；2026-10-15核读其条件更新、创建提交和显式进程环境语义。组件按已安装工具调用，不在本仓复制分发其源码；没有为旧仓补造许可证。运行需要Python 3.12+、PATH可用的Git；本批验证范围为Linux/POSIX，不宣称所有平台已验。

## 保存、引用与失败

输入限定为有界UTF-8正文、CSV数据表或有明确限制的外部定位。来源 `custody` 区分provided/excerpt/external；provided只表示确实保管提交者提供的字节，**不认证这些字节就是外部原件**，读取结果的 `original_verified` 保持false。external无保管正文且必须给限制；excerpt也必须注明限制。解析和接收不会请求origin URL。

```text
store.json
source-views/<source-id>/identity.json
source-views/<source-id>/<version-digest>/manifest.json
source-views/<source-id>/<version-digest>/content.txt 或 data.csv
research/cases/<case-id>/revisions/<revision-id>/record.json
research/cases/<case-id>/revisions/<revision-id>/research.md
research/cases/<case-id>/current.json
submissions/<submission-id>.json
```

目录来自安全ASCII ID与版本摘要，不由原始标题或上传路径决定。来源版本摘要包含输入元数据和原文字节；存储时间单独由接收者产生，不混入来源披露/取得/报告期。日期仅为日期时不补午夜，无来源日期保留null。

SourceRef内嵌一个实际可读的manifest BlobRef（repository/commit/path/SHA256）及selection。正文/表格字节摘要、路径、长度由manifest保存并验证。正文按原换行读取或按1起始闭区间选行；数据表按CSV逻辑数据行及精确列名选择，带换行字段仍是一条记录，数字不自动转型，unknown不填0。非本仓引用、不可达commit、缺对象、损坏、越界分别报错，绝不偷偷切到最新版本。

研究修订含正文、原委托/方法定位、来源引用、前驱和未决说明；无来源的部分成果必须有明确未决说明，不强制股价、概率或机器估值。每个修订保留其SourceRef，是现行引用保留依据；当前指针不是历史依据。新修订保持 `human_acceptance=not-confirmed`，不接受输入自行签署人工确认。

同一意图ID+相同内容重读原提交回执，不重复写；同键不同内容或不同操作报Conflict。revision ID不覆盖；提交前驱必须等于该案例的确切当前回执。每次先验证来源可读，再将修订、当前指针、意图记录放进同一Git tree，使用旧head条件发布一个ref。并发竞争只有一个成功，另一方明确冲突，先对账再重新决定，不自动覆盖。

对象创建后、ref发布前失败时没有可见部分提交，可能留不可达Git对象；不自动gc或危险清理。发布响应不确定时报UnknownWrite；使用同一意图对账即可找到已成功提交，不能改键重发。发布成功但读回失败也不得报告完整保存成功，先恢复读取再对账。Git ref原子更新不承诺多介质原子事务、机器断电后的绝对耐久或远端备份恢复。

## 查询、恢复与权限限制

查询采用NFKC/casefold子串匹配标题、来源ID、日期/期间、实体ID/名/别名/属性值及正文/数据值；双字中文、代码与全角字符有明确用例。不会把JSON字段名当内容命中。同名公司与证券等仍用不同显式实体ID，不自动推导经济联系。

默认声明 `all-stored-versions`，不把历史版本混称最新覆盖。每页1—100项，cursor绑定实际Git快照和查询摘要；下一页期间新提交不会改变该页集合。最多处理1024个来源版本，超限显式报错，不静默返回一个完整性未知的子集。这是有限扫描，不是全文索引或性能SLA；扩大规模时由真实用途驱动替换查询实现。

本地目录恢复测试证明完整静态副本保留逻辑身份、原commit和引用可读，不证明生产备份策略已执行。首次启用实际私有位置需验证相应备份/恢复、保留关系与健康读取。没有清理接口，不能擅自删“孤儿”。

输入记录不授予角色权限。当前进程可写本地目录的所有者仍能绕过库去修改Git，故此处不宣称服务端角色隔离。Git子进程不继承GH_TOKEN、模型密钥、Git环境覆盖；不运行shell/钩子/checkout，也不执行下载内容。高影响覆盖/删除/权限管理及正式Decision要等独立可信控制，工程main不设保护不能替代这些责任。

## 可执行的合成示例

先正常安装工程包，并在仓库外准备一个新目录位置及合成JSON文件：

```bash
decision-os store --path /tmp/dos2-example.git init-synthetic
# /tmp/source.json 仅包含下方合成JSON；输出引用保存后才能使用。
decision-os store --path /tmp/dos2-example.git put-source --submission-id example-one --input /tmp/source.json > /tmp/source-ref.json
decision-os store --path /tmp/dos2-example.git read-source --ref /tmp/source-ref.json
decision-os store --path /tmp/dos2-example.git search --query 铜价 --limit 10
```

```json
{"source_id":"example-copper","kind":"content","title":"合成铜价观察","origin":"synthetic:example","text":"仅用于工程测试，不是真实行情。"}
```

`submit-revision --submission-id <id> --input <json>` 接收 `case_id/revision_id/kind/body/mandate/method/sources/predecessor/unresolved`；sources使用实际put-source回执，predecessor使用实际current回执，不能编造SHA。`read-revision --ref <json>` 读取准确正文和来源引用；`current --case-id <id>` 返回当前引用或null。所有输出JSON；输入/冲突/缺失失败返回结构化错误码和exit2，不在错误中回显来源正文、密钥或Git stderr。

## 验证、维护与退出

`test_versioned_store.py` 维护实际Git并发CAS、同意图恢复、来源/修订不可静默替换、日期、中文查询/分页及失败边界；`test_store_cli.py` 维护真实CLI接线及未授权操作不出现；现有 `test_distribution.py` 在同一个真实wheel安装中验证仓库外来源→引用→修订→读取。合成输入不是新经济事实，CI不签研究质量或私有远端消费。

这些测试随现役保管/CLI责任维护，不随#6关闭删除。替换本地适配器时，只退出其专属初始化/路径/环境反例；精确引用、幂等意图、前驱和引用保留合同必须由新实际消费者承担。真实私有目标、远端G读取、第一份正常保存与运行保障仍按#6及#13实际任务验收。
