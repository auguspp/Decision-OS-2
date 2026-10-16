# 新闻工作台、显式 Attention 与实际 Brief 基线

责任：#13 新闻消费子范围；来源与滚动仍由 #46/#48 维护。用户已明确开始最后一段新闻施工，并继续暂停 `major_news` / `cctv_news`。本能力不重新采集，不替代现有研究目录、实质 Quick/Full、人工 Decision 或生产运营。

## 1. 一条可使用的交付路径

沿原 `news-rolling.yml` 的手动入口，`operation=desk` 读取一个**精确已保管的24小时索引**，从保留的来源版本完整重建，产生：

- `news-desk.html`：自包含离线阅读页，含全部可读行；来源、标题、最后观察时间和选择状态筛选、每页40条、原链接和完整来源健康。
- `news-brief.md`：本期实际生成的晨间、晚间或即时新闻简报正文。
- `edition.json`：有界的简报/采用、必要省略、显式选择、原问题、精确窗口与基线合同。
- `quick-candidates.json`：显式关注且给出原问题的候选交接，**未执行研究**。

只有 `save=true` 且原 writer 返回确认回执，简报才沿同一 `ResearchArtifacts` 存入 `records`。HTML只是原版本投影，不是第二内容库，也不是已部署的联网网站。页面离线打开不需要凭据、外部字体、CDN、数据库或服务器；不能自动刷新来源、提交选择或发送通知。

普通 `build/read` 的语义、旧来源和旧窗口字节不变。新 `read-desk` 按确切 ArtifactRef 重新读取简报、上一实际基线和有关窗口，复算后生成同样正文与完整阅读页，不重新请求任何新闻源。

## 2. 简报规则，不冒充经济研究

`kind` 为 `morning` / `evening` / `adhoc`，只改变交付类型，不新增定时。`cutoff` 是显式实际交付截止，原观察窗口的 `as_of` 必须不晚于它。原媒体发布时间保留为来源声明；抓取/观察时间不是发布时间，版本不是独立事件。

默认最多12条（可显式1—24）：先采用显式继续与到期待复核的版本，再按原最近观察次序在不同来源间轮选。无投资信心评分、标题模型或凭空公司关联。源未核与未研究材料明确为**新观察，待解释**。没有真实研究运行回执，不能称 Quick 已启动，也不能把保管或界面浏览当用户接受。

`baseline` 必须是**实际保存的上一份正文 ArtifactRef**，不是定时时间、last_run_time、文件名猜测或自动“最新成功”。有引用却不可读/摘要不符则拒绝依赖本次对比；未提供则明确缺失，计数为 null 而不是0。

当前版与上一实际正文分开记录：已采用同版（默认省略）、之前在窗口但未采用、迟到进入交付窗口、新进入窗口、退出窗口。迟到以首次观察时间相对上一实际截止判断，不证明媒体迟发或当时全网可得。之前未采用不是永久否决；显式继续与复核到期可重新带回，并说明原因。晨报可以用晚报的实际 Ref，类型不强制先后、不会制造一份从未存在的晚报。

`omissions` 完整归因于同版重复、明确暂缓、明确移出、简报容量。未入简报的条目仍可在工作台阅读。来源结果、失败/未知、裁剪、前驱和覆盖限制随整窗保留，筛选零条不显示成“市场没有新闻”。此新闻简报尚不自动采用一般研究成果，不宣称整个 #13 的研究议程与运营完成。

## 3. 显式选择与隐私

每个选择绑定 `[provider, source_id, article_id, version_id]` 的稳定摘要，不因重复观察而换键，不屏蔽同一公司的新证据或新闻新版本。

`attention` 每次最多128项，无重复键。每项完整形状为：

```json
{"entry_id":"<确切64位版本摘要>","action":"focus","reason":"具体选择理由","question":"待核问题，可留空","review_at":null}
```

动作：`focus` 继续、`defer` 暂缓、`dismiss` 移出、`reset` 撤销选择。理由必填；问题只属于 focus；可选复核时刻只属于 defer，需带时区且新设置在本次截止之后。到期只表示待复核，不暗改为人工继续。旧状态由实际基线承接，变更有版本记录；不读取额外永续状态表。

