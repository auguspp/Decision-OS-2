"""Bounded NBS material-price parsing and mechanical changes, without I/O."""
from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, localcontext
from html.parser import HTMLParser
import re

from decision_kernel.domain.records import RecordError, digest, encoded, require

ROOT = "https://www.stats.gov.cn/sj/zxfb/"
CATALOGUES = (ROOT, ROOT + "index_1.html", ROOT + "index_2.html")
MAX_BODY = 4 * 1024 * 1024
TITLE = re.compile(r"(20\d{2})年(\d{1,2})月([上中下])旬流通领域重要生产资料市场价格变动情况")
RULE = {"version": "nbs-material-change-v1", "threshold_pct": "3",
        "max_period_age_days": 20, "window": "adjacent-official-ten-day-periods",
        "sustained": "two-same-direction-changes-and-total-absolute-change-at-least-threshold"}
ATTRIBUTION = "数值引自国家统计局网站 https://www.stats.gov.cn；机械计算不代表公司受益。"
CHINA = timezone(timedelta(hours=8))


def text(value):
    return "".join(value.split())


def period(title):
    match = TITLE.fullmatch(text(title))
    require(match is not None, "MATERIAL_RELEASE_TITLE")
    year, month, part = match.groups()
    year, month = int(year), int(month)
    try:
        start, end = {"上": (1, 10), "中": (11, 20),
                      "下": (21, monthrange(year, month)[1])}[part]
        return date(year, month, start).isoformat(), date(year, month, end).isoformat()
    except ValueError:
        raise RecordError("MATERIAL_PERIOD") from None


def adjacent(before, after):
    return date.fromisoformat(before["period_end"]) + timedelta(days=1) == date.fromisoformat(after["period_start"])


def allowed(url):
    require(isinstance(url, str) and (url in CATALOGUES or
            re.fullmatch(re.escape(ROOT) + r"20\d{4}/t20\d{6}_\d+\.html", url)),
            "MATERIAL_URL_OUTSIDE_SCOPE")
    return url


class _Page(HTMLParser):
    """Only links, metadata, and bounded flat tables; no script execution."""

    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_BODY, "MATERIAL_BODY_BOUND")
        self.links, self.meta, self.tables, self.parts = [], {}, [], []
        self.anchor, self.href, self.table, self.row, self.cell = "", None, None, None, None
        self.skipping = 0
        try:
            self.feed(raw.decode("utf-8-sig"))
            self.close()
        except (UnicodeError, RecursionError):
            raise RecordError("MATERIAL_HTML_ENCODING") from None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in ("script", "style"):
            self.skipping += 1
        if tag == "meta" and attrs.get("name") in ("ArticleTitle", "PubDate"):
            key, value = attrs["name"], attrs.get("content", "")
            require(key not in self.meta or self.meta[key] == value, "MATERIAL_META_CONFLICT")
            self.meta[key] = value
        if tag == "a":
            self.href, self.anchor = attrs.get("href"), ""
        if tag == "table":
            require(self.table is None, "MATERIAL_NESTED_TABLE")
            self.table = []
        if tag == "tr":
            self.row = []
        if tag in ("td", "th"):
            self.cell = ""

    def handle_data(self, value):
        if self.skipping:
            return
        self.parts.append(value)
        if self.href is not None:
            self.anchor += value
        if self.cell is not None:
            self.cell += value

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skipping = max(0, self.skipping - 1)
        if tag == "a" and self.href is not None:
            self.links.append((text(self.anchor), self.href))
            self.href = None
        if tag in ("td", "th") and self.cell is not None:
            if self.row is not None:
                self.row.append(text(self.cell))
                require(len(self.row) <= 16, "MATERIAL_TABLE_BOUND")
            self.cell = None
        if tag == "tr" and self.row is not None:
            if self.table is not None:
                self.table.append(self.row)
                require(len(self.table) <= 256, "MATERIAL_TABLE_BOUND")
            self.row = None
        if tag == "table" and self.table is not None:
            self.tables.append(self.table)
            require(len(self.tables) <= 32, "MATERIAL_TABLE_BOUND")
            self.table = None


def discover(raw, url):
    from urllib.parse import urljoin
    allowed(url)
    results = {}
    for title, href in _Page(raw).links:
        if TITLE.fullmatch(title):
            target = allowed(urljoin(url, href))
            require(target not in results or results[target] == title, "MATERIAL_LINK_CONFLICT")
            results[target] = title
    return [{"url": key, "title": value} for key, value in results.items()]


