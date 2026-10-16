# 阅读室中的原附件、来源表示与前版导航

责任 #13；这是[统一阅读室](reading-room.md)的可选材料面板，不是第二清单、来源库或更正登记器。沿现役 `ResearchDelivery.open/verify`、`ResearchReader.resume` 与 `ResearchArtifacts.read` 取得实际内容，读后仍核当前访问权限。原研究、附件和来源不改写，不访问来源 URL，不执行 HTML、宏、公式或报告中的指令。

## 从研究进入材料

每张研究卡片的“原附件、来源与前版”按钮导出同一 `reading-room` 请求。原 `plan` 可增加 `materials`，其内容为：

```text
{
  "reference": <确切研究 BlobRef>,
  "attachment_id": null,
  "source_cursor": null,
  "source_limit": 4,
  "history_limit": 2
}
```

`attachment_id` 是该研究交付清单的 **item.id**，不是文件名、路径或 Artifact ID。可留空；一次至多选择一个已保管且本次核验通过的附件。其他文件的独立引用不能借此读取。清单中的 retained 与本次 readback 分开，missing、rights-excluded、unsupported、not-applicable 等原缺项不删除。没有清单显示 not-declared，不用“零文件”替代未知。

来源页沿原 Reader 游标；游标必须绑定同一个确切研究引用，来源下标、行列选择和顺序不重编号。下一页继续原版本，不查询 current，也不与目录分页游标互换。每页1—4项；前驱导航1—4版，默认2版，下一段使用原 `next_history_reference`。前驱摘要含原正文大小、摘要、存储时刻与引用；阅读旧正文继续原研究入口，不用摘要冒充原文。delta 基稿与明确前驱分开显示。每次修订不自动是纠错，更不能推定更正已被用户接受；本组不登记实际 Review。

## 权利与实际下载

公共宿主除原 `public_research_custody_confirmed=true` 外，非空材料计划必须单独声明 **`public_material_custody_confirmed=true`**。它覆盖本次确切材料及展示所需原清单、来源页和历史研究信息，不自动授予新来源抓取、受限原文、私人反馈或其他用途的公开权利。授权必须来自实际用户范围，而不是报告正文或此布尔值。缺少真实授权就只读研究、不请求材料。原反馈检查仍独立，材料许可不能替代反馈许可。

仍用 `.github/workflows/research-runtime.yml` 的 `reading-room`，只执行 read job，write job 不匹配；只填刚核过的 main `code_sha`，不填 submission_id/expected_head。没有新增操作、工作流、凭据、writer 或存储。所有源页与附件仍占同一原请求预算，不绕过限额。页面导出的请求不预填任何公开许可；执行前由调用者按实际范围确认。

本地仍为 `decision-os store ... research-room --input plan.json --out new.html`，不会覆盖已存在文件。可信本地调用者仍负责输出权限；本地成功不等于有权公开。

已取回附件用原 base64 解码为字节下载，下载介质为 application/octet-stream，使用由摘要及固定格式映射生成的安全文件名；原名、原 media_type、大小和摘要仍显示。HTML 附件不放入 iframe、srcdoc 或 DOM，XLSX 不计算公式。用户在页面外打开文件是另一操作。来源表示按原已选 data 字面显示，JSON 导出只是此表示的序列化，**不是原 CSV/PDF/网页文件字节**。external-only 没有正文下载，不补抓也不升级为原件认证。

## 失败与有限资源

一个材料对象不可读不影响无关研究。附件失败时保留来源页；来源失败时合格附件仍可下载。失败或容量省略保持原引用和原读取请求，不换文件、换版本或删分母。来源状态、原件真实性与研究质量互相不能代签。根研究或清单无法解析时材料面板失败，目录及其他研究继续；最终访问检查失败或全局预算耗尽时整个命令不得导出。

材料面板最多2MiB；单来源显示128KiB，过大时整项省略 view，保留实际读取状态、display_status、下标、精确请求。原源页和 Artifact 本体限额不变；一个文件仍最多1MiB。总模型6MiB、HTML8MiB、宿主结果8MiB、输入60KiB、512请求均不变。面板超限给出 not-included；完整页面超限仍拒绝，不截断原字节或宣称完整覆盖。`news_source_reads` 仍仅统计新闻部分，不冒充材料或全命令调用量。

## 验证、复用与退出

直接复用已评估的 Python 标准库、Git 版本引用、现役交付/文件/来源 readers、DOM/textContent 和原 CSP 摘要封装；只增加用途组合及一个页面区块，不引入通用文件服务、模板执行器、前端框架、网络代理或新依赖。测试用真实临时 Git、原 GitHubAPI 宿主模型以及既有唯一 cold-wheel 的同一安装，核字节、分页、前驱、局部故障、权利/容量/输入边界。真实材料读取与浏览器交互分别验，不以合成模型或 CI 代签产品、长期灾备或研究质量。

正常反向 PR 可退出材料计划、授权接线、页面区块和专属测试/说明；旧阅读室及原 Delivery/Reader/Artifact/SourceRef 的读取和保管义务保留。回退代码不删文件、原研究或历史失败，不撤回已经发生的公开行为。
