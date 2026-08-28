"""Read-only lookup for the public USTC iCourse review community.

The site does not expose a documented public read API, so this service uses
the same public search and course pages a browser uses.  It deliberately:

- never logs in or calls write endpoints;
- caps each query at three course pages;
- caches results for an hour through :class:`BaseMCPService`;
- returns short, attributed summaries instead of mirroring review content.
"""
from __future__ import annotations

import asyncio
import html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from .base import BaseMCPService, CachePolicy, MCPServiceConfig, ServiceResult

_BASE_URL = "https://icourse.club"
_COURSE_PATH = re.compile(r"^/course/(\d+)/?$")
_SPACE = re.compile(r"\s+")
_VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})
_USER_AGENT = (
    "YmaKmernBot/0.7 (+https://github.com/Shaywww/YmaKmern-prototype; "
    "read-only course review lookup)"
)


def _clean_text(value: str, limit: int = 0) -> str:
    text = _SPACE.sub(" ", html.unescape(value or "")).strip()
    if limit > 0 and len(text) > limit:
        return text[:max(1, limit - 1)].rstrip() + "…"
    return text


def _clean_query(value: str) -> str:
    """Keep course/teacher names while removing conversational filler."""
    q = re.sub(r"@\S+", " ", value or "")
    q = _SPACE.sub(" ", q).strip()
    q = re.sub(
        r"^(?:帮我|请你?|麻烦你?|给我|能不能|可以)?\s*"
        r"(?:在)?(?:中科大|USTC)?(?:评课社区|icourse)(?:里|上)?\s*"
        r"(?:查一下|查查|搜索|搜一下|看看|看一下|查|搜)?\s*",
        "", q, flags=re.I)
    q = re.sub(
        r"^(?:帮我|请你?|麻烦你?|给我)?\s*"
        r"(?:查一下|查查|搜索|搜一下|看看|看一下|查|搜)\s*",
        "", q, flags=re.I)
    q = re.sub(
        r"(?:这门)?(?:课程|课)?(?:在评课社区)?"
        r"(?:怎么样|怎样|如何|好不好|值不值得选|值得选吗|推荐吗|"
        r"评价如何|评价怎么样|给分怎么样|作业多吗|难不难)\s*[？?。！!]*$",
        "", q, flags=re.I)
    q = re.sub(r"老师\s*$", "", q).strip(" ，。！？、?！")
    return q


def _bounded_limit(value) -> int:
    try:
        return max(1, min(int(value or 3), 3))
    except (TypeError, ValueError):
        return 3


class _SearchParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.results: list[dict] = []
        self._href = ""
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a" or self._href:
            return
        values = dict(attrs)
        href = str(values.get("href", ""))
        if _COURSE_PATH.fullmatch(href):
            self._href = href
            self._text = []

    def handle_data(self, data):
        if self._href:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or not self._href:
            return
        title = _clean_text("".join(self._text), 160)
        if title and not any(x["path"] == self._href for x in self.results):
            self.results.append({
                "path": self._href,
                "title": title,
                "course_id": _COURSE_PATH.fullmatch(self._href).group(1),
            })
        self._href = ""
        self._text = []


