# 研究成果进入现有 Brief

责任为 #13。本能力扩展原 `news-rolling.yml` 的 `desk/read-desk`，不建立第二 Brief、目录、研究或保管平台。新闻选择、24小时窗口、原 Artifact 写入/CAS/未知回执对账和完整离线页面沿原实现；本组只接入确切研究片段、原限制和实际采用关系。

## 使用原宿主

从现有 `catalogue` 取得确切研究 reference，再通过原 `resume`/`prepare` 实际读取研究正文，选择要采用的**原文行段**。不要按公司名重新查询最新版本，不从文件名或计划时间推测研究完成。原 `desk` 计划可增加：

```json
{
  "research": {
    "items": [
      {"reference": {"repository": "auguspp/Decision-OS-2", "commit": "<exact commit>", "path": "research/cases/<case>/revisions/<revision>/record.json", "sha256": "<exact digest>"}, "start": 1, "end": 8}
    ],
    "max_items": 4,
    "agenda": null
  }
}
```

这是原计划的附加字段；原 `window/cutoff/kind/baseline/attention/max_items` 保留。研究 `items` 明确给出候选版本和1起算、两端包含的正文行段；行段保留原行结束字节，不自动改写摘要。正文SHA256、片段SHA256、原引用、行号、委托、原未决问题/挑战、议程及来源实际读取范围一起保留。普通报告没有结构化底稿时不能推断研究已经完成。

`agenda` 可提供 #89 已存议程的完整 ArtifactRef。复用原议程读取和版本绑定，不接受新的自由反馈文本，不把选了一个引用写成人工选择。需要改变关注/暂缓时沿原 `research-agenda` 合同先保存，不由 Brief 创建另一份选择状态。

宿主仍显式 `workflow_dispatch`、`ref=main`、刚核过适用CI的 `code_sha`。包含 `research` 的计划，无论 `save=true/false`，均需 **`public_research_custody_confirmed=true`**；研究原文与限制必须有公开权利，不能继承新闻元数据许可。有 `agenda` 还须独立 `public_feedback_custody_confirmed=true`。调用者在发出 dispatch 之前检查权利：工作流输入/日志/Artifacts也可能公开，宿主检查不是私有保险箱。

`read-desk` 仍使用 `{reference: <edition ArtifactRef>}`。读取混合版也需研究公开确认；只有读到原件才能识别其schema，未确认时不导出正文/页面，已读取的确切引用与失败状态可能保留。旧新闻版不需要新确认。保存依然要求原 `public_metadata_custody_confirmed`、原 submission_id 和 expected_head，使用原 put-artifact；未知写使用原 result/inputs.json 的同一载荷和键执行 reconcile-artifact，不修改截止或重造键重发。

## 真实采用与分类

一份 edition JSON 内同时保存新闻与研究采用，只有同一份 `brief_markdown` 真正含原作者片段时才记在研究 `included`。原 `news-brief.md` 和页面的简报正文显示/导出该同一正文，没有额外的“研究简报”真源。未采用研究候选留在有界 rows 及省略计数中，不等于已被用户拒绝。

复用已存议程在本次截止的投影。显式关注或复核到期优先，再按调用者声明的版本顺序；不是经济评分。问题级选择不扩成整家公司屏蔽：有任何 focus/到期待复核可优先；所有问题均dismiss才省略整份；所有问题均defer/dismiss且无其他开放问题才暂缓。到期不改变原选择，不证明事件发生，不启动Quick/Full。原问题级状态全部显示。

对照**上一份实际保存正文**，总比较摘要绑定那份完整混合正文的SHA256，不用旧新闻投影或计划时刻替代。研究比较只覆盖上一份实际简报和本次明确指定版本，**不是全历史去重或全目录扫描**：

| 分类 | 已证明的关系 |
|---|---|
| baseline-not-supplied | 未提供实际前版；不声称去重/无迟到 |
| repeat-explicitly-requested | 前版已采用同版，本版有已保存议程的关注/到期待复核 |
| previously-available-not-included | 前版实际可读但未采用该版；不叫新研究 |
| late-to-delivery | 本次可读、前版未列为可读，原保存时刻不晚于前版实际截止；跨周末不清零 |
| newly-stored-research | 原保存时刻晚于前版截止且不晚于本版截止；不等于经济事实刚发生 |

同一 `repository/path/sha256` 从另一个可达commit读取，不算新研究；保留各实际完整引用，但同一计划拒绝同版重复。旧版暂缓不继承给新revision。前版是旧新闻schema时，明确 `research_tracked=false`，不假装它扫描过研究。前版是混合版时后继必须显式给research（可items为空），不能无提示退出研究跟踪。

保存时间来自原研究记录，不是报告期、新闻时间、研究截止或启动回执。没有读取到研究、读取失败、正文行段不可用、超限与晚于截止分别显示；不把它们写成零成果或市场安静。含片段也不等于Full通过质量验收。

## 原版恢复和资源边界

原 `dos2-news-edition-v1` 的JSON/正文不改写；无research计划仍生成原v1结果。有research采用 `dos2-news-edition-v2`，同一Artifact路径/读写通路。新增组合层复用原 news_delivery 的窗口、校验和确定性选择函数，不复制另一套新闻政策。

读取保存版重新取得实际前版和确切原研究，核原文片段及研究时间，再重建本版对照。只核当前一步，不递归全部历史。现时源访问或研究原件不再可读时不能签重建一致；原 `read-artifact` 仍可恢复获准的已保管原版字节，但不升级成依赖重新核验成功。不静默回最新版或用缓存伪装权限仍然有效。

每计划最多8个研究版本，最多8个采用；每段1—80行且不超过8KiB，每研究上下文24KiB、研究区128KiB。超大的研究上下文不截断成“完整摘要”，保留确切引用与明确省略。整份edition仍遵守原256KiB边界；API512请求、输入60KiB、来源缓存8MiB、原件大小和公开限制不放宽。有限输出不代表研究费用或分析长度政策。

## 验证与退出

专属测试保护：原v1字节、确切原文采用、正文基线hash、同版/新修订、跨周末迟到、容量与已可读未采用、坏行段/失败隔离、原议程暂缓/重开、公开权限、原CAS未知写对账，以及真实临时Git保存后独立重开。安装验证扩展**原唯一cold-wheel**中的news消费者，不新增安装/CI平台。原页面已以textContent显示正文并原样导出，通过展开计划保留research字段；本组不冒称完成统一市场/研究网页。

复用理由：现有 ResearchAgenda、ResearchReader、VersionedStore、ResearchArtifacts 和标准库已拥有所需功能；这里只做产品组合与有限确定性选择，不引入摘要SDK、数据库或调度库。不更改研究方法、源客户端、市场/Watch和CI策略。维护归本交付用途；退役新生成能力时撤专属路由接线、输入和测试，保留已有v1/v2历史引用和原字节读取义务，不删除研究或Artifacts。

工程和合成测试不代签真实用户选择、实质Review、全部研究采用或#7研究质量。真实产品运行回执在#13，长期独立备份、统一阅读入口、连续自动运行、通知/部署和旧服务切换按各自授权与后继责任验收。

产业系列使用同一简报的可选 v3 扩展，见[产业 Brief](industry-brief.md)。原 v1/v2 继续独立读回；来源公开许可不由研究或反馈确认代签。
