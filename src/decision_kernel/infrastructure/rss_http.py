"""One bounded GET to a fixed official RSS feed; no credentials, redirects or retry."""
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from decision_kernel.domain.records import RecordError, require
from decision_kernel.observations.rss_news import FEEDS, MAX_BODY


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def fetch_feed(feed_id):
    require(isinstance(feed_id, str) and feed_id in FEEDS, "RSS_FEED_REJECTED")
    url = FEEDS[feed_id]
    request = Request(url, headers={"User-Agent": "Decision-OS-2 RSS reader", "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml", "Accept-Encoding": "identity"})
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=30) as response:
            require(response.status == 200 and response.geturl() == url, "RSS_HTTP_RESPONSE_REJECTED")
            require(response.headers.get("Content-Encoding", "identity").lower() == "identity", "RSS_HTTP_ENCODING_REJECTED")
            length = response.headers.get("Content-Length")
            require(length is None or (length.isascii() and length.isdecimal() and 0 < int(length) <= MAX_BODY), "RSS_HTTP_SIZE_REJECTED")
            raw = response.read(MAX_BODY + 1)
            require(0 < len(raw) <= MAX_BODY and (length is None or int(length) == len(raw)), "RSS_HTTP_SIZE_REJECTED")
            return raw
    except HTTPError as exc:
        raise RecordError("RSS_HTTP_STATUS_" + str(exc.code)) from None
    except (URLError, OSError, TimeoutError, HTTPException):
        raise RecordError("RSS_HTTP_UNAVAILABLE") from None
