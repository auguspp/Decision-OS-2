# 完整长研究载荷：有界、无损的离线打包

本入口归 #7。它补齐宿主单字段60 KiB不够装入较长正文的传输缺口，**不是研究压缩摘要、保管新格式、上传服务或模型执行器**。研究/来源仍用同一 JSON 合同，保存/回执/CAS/方法/前驱/公开权限完全沿原宿主。

## 使用

先完成对原文的公开权利、来源与隐私检查，再将**完整原 payload 对象**保存为 UTF-8 JSON。不得为取得传输成功删去问题、正文、挑战或引用。安装后的离线命令不需要网络、token或运行宿主：

```bash
python -m decision_kernel.interfaces.research_payload --input /approved/checkpoint.json > /approved/payload-wire.json
```

输出是可直接作为现有 `research-runtime.yml` 的 `inputs.payload` **字符串值**使用的封装 JSON，不是整个 dispatch 请求；不要再把它包成 payload 内的一层业务对象。`operation`、`code_sha`、`submission_id` 和 `expected_head` 与原来一样传入。小的明文 JSON 继续原样支持。新研究传输不需要创建分支、向main提资料PR或配置第二存储。

需要程序调用时：`pack_payload(raw_json_bytes)` 生成字符串；`decode_payload(wire_string)` 还原 payload 对象。原 JSON 的无意义空白/键顺序被原 `encoded` 规范化，所有字段、类型、正文字符和数字值按该合同保留；字符串内的换行、引文与代码样本不被执行或缩写。解码后再次进入原操作的字段/方法/身份/版本/来源校验；传输成功不是研究质量或保管成功。

## 容量与失败

外层原始及规范化 UTF-8 均不得超过 **60 KiB**，遵守原生 dispatch 总输入限制。gzip 解码后的规范 JSON 最多 **2 MiB**（现行 `records.MAX_BYTES`），每种业务对象/整个ResearchRevision仍单独执行自己的大小上限；渲染后的工作底稿如果超限，仍拒绝保存。这里不是可保存任意长度报告的保证。

压缩率不够、正文已超过解码预算或后继对象超限，均明确失败并保留原文件。不得改成只传摘要、删源引用、拆成互不关联的研究以掩盖失败。既有受信本地/远端CLI合同仍可使用；本增量没有交付任意长度附件、外部URL下载、分块上传或无限传输。用户授权的研究分析不因一个传输失败而自动停止，保存情况必须如实说明。

使用标准库 gzip 容器的 DEFLATE 与严格 base64，封装只含版本标记、解码字节数、SHA256与压缩数据。接收侧用 `zlib.decompressobj(wbits=31)` 和非零 `max_length=声明大小+1` 限制输出，不用无界 `flush/decompress`。要求单个完整流、正确校验尾、无多余成员/尾随字节；声明超限、截断、CRC错误、大小/摘要错配、重复键、非有限数字、非对象、嵌套封装与非规范解码JSON均拒绝。不会把损坏资料转换为空正文或部分成功。

## 权限、公开与回执

**压缩/base64不是加密，不赋予发布权，更不能用来隐藏未经批准的原文或绕过平台拒绝。** 连接器调用前仍需审阅可读原文，密钥、个人/第三方隐私、受限原件及正式Decision不得进入此公开通路。封装和Actions输入/产物都按公开内容处理；日志依旧不输出正文。宿主job权限、只读默认和GitHub凭据范围没有改变。

同一对象明文或打包后得到相同的规范 payload 摘要与保存意图。字节身份属于解码后的内容，不属于某个Python/zlib版本生成的压缩流。保存后丢回执，使用原 `inputs.json` 和 submission_id，操作改为对应只读 `reconcile-*`、代码SHA为本次已核main，保留原method_sha256及完整payload；不用重做研究或更换key。只读对账查无结果仍不可新写；解码摘要不能代替认证或证明内容真实。

## 复用、验证、退出

复用 Python [zlib 有界流解码](https://docs.python.org/3.12/library/zlib.html) 和 [原生 workflow_dispatch](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#workflow_dispatch)，没有第三方运行依赖、新文件服务或另一个保管引擎。实际算法由标准库承担，新增代码仅规范接口边界与现有合同适配。

专属测试验证容量/格式反例，以及真实本地Git支撑的模拟宿主保存完整来源与研究、原委托和挑战读回、同键明文/压缩重放、方法/预期head不被绕过和未知写只读恢复。同一次包外cold-wheel消费者实际运行离线打包并从已安装宿主解析；工程测试不冒充真实Full/平台写权限验收。

本传输版本一旦出现在需要恢复的原输入中，就保留相应解码支持；退役需同步清理专属CLI/测试/文档并处置仍需对账的输入，不改历史研究或SourceRef。其他 #7 深度/自动数值核对/正向Quick与 #13 交付责任不因本功能完成而消失。
