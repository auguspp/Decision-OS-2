# 生产准备：数据与确切阅读代码的离线恢复包

责任：#13；共享快照保管沿#6。复用现役手动 `store-runtime.yml`、Git bundle、`GitSnapshot`、逐引用审计和原阅读室。没有第二备份服务、写入者或自动故障切换。现有资料/Brief/研究的权利和人工接受边界不变。

## 为什么同时保留读取代码

数据包只包含 records 的完整可达历史；其中初始化时可达的旧工程提交不等于当前阅读器。新附件格式、研究方法和 Registry 需要相应软件。因此现役 `operate` 在原数据备份及恢复校验后，同时从本次 checkout 的确切 Git commit 导出 `reader-source.tar`。

只有固定白名单：`src/decision_kernel` 的 Python 源码、`docs/sources` 的说明/Registry及其相依快照、只读 `config/store-binding.json`、`pyproject.toml`、README和两份恢复说明。没有工作树、未提交文件、`.git`、令牌、操作系统、Python/Git二进制、依赖缓存或私人配置。归档绑定当前HEAD；每个文件的模式、Git blob身份及字节与独立 `ls-tree` 清单核对，`export-ignore`/`export-subst`造成遗漏或变化也拒绝。归档中的绑定必须与实际认证的数据绑定相等。

`report.json.reader` 包含确切代码commit/tree、归档大小/SHA256、逐文件Git blob/SHA256及本次Python/Git版本；原 `snapshot`、bundle摘要、原审计和失败状态保留。数据包先验证成功但源码归档失败时，操作仍报失败，已经验证的数据包和回执保留，不删除或重发数据。旧两文件备份继续支持原 `verify-bundle`，不伪造历史源码归档。

这是有限传输包，不是长期异地灾备。Actions仍30天；是否建立外部持久副本、更新频率、可接受数据丢失/恢复时间和生产切换均需另行确定并实测。没有配置定时、通知、网站部署或停旧Kernel。

## 核验与启动

先通过独立GitHub读取核原生run/attempt/code、产物身份与外层ZIP摘要，再将它作为数据解包，核原 `records.bundle` 和 `reader-source.tar` 摘要。**与文件同放的一段自述SHA不能独立认证发布者。** 软件在执行前必须确认来自已审核的确切main，不执行来源附件中的代码。Python >=3.12 和 Git须由可信环境预先提供；恢复路径不需要第三方Python包或联网安装。可选RSS采集依赖不属于这个只读恢复用途。

在已核可信归档后，解到一个新目录，不自动运行它：

```bash
python -S - /approved/reader-source.tar <独立核过的reader.sha256> /new/reader <<'PY'
import hashlib, pathlib, sys, tarfile
archive, expected, target = pathlib.Path(sys.argv[1]), sys.argv[2], pathlib.Path(sys.argv[3])
assert archive.is_file() and not archive.is_symlink() and 0 < archive.stat().st_size <= 16 * 1024 * 1024
assert hashlib.sha256(archive.read_bytes()).hexdigest() == expected
with tarfile.open(archive, 'r:') as source:
    members = source.getmembers()
    assert len(members) <= 2048 and sum(m.size for m in members) <= 16 * 1024 * 1024
    assert len({m.name for m in members}) == len(members)
    assert all((m.isdir() or m.isfile()) and not m.sparse and not pathlib.PurePosixPath(m.name).is_absolute()
               and '..' not in pathlib.PurePosixPath(m.name).parts for m in members)
    target.mkdir(parents=False, exist_ok=False)
    source.extractall(target, members=members, filter='data')
PY
```

这一步只解码已核工程源文件，没有执行归档脚本。上述路径由受信操作者选择，不从资料正文自动提取。

使用既有 `reading-room` 的原计划JSON（不是workflow payload，不附带来源获取/写入请求）：

```bash
env -i PATH="$PATH" LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH=/new/reader/src python -S -m decision_kernel.interfaces.store_runtime recover-room \
  --bundle /approved/records.bundle \
  --bundle-sha256 <独立核过的bundle.sha256> \
  --binding /new/reader/config/store-binding.json \
  --snapshot <原报告snapshot> \
  --plan /approved/reading-room-plan.json \
  --market-registry /new/reader/docs/sources/relay-registry.json \
  --out /new/recovered-reading
```

