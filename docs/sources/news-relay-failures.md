# Relay 新闻失败上下文

归属 #46，消费入口仍是[新闻采集与保管](news-intake.md)和[24小时联合阅读](news-rolling.md)。不增加来源、writer、自动重试或第二内容池。

`news_capture.capture_relay_news` 在失败回执的可选 `failure` 字段保留既有 `RelayHTTP.last_error_meta` 的独立副本。共享 `relay_failure.http_failure/transport_failure` 继续拥有分类和脱敏：HTTP状态、固定业务错误、正文解析状态、有效/缺失/无效的 Retry-After，以及已筛选的请求标识。新闻层不重复解析错误正文、不自造错误类别、不保存任意错误信息或凭据；源码未提供安全上下文时保持旧回执形状，不补猜。

裸503不自动等于暂时超时；400不自动证明调用参数错误。`data_source_unavailable`、`invalid_params`、权限、限流和暂时上游故障按原分类分别呈现。等待秒数是来源事实，不是本模块发出重试或调度承诺；不削短长等待，不因诊断出现而改写失败为成功。

旧 `error_code`、失败结束时刻、`observations=null`、`source_body_retained=false` 及单次固定请求保持。客户端后续请求不会改写已生成回执；成功请求不继承上一次失败。旧元数据没有该字段仍可读，历史字节不回填。

正常 `news_cli read/merge/export-locators`、原 `news-custody` 和精确 `SourceRef`/24小时消费者直接保留整批回执；零命中过滤仍能看到失败。空 observations 与不可用来源不是“没有新闻”的同义词。原响应摘要和安全错误事实都不等于取得新闻正文。

现场核验与对应run/SourceRef在#46维护；本说明及合成测试不证明 major_news/cctv_news 已取得合格样本。来源权限、原件权利、定时采集、Brief和研究采用分别验收。

本接线和 `tests/test_news_relay_failures.py` 随新闻失败消费维护/退役；共享HTTP分类、脱敏、存储和历史reader保护继续由其原owner维护。
