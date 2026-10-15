# Quick / Full 研究成果接续

本入口读取已经保管的研究，不执行研究。`application/research_reader.py` 的 `ResearchReader` 组合现成 `VersionedStore.current/read_revision/read_source`，Quick、直接 Full 和已有 Quick 后形成的 Full 都使用同一 `RevisionInput` / `SourceRef`。没有第二真源、研究状态库、调度器、模型调用或新依赖；存储位置和认证仍由[既有适配器](../architecture/github-store.md)负责。

## 从保存结果继续，而不是重新研究

`resume-research` 返回确切修订的正文、委托/方法文本、前驱、未决问题、原保存时间和 `human_acceptance`，以及本页实际读取的来源。正文与资料是数据，不成为指令、执行授权或人工接受；`research_assessment=not-performed` 明确表示本入口没有判断研究是否充分。

选择当前案例时只解析一次当前指针，随后使用那个 `repository/commit/path/SHA256`。选择已有引用或分页游标时，直接读指定版本，不重新选当前。引用或研究正文自身损坏/缺失/无权会失败，不回退另一版，不返回空成功。存储中的委托描述不是新的可信授权，实际接续仍须核原委托及剩余范围。

来源逐项沿已有 `SourceRef.selection` 读相同版本的正文行或CSV行列；不跟外部URL、前驱或来源文本自动取证。前驱作为确切引用返回，需要查看旧研究时显式选择该引用；不遍历全历史。对研究的更正/新修订仍走已有 `submit-revision`，使用原回执作为前驱并复用同次意图的submission_id；选择旧版来读不意味着它仍可作为当前前驱，过期写由现有存储合同拒绝。

## 安装后调用

先按[README](../../README.md)安装。沿既有只读绑定，在拥有实际读取凭据的宿主中：

```bash
decision-os store --binding config/store-binding.json resume-research --case-id example-case
decision-os store --binding config/store-binding.json resume-research --ref /runtime/revision-ref.json
decision-os store --binding config/store-binding.json resume-research --cursor /runtime/next-cursor.json
```

三个选择器必须且只能选一个。`revision-ref.json` 是保存返回的研究 `BlobRef`，不是SourceRef，也不是整个接续结果。`next-cursor.json` 只保存结果的非空 `next_cursor` 对象。`--source-limit` 默认4、范围1—8。现有本地合成库可将 `--binding ...` 换成 `--path /tmp/example.git`；不要把本地合成位置当真实远端运行。

Python调用为 `ResearchReader(existing_store).resume(case_id)`；精确读取用 `resume(reference=saved_blob_ref)`，继续来源页用 `resume(cursor=previous_result['next_cursor'])`。`next_cursor` 为null时已到列表末尾，不再调用分页。调用入口只读，不要求 `write_enabled=true`，不更改绑定或获取工程凭据。

## 分页、来源失败与“完整”的含义

游标只有版本化格式、确切研究引用和来源偏移；它是可检查的读取定位，不是签名锁，也不证明此前页面确实被看过。每次调用仍重新验证引用和权限。期间出现新研究版本，旧游标继续旧版；要看当前版，重新使用 `--case-id`。不缓存“已完成”“已审阅”或来源可用性。

`source_page` 给出total/offset/returned/remaining、逐项原始序号和引用。`coverage=this-page-only`，`end_of_list` 只表示本页到达引用列表末尾：**不表示所有页面都已读、所有原文都齐全、研究已经完成或投资判断正确**。没有来源时保留研究自身未决说明，不补造来源。

| status | 本次实际含义 |
|---|---|
| `stored-representation` | 该确切表示及指定行列已读回；保留provided/excerpt、原有日期/限制及original_verified=false，不认证外部原件 |
| `external-only` | 只保管了外部定位与限制，data为null；没有自动抓原文 |
| `missing` | 已有读取权限/合同所能确定的对象缺失 |
| `unavailable` | 暂时不可读或无权；包含远端404的“不存在或无权”歧义，不泄露服务端错误正文 |
| `invalid` | 校验、格式或引用范围不成立；不以新版本替换 |
| `not-included` | 一项来源视图超过本页1MiB预算，明确不返回正文，保留确切引用供单独读取 |

成功来源视图及其条目按JSON字节合计最多1MiB；预算不足时下页从未返回的那项继续，不截断文本或伪称部分表是全表。单项超过预算则明确标记not-included并前进，避免不前进的游标。1MiB限制针对来源页，不含本身受现有研究载荷限制的研究正文；它不是整个传输/解析内存上限。底层仍执行现行请求、原件大小、树与身份限制，不由本入口放宽。个别来源失败保留研究与其他来源；未预期的程序异常不伪装成正常缺失。

## 验证、维护与未交付范围

复用决策是直接组合已合入的保管/身份/精确读取合同和现有argparse/JSON入口，不增加SDK、代理框架或另一个Git读写适配器。新测试由研究接续责任维护：`test_research_reader.py`使用真实本地Git与合成修订保护固定版本、选择、隔离及分页；`test_research_reader_cli.py`复用已有GitAPI合同模型验证只读绑定的三种选择器；`test_distribution.py`在同一个实际wheel/离线安装消费者中验证跨进程保存、恢复、再修订与旧页继续。模拟API或合成研究不证明真实托管使用或研究质量。

本入口不持有业务新写入权，不启动采集、Quick/Full、通知、部署或Decision。真实直接Full、主动发现、合理停止、后继实际采用、宿主调用和生产恢复仍由[#7](https://github.com/auguspp/Decision-OS-2/issues/7)及相应责任独立验收。公开/受限材料边界沿[统一协议](../../WORKING-PROTOCOLS.md#permissions)。

将来替换研究阅读入口时同步处理本模块、CLI子命令、专属测试与本文；共同存储、安装与来源/修订合同仍由现役owner维护。关闭Issue不退出仍在用的测试，代码回退也不删除已保存研究。
