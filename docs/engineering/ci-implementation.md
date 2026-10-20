# 可安装入口与 CI 实现

本页解释现役实现及维护边界，不维护任务进度。规范以[完整 CI 合同](ci-contract.md)为准；首次交付及实际 PR/main/设置证据见 [#5](https://github.com/auguspp/Decision-OS-2/issues/5)。代码存在、测试通过、原生保护设置生效和产品正常使用分别成立。

## 安装与使用

Python 3.12 或以上；分发 `decision-os-2`、导入 `decision_kernel`、命令 `decision-os`。`decision-os --version`、`decision-os info --json` 只报告真实安装信息，不表示来源、研究或后台服务已上线。未实现的业务命令拒绝执行。安装包无运行时第三方依赖，不迁入旧研究 schema、数据或隐式任务。

```sh
python -m pip install .
decision-os info --json
python -m decision_kernel --version
```

工程安装使用 `.github/actions/ci-python`：固定 Python 3.12.15、uv 0.12.17、独立虚拟环境；以 `uv pip sync` 消费 `requirements/contracts.txt` 或 `requirements/dev.txt`，再无依赖/无构建隔离安装本项目并执行 `uv pip check`。版本锁包括构建及测试依赖；这是精确版本锁，不是带分发文件哈希的供应链锁。真实远端安装仍需成功，不用本地其他 Python/pytest 版本替代。无持久环境/结果缓存，依赖获取不是业务来源采集。

本地已安装工程依赖后运行 `python -m pytest -q`。`tests/test_distribution.py` 在独立目录构建 wheel，再离线安装进新虚拟环境，从仓库外调用 CLI/导入；不访问 PyPI 发布接口。该测试需要 `uv` 在 PATH 中。

CI 沿 Python/pytest 默认行为，在每个新 runner 内生成临时字节码，让同一分片收集后的 worker 及同次安装的后继 CLI 进程复用编译结果。完整测试仍重新收集并实际执行，断言重写继续；没有保存或恢复跨 run 的字节码、虚拟环境或测试结果。源码复制继续排除 `__pycache__`，冷构建、离线安装、仓库外消费者和 tracked tree 检查保持。这里退出的是全局 `PYTHONDONTWRITEBYTECODE=1` 引起的重复编译，不改变分片算法或合并资格；如需回退可恢复该设置。[pytest 的字节码说明](https://docs.pytest.org/en/stable/how-to/assert.html#assertion-rewriting-caches-files-on-disk)解释此默认行为，性能结果只以相关 PR 的同版本实测为准。

## 实现归属

- `.github/workflows/ci.yml`：原生事件、并发、prepare、合同小环境、八个 runner、最终 `CI Gate`。每个剩余分片由 pytest-split 分配，pytest-xdist 按可用 CPU 启用最多四个 worker；没有自建调度器或影响图。
- `.github/scripts/ci-runtime.py`：Git/Actions 只读适配、安装后实际环境观察、pytest 调用、固定数据文件恢复和最终归档。不执行下载产物里的代码。
- `.github/scripts/ci-evidence.py`：身份、ZIP、集合、环境和原生执行结果的纯检查。失败、取消、缺片、异常 skipped、重复测试或并集缺项不能通过。
- `.github/ci-contracts.txt`：真实合同测试文件清单；prepare 收集全部现役测试后分区，小环境和剩余集合必须互斥且并集完整。空分片只接受实际收集为空及 pytest 的空执行回执，不造测试填片。

测试保护现役安装、路由、证据、工作流和版本锁责任，不因 Issue 关闭而退出。删除能力时同组处理专属工作流、锁、反例、配置与文档；共享保护由剩余工程 owner 接手。

## 四种 scope 与证据

| scope | 资格与实际行为 |
|---|---|
| `content` | 精确 base 已有本仓最新合格 main 证据，Git raw/NUL 差异仅为普通文件 `CHANGELOG.md` 的新增/修改；校验提交字节。规则、方法、README、删除、重命名和模式变化不享有此资格。 |
| `draft_feedback` | 同仓明确 Draft、普通 src/tests 修改且不触及 CI/合同/全局配置；固定安装入口 smoke 加直接变更测试，明确 `merge_eligible=false`。 |
| `full` | 本次完整 collect；独立合同小环境和八片实际执行；每片 JUnit 与 collection 对应，全局精确并集；最终原生 needs 全部成功。 |
| `merge_reuse` | main 首次运行、真实两父普通 merge、同 tree、无策略变化、最新同仓 Ready PR full 原件、环境严格相同；本次仍执行 main smoke，明确复用而非重跑全量。任一条件不成立则新 full。 |

首次没有可信 base，必须由首个代码 PR 自举 full。CI/锁本身变化即使在 Draft 也运行 full，但 Draft 产物不可作为 Ready 合并资格；`ready_for_review` 触发新的正式运行。第一次 main 因策略变化预期 full。content/reuse 未在自然变更中采用时如实标明，不制造空 PR 取证。

每个 job 实测代码 tree、完整 Python、系统/架构、托管镜像标识及实际包版本。新执行仅允许格式合格的镜像批次差异，保留各自观察；小环境只能省包、不能漂移共享版本。main 复用不接受镜像差异。

时长仅为分配提示：优先读取精确 base 的最新一次 main 合格最终产物。未取得合格 base 摘要时，保持其失败原因和 `baseline=None`，只额外解析一次 `base^1`，按相同原生运行、摘要、原件哈希与时长格式规则核第一父提交的最新一次 main 产物；仅采用其中时长，以独立 `timing_fallback` 保留 head/run/attempt/artifact/digest 和仅供分配的标记。父也不合格、无有效时长或读取失败就保持无提示，不继续查祖先，也不跳过同一 head 的最新失败。base 摘要已有效而仅时长文件有误时，保留原 baseline 赋值。此提示不授予 content 或 merge_reuse 资格；独立 merge_reuse 证据覆盖时长时清除该标记。

现役保留八个 runner，使用锁定 pytest-split 的 `least_duration`；分片数跟随证据组，片内使用 pytest-xdist 的 `loadfile` 与 `--no-loadscope-reorder`，保留原文件顺序，避免按测试数量把耗时较长的单测试文件排到末尾。使用 xdist 现成的 `-n auto --maxprocesses=4`，按实际可用 CPU 启用最多四个 worker；同一片中的同文件测试仍在同一 worker，共用原模块夹具。[GitHub 官方规格](https://docs.github.com/actions/using-jobs/choosing-the-runner-for-a-job)中公开仓库的标准 `ubuntu-latest` 为四 CPU，私有仓库为两 CPU；当前锁无 psutil，xdist 沿系统 CPU affinity 检测，实际 worker 数可从每片 `junit.log` 读回。[xdist 文档](https://pytest-xdist.readthedocs.io/en/stable/distribution.html)说明自动检测、上限与文件分组；没有增加 runner、依赖或另建调度器。两 CPU 环境仍使用两个 worker，合同组和实际空片不启用 xdist。后续可按完整 CI 合同继续校准参数；未知新测试照常执行，原 300 秒测试进程时限、完整集合和实际运行门禁不变。需要回退时恢复固定 `-n 2` 并同步步名与本说明，不改变原件或共享保护。

pytest 保留致命信号的 faulthandler，显式 `faulthandler_timeout=0` 停用异步定时线程栈输出；外层测试进程仍有 300 秒硬限，worker 异常退出不重启、不计通过。此前实际 main 的 worker 在 60 秒诊断输出中途失联，原件没有退出码或信号，根因尚未确认（[#10 原始检查点](https://github.com/auguspp/Decision-OS-2/issues/10#issuecomment-6265853675)）。[CPython 的并发 faulthandler 报告](https://github.com/python/cpython/issues/116008)支持停用可选异步诊断的风险缓解；不能据此确认本次解释器根因。代价是较长测试不再自动输出第 60 秒线程栈，原超时、失败和完整证据门禁保持。

产物名绑定 `run_id` 与 `run_attempt`：`dos2-plan-*`、`dos2-part-{contracts,1,2,3,4,5,6,7,8}-*`、`dos2-ci-*`。最终产物保留计划、实际 summary、九组原始 ZIP/API 元数据及时长。下载须通过原生归属、唯一产物、大小、SHA-256、CRC、安全路径和展开上限；只读取固定数据名。最终 full 可重新验证所有原始分片。原生最新失败或 rerun 不被旧成功替代；读取不确定不产生复用资格。

合同与八个分片仍在各自执行目录保留完整 `plan.json` 和 `durations-hint.json`，上传时用固定版本 [upload-artifact 的多路径排除](https://github.com/actions/upload-artifact/blob/043fb46d1a93c77aae656e7c1c64a875d1fc6a0a/README.md#upload-using-multiple-paths-and-exclusions)仅省略这两份重复公共数据；prepare 与最终产物继续保留公共计划和时长。各片自己的收集结果、JUnit、实际环境、结果与全部诊断日志仍上传，结果中的 `timing_digest` 继续逐片核对提示。排除路径不改变产物根目录，原件仍按实际大小与摘要验证；没有删本地执行输入或放宽失败与完整集合检查。需要回退时将两个分片上传路径恢复为整个目录，不改变历史产物或 reader。

最终 gate 在必需 jobs 成功后，只读取一次本 run 的完整原生产物目录，供 plan 与九组分片共同使用；原来的十次相同目录读取减为一次。每件原件仍按固定 artifact ID 下载并逐件验证；目录缺项、重名、不完整，目录读取后原 ID 被删除导致的下载失败，以及下载字节与原摘要不符均会失败，不重新按名称寻找替代物。目录只存于本次 gate 调用，路由、baseline 和 merge_reuse 的读取及最新运行复核保持独立。[GitHub 的 artifact 迁移说明](https://github.com/actions/upload-artifact/blob/main/docs/MIGRATION.md)说明新版本产物不可变，覆盖会删除旧 ID 并产生新 ID。此窄适配由现役 CI owner 维护，可恢复逐件目录读取；不会删除原件/历史或退出共享身份、集合和失败保护。

仅当 prepare 已输出确切 `content` scope 时，Gate 与 prepare 一样跳过 `ci-python` 安装，使用 runner 自带 Python 标准库核原生 needs、plan 身份、ZIP、内容检查与可信 base。`full`、`draft_feedback`、`merge_reuse` 及 prepare 没有输出 scope 的失败情况仍走原锁定安装；pytest 只由需要检查测试结果的路径加载。此条件不扩大内容资格，也不省略最终 Gate。现役路由测试以隔离、禁用 site-packages 的独立 Python 进程实际调用 content Gate，保留坏摘要拒绝；自然 content 采用仍以正常运行记录为准，不制造空 PR。回退可移除 Gate 安装条件；由现役 CI owner 维护，能力退出时连同该消费者验证处理。

GH_TOKEN 仅给必要 Git/Actions 只读步骤，不给安装、collect 或测试；checkout 不保留凭据。CI 不接收业务 secrets，不配置外发/研究任务。最终 gate 的 `always()` 只保证验证失败路径也被执行，不把前置失败转成功。原始失败日志保留，不循环 rerun 换绿。

## 复用与来源

固定技术起点为用户同一账户的旧仓 [decision-kernel@cb3bbc87](https://github.com/auguspp/decision-kernel/tree/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8)。按本次明确工程迁入授权使用；不是因为仓库公开就推定任意重许可。读取的旧根目录与 pyproject 未提供项目许可证声明，本批不补造 MIT/其他项目许可证。外部组件正常安装/调用、不把其源码移入本仓；其各自许可义务仍保留。

| 旧定位 | 新 owner / 保留与差异 |
|---|---|
| `docs/CI-MAINLINE.md`、`.github/workflows/ci-full-v2.yml` | 本仓工作流；从四 scope、原生矩阵与旧 4×2 起点接续，现役八个 runner、每片按 CPU 最多四 worker，重绑 repo/event/head/artifact；不重跑旧性能实验。 |
| `.github/scripts/ci-content-scope.py` | runtime 路由；保留 raw/NUL 精确差异、可信 base、Draft/Ready 分离；内容范围缩到本仓真实非规范 CHANGELOG，不带旧研究文件例外。 |
| `.github/scripts/ci-merge-reuse.py` | evidence 与 runtime 适配；提炼环境、JUnit、ZIP、严格原生 merge 关联；退出旧单体/旧分组 codec 和旧研究身份。 |
| `.github/scripts/ci-matrix.py` | runtime 固定数据恢复和 full 汇总；仍由 pytest-split 分配，证据只检查不调度；显式支持真实空分片。 |
| `.github/actions/ci-python/action.yml`、旧 pyproject | 固定 action SHA、Python/uv 和已选 pytest/xdist/split 起点；新仓显式版本锁、隔离环境及实际安装；不复制旧业务 extras。 |

Python CLI 使用标准库 argparse，打包使用 setuptools；CI 使用 GitHub Actions、pytest、pytest-split、pytest-xdist 和 uv 的现成能力。没有因“更灵活”自建通用平台。新包没有业务消费者的部分不预建领域对象或存储接口。

## 原生设置与回退

按用户已采纳的[CI 合同第5节](ci-contract.md#settings)，本仓不启用主线保护，也不通过 ruleset 或 required-check 设置变相启用。`CI Gate` 仍必须实际执行并按当前 head 核验；main 经 PR、普通 merge，force/delete 及自动关 Issue 设置按专项合同处理。工作流不能自称这些管理设置已经生效；具体配置成功/拒绝/工具缺口在 #5 留真实回执。继续使用无自动关闭词的 `Refs` 关联，不宣传主线强保护。

回退是对本责任组提交做普通反向 PR；不 force main，不改旧系统或私人数据。删除 CI 能力不能留下指向不存在 check 的保护配置，须由获准管理员同步处理并读回；已生成运行/失败/产物不能因代码回退被宣传为撤销。工程交付不签研究质量或人工接受。
