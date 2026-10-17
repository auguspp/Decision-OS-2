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
        return text(f"{r['id']} / attempt {r['run_attempt']} / {r['conclusion'] or r['status']}")
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
