# A 股原价与复权进入同一阅读室

责任：#13；来源和市场资格仍由 #12/#10 维护。`reading-room` 的可选 `market` 计划复用 `market_custody.read_stored_market`、原 Registry 和 VersionedStore，不建行情库、采集器、收益算法或新的写入口。

## 输入与显示

```json
{"as_of":"2026-10-17T12:00:00+08:00","catalogue":{"limit":1},"market":{"reference":"替换为已保存整根的完整 SourceRef 对象","offset":0,"limit":20,"entity_id":null}}
```

`reference` 必须是现役 `market-reading-custody-v1` 的完整整根 SourceRef，其 reading 为 `relay-security-stock-reading-v1`。根、query、页、Registry 和选择由原保管层核验；在恢复 query 前拒绝其他家族。计数检查回执、仅有缺失日期的 factor-audit、研究中的一个价格、裸 SourceInput 和调用者手填收益均不能替代该根。没有合法保管源不启动补抓，也不将原供应商数据写入公开分支。

每页1—50证券（默认20），offset沿原读取器；可指定六位代码加 `.SH` 或 `.SZ`。下一页从本次原 `page.next_offset` 导出，固定同一根而不选择最新；原价/复权必须同页。未知证券的空查询不改变完整主表分母；证券代码不是永久公司身份。目录分页与市场分页各自固定，不把同页研究当市场发现或公司匹配。

`result.market.view` 是原 `read_stored_market` 的完整结果，未改数值或字段。原价和复权分别显示窗口状态、基准/截止日及原 `change_pct`，缺失显示未取得，不填零、不沿用因子。主表资格/隔离、原停复牌解释、因子缺口、来源版本和完整范围计数保留。查询表示恢复成功不等于声明窗口、全区间因子、当前健康、总回报或投资判断全部通过。页面不重新计算，不建立相对收益基准，不自动采用进 Brief。

## 宿主与权限

仍用现役 research-runtime 的 `reading-room`，无新工作流选项。payload 的 `plan` 为上述计划；公开前须按实际权利分别确认 `public_research_custody_confirmed=true` 和 **`public_sources_custody_confirmed=true`**。材料/反馈确认不能替代来源确认。市场许可在 Store 和公共输入产物创建前检查；布尔值是调用声明，不认证法律权利。实际代码使用刚核过的 main/code_sha，submission_id/expected_head留空，只读作业不匹配writer。

原宿主使用checkout中 `docs/sources/relay-registry.json` 及其依赖；payload不能指定 Registry 路径或网络地址。本地包外读取可在原命令传可信 `--market-registry`，与原市场CLI相同，不从来源文字取得配置：

```bash
decision-os store --path /tmp/existing-synthetic.git research-room \
  --input /tmp/room-plan.json --out /tmp/new-room.html \
  --market-registry /trusted/relay-registry.json
```

Registry的原手册/目录/回执相对依赖也须可读，身份改变时沿原保管合同拒绝。无market计划不加载Registry，不改变旧入口模型。页面只导出原根read-source或同一reading-room请求，不自动提交/预填许可；下载JSON是读取结果，不是供应商原始响应文件。

## 故障、预算和退出

根损坏/缺失、Registry不适用、关键上下文失败只限制市场面板；缺单个因子query由原owner降级相依视图，原价好分支可保留。来源恢复、窗口缺口和未提供分别表达；原研究、议程、Brief权限不变。市场部分上限2MiB，超限整项省略、保留精确根和请求，不截断原结果；查询恢复仍受原32-query/原页限额约束。原512请求、60KiB宿主输入、6MiB完整模型、8MiB输出及末次访问核验不提高或重置；总预算或最终权限核验失败不能冒称局部成功。

测试使用合成资料的真实临时Git、原宿主和原唯一安装消费链；包外两进程在捕获输入删除后独立恢复。分页、未知代码、计数伪根、来源缺失/损坏、容量和撤权分别验证。合成成功不代签真实行情资料、合法公开表示或市场研究采用；真实业务验收须原owner给出获准根，不重抓成功样本制造验收。

维护者同步维护 `application/reading_market.py`、原阅读室/命令/页面和 `tests/test_market_room.py`。退出时普通PR移除可选计划、确认接线、页面和专属消费者；原市场Reader、Registry、Store、历史SourceRef及其保管义务保留。BSE/其他market家族、自然运行、Brief采用、用户接受和长期备份不由本组代签。
