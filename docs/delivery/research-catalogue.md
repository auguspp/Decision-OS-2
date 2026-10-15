# 研究目录与未完成工作：只读入口

责任在 #13。本功能解决“已经存了研究，但必须先知道 case_id 才能打开”的入口缺口。复用现有 `VersionedStore.current(snapshot=...)`、`ResearchReader` 和研究底稿派生的 `continuation`；不维护另一份研究索引、摘要真源或任务状态。

## 实际使用

安装后沿原有存储绑定使用：

```bash
# 已有合成库，不会初始化或写入。
decision-os store --path /tmp/existing-synthetic.git research-catalogue --limit 4

# 已授权远端只读。需要原专用读取凭据，不接受来源文本提供的配置。
decision-os store --binding config/store-binding.json research-catalogue --limit 4

# catalogue.next_cursor 原样存为 JSON 后读取后页。
decision-os store --binding config/store-binding.json research-catalogue --cursor /tmp/catalogue-cursor.json
```

正常连接器使用现有 `research-runtime.yml`，显式 `workflow_dispatch`、`ref=main`，携带刚核过的当前 `code_sha`，操作为 **`catalogue`**，payload 为 `{"limit":4}` 或 `{"cursor":<原next_cursor>}`。不传 submission_id/expected_head，不修改绑定和 token；只有原来的 read job 执行，write job 不匹配。获取原生运行产物并核输入/结果身份的方法见[宿主文档](../research/runtime.md)。

每条可读目录项的 `open_operation` 与 `open_payload` 可直接交给同一宿主：有工作底稿用 prepare，普通已存报告用 resume；二者都固定在目录返回的 **确切 revision 引用**，不再次查询当前最新版。本地可把该项 reference 保存为 JSON，再用原 prepare-research / resume-research 的 `--ref` 打开。显示、点击/读取与人工选择、人工接受、开始新Full是不同事实。

## 一页里有什么

按案例 ID 排序，**不是按推荐程度或新旧排序**。每个案例选择该快照中的当前研究一次；返回原委托、模式、修订号、方法、存储时间、前驱、原未决问题、逐题未完成工作与未解挑战，供人或获准研究者继续。存储时间不当研究截止/事件发生或今日行情时间。

完整正文不复制进目录，使用确切 open_payload 读取；这不是截断已保存研究。结构化底稿不存在时保留原 `unresolved` 文本，明确 `structured_workpaper=false`，不猜问题列表、不用“没有结构化待办”推定完成。作者声明 concluded 仍带研究质量复核限制；目录不生成公司结论、价位或投资决定。

每条可读记录沿原 Reader 实际读取最多一项来源，保留该项的实际状态及剩余数量。`source_page` 表示这次只读到哪一页；`source_next_cursor` 是原 Reader 的来源翻页游标，用 **resume** 继续。顶层 `next_cursor` 是研究目录的案例翻页游标，用 **catalogue** 继续；两者不可互换。外部定位/摘录不升级原件，缺失/无权/损坏/预算省略分别表达，研究正文可读不等于所有证据可用或经济判断充分。

## 快照、一致性和局部失败

每次新目录从 records 取一次快照，后续 cursor 固定 snapshot/repository/offset；重新验证权限与提交可达性。新研究、更新的 current 或新增字典序更早的案例不进入旧分页，不造成重复/漏项；要看新快照显式从首页开始。cursor 是位置，不是授权，也不证明前页已被用户读取。

从已有 `research/cases/<合法case_id>/...` 布局枚举案例，包括当前指针缺失但尚存修订的案例。当前指针/修订无效或不可读时该项显示 missing/unavailable/invalid，其他可读案例继续返回，顶层为 partial-page；不删坏项隐藏故障。全局仓库/快照读取失败则明确失败，不能返回空研究目录。

空快照显示 **no-saved-research**：仅说明当前可读快照没有保存的研究案例，不证明任务未执行、来源没有新内容或市场没有机会。目录中的零待办也不证明所有研究问题都解决。

一页1—8案例，目录卡片预算合计1 MiB；超大的单卡返回 not-included 与确切引用，正文仍用原入口读，不缩写或改写真源。多卡达到预算时未返回项留在下一页，不跳过。最多8192个合法案例超限明确失败，不宣称已覆盖全部；这些是当前内存/调用边界，不是研究费用政策。原 backend/Reader 的权限、请求和来源预算保持不变。

## 未交付的产品责任

本组是研究直接阅读与已有未决问题视图，不是完整 Attention/Review/Brief 系统。没有记录用户选择/暂缓/移出，没有自动注册事件窗/定时器，没有推断任务在途，没有保存 Brief 的实际采用、遗漏或比较基线。真正研究议程的授权变更、日常Brief/迟到采用、用户反馈、最小Review及正常运行，仍在 #13 按原合同接续；市场/产业/来源由原工作组提供。

本组不改变 #7 的真实Full质量待验事实。正常研究公开写入与跨对话采用须使用实际获准成果，不能拿合成用例或空目录代签。

## 维护与验证

实现和专属测试归直接研究阅读用途；退役时处理目录路由、文档和专属测试，原报告/引用/保存/Reader责任不随之退出。复用现有 Python 标准库、Git 版本保管、原 CLI 和宿主，只加一个只读操作；没有新依赖、调度、SDK、浏览器站点或第二存储。单独网页/手机交互尚未交付，不因有JSON就称完成UI验收。

合成测试覆盖真实本地Git支撑的保存→列出→精确打开、分页后新增/修订、坏案隔离、普通报告、来源故障和原来源游标；同一次包外安装消费目录，不新造安装试验。新宿主仅作获准既有公开记录的只读采用时，空目录也是正常但有限的运行事实，不当正向研究交付证明。
