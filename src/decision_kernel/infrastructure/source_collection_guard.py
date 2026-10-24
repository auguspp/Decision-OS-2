"""有界核验原生来源采集冲突；不是分布式锁，也不替代原有提交条件。"""
from __future__ import annotations

import re

from decision_kernel.domain.records import Conflict, StoreUnavailable, require
from decision_kernel.infrastructure.runtime_publication import REPOSITORY

SOURCE_COLLECTION_WORKFLOWS = frozenset({
    ".github/workflows/daily-market.yml",
    ".github/workflows/ashare-data.yml",
    ".github/workflows/market-source-check.yml",
    ".github/workflows/relay-source-runtime.yml",
    ".github/workflows/relay-capabilities.yml",
})
MAX_PAGES = 10
PER_PAGE = 100
PREFIX = "/repos/" + REPOSITORY + "/actions/runs"
_WORKFLOW_PATH = re.compile(r"\.github/workflows/[A-Za-z0-9_][A-Za-z0-9_.-]*\.ya?ml")


def _get(api, path):
    try:
        return api.request("GET", path)
    except Exception:
        raise StoreUnavailable("SOURCE_COLLECTION_API_UNAVAILABLE") from None


def _run_id(value):
    if type(value) is str and re.fullmatch(r"[1-9][0-9]{0,19}", value):
        value = int(value)
    require(type(value) is int and value > 0, "SOURCE_COLLECTION_RUN_ID_INVALID")
    return value


def _peer(row):
    require(isinstance(row, dict), "SOURCE_COLLECTION_PEER_INVALID")
    require(type(row.get("id")) is int and row["id"] > 0
            and row.get("status") == "in_progress", "SOURCE_COLLECTION_PEER_INVALID")
    path = row.get("path")
    require(isinstance(path, str) and _WORKFLOW_PATH.fullmatch(path) is not None,
            "SOURCE_COLLECTION_PEER_PATH_INVALID")
    return row["id"], path


def _own(row, run_id, code_sha, workflow_path):
    require(isinstance(row, dict) and type(row.get("id")) is int
            and row["id"] == run_id and row.get("status") == "in_progress"
            and type(row.get("run_attempt")) is int and row["run_attempt"] == 1
            and row.get("head_sha") == code_sha and row.get("path") == workflow_path,
            "SOURCE_COLLECTION_NATIVE_RUN_INVALID")


def detect_peer_collections(api, *, run_id, code_sha, workflow_path, max_pages=MAX_PAGES):
    """返回其他正在采集的运行的 ``({id, path}, ...)``，不把排队者算成冲突。

    先 GET 本运行，完整读取有界 in_progress 列表，再 GET 本运行复核。
    列表不完整、总数变化、重复、必要字段非法或本运行缺失时均拒绝。
    最多 max_pages + 2 次 GET；只查询正在运行者，不取消或修改任何运行。
    分页不是原子快照，同总数的成员更替或核验后的变化未必可见。
    调用方仍负责原有仓库资格、授权及主线检查；本核验没有原子互斥保证。
    """
    run_id = _run_id(run_id)
    require(isinstance(code_sha, str) and re.fullmatch(r"[a-f0-9]{40}", code_sha) is not None,
            "SOURCE_COLLECTION_CODE_SHA_INVALID")
    require(isinstance(workflow_path, str) and workflow_path in SOURCE_COLLECTION_WORKFLOWS,
            "SOURCE_COLLECTION_WORKFLOW_INVALID")
    require(type(max_pages) is int and 1 <= max_pages <= MAX_PAGES,
            "SOURCE_COLLECTION_PAGE_BOUND_INVALID")
    _own(_get(api, PREFIX + "/" + str(run_id)), run_id, code_sha, workflow_path)

    seen = set()
    peers = []
    total = None
    for page in range(1, max_pages + 1):
        result = _get(api, PREFIX + f"?status=in_progress&per_page={PER_PAGE}&page={page}")
        require(isinstance(result, dict) and type(result.get("total_count")) is int
                and 0 <= result["total_count"] <= max_pages * PER_PAGE,
                "SOURCE_COLLECTION_LIST_INCOMPLETE")
        if total is None:
            total = result["total_count"]
        require(result["total_count"] == total, "SOURCE_COLLECTION_LIST_CHANGED")
        rows = result.get("workflow_runs")
        require(isinstance(rows, list) and len(rows) == min(PER_PAGE, total - len(seen)),
                "SOURCE_COLLECTION_LIST_INCOMPLETE")
        for row in rows:
            ident, path = _peer(row)
            require(ident not in seen, "SOURCE_COLLECTION_DUPLICATE_RUN")
            seen.add(ident)
            if ident == run_id:
                _own(row, run_id, code_sha, workflow_path)
            elif path in SOURCE_COLLECTION_WORKFLOWS:
                peers.append({"id": ident, "path": path})
        if len(seen) == total:
            require(run_id in seen, "SOURCE_COLLECTION_NATIVE_RUN_MISSING")
            _own(_get(api, PREFIX + "/" + str(run_id)), run_id, code_sha, workflow_path)
            return tuple(peers)
    raise StoreUnavailable("SOURCE_COLLECTION_LIST_INCOMPLETE")


def require_no_peer_collection(api, *, run_id, code_sha, workflow_path, max_pages=MAX_PAGES):
    """每次调用重新核验；来源 peer 存在则拒绝后续采集或写入。"""
    peers = detect_peer_collections(api, run_id=run_id, code_sha=code_sha,
                                    workflow_path=workflow_path, max_pages=max_pages)
    if peers:
        raise Conflict("SOURCE_COLLECTION_PEER_CONFLICT")


class SourceCollectionGuardStore:
    """提交前调用无参 guard；原样透传 expected，不刷新 head 或重试 CAS。

    guard 必须以抛异常拒绝；返回值不作为授权信号。其他能力（包括读 scope）
    保持原委托，故 scope 返回的 reader 不会被包装成新的受保护 writer。
    """

    def __init__(self, backend, guard):
        require(callable(guard), "SOURCE_COLLECTION_GUARD_INVALID")
        self._backend = backend
        self._guard = guard

    def commit(self, files, *, expected):
        self._guard()
        return self._backend.commit(files, expected=expected)

    def __getattr__(self, name):
        return getattr(self._backend, name)
