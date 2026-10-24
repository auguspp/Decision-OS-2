"""单证券按需计划的纯接收与只读恢复；不保管、不预留、不派发。"""
from copy import deepcopy
import re

from decision_kernel.application import ftshare_collection as ft
from decision_kernel.application.market_custody import market_read_scope
from decision_kernel.application.relay_registry import RelayRegistry
from decision_kernel.domain.records import checked_text, digest, encoded, identifier, require


REQUEST_SCHEMA = "ftshare-on-demand-request-v1"


def plan_ftshare_on_demand(request, registry, store, *, created_at, protected_symbols=()):
    """只做计划或只读恢复；本身不保管、不预留、不派发。

    现役入口复用同一 runtime 的保管与单次请求路径，因此本函数返回的计划
    仍须经该路径的权限、预留与硬余量核验才会成行。
    request 精确包含 schema/id/base_reference/symbol/adjust_kind/reason；同一
    collection 内 id 的内容固定，generation 不随 created_at 或字典键序改变。
    新计划只在完整成功的冻结前驱上增加一个 eligible 证券的一种复权 slot，
    source_budget 固定为 1；protected_symbols 必须由可信宿主显式绑定。
    恢复保留原时钟、状态及 reference，忽略本次 created_at，不重开额度。
    返回的 source_requests 恒为 0；宿主仍须另行保管、对账和授权执行。
    """
    require(isinstance(request, dict) and set(request) == {
        "schema", "id", "base_reference", "symbol", "adjust_kind", "reason"
    } and request["schema"] == REQUEST_SCHEMA, "FTSHARE_ON_DEMAND_REQUEST")
    identifier(request["id"])
    checked_text(request["reason"], limit=1024)
    target = {key: request[key] for key in ("symbol", "adjust_kind")}
    ft._single_targets([target])
    require(isinstance(registry, RelayRegistry), "FTSHARE_COLLECTION_REGISTRY_REQUIRED")
    require(isinstance(protected_symbols, (list, tuple)) and len(protected_symbols) <= ft.MAX_REQUESTS
        and all(isinstance(symbol, str) and re.fullmatch(r"[0-9]{6}\.(SH|SZ)", symbol)
                for symbol in protected_symbols)
        and len(protected_symbols) == len(set(protected_symbols)), "FTSHARE_EXPANSION_PROTECTED_SYMBOLS")
    require(request["symbol"] not in protected_symbols, "FTSHARE_EXPANSION_PROTECTED_SYMBOL")
    with market_read_scope(store) as reader:
        intent = deepcopy(request)
        intent["base_reference"] = ft._ref(request["base_reference"], reader.backend.repository)
        # 现有 plan 不允许新增元数据；generation 同时携带稳定 id 和内容指纹，
        # 使用原有 genesis 索引识别同 id 异内容，不另建指针或重开额度。
        prefix = "od-" + digest(encoded(intent["id"]))[:40] + "-"
        generation = prefix + digest(encoded(intent))[:48]
        anchor = ft.restore_ftshare_collection(intent["base_reference"], registry, reader)
        collection_id = anchor["collection"]["plan"]["collection_id"]
        existing = ft.latest_ftshare_reference(reader, registry,
            collection_id=collection_id, generation=generation)
        if existing is not None:
            restored = ft.restore_ftshare_collection(existing, registry, reader)
            collection = restored["collection"]
            plan = collection["plan"]
            require(plan["schema"] == ft.SINGLE_PLAN_SCHEMA and plan["targets"] == [target]
                and plan["source_budget"] == 1
                and plan["new_slots"] == [intent["adjust_kind"] + ":" + intent["symbol"]],
                "FTSHARE_ON_DEMAND_INTENT_CONFLICT")
            # 只证明原计划的成功前驱，不查看其后的当前前沿，也不重新规划。
            frontier = ft.resolve_ftshare_expansion_frontier(intent["base_reference"], registry, reader,
                protected_symbols=protected_symbols, through_reference=plan["base_reference"])
            require(frontier["reason_code"] is None,
                frontier["reason_code"] or "FTSHARE_ON_DEMAND_PREDECESSOR")
            reason = ft._expansion_edge_reason(frontier["restored"]["collection"], collection,
                set(protected_symbols))
            require(reason is None, reason or "FTSHARE_ON_DEMAND_INTENT_CONFLICT")
            ft._single_sources(plan, collection["units"], reader)
            return {"status": "restored-ftshare-on-demand", "collection": collection,
                "reference": existing, "source_requests": 0}

        candidates = [plan for plans in ft._expansion_successors(reader, registry).values()
            for plan in plans if plan["collection_id"] == collection_id
            and plan["generation"].startswith(prefix)]
        require(not candidates, "FTSHARE_ON_DEMAND_INTENT_CONFLICT")
        frontier = ft.resolve_ftshare_expansion_frontier(intent["base_reference"], registry, reader,
            protected_symbols=protected_symbols)
        require(frontier["reason_code"] is None,
            frontier["reason_code"] or "FTSHARE_ON_DEMAND_PREDECESSOR")
        prior = frontier["restored"]["collection"]["plan"]
        require(not any(request["parameters"]["symbol"] == intent["symbol"]
            and request["parameters"]["adjust_kind"] == intent["adjust_kind"]
            for request in prior["requests"].values()), "FTSHARE_SINGLE_REQUEST_ALREADY_CONSUMED")
        collection = ft.plan_ftshare_single_collection(frontier["reference"], registry, reader,
            collection_id=collection_id, generation=generation, created_at=created_at,
            start=prior["start"], end=prior["end"], targets=[target], source_budget=1,
            calendar_references=prior["calendar_references"], master_references=prior["master_references"],
            completion_policy=prior.get("completion_policy"))
        return {"status": "planned-ftshare-on-demand", "collection": collection,
            "reference": None, "source_requests": 0}