页面选择仅存在本页内存；导出前提示未存档。导出的计划绑定本页已保存简报作为下一基线，未保存时合并原输入与本地变更。**导出不等于提交或公开。** 未提交选择关闭页面会丢失，可先保留导出文件。本页不使用 localStorage，不把点击、停留或打开原链接记成已读/接受。

GitHub 原生工作流产物可能公开。因此非空新选择无论 `save` 是否为 true，都须**单独**设置 `public_feedback_custody_confirmed=true`，否则在生成输出和访问 Store 前拒绝。它不是通用元数据权利的继承。私人笔记不可填入公共工作流；本地浏览、导出后可由用户在许可范围内另行交回。该字段是调用者声明，不是身份认证，文档始终 `human_acceptance=not-established-by-artifact`，不伪造人类投资审批。

## 4. 原生调用

继续使用原手动工作流 `.github/workflows/news-rolling.yml`，核当时 `main` 的代码 SHA 和适用成功 CI。

`operation=desk` 的 `plan`：

```json
{"window":{},"cutoff":"2026-10-16T12:00:00Z","kind":"evening","baseline":null,"attention":[],"max_items":12}
```

`window` 替换成完整 SourceRef（仓库、commit、manifest路径/摘要、selection=all），不得用短SHA或只有窗口标题。`baseline` 为空或完整 ArtifactRef。历史窗口可以生成明确标注观察时点的简报，但不能称本次采集或最新全市场新闻。

保存：原 `public_metadata_custody_confirmed=true`、`save=true`、稳定 `submission_id`、新核验的 `expected_head`；新反馈另核其许可。只读 desk 的两个写入字段留空。`operation=read-desk` 强制 `save=false`，`plan={"reference":<完整简报ArtifactRef>}`，两个写入字段留空，不接受旁带选择或新窗口。

保存复用原 `run_command(operation=put-artifact)`、`ResearchArtifacts`、权限/CAS/日志与备份。完整原命令在 `result/inputs.json`；未知写必须按它的**原 payload、原 submission**执行已有 `reconcile-artifact` 只读对账，不重建新 cutoff、改键或将提交重试解释成新简报。明确未写的冲突可在对账后沿原意图和新 expected_head 恢复；本入口不是重试队列。

只读和写入仍沿原分离作业与主线CI预检，未增加 schedule、Actions写权限或来源凭据。

## 5. 安全、容量、维护与验证

复用原来源读取和 Artifact 保管、原字节摘要/条件写，不引入通用 CMS、React/Streamlit 服务或新的状态平台：这里仅是小型固定离线投影。浏览器使用标准 DOM `textContent/createElement` 与事件监听，不把新闻/问题插入 `innerHTML`；源链接仅允许无凭据的 HTTP(S)，新窗加 `noopener noreferrer`；JSON封装转义HTML边界，CSP以固定脚本/样式摘要准入，禁止联网与表单提交。通用组件检查依据是 MDN 的 CSP/script-src、DOM textContent 及 Python 标准 `html.escape`，无需第三方模板执行器。

现役源码：`application/news_delivery.py`、`interfaces/news_page.py`、`interfaces/news_delivery_cli.py`；原新闻host仅窄路由，原 Store/来源验证不变。新文档256KiB、HTML8MiB、选择128、采用24；原60KiB计划/传输、512次存储API及同操作8MiB精确来源缓存保持。超限或不可恢复时明确失败，不丢条目凑绿；一份父简报只核其自身正文/来源及必要状态，不递归重放全部历史简报。打开当前版本则复算它与这份真实父正文的关系，不把这个边界称完整链审计。

核心测试覆盖真正 Git、原 GitHubAPI/Store/CAS、两次独立读取、未知ACK的原意图对账、缺基线/未来基线拒绝、重复/迟到/到期选择和公开反馈门禁。单一现役 cold-wheel consumer 增补安装后完整新闻消费，不重复建轮子。独立 Chromium 测试覆盖分页、零筛选健康、选择导出再保管/恢复、移动屏及脚本逃逸；测试合成选择不冒充用户采用。受管浏览器若阻止 file URL，记录其限制，仅渲染相同HTML字节做交互验收，不声称验证了宿主文件访问权限。

退休时同步移除专属投影/路由/测试/文档，已有 Artifact/SourceRef、来源窗口、失败历史与共享Store保护保留。公开原文权利、实质研究、私有反馈、自动调度、外发通知、独立持久备份及生产切换需各自实际验收；不能靠此页或CI代签。
