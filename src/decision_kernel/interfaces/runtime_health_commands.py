"""Explicit metadata health check on the existing storage operations host."""
from pathlib import Path

from decision_kernel.application.runtime_health import inspect_runtime
from decision_kernel.application.news_delivery import _escape_md
from decision_kernel.domain.records import RecordError, encoded
from decision_kernel.infrastructure.github_store import GitHubBinding


def configure(commands):
    parser = commands.add_parser("runtime-health", help="read native delivery runs and artifact expiry; no restart or timer")
    parser.add_argument("--binding", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="new local report directory")
    parser.add_argument("--backup-run-id", type=int, help="explicit completed backup run; never inferred from a green health run")
    parser.add_argument("--as-of", help="explicit timezone-aware evaluation clock, otherwise actual completion time")
    parser.add_argument("--expected-head", help="optional already observed records head; not a write permission")


def markdown(report):
    def text(value):
        return _escape_md(str(value).replace("\n", " ").replace("\r", " "))
    def run(detail):
        if not detail:
            return "未取得"
        r = detail["run"]
        return text(f"{r['id']} / attempt {r['run_attempt']} / {r['event']} / {r['conclusion'] or r['status']}")
    lines = ["# Decision OS · 交付运行巡检", "", "评估时刻：" + text(report["as_of"]), "",
             "只核原生执行元数据；不签业务内容、数据新鲜度、持久备份或生产启用。", "",
             "| 宿主 | 当前观察 | 最近一次尝试 | 最近可见成功 | 最近产物 |",
             "|---|---|---|---|---|"]
    for p in report["workflows"]:
        d = p.get("latest") or {}
        lines.append("| " + " | ".join((text(p["path"]), text(p["status"]), run(p.get("latest")),
                                      run(p.get("last_visible_success")), text(d.get("artifact_state", "未知")))) + " |")
    for p in report["workflows"]:
        lines += ["", "## " + text(p["path"])]
        if p["status"] == "unknown":
            lines.append("本宿主无法核验：" + text(p["error"])); continue
        scope = p["recent_scope"]
        lines += [f"原生工作流状态：{text(p['workflow_state'])}；active只说明可响应触发，不代表已配置定时。",
                  f"本次返回 {scope['returned']} / {scope['total']} 个main运行；只核最近{scope['limit']}个，老运行及历史重试可能在范围外。",
                  f"本范围观察到 {len(p['observed_active'])} 个未结束运行；不是全局无写入者证明。"]
        for title, key in (("最近尝试", "latest"), ("最近可见成功（非业务验收）", "last_visible_success")):
            d = p.get(key)
            if not d:
                lines.append(title + "：未取得，不能回填零变化。"); continue
            r = d["run"]
            lines += [f"{title}：{run(d)}；启动 {text(r['run_started_at'])}；代码 {text(r['head_sha'])}。",
                      "原生定位：" + r["url"],
                      "作业：" + text(", ".join(f"{j['name']}={j['conclusion'] or j['status']}" for j in d["jobs"]))]
            a = d["artifact"]
            if a:
                lines.append(f"产物 {text(a['name'])}：{text(d['artifact_state'])}；到期 {text(a['expires_at'])}；{a['size_in_bytes']} 字节。仅元数据存在，未下载验字节。")
            else:
                lines.append("没有取得当前attempt对应的产物；不借用旧attempt附件充数。")
        clock_policy = p.get("clock_policy")
        if clock_policy:
            lines += ["", "### 日更主时钟选择", ""]
            if "primary" in clock_policy:
                lines += ["当前主时钟：" + text(clock_policy["primary"]) + "；配置只声明仓库准入，不证明外部账户已配置或启用。",
                          f"固定main {text(clock_policy['main'])} / {text(clock_policy['path'])}；Git blob {text(clock_policy['git_blob'])}；SHA256 {text(clock_policy['sha256'])}。"]
            else:
                lines.append("时钟策略无法核验：" + text(clock_policy.get("error", "unknown")))
        schedule = p.get("schedule")
        if schedule:
            lines += ["", "### 日更自然 schedule 观察", "",
                      "当前观察：" + text(schedule["status"]) + "。手动status、update或恢复成功不填补自然触发证据。"]
            if "error" in schedule:
                lines.append("核验限制：" + text(schedule["error"]))
            if "policy" in schedule:
                policy = schedule["policy"]
                lines += [f"策略固定到main {text(policy['main'])} / {text(policy['path'])}；Git blob {text(policy['git_blob'])}；SHA256 {text(policy['sha256'])}。",
                          f"已采用cron {text(policy['schedule_utc'])}；上海名义时刻 {text(policy['not_before_local'])}；enabled={text(policy['enabled'])}。",
                          "这里只按本次固定main策略解释原生时钟；历史as-of不是以前部署策略或自然验收的证明。",
                          f"观察时钟 {text(schedule['observation_started_at'])} → {text(schedule['as_of'])}；{text(schedule['clock_basis'])}。"]
                slot = schedule["expected_slot"]
                if slot:
                    lines.append(f"最近有效槽：业务日 {text(slot['business_date'])}；名义 {text(slot['scheduled_for'])}；距名义时点 {text(slot['delay_seconds'])} 秒。")
                else:
                    lines.append("当前无不足24小时的已过工作日槽；这不表示旧缺口已经恢复或从未发生。")
                scope = schedule["recent_scope"]
                lines.append(f"独立event=schedule页返回 {scope['returned']} / {scope['total']}，上限{scope['limit']}；完整历史={text(scope['complete_history'])}，目标槽覆盖={text(scope['expected_slot_covered'])}。")
                for title, key in (("最近可见自然尝试", "latest"), ("最近可见自然成功", "last_visible_success"),
                                   ("最近可见自然非成功", "last_visible_non_success")):
                    item = schedule[key]
                    if item:
                        r = item["run"]; admitted = item["admission_slot"]
                        lines += [f"{title}：{run(item)}；原生创建 {text(r['created_at'])}；代码 {text(r['head_sha'])}；按created_at归槽 {text(admitted['business_date'] if admitted else '无有效槽')}。",
                                  "原生定位：" + r["url"]]
                    else:
                        lines.append(title + "：本页未取得。")
                lines += ["目标槽原生运行：" + (text(", ".join(str(item["run"]["id"]) for item in schedule["current_slot_runs"])) or "未观察到"),
                          "自然子节只核原生运行摘要，不另读任务payload、jobs、附件或业务正文；成功不代签行情更新、Watch或Brief。",
                          "未观察到不推断GitHub未触发、排队丢弃或其他根因；24小时只是原入场限制，不是平台永久丢失的证明。"]
        external = p.get("external_entry")
        if external:
            primary = (clock_policy or {}).get("primary")
            lines += ["", "### 外部时钟入口观察", "",
                      "当前观察：" + text(external["status"]) + "；" + ("被选为主时钟。" if primary == "external-cron" else "当前不作为主时钟验收依据。"),
                      "固定run-name只标识外部入口请求；不证明cron-job.org、其他服务商或自然触发来源。外部配置与自然来源未核验；probe不能代替生产。"]
            if "error" in external:
                lines.append("核验限制：" + text(external["error"]))
            if "recent_scope" in external:
                scope = external["recent_scope"]
                lines.append(f"独立event=workflow_dispatch页返回 {scope['returned']} / {scope['total']}，入口匹配 {external['matched_entries']}；完整历史={text(scope['complete_history'])}，目标槽覆盖={text(scope['expected_slot_covered'])}。覆盖按完整dispatch页计算，其他手动操作不会被算作外部生产。")
                for title, key in (("最近可见外部入口尝试", "latest"), ("最近可见入口原生成功（未代签业务）", "last_visible_success"),
                                   ("最近可见入口非成功", "last_visible_non_success")):
                    item = external[key]
                    lines.append(title + "：" + run(item) + ("；" + item["run"]["url"] if item else "。"))
                detail = external["latest_detail"]
                if detail:
                    lines.append("最近入口作业元数据：" + text(", ".join(f"{j['name']}={j['conclusion'] or j['status']}" for j in detail["jobs"])) + "；只核此运行，不下载附件或业务正文。")
                probe = external["latest_probe"]
                lines.append("最近probe：" + (run({"run": probe}) if probe else "本页未取得") + "；probe不进入生产成功计数。")
                lines.append("只有当前main、目标槽与实际daily作业成功均经核验，才报告daily-job-success；其含义仍不包括外部自然来源、业务验收或历史缺口恢复。")
    b = report["specified_backup"]
    lines += ["", "## 指定备份（独立于巡检运行）", "",
              "状态：" + text(b["status"])]
    if "run" in b:
        lines.append("确切运行：" + run(b) + "；作业：" + text(b["observed_operation"]))
        a = b["artifact"]
        lines.append("产物：" + text(b["artifact_state"]) + ("；到期：" + text(a["expires_at"]) if a else ""))
    else:
        lines.append("未核到指定成功备份；不从巡检成功推断。")
    if report["excluded_inspection"]:
        lines.append("当前巡检自身仅按确切run/attempt排除：" + text(report["excluded_inspection"]))
    lines += ["", "## 启用与处置边界", "",
              "本检查不执行重试、取消、启停、通知、业务保存或切换。新失败不能被上一成功覆盖；失败后的未知写先按原意图对账。",
              "备份存在不证明覆盖当前records或已有独立持久副本；需沿原报告固定快照、摘要及recover-room核对。",
              "无定时目标时不把运行年龄自动判成服务超时；已启用周期和恢复目标另按用户确认的策略验收。",
              "生产启用清单和备份方案见 docs/engineering/production-readiness.md。"]
    return "\n".join(lines) + "\n"


def execute(binding: GitHubBinding, api, output: Path, *, as_of=None, expected_head=None, backup_run_id=None, self_run=None):
    # Never overwrite an old report. On failure keep a small explicit unknown
    # receipt, not partial successful rows after a final permission failure.
    try:
        output.mkdir(mode=0o700, parents=False, exist_ok=False)
    except OSError:
        raise RecordError("HEALTH_NEW_OUTPUT_REQUIRED") from None
    try:
        report = inspect_runtime(binding, api, as_of=as_of, expected_head=expected_head,
                                 backup_run_id=backup_run_id, self_run=self_run)
    except (RecordError, OSError) as error:
        code = str(error) if isinstance(error, RecordError) else "HEALTH_IO_FAILURE"
        (output / "health.json").write_bytes(encoded({"schema": "runtime-health-v1", "status": "unknown",
            "error": code, "writes": 0, "dispatches": 0, "production_acceptance": "not-established"}))
        raise RecordError(code) from None
    (output / "health.json").write_bytes(encoded(report))
    (output / "health.md").write_text(markdown(report), encoding="utf-8")
    return report
