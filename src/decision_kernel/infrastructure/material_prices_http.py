"""One finite NBS check: three catalogues and at most three official releases."""
from datetime import datetime, timezone
from http.client import HTTPException
from time import monotonic
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from decision_kernel.domain.records import RecordError, digest, require
from decision_kernel.observations.material_prices import (
    CATALOGUES, MAX_BODY, allowed, discover, monitor, parse_release, period,
)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, url):
        return None


def fetch(url):
    allowed(url)
    request = Request(url, headers={"User-Agent": "DecisionOS2-MaterialPrices/0.1",
                                   "Accept-Encoding": "identity"})
    try:
        with build_opener(_NoRedirect()).open(request, timeout=12) as response:
            require(response.status == 200 and response.geturl() == url, "MATERIAL_HTTP_RESPONSE")
            require(response.headers.get("Content-Encoding", "identity") in ("", "identity"),
                    "MATERIAL_HTTP_ENCODING")
            raw = response.read(MAX_BODY + 1)
            require(0 < len(raw) <= MAX_BODY, "MATERIAL_BODY_BOUND")
            return raw
    except HTTPError as exc:
        status = exc.code
        exc.close()
        raise RecordError("MATERIAL_HTTP_" + str(status)) from None
    except RecordError:
        raise
    except (OSError, HTTPException, ValueError):
        raise RecordError("MATERIAL_TRANSPORT_UNAVAILABLE") from None


def now():
    return datetime.now(timezone.utc).isoformat()


def capture(*, fetcher=fetch, clock=now, predecessor=None):
    receipts, originals, links, releases = [], {}, {}, []
    started = monotonic()
    refused = False

    def request(url, title=None):
        nonlocal refused
        requested = clock()
        receipt = {"url": allowed(url), "title": title, "requested_at": requested,
                   "status": "not-attempted-budget-or-refusal"}
        raw = None
        if not refused and len(receipts) < 6 and monotonic() - started < 120:
            try:
                raw = fetcher(url)
                require(isinstance(raw, bytes) and 0 < len(raw) <= MAX_BODY, "MATERIAL_BODY_BOUND")
                receipt.update(status="captured", response_sha256=digest(raw), response_bytes=len(raw))
                originals[url] = raw
            except (RecordError, OSError, HTTPException) as exc:
                code = str(exc) if isinstance(exc, RecordError) else "MATERIAL_TRANSPORT_UNAVAILABLE"
                receipt.update(status="unavailable", error_code=code)
                refused = code in ("MATERIAL_HTTP_401", "MATERIAL_HTTP_403", "MATERIAL_HTTP_429")
        receipt["received_at"] = clock()
        if raw is not None:
            try:
                if title is None:
                    for link in discover(raw, url):
                        require(link["url"] not in links or links[link["url"]] == link["title"],
                                "MATERIAL_LINK_CONFLICT")
                        links[link["url"]] = link["title"]
                else:
                    releases.append(parse_release(raw, url=url, title=title, received_at=receipt["received_at"]))
            except RecordError as exc:
                receipt.update(status="rejected", error_code=str(exc))
        receipts.append(receipt)

    for url in CATALOGUES:
        request(url)
    candidates = sorted(links.items(), key=lambda pair: (period(pair[1])[1], pair[0]), reverse=True)
    # Multiple links for the same statistical period are ambiguous, not fallback.
    periods = [period(title)[1] for _, title in candidates]
    if len(periods) != len(set(periods)):
        for receipt in receipts:
            if receipt["status"] == "captured":
                receipt.update(status="rejected", error_code="MATERIAL_DISCOVERY_PERIOD_CONFLICT")
        candidates = []
    for url, title in candidates[:3]:
        request(url, title)
    return monitor(releases, as_of=clock(), receipts=receipts, predecessor=predecessor), originals
