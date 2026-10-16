# 统一阅读室：目录、研究原文、议程与指定简报

责任：#13。继承 #33 目录、#89 议程、#90/#93 的混合 Brief 与独立重开；本入口只组合真实读取，不新建研究索引、简报库、当前指针、保存入口或调度平台。源码位于 `application/reading_room.py`、`interfaces/reading_commands.py` 与 `interfaces/reading_page.py`。

## 一次读取与页面

`reading-room` 从现有 `ResearchCatalogue.page` 取得一页固定 records 快照，打开其中确切研究引用的完整原文。可另外指定一份历史 `reference`，与目录当前版本分别显示，不反查当前后偷换引用。每页1—4案例；额外历史版本不计入目录分母。案例排序沿原目录，不是推荐次序。搜索与类型筛选仅作用于本页，下一页导出原 catalogue cursor；刷新另起快照，不将未读页说成已经覆盖。

`ResearchAgenda.build` 在原委托问题上生成只读投影，`changes=[]`，可按确切已存议程作基线。没有提供议程时显示原稿后续工作，不制造人工选择；提供却不可读时明确未知，不退回“未选择”。旧版本的选择不作用于新版本，未选版本状态继续保留。`as_of` 仅为议程复核评估钟，不是研究截止、事件发生或PIT快照日期。

`brief` 是明确指定的已保存 edition ArtifactRef，不自动发现“最新简报”。复用原 `brief_delivery.open_edition` 重建原文与来源，保留实际正文及前版比较。只有完整精确研究引用匹配原研究 rows/included 才展示采用关系；同公司或同案例不同版本不相等。未采用、未列入指定范围、旧新闻版未接研究和无法判断分别显示。本入口不创造新的采用/省略决定，也不把浏览标成接受。

HTML 自包含、离线只读，支持目录搜索/类型筛选、研究原文、逐题复核、已存 Brief、确切引用及原文下载。页面中的导出按钮只生成原操作请求或后继阅读计划，不联网、不调宿主、不写库、不新开研究。点击简报中的研究，已在本页的直接显示；否则导出带确切 `reference` 的同一阅读入口请求。已有新闻工作台仍由原 `read-desk` 打开，不另复制新闻页面。

## 正常调用

原 `.github/workflows/research-runtime.yml` 仅增加一个只读选项 `reading-room`。仍为手动 `main`、刚核过的 `code_sha` 和适用CI；`submission_id`、`expected_head` 留空，write job 不匹配。

```json
{
  "plan": {
    "as_of": "2026-10-17T00:00:00+08:00",
    "catalogue": {"limit": 4},
    "reference": null,
    "agenda": null,
    "brief": null
  },
  "public_research_custody_confirmed": true,
  "public_feedback_custody_confirmed": false
}
```

`reference` 用完整研究 BlobRef，`agenda`/`brief` 用完整 ArtifactRef，不接受别名、短SHA、外部URL或来源文本选择的执行地址。`report.json.result` 保存当次读取模型，`reading-room.html` 为该模型的界面投影。Actions30天产物不是永久保管或灾备，原研究/议程/Brief的保管责任及精确引用不变。

所有内容进入公共工作流前先核公开权利，布尔字段只是调用者声明，不做人类认证。研究公开确认在目录、存储访问及输入产物生成前检查；显式 `agenda` 还必须单独确认反馈公开。指定 Brief 在读取后若发现既存选择或议程关联而未确认反馈导出，整个简报面板返回 `permission-required`，不把其正文/笔记带入输出。其他合格研究仍可读。元数据权利不自动包含私人笔记、受限原文或正式 Decision。

本地安装沿原 store CLI，输入为上面 `plan` 的内容：

```bash
decision-os store --path /tmp/existing-synthetic.git research-room \
  --input /tmp/room-plan.json --out /tmp/new-reading-room.html
# 获准远端阅读可换为原 --binding；不接受来源文本提供的凭据配置。
```

本地命令只写调用者指定的**新**HTML文件与stdout JSON，不覆盖已有文件或写records。页面导出的请求不自动填公开授权，不带固定过期代码SHA；实际调宿主前重新核准。本地导出也不是公开许可。

## 失败、资源与验证边界

目录权限/快照错误沿原owner失败，不能返回空目录冒称成功；单份研究、议程或简报失败显示missing/unavailable/invalid，只限制相依面板。读取正文不刷新来源资格或研究经济质量。重要未决项、挑战、来源分页和原读取请求保留；源缺失时可保留研究正文，但无法重建的简报不获得采用认证。

原Catalogue卡片和Reader源页限额不变。本页单研究正文1MiB、正文总展示2MiB；可选面板768KiB、完整模型6MiB、HTML8MiB。超限对象整项显示not-included并保留原引用/读取通路，不截断、改写或删除原研究。过大整页或无法再次验证访问则明确失败。原8MiB结果、60KiB输入、512存储请求均不提高、不重置；Brief来源复用原 `_SourceReads`，统计仅代表新闻部分。全局请求预算耗尽可能阻止最终权限复核，不能伪称局部成功。

沿原新闻页评估过的标准 DOM/textContent、JSON HTML边界转义、CSP脚本/样式摘要与标准库；这是有界固定离线投影，不需要React、CMS、模板执行器或第三方服务。来源与原文中的脚本、HTML和指令只是文本。无外部字体、自动网络、localStorage、表单或用户选择表。

专属测试覆盖版本精确关联、固定分页、空库/坏案/缺源、组件故障、容量、反馈权利和页面封装；真实临时Git/原宿主/原唯一cold-wheel验证同时打开已存研究、议程和Brief及独立进程恢复。页面交互与真实获准材料采用另留原生回执，合成成功不签实质Review、研究质量或用户接受。实际未完成验证不能靠本说明代签。

退役时用普通PR退出本只读路由、页面和专属消费者；保留原Reader、Catalogue、Agenda、Brief、Artifact/SourceRef和历史读取。市场/产业统一展示、实际用户更正与Review、独立长期灾备及生产运行仍有各自责任，不因统一入口完成而关闭 #13。
