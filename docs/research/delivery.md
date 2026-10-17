# 研究阶段交付：正文、原文件、来源与精确接续

责任 #7，复用 #6 的 VersionedStore、原提交日志、SourceRef、GitHub 条件写、研究 checkpoint 和原备份，不建立第二真源。工程能力、声明清单完整性、研究充分与人工接受分别证明。

## 默认执行责任，不等待再次说“存档”

在已经获得本案研究和相应公开保管授权的会话内，研究者形成有价值的阶段版本后，应检查产物清单，调用一次 `deliver`，核对保存回执和独立读回，再交付。仍有重大可解决问题时继续研究；保存阶段版本不等于停止研究。已获准的原创成果不重复索要同一授权；第三方原件、隐私、其他案例及正式 Decision 不因此获得公开权利。

自动化发生在**显式调用交付入口之后**：保留整体意图 → 已提供来源/文件逐项保管 → 现役数值核算（如声明）→ 原研究 checkpoint → 新建只读 reader 重新校验。程序不会监听 ChatGPT 对话结束、扫描模型沙箱、自动发现新文件、调用模型或自行批准论文/研究。研究会话/工具宿主负责实际调用，不把方法文本称为自动后台执行器。没有调用及回执就没有完成交付。

来源取得仍由原资料工具/#12承担。`source` 必须是已经取得且可以保管的 SourceInput；`source_ref` 为既有确切版本。程序不会访问来源 URL，未读原件不能从 URL 自造 SourceRef。只保留外部定位时必须使用原 external custody 和限制，读回会继续显示 external-only，不升级为原件。

## 一份清单与一份研究修订

`research-delivery-v1` 包含原 `request/progress/revision_id/method_sha256`，以及 `rights_confirmed=true`、`rights_basis`、`body_mode`、`items`，可选 `numerical_workpaper`。授权字段是调用者须事先核实的声明，不是来源正文提供的工具权限，也不是服务端身份隔离。

每项 `id/role/status/required/reason` 必须齐全。三类责任均要交代：`report-original`、`calculation-file`、`source-evidence`；其他文件可用 `supporting-file`。没有工作簿可说明不适用，不要求制造空 Excel。

| 状态 | 输入和结果 |
|---|---|
| include | 恰好一个 artifact、artifact_ref、source 或 source_ref；实际保存/读取成功后转 retained |
| missing | 原件未取得；不从摘录重建并冒充原文件 |
| rights-excluded | 无公开权利，输入不得含该文件正文、字节或私密路径 |
| unsupported | 超出当前格式/容量通路；说明范围，不截断或改名绕过 |
| not-applicable | 原委托确实不需要，必须 required=false 并说明依据；不是“没做” |

后继交付保留前版清单中各 ID，或逐项说明缺失/排除，不静默删除。已经保管的文件用 `artifact_ref` 复用；同一源引用在 progress.sources 与清单中重复出现只保留一次。来源顺序为原 progress.sources，随后按清单顺序加入未重复的 SourceRef；findings/challenges 的 evidence 下标绑定这一顺序。不会重新编号既有引用。

来源身份沿现有 `SourceRef` 规则比较：省略默认 `selection`、空选择对象和完整的 `all` 选择表示同一引用；不同选区仍是不同引用。交付只规范化内部合并列表，原输入与整体意图摘要不改变。此前因同一引用的简写/完整写法混用而停在部分阶段的交付，可以在只读对账后用原 payload、原提交号和重新核过的 head 恢复，不需要重存来源或更换意图。

`body_mode=snapshot` 表示作者提供的当前完整叙事；`delta` 必须有确切 predecessor，并保留它为阅读基稿。选择不认证作者确实给全内容。清单附录和原结构化底稿共同保存在同一 research.md，记录本次输入摘要、原意图引用、基稿引用、文件摘要/大小/原名及来源引用。RevisionInput、旧方法和历史正文不改写。

## 原字节附件，不拿转换件替原件

