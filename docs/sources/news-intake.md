# NewsNow + Relay 新闻有限恢复与消费

归属 [#46](https://github.com/auguspp/Decision-OS-2/issues/46)，父责任 #12。新 OS2 的新闻能力不能只留在实施基线的一行，但本批也不等于已恢复旧版完整新闻站点。

## 真实复用定位

旧固定版 [decision-kernel@cb3bbc87](https://github.com/auguspp/decision-kernel/tree/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8)：
- 原 `runtime/news_daily.py`／`radar-newsnow-daily.yml` 的NewsNow七个来源、固定镜像、每源最多30条及明确失败已验，是当前适配起点；
- `external_radar_observations.news` 的来源文章、版本、URL域名与发布时间仅为声明的界限由新纯转换保留；
- `runtime/news_rolling.py`、NBS RSS（feedparser 6.0.14）、News Workbench是**具名后继**，不把旧状态/schema、长时调度、Site发布直接搬回来。

本仓本组代码：`observations/news_windows.py`纯解析／阅读，
`infrastructure/news_capture.py`有限NewsNow及Relay请求，
`interfaces/news_cli.py`显式CLI，`news-intake.yml`手动来源工作流。
原版固定镜像SHA256保留；未独立证明镜像源码与标签等价。
研究/保管继续复用#6的`VersionedStore/SourceInput`，不建第二内容池。

## 新闻源及实际资格

- NewsNow七路：cls、wallstreetcn、fastbull、jin10、mktnews、gelonghui、thepaper。仅访问固定本地端点，每路最多1MiB、最多30条；读取公开 HTTPS 文章链接但不跟随。
- 2026-10-16 06:51:29 +08:00实际 Relay `/tushare/capabilities` 快照中，`news`、`major_news`、`cctv_news` 为 enabled；这三项已有官方可核输出字段，当前按精确来源和24小时以内窗口／CCTV日历日期提供有限适配。参考 Tushare官方新闻接口文档143／195／154，但**官方接口说明与 Relay 真实数据成功是两种证据**。
- `express_news`标 enabled，但本轮还缺已核输出字段，**不开放请求**；`news_cctv`及新闻Baidu外部接口标disabled，不能当作上线来源或绕权限。
- Relay传输复用#12现有`RelayHTTP._get_json`固定HTTPS、独立 `TUSHARE_PROXY_API_KEY`、响应上限、secret不回显与原生拒绝；仅本模块白名单能进入这一窄调用。#12在途通用来源接线与本组无文件重叠，未来共享HTTP重构由真实owner维护适配消费。

## 手动采集和读取

在已安装OS2且获准来源调用的运行环境：

```sh
python -m decision_kernel.interfaces.news_cli capture-newsnow --output news-window.json
python -m decision_kernel.interfaces.news_cli capture-relay --api news \
  --source cls --start '2026-10-15 09:00:00' --end '2026-10-15 10:00:00' \
  --output relay-news-window.json
python -m decision_kernel.interfaces.news_cli capture-relay \
  --api cctv_news --date 20261015 --output cctv-window.json
python -m decision_kernel.interfaces.news_cli read \
  --window news-window.json --limit 40
python -m decision_kernel.interfaces.news_cli export-locators \
  --window news-window.json --output news-locators.json
```

本期只生成有限新闻观察：保留标题、链接／来源、获取时钟、发布时间原字段声称、文章/版本身份和原HTTP SHA256；**不在结果里保留供应商全文或聚合HTTP原字节**。相同标题仅为重复曝光候选，新闻没有自动升级成独立证据、经济事件、公司利好或Quick。来源失败与缺口不能报“无新闻”；初次捕获不能证明完整滚动历史。主请求的wire哈希也不意味着可从hash恢复原文字节。

`export-locators` 仅生成适配现有SourceInput的 `custody=external`、`text=None` 候选，不自动写公开 `records`。无文章URL的Relay项，origin仅是**聚合API**，不是原文链接；`acquired_at` 为本次实取时钟，未核时区的提供方时间不填进已确认`published_at`。普通来源持久化须有实际公开/保管权利，并用#6真实SourceRef写入、读回。新闻许可不因为其可以公开GET而自动成立。

## 手动工作流及安全边界

`.github/workflows/news-intake.yml`只有`workflow_dispatch`，运行者显式选择NewsNow或Relay、确认公开仓7天元数据保留，并给出当时确切main SHA。执行前要求主线最新HEAD及同代码独立成功CI；NewsNow容器不接任何密钥，Relay Key仅在来源采集步骤注入。所有 job 只读GitHub工程，成功/局部失败真实标示，产物只保存有限新闻观察七天，不是原件档案。没有自动10分钟cron、Site发布、GPT任务、通知或正式投资动作。

## 尚需具名交付

旧NBS RSS/feedparser定点来源与前驱真实恢复、NewsNow 18小时滚动版本/缺口、日报/晚报消费是接续工作，不重新复制旧技术验收，也不将旧source状态/持仓/Research历史带入新仓。由#12维护采集/保管责任，#13实现新闻搜索/过滤/分页、Attention与Brief，#7负责经过查证的新闻线索→可反驳主动Quick问题。Relay`express_news`等只有实际字段/权限/原件规则合格后才能增加。启用定时、邮件/ChatGPT提醒或常驻Sites需要另核实际用户授权与生产状态。