用受信绝对路径显式指定 Registry 后，可从任意工作目录或已安装的 reader 启动。`--market-registry` 同时传给完整数据审计中的 Watch 引用消费者，以及市场面板和 Odds 等原阅读消费者；没有顶层 `market` 的计划也会使用该参数。完整审计会核包内已登记的 Full Watch 及其原 Odds，计划未展示 Watch 不会跳过这条依赖。读取保留行情的 Odds 详情或审计绑定此类 Odds 的 Full Watch 仍需要原 Registry，不能只凭顶层面板推断它不需要。Registry使用随源码保留的手册和能力快照，不访问Relay。显式传入的路径或内容无效时，在完整数据审计和创建输出目录前拒绝；计划含 `market` 时仍必须传此参数。计划和完整审计均不需要 Registry 时（例如无保留行情 Watch 的仅研究/Brief、原 declared-price Odds及其Full Watch）可以省略。

不按公司名找新源，不以计数回执代替市场根。计划含历史目录cursor仍保持它的确切快照；整个恢复包的snapshot不是将所有对象升级到“最新”的命令。

## 消费、结果与失败

`recover-room`先校验只读绑定、原计划、bundle外部摘要，并将**已核字节**复制到本次私有临时输入；外部文件之后被替换不会偷换消费对象。复用原空bare库 `git bundle verify`、完整clone/fsck、绑定anchor及逐引用审计，再由 `reading_commands.perform` 读取原研究、来源、已存Brief、议程及适用面板。所有Git子进程沿原禁网络/禁hook环境；不创建GitHubAPI，不要求令牌。临时恢复库不是第二真源，退出即释放；没有push、生产writer或外部通知。

只在操作者指定的**新目录**写 `room.json`、原模板 `reading-room.html` 和 `recovery-report.json`。原阅读模型保持，报告另存快照/输入/输出摘要、实际耗时、审计及阅读状态，不把耗时一次测量叫恢复时间承诺。`completed`退出0；面板有缺口时仍保留可读部分，`completed-with-gaps`退出2；结构/完整性失败报failed、不得生成空成功页。输入和输出错误不会覆盖既存目录。恢复状态、资料完整性、源原件认证、经济判断、用户接受和线上权限分别成立。

原bundle64MiB、计划60KiB、阅读模型6MiB/HTML8MiB及原组件限制不提高；源码归档独立限16MiB/1024文件，只用于已提交工程文件。没有新永久索引、工作流或依赖；有限回执不是人工同步进度库。

## 验证、维护和退出

专属验证保护确切Git字节/脏工作树排除、归档属性/链接/路径/容量、绑定、既有bundle回归、坏摘要/快照/目录、完整审计、实际Brief与研究恢复、部分失败及安装后独立进程消费；非项目工作目录及原安装消费者中的保留行情 Odds 详情和已登记 Full v3 Watch 验证显式 Registry 同时到达完整审计与阅读消费者，原 declared-price Odds及其Full Watch保留无 Registry 支持。合成工程、真实备份、无网络恢复阅读、长期持久保管和正式运行分别验收，不用本说明代签结果。

`reader_archive`只拥有代码归档；`git_snapshot`仍拥有Git恢复；`recovery_commands`只组合原阅读消费者；`store_runtime`仍是原显式宿主。退役用普通PR同时撤源码归档接线、恢复子命令、专属测试/安装helper和此说明，原 `verify-bundle`、GitSnapshot、SourceRef/Artifact/研究历史及共享保护继续；不得删除已保管历史或扩大读取权利。

有界复用依据：Git官方 [archive](https://git-scm.com/docs/git-archive) 的固定commit/PAX及export属性、[bundle](https://git-scm.com/docs/git-bundle)；Python 3.12 [tarfile安全边界](https://docs.python.org/3.12/library/tarfile.html#extraction-filters)。原生工具已具备所需压包/恢复能力；本组仅做既有reader白名单和应用接线，不采用额外备份平台或自制压缩格式。
