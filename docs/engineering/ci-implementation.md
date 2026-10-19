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

## 实现归属

- `.github/workflows/ci.yml`：原生事件、并发、prepare、合同小环境、四个 runner、最终 `CI Gate`。每个剩余分片由 pytest-split 分配，pytest-xdist 执行两个 worker；没有自建调度器或影响图。
- `.github/scripts/ci-runtime.py`：Git/Actions 只读适配、安装后实际环境观察、pytest 调用、固定数据文件恢复和最终归档。不执行下载产物里的代码。
- `.github/scripts/ci-evidence.py`：身份、ZIP、集合、环境和原生执行结果的纯检查。失败、取消、缺片、异常 skipped、重复测试或并集缺项不能通过。
- `.github/ci-contracts.txt`：真实合同测试文件清单；prepare 收集全部现役测试后分区，小环境和剩余集合必须互斥且并集完整。空分片只接受实际收集为空及 pytest 的空执行回执，不造测试填片。

测试保护现役安装、路由、证据、工作流和版本锁责任，不因 Issue 关闭而退出。删除能力时同组处理专属工作流、锁、反例、配置与文档；共享保护由剩余工程 owner 接手。

## 四种 scope 与证据

| scope | 资格与实际行为 |
|---|---|
| `content` | 精确 base 已有本仓最新合格 main 证据，Git raw/NUL 差异仅为普通文件 `CHANGELOG.md` 的新增/修改；校验提交字节。规则、方法、README、删除、重命名和模式变化不享有此资格。 |
| `draft_feedback` | 同仓明确 Draft、普通 src/tests 修改且不触及 CI/合同/全局配置；固定安装入口 smoke 加直接变更测试，明确 `merge_eligible=false`。 |
| `full` | 本次完整 collect；独立合同小环境和四片实际执行；每片 JUnit 与 collection 对应，全局精确并集；最终原生 needs 全部成功。 |
| `merge_reuse` | main 首次运行、真实两父普通 merge、同 tree、无策略变化、最新同仓 Ready PR full 原件、环境严格相同；本次仍执行 main smoke，明确复用而非重跑全量。任一条件不成立则新 full。 |

首次没有可信 base，必须由首个代码 PR 自举 full。CI/锁本身变化即使在 Draft 也运行 full，但 Draft 产物不可作为 Ready 合并资格；`ready_for_review` 触发新的正式运行。第一次 main 因策略变化预期 full。content/reuse 未在自然变更中采用时如实标明，不制造空 PR 取证。

每个 job 实测代码 tree、完整 Python、系统/架构、托管镜像标识及实际包版本。新执行仅允许格式合格的镜像批次差异，保留各自观察；小环境只能省包、不能漂移共享版本。main 复用不接受镜像差异。

时长仅为分配提示：优先读取精确 base 的最新一次 main 合格最终产物。未取得合格 base 摘要时，保持其失败原因和 `baseline=None`，只额外解析一次 `base^1`，按相同原生运行、摘要、原件哈希与时长格式规则核第一父提交的最新一次 main 产物；仅采用其中时长，以独立 `timing_fallback` 保留 head/run/attempt/artifact/digest 和仅供分配的标记。父也不合格、无有效时长或读取失败就保持无提示，不继续查祖先，也不跳过同一 head 的最新失败。base 摘要已有效而仅时长文件有误时，保留原 baseline 赋值。此提示不授予 content 或 merge_reuse 资格；独立 merge_reuse 证据覆盖时长时清除该标记。仍使用现有 pytest-split 算法、4×2 与执行时限，未知新测试照常执行，最终集合和实际运行门禁不变。

产物名绑定 `run_id` 与 `run_attempt`：`dos2-plan-*`、`dos2-part-{contracts,1,2,3,4}-*`、`dos2-ci-*`。最终产物保留计划、实际 summary、五组原始 ZIP/API 元数据及时长。下载须通过原生归属、唯一产物、大小、SHA-256、CRC、安全路径和展开上限；只读取固定数据名。最终 full 可重新验证所有原始分片。原生最新失败或 rerun 不被旧成功替代；读取不确定不产生复用资格。

GH_TOKEN 仅给必要 Git/Actions 只读步骤，不给安装、collect 或测试；checkout 不保留凭据。CI 不接收业务 secrets，不配置外发/研究任务。最终 gate 的 `always()` 只保证验证失败路径也被执行，不把前置失败转成功。原始失败日志保留，不循环 rerun 换绿。

## 复用与来源

固定技术起点为用户同一账户的旧仓 [decision-kernel@cb3bbc87](https://github.com/auguspp/decision-kernel/tree/cb3bbc87eeeeadb6bf24d7adc8cc214cd80ab9b8)。按本次明确工程迁入授权使用；不是因为仓库公开就推定任意重许可。读取的旧根目录与 pyproject 未提供项目许可证声明，本批不补造 MIT/其他项目许可证。外部组件正常安装/调用、不把其源码移入本仓；其各自许可义务仍保留。

| 旧定位 | 新 owner / 保留与差异 |
|---|---|
| `docs/CI-MAINLINE.md`、`.github/workflows/ci-full-v2.yml` | 本仓工作流；保留四 scope、原生矩阵与 4×2，重绑 repo/event/head/artifact；不重跑旧性能实验。 |
| `.github/scripts/ci-content-scope.py` | runtime 路由；保留 raw/NUL 精确差异、可信 base、Draft/Ready 分离；内容范围缩到本仓真实非规范 CHANGELOG，不带旧研究文件例外。 |
| `.github/scripts/ci-merge-reuse.py` | evidence 与 runtime 适配；提炼环境、JUnit、ZIP、严格原生 merge 关联；退出旧单体/旧分组 codec 和旧研究身份。 |
| `.github/scripts/ci-matrix.py` | runtime 固定数据恢复和 full 汇总；仍由 pytest-split 分配，证据只检查不调度；显式支持真实空分片。 |
| `.github/actions/ci-python/action.yml`、旧 pyproject | 固定 action SHA、Python/uv 和已选 pytest/xdist/split 起点；新仓显式版本锁、隔离环境及实际安装；不复制旧业务 extras。 |

Python CLI 使用标准库 argparse，打包使用 setuptools；CI 使用 GitHub Actions、pytest、pytest-split、pytest-xdist 和 uv 的现成能力。没有因“更灵活”自建通用平台。新包没有业务消费者的部分不预建领域对象或存储接口。

## 原生设置与回退

`CI Gate` 必须以本仓实际 check-run 名称和实际来源读回后配置 required check；main 经 PR、普通 merge、force/delete 及自动关 Issue 设置按专项合同处理。工作流不能自称这些管理设置已经生效；具体配置成功/拒绝/工具缺口在 #5 留真实回执。未配置前继续使用无自动关闭词的 `Refs #5`，不得宣传主线强保护。

回退是对本责任组提交做普通反向 PR；不 force main，不改旧系统或私人数据。删除 CI 能力不能留下指向不存在 check 的保护配置，须由获准管理员同步处理并读回；已生成运行/失败/产物不能因代码回退被宣传为撤销。工程交付不签研究质量或人工接受。