def number(value, *, missing=False):
    if missing and value in ("", "-", "—", "...", "…"):
        return None
    require(isinstance(value, str) and len(value) <= 32
            and re.fullmatch(r"[+-]?\d+(?:\.\d+)?", value), "MATERIAL_NUMBER")
    return format(Decimal(value), "f")


def parse_release(raw, *, url, title, received_at):
    allowed(url)
    page = _Page(raw)
    require(text(page.meta.get("ArticleTitle", "")) == title, "MATERIAL_TITLE_DIFFERS")
    start, end = period(title)
    try:
        published = datetime.strptime(page.meta["PubDate"], "%Y/%m/%d %H:%M").replace(
            tzinfo=CHINA)
        acquired = datetime.fromisoformat(received_at)
        require(acquired.tzinfo is not None and published <= acquired
                and date.fromisoformat(end) <= published.date(), "MATERIAL_SOURCE_CLOCK")
    except (KeyError, ValueError, TypeError):
        raise RecordError("MATERIAL_SOURCE_CLOCK") from None
    content = text("".join(page.parts))
    require(all(s in content for s in ("流通费用、利润和税费", "50种", "上期为")),
            "MATERIAL_METHODOLOGY_CHANGED")
    previous = re.findall(r"上期为(20\d{2}年\d{1,2}月[上中下]旬)", content)
    require(bool(previous) and len(set(previous)) == 1, "MATERIAL_PREVIOUS_PERIOD")
    old_start, old_end = period(previous[0] + "流通领域重要生产资料市场价格变动情况")
    require(date.fromisoformat(old_end) + timedelta(days=1) == date.fromisoformat(start),
            "MATERIAL_PREVIOUS_PERIOD")
    selected = [t for t in page.tables if t and len(t[0]) == 5
                and t[0] == ["产品名称", "单位", "本期价格（元）", "比上期价格涨跌（元）", "涨跌幅（%）"]]
    require(bool(selected) and all(t == selected[0] for t in selected), "MATERIAL_PRICE_TABLE_CHANGED")
    rows, category = [], None
    for row in selected[0][1:]:
        if len(row) == 5 and re.match(r"^[一二三四五六七八九十]+、", row[0]):
            require(not any(row[1:]), "MATERIAL_CATEGORY_CHANGED")
            category = row[0]
            continue
        if len(row) == 1 and row[0].startswith("注：上期为"):
            continue
        require(len(row) == 5 and category is not None and row[0]
                and row[1] in ("吨", "千克", "立方米"), "MATERIAL_PRODUCT_ROW_CHANGED")
        name, unit, price, change, pct = row
        value = number(price, missing=True)
        require(value is None or Decimal(value) > 0, "MATERIAL_NONPOSITIVE_PRICE")
        identity = "nbs-material-" + digest(encoded([category, name, unit]))[:24]
        rows.append({"subject_id": identity, "name": name, "category": category,
                     "unit": "CNY/" + unit, "value": value,
                     "reported_delta": number(change, missing=True),
                     "reported_percent_change": number(pct, missing=True)})
    require(1 <= len(rows) <= 50 and len({r["subject_id"] for r in rows}) == len(rows),
            "MATERIAL_PRODUCT_COUNT")
    return {"url": url, "title": title, "published_at": published.isoformat(),
            "received_at": received_at, "response_sha256": digest(raw), "response_bytes": len(raw),
            "period_start": start, "period_end": end, "previous_period_start": old_start,
            "previous_period_end": old_end, "rows": rows,
            "basis": {"currency": "CNY", "region": "China-NBS-circulation-survey",
                      "price_kind": "survey", "market": "spot", "contract": "not-applicable",
                      "tax_basis": "includes-circulation-cost-profit-and-taxes",
                      "frequency": "ten-day", "period_kind": "price-level",
                      "assessment": "period-average",
                      "metric": "circulation-wholesale-and-sales-price",
                      "method": "NBS-circulation-survey-not-ex-factory-or-executed-price"}}


def _delta(before, after):
    if before is None or after is None:
        return None
    with localcontext() as context:
        context.prec = 80
        a, b = Decimal(before), Decimal(after)
        require(a > 0 and b > 0, "MATERIAL_NONPOSITIVE_PRICE")
        return {"delta": format(b - a, "f"), "percent_change": format((b / a - 1) * 100, "f")}