`ArtifactInput` 字段是 `artifact_id/name/media_type/data_base64/sha256/size`。文件按原始字节计算 SHA-256 并再次读回；HTML 的样式、导航、CRLF 和 Unicode 不剥除，XLSX 的 ZIP 字节、公式和样式不转换成 CSV。保管不解析/执行 HTML、JavaScript、宏、公式、PDF，也不认证扩展名与文件语义。结构化财务字段仍通过独立 SourceRef 表示。

路径由安全 ID 与版本摘要生成，原中文名/大小写仅为元数据：

```
research/artifacts/<artifact-id>/<version-digest>/manifest.json
research/artifacts/<artifact-id>/<version-digest>/content.bin
```

同一 VersionedStore 的 `_replay/_save` 与 submissions 日志承担保管；原 GitHubStore 承担身份、可见性、不可变路径及 expectedHeadOid。原生 Git bundle 包含附件字节，原 audit_store 同时核附件的历史和当前表示及修订清单。没有新数据库、孤儿清理或并行 current 指针。

**先升级使用该数据分支的所有 reader/备份消费者，再实际写入新附件。** 旧代码的严格路径白名单不能读取新增路径，不能宣传新文件对旧程序透明兼容。新代码仍可读取所有旧正文研究；旧档没有清单时显示 not-declared，不补造完整性。

容量：每个文件最多 1 MiB（允许空文件但不声称有内容）、每次清单最多32项、规范化总输入最多2 MiB；原宿主传输字段仍为60 KiB、解码最多2 MiB、结果最多8 MiB，原请求数量/写入预算不变。**XLSX通常已经压缩，二进制不保证压缩后能进入60 KiB字段。** 本地可信配置 CLI 可处理1 MiB文件；公开 Actions 仅支持实际能够打包进入该字段的输入。超限需分开使用获准的文件保管入口后引用已有 artifact_ref，或明确保留 unsupported；未交付通用分块上传、大文件服务或任意长度附件。分开提交也各自受同一字段限制，不能用“拆开”掩盖单文件仍超限。

## 命令与宿主

离线清单打包，不接触网络或存储：

```bash
python -m decision_kernel.interfaces.delivery_pack --input inventory.json > delivery.json
python -m decision_kernel.interfaces.research_payload --input delivery.json > wire.json
```

本地 inventory 可用 `progress.body_file` 代替 body，以及 `artifact_file:{path,artifact_id,name,media_type}` 或 `source_file:{path,metadata}` 代替内嵌字节/文本；路径仅相对清单所在目录，不允许越界、符号链接或目录。排除项的文件不读取、路径不进入输出。没有显式选择的文件不会被扫描。缺文件记录 missing，不能替它生成另一份“原件”。

配置了真实获准写入目标和凭据的 CLI：

```bash
decision-os store --binding approved-binding.json deliver-research --input delivery.json --submission-id <原稳定意图>
decision-os store --binding readonly-binding.json verify-delivery --ref research-ref.json
decision-os store --binding readonly-binding.json open-delivery --ref research-ref.json --history-limit 2
decision-os store --binding readonly-binding.json read-artifact --ref artifact-ref.json
```

仓内 config/store-binding.json 仍为只读，不修改它来自动提权；本地 `--path` 仍仅限合成测试。`put-artifact --input` 的对象为 `{artifact,rights_confirmed,rights_basis}`，支持预先保管获准文件。read-artifact 返回已校验 base64；消费者自行决定是否保存及如何打开，不能直接把不可信 name 当本地路径。

原 research-runtime.yml 提供 deliver、reconcile-delivery、verify-delivery、open-delivery、put-artifact、reconcile-artifact、read-artifact。写操作只有 deliver/put-artifact 加原 put-source/checkpoint；验证、阅读和对账都在只读 job，不携带写权限。既有 code_sha、原意图、expected_head 及公开产物约束保持。所有 inputs 和 Actions 产物都可能公开，编码不等于加密。

## 中断、重试与真实完成状态

交付含多个原生提交，**不是跨对象原子事务**。最先在原保管日志登记整体输入摘要，防止同提交号的不同正文与先前已存文件混装。每阶段提交号由整体 submission_id 与阶段 ID 确定；未知效果停止，不自动换号或重发。宿主只接受先前计划 head 或本调用自己刚提交的 head，不把别人的并发提交当成新的写入许可。

