# Relay 实时能力目录：取得、证据与复用

归属 #12，供 #7/#10/#11/#13 消费。依据用户提供并已归档的
[客户手册第5和12节](vendor/tushare-relay-v0.5.61.md)。不再用静态手册数量推定实时目录。

## 原始元数据

仅查询 `GET https://pcd.mobcvb.cn/tushare/capabilities`，或其单个具名子项。
同一个 `TUSHARE_PROXY_API_KEY` 只通过 `X-API-Key` 发送，复用现役 RelayHTTP 的
HTTPS、大小、身份、重复JSON键与密钥反射保护；不扩大业务API白名单。
不会查询 `__probe`、调用目录中的URL/示例、读取个人组合或执行写入/删除。

安装后的显式入口：

```bash
python -m decision_kernel.interfaces.relay_capabilities_cli --live \
  --code-sha <已检查的完整代码SHA> --public-rights-confirmed --out /tmp/new-catalogue
```

GitHub Actions 入口 `decision-os-relay-capabilities` 只允许本仓main手动运行；填确切
main SHA和元数据保管许可即可，不用再填写Key。Secret不提供给安装和CI，作业只有
contents:read，和现有来源作业使用同一串行组。没有定时器或records写入。

产物为**原始HTTP响应字节** `capabilities.raw.json` 和 `receipt.json`，后者保留请求/接收
时间、endpoint、SHA256、字节数、HTTP状态、run/attempt及代码版本。失败不变成空目录。
只在经过密钥反射与敏感字段检查后保留正文；不承诺自动检测任意编码的秘密。

## 使用边界

服务器 `enabled/provider/required/required_any/methods/probe_example` 属于服务自述。
登记时保留字段缺失/null/值，未知字段不填造。启用不证明给定证券、日期一定有数据，
也不证明当前OS2已有该资料族的转换器，更不自动授予写入权限。

本次用户要求的全量目录清册将在**真实响应取得后**登记：保留原始声明，另列功能分类、
对应模块及已实现/已实测引用。分类是OS2工程解释，不是供应商额外保证。
原始工程能力快照与接口说明可在main版本化；业务财务数据仍按records规则保管。
7天Actions产物不是永久登记，取得后需用正常PR保存工程目录或以版本SourceRef保管。

读取接口维护时保留旧原件与回执；新快照新版本。移除此入口同时移除专属workflow/CLI/
测试，保留仍有效的业务RelayHTTP及历史来源引用。自查/CI与真实目录读取分别验收。