def monitor(releases, *, as_of, receipts, predecessor=None):
    """Adjacent observed prices only; reported deltas never fabricate history."""
    now = datetime.fromisoformat(as_of)
    require(now.tzinfo is not None, "MATERIAL_TIMEZONE")
    require(len(releases) <= 3, "MATERIAL_RELEASE_BOUND")
    ordered = sorted(releases, key=lambda r: r["period_end"])
    require(len({r["period_end"] for r in ordered}) == len(ordered), "MATERIAL_PERIOD_CONFLICT")
    latest = ordered[-1] if ordered else None
    previous = ordered[-2] if len(ordered) > 1 else None
    older = ordered[-3] if len(ordered) > 2 else None
    previous_rows = {r["subject_id"]: r for r in previous["rows"]} if previous else {}
    older_rows = {r["subject_id"]: r for r in older["rows"]} if older else {}
    age = (now.astimezone(CHINA).date() - date.fromisoformat(latest["period_end"])).days if latest else None
    require(age is None or age >= 0, "MATERIAL_FUTURE_PERIOD")
    fresh = age is not None and age <= RULE["max_period_age_days"]
    old = predecessor["releases"][-1] if predecessor and predecessor["releases"] else None
    transition = ("bootstrap-existing-release" if old is None else "source-unavailable" if latest is None
                  else "new-statistical-period" if latest["period_end"] > old["period_end"]
                  else "source-period-regressed" if latest["period_end"] < old["period_end"]
                  else "same-period-revision" if any(latest[k] != old[k] for k in ("rows", "basis"))
                  else "same-period-page-change" if latest["response_sha256"] != old["response_sha256"]
                  else "unchanged-release")
    results = []
    for row in latest["rows"] if latest else []:
        prior = previous_rows.get(row["subject_id"])
        comparable = (previous is not None and adjacent(previous, latest)
                      and previous["basis"] == latest["basis"] and prior is not None)
        delta = _delta(prior["value"], row["value"]) if comparable else None
        sustained = None
        sustained_trigger = False
        sustained_comparison = None
        if delta and older and adjacent(older, previous) and older["basis"] == previous["basis"]:
            first = older_rows.get(row["subject_id"])
            earlier = _delta(first["value"], prior["value"]) if first else None
            if earlier and Decimal(earlier["delta"]) * Decimal(delta["delta"]) > 0:
                sustained = "up" if Decimal(delta["delta"]) > 0 else "down"
                total = _delta(first["value"], row["value"])
                sustained_comparison = {"from_period": older["period_end"],
                                        "to_period": latest["period_end"], **total}
                sustained_trigger = abs(Decimal(total["percent_change"])) >= Decimal(RULE["threshold_pct"])
        magnitude = Decimal(delta["percent_change"]) if delta else None
        trigger = delta is not None and abs(magnitude) >= Decimal(RULE["threshold_pct"])
        eligible = fresh and transition != "source-period-regressed"
        results.append({**row, "from_period": previous["period_end"] if previous else None,
                        "to_period": latest["period_end"], "comparison": delta,
                        "comparison_status": "comparable" if delta else "missing-or-incomparable",
                        "direction": None if magnitude is None else "up" if magnitude > 0 else "down" if magnitude < 0 else "flat",
                        "sustained_direction": sustained, "triggered": eligible and (trigger or sustained_trigger),
                        "sustained_comparison": sustained_comparison,
                        "trigger_reason": ("absolute-percent-threshold" if trigger else "sustained-total-threshold"
                                           if sustained_trigger else None) if eligible else None,
                        "source_url": latest["url"], "source_sha256": latest["response_sha256"],
                        "unresolved": ["Company business share, volumes, contracts, hedging and transmission remain unverified."]})
    comparable = sum(r["comparison"] is not None for r in results)
    healthy = (len(results) == 50 and comparable == 50 and fresh
               and all(r["status"] == "captured" for r in receipts)
               and transition != "source-period-regressed")
    triggers = sum(r["triggered"] for r in results)
    return {"schema": "nbs-material-monitor-v1", "as_of": as_of, "rules": RULE,
            "attribution": ATTRIBUTION, "transition": transition,
            "new_to_capture": transition in ("new-statistical-period", "same-period-revision"),
            "status": "triggers" if healthy and triggers else "no-trigger" if healthy else "partial-or-unavailable",
            "freshness": {"period_age_days": age, "within_rule_window": fresh,
                          "meaning": "declared-age-rule-not-proof-of-latest-publication"},
            "coverage": {"target_products": 50, "current_products": len(results),
                         "comparable_products": comparable,
                         "fresh_monitored_products": comparable if fresh else 0,
                         "triggered_products": triggers, "other_source_families": "not-connected"},
            "rows": results, "receipts": receipts, "releases": ordered,
            "authority": {"company_benefit_established": False, "research_started": False,
                          "notification_sent": False, "investment_authority": "NONE"}}