未知写后用原 payload/method_sha256/submission_id 调 `reconcile-delivery`，只读重建已有阶段；某阶段不存在就以 SUBMISSION_NOT_FOUND_NO_WRITE 结束，绝不顺手补写。核对现状后，在原授权下以当前明确读取的 expected_head 和**完全相同** payload/submission_id 显式恢复 deliver。修改正文或清单须新修订/新意图，引用已保管对象，不覆盖原意图。失败前已发布阶段见 published_stages；不声称全部回滚。

成功 checkpoint 的 reference 不因随后核验失败而消失。返回保存回执、独立 delivery_verification 和独立 numerical_audit。`verified-declared-inventory` 仅表示声明的适用项均有可读版本；missing/rights-excluded/unsupported 为 partial，读取损坏/权限失败为 failed，没有清单为 not-declared。CLI/宿主对非完整清单非零退出但保留 JSON 与已保存回执；不存在“绿色表示整篇Full已完成”。对账仅重建已存结果，不重建文件内容。

保存和随后fresh-reader在同一宿主调用内共用512次API预算；新reader的对象及祖先证明缓存独立，不继承writer缓存，也不重置请求数。GitHubStore有界复用已验证的确切提交祖先关系，减少同一清单的重复compare，每次仍读取实际仓库权限和当前分支。32项清单上限不保证任意来源数、前驱深度或容量组合都能在预算内完成。

若已取得明确的published修订回执、只是随后读回耗尽预算，可在原授权下用该确切reference单独调用只读`verify-delivery`；不为补核验重交deliver。核对原输入、发布阶段和实际记录后再决定是否还需写入。独立核验全部retained项成功且errors为空时，声明的missing/rights-excluded/unsupported仍使结果保持partial及非零退出，不能据工作流失败误称原件未保管，也不能为追求绿灯重跑或抹去缺口。

Full 首次交付、成功重放和 `reconcile-delivery` 都返回绑定原确切研究版本的 `source_use_acceptance` 待核入口。即使 current 已前进，旧意图回执仍指向原版本；保存与读回不会把来源使用状态升级为通过。Quick 不增加 Full 专属要求。

可选 numerical_workpaper 复用 research-numbers-v1 的 inputs/calculations/template，不在其中再传 sources；取得实际引用后自动接同一 sources 列表，生成原数值附录并由 checkpoint 返回检查结果。数值 blocked 不抹去归档事实，也不提升为经济正确。未声明数值仍显示 not-declared。

## 下一会话怎样打开

从现有 catalogue 找到确切 reference 后调用 open-delivery。它一次返回当前正文、声明清单及核验、方法、原问题/挑战与待做事项，并有界读取1—4个前驱正文（每页额外4 MiB），余项用 next_history_reference 明示。缺权限、缺前驱或损坏单独提示；不偷偷切到最新，历史观点不冒充当前观点。旧增量稿虽没有 body_mode 仍可沿确切前驱读取，不凭关键词猜哪版是完整报告。

answered/reframed 仍带 next_action 的事项同时进入 `review-declared-followup`，原 finding 和状态保留；不再把“改写问题”当作没有待做工作。新 deliver 不允许同时声明 concluded 和未完成行动。旧字段、旧研究和旧回执不事后改写；该工作清单并不自动执行或授权研究。

## 验证、依赖与退出

验证使用合成 HTML/XLSX 原字节、真实本地 Git、GitHub API 的既有真实Git模拟器、同一次包外 cold-wheel、原生 bundle恢复，以及获准真实档案的只读打开。每层回执分别记录，未取得的真实工作簿不由合成成功签收。独立reader是不同存储实例/缓存，不冒充独立研究者或人工接受。

沿用标准库 base64/hashlib 和已有 Git blob/条件提交，不新增文件服务、包依赖或模型执行器。参考 Python 3.12 base64 官方文档及 GitHub REST Git blobs 文档，仅使用现成能力，不复制第三方源码。专属命令、宿主操作、路径及测试随能力维护；退役写入口不删除历史附件/清单的读取责任。普通反向PR回退工程，不删除或重写数据。