class _CourseParser(HTMLParser):
    """Extract a small stable subset from the public course profile page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.page_title = ""
        self.meta_description = ""
        self.header = ""
        self.metrics: list[str] = []
        self.details: list[str] = []
        self.summary = ""
        self.review_samples: list[str] = []
        self._in_reviews = False
        self._capture_kind = ""
        self._capture_depth = 0
        self._capture_text: list[str] = []
        self._summary_depth = 0
        self._summary_text: list[str] = []
        self._review_depth = 0
        self._review_text: list[str] = []

    @staticmethod
    def _attrs(attrs) -> dict[str, str]:
        return {str(k): str(v or "") for k, v in attrs}

    def handle_starttag(self, tag, attrs):
        values = self._attrs(attrs)
        elem_id = values.get("id", "")
        classes = set(values.get("class", "").split())

        if tag == "meta" and values.get("name") == "description":
            self.meta_description = _clean_text(values.get("content", ""), 160)

        if self._summary_depth:
            if tag not in _VOID_TAGS:
                self._summary_depth += 1
            return
        if elem_id == "course-summary":
            self._summary_depth = 1
            self._summary_text = []
            return

        if self._review_depth:
            if tag not in _VOID_TAGS:
                self._review_depth += 1
            return
        if (elem_id.startswith("review-content-")
                and len(self.review_samples) < 2):
            self._review_depth = 1
            self._review_text = []
            return

        if elem_id == "review-anchor":
            self._in_reviews = True

        if self._capture_depth:
            if tag not in _VOID_TAGS:
                self._capture_depth += 1
            return
        if tag == "title":
            self._begin_capture("title")
        elif (not self._in_reviews and tag == "span"
              and {"small", "grey", "align-bottom"}.issubset(classes)
              and not self.header):
            self._begin_capture("header")
        elif (not self._in_reviews and tag == "li"
              and "right-mg-md" in classes and len(self.metrics) < 4):
            self._begin_capture("metric")
        elif not self._in_reviews and tag == "td":
            self._begin_capture("detail")

    def _begin_capture(self, kind: str) -> None:
        self._capture_kind = kind
        self._capture_depth = 1
        self._capture_text = []

    def handle_data(self, data):
        if self._summary_depth:
            self._summary_text.append(data)
        elif self._review_depth:
            self._review_text.append(data)
        elif self._capture_depth:
            self._capture_text.append(data)

    def handle_endtag(self, tag):
        if self._summary_depth:
            self._summary_depth -= 1
            if self._summary_depth == 0:
                self.summary = _clean_text(" ".join(self._summary_text), 900)
            return
        if self._review_depth:
            self._review_depth -= 1
            if self._review_depth == 0:
                sample = _clean_text(" ".join(self._review_text), 260)
                if sample:
                    self.review_samples.append(sample)
            return
        if not self._capture_depth:
            return
        self._capture_depth -= 1
        if self._capture_depth:
            return
        value = _clean_text(" ".join(self._capture_text), 300)
        kind = self._capture_kind
        if kind == "title":
            self.page_title = re.sub(
                r"\s*-\s*USTC评课社区\s*$", "", value).strip()
        elif kind == "header" and value:
            self.header = value
        elif kind == "metric" and value:
            self.metrics.append(value)
        elif kind == "detail" and value:
            self.details.append(value)
        self._capture_kind = ""
        self._capture_text = []


def _parse_search_page(body: str) -> list[dict]:
    parser = _SearchParser()
    parser.feed(body)
    parser.close()
    return parser.results


def _split_label(items: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        match = re.match(r"\s*([^：:]{1,12})[：:]\s*(.+?)\s*$", item)
        if match:
            result[match.group(1).strip()] = match.group(2).strip()
    return result


def _parse_course_page(body: str, url: str, fallback_title: str = "") -> dict:
    parser = _CourseParser()
    parser.feed(body)
    parser.close()

    title = parser.page_title or fallback_title
    course_name, teacher = title, ""
    match = re.match(r"^(.*?)（(.*?)）$", title)
    if match:
        course_name, teacher = match.group(1).strip(), match.group(2).strip()

    rating = None
    review_count = 0
    match = re.search(
        r"([0-9]+(?:\.[0-9]+)?)\s*分[，,]\s*(\d+)\s*人评价",
        parser.meta_description)
    if match:
        rating, review_count = float(match.group(1)), int(match.group(2))

    course_code = ""
    semesters = ""
    if parser.header:
        parts = re.split(r"课程号[：:]", parser.header, maxsplit=1)
        semesters = parts[0].strip()
        if len(parts) == 2:
            course_code = parts[1].strip()

    metrics = _split_label(parser.metrics)
    details = _split_label(parser.details)
    summary = parser.summary
    if not summary and parser.review_samples:
        summary = "；".join(parser.review_samples)

    facts = []
    if rating is not None:
        facts.append(f"评分 {rating:g}/10（{review_count} 人评价）")
    elif review_count == 0:
        facts.append("暂无评分")
    if course_code:
        facts.append(f"课程号 {course_code}")
    if semesters:
        facts.append(f"开课学期 {semesters}")
    for label in ("课程难度", "作业多少", "给分好坏", "收获大小"):
        if metrics.get(label):
            facts.append(f"{label} {metrics[label]}")
    for label in ("开课单位", "课程类别", "课程层次", "学分"):
        if details.get(label):
            facts.append(f"{label} {details[label]}")
    if summary:
        facts.append("页面摘要/点评要点：" + _clean_text(summary, 420))

    return {
        "title": title,
        "link": url,
        "snippet": "｜".join(facts),
        "course": course_name,
        "teacher": teacher,
        "rating": rating,
        "review_count": review_count,
        "course_code": course_code,
        "semesters": semesters,
        "metrics": metrics,
        "details": details,
    }


class ICourseReviewsService(BaseMCPService):
    """Search public USTC course/teacher reviews without authentication."""

    def __init__(self):
        super().__init__(MCPServiceConfig(
            service_name="评课社区查询",
            description=(
                "Search the public USTC iCourse review community for courses, "
                "teachers, ratings, workload, grading and review summaries"
            ),
            cache_policy=CachePolicy.MEDIUM,
            timeout_seconds=18.0,
            max_retries=1,
            base_url=_BASE_URL,
            mock_mode=False,
        ))
        self._lookup_lock = asyncio.Lock()

    def _get_mock(self, **kwargs):
        return []

    async def _request_html(self, client: httpx.AsyncClient, path: str,
                            params: dict | None = None) -> str:
        response = await client.get(path, params=params)
        response.raise_for_status()
        if urlsplit(str(response.url)).hostname != "icourse.club":
            raise RuntimeError("unexpected iCourse redirect host")
        content_type = response.headers.get("content-type", "").lower()
        if "text/html" not in content_type:
            raise RuntimeError("unexpected iCourse response type")
        if len(response.content) > 2_000_000:
            raise RuntimeError("iCourse page exceeds size limit")
        return response.text

    async def _fetch_live(self, **kwargs):
        q = str(kwargs.get("q", ""))
        limit = _bounded_limit(kwargs.get("limit", 3))
        timeout = httpx.Timeout(12.0, connect=6.0)
        async with httpx.AsyncClient(
                base_url=_BASE_URL,
                headers={"User-Agent": _USER_AGENT, "Accept": "text/html"},
                timeout=timeout,
                follow_redirects=True) as client:
            search_body = await self._request_html(
                client, "/search/", params={"q": q})
            candidates = _parse_search_page(search_body)[:limit]
            if not candidates:
                return []

            semaphore = asyncio.Semaphore(2)

            async def load(candidate: dict) -> dict:
                url = urljoin(_BASE_URL, candidate["path"])
                try:
                    async with semaphore:
                        body = await self._request_html(client, candidate["path"])
                    return _parse_course_page(body, url, candidate["title"])
                except Exception:
                    return {
                        "title": candidate["title"],
                        "link": url,
                        "snippet": "详情页暂时不可用，可打开链接查看",
                        "course": candidate["title"],
                        "teacher": "",
                        "rating": None,
                        "review_count": 0,
                    }

            return list(await asyncio.gather(*(load(c) for c in candidates)))

    async def search(self, q: str = "", keyword: str = "",
                     limit: int = 3) -> ServiceResult:
        query = _clean_query(q or keyword)
        if not query:
            return ServiceResult.fail("course or teacher keyword required")
        if len(query) > 80:
            return ServiceResult.fail("query too long")
        safe_limit = _bounded_limit(limit)
        cache_key = f"search:{query.casefold()}:{safe_limit}"
        async with self._lookup_lock:
            return await self.query(
                cache_key=cache_key, q=query, limit=safe_limit)
