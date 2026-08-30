"""USTC public course-catalog snapshot service.

The official catalog SPA currently requires an authenticated session for its
JSON APIs. This service consumes the public, read-only snapshots published by
``RaymondzyLei/class-arrange`` and identifies them as cached rather than
personal or real-time registrar data.

No code is copied from that project. Its generated course data is used under
the project licence for non-commercial USTC course-planning/academic purposes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import ssl
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import httpx
import certifi

from .base import (
    BaseMCPService, CachePolicy, MCPServiceConfig, ServiceHealth, ServiceResult,
)

logger = logging.getLogger("dududa20.mcp.course_schedule")

DEFAULT_MANIFEST_URL = (
    "https://raw.githubusercontent.com/RaymondzyLei/class-arrange/"
    "main/public/data/semesters/index.json"
)
OFFICIAL_CATALOG_URL = "https://catalog.ustc.edu.cn/catalog"
SNAPSHOT_PROJECT_URL = "https://github.com/RaymondzyLei/class-arrange"
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024


def _normalise(value: Any) -> str:
    return re.sub(
        r"[\s\-—_·（）()【】\[\]，,。.!！?？:：/\\]+", "", str(value or "")
    ).lower()


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _department_matches(requested: str, actual: str) -> bool:
    wanted = _normalise(requested)
    candidate = _normalise(actual)
    if not wanted or wanted in candidate:
        return True
    removable = ("科学与技术", "科学技术", "科学", "技术", "学院", "系")
    for token in removable:
        wanted = wanted.replace(token, "")
        candidate = candidate.replace(token, "")
    return bool(wanted) and (wanted in candidate or candidate in wanted)


def _canonical_grading(value: Any) -> str:
    """Normalize common names for USTC's grading-system field."""
    raw = str(value or "").strip()
    normalized = _normalise(raw)
    if any(alias in normalized for alias in (
            "二分制", "二等级制", "二级制", "两级制", "合格不合格")):
        return "二分制"
    if "五分制" in normalized or "五等级制" in normalized:
        return "五分制"
    if "百分制" in normalized:
        return "百分制"
    return raw


def _base_course_id(value: Any) -> str:
    return re.sub(r"\.[A-Za-z0-9]+$", "", str(value or "").strip())


def _default_cache_dir() -> Path:
    override = os.environ.get("DUDUDA_CATALOG_CACHE_DIR", "").strip()
    if override:
        return Path(override)
    data_root = os.environ.get("DUDUDA_MCP_DATA_DIR", "").strip()
    if data_root:
        return Path(data_root) / "ustc_catalog"
    return Path.cwd() / "data" / "ustc_catalog"


class CourseScheduleService(BaseMCPService):
    """Search public USTC course offerings with revision-aware disk caching."""

    def __init__(
        self,
        manifest_url: Optional[str] = None,
        cache_dir: Optional[Path | str] = None,
        refresh_seconds: Optional[float] = None,
    ):
        url = (
            manifest_url
            or os.environ.get("DUDUDA_CATALOG_MANIFEST_URL")
            or DEFAULT_MANIFEST_URL
        ).strip()
        super().__init__(MCPServiceConfig(
            service_name="course_schedule",
            description=(
                "Search the public USTC course-offering snapshot by course, "
                "teacher, department, semester or grading system such as "
                "binary/pass-fail (not personal registrar data)"
            ),
            cache_policy=CachePolicy.LONG,
            timeout_seconds=90.0,
            max_retries=1,
            base_url=url,
            mock_mode=False,
        ))
        self._manifest_url = url
        self._cache_dir = Path(cache_dir) if cache_dir else _default_cache_dir()
        configured_refresh = (
            refresh_seconds if refresh_seconds is not None
            else os.environ.get("DUDUDA_CATALOG_REFRESH_SECONDS", "21600")
        )
        self._refresh_seconds = max(60.0, float(configured_refresh))
        self._manifest: Optional[dict[str, Any]] = None
        self._datasets: dict[str, tuple[str, dict[str, Any], bool]] = {}
        self._lock = asyncio.Lock()

    async def _fetch_live(self, **kwargs) -> Any:
        """Base-class compatibility; normal queries use the snapshot loader."""
        manifest, _ = await self._load_manifest()
        return manifest

    def _get_mock(self, **kwargs) -> Any:
        raise RuntimeError("course catalog mock data has been removed")

    @property
    def _manifest_cache_path(self) -> Path:
        return self._cache_dir / "manifest.json"

    def _semester_cache_path(self, key: str) -> Path:
        safe_key = re.sub(r"[^a-zA-Z0-9._-]", "_", key)
        return self._cache_dir / f"{safe_key}.json"

    @staticmethod
    def _read_json(path: Path) -> Optional[dict[str, Any]]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)

    @staticmethod
    def _is_fresh(path: Path, max_age: float) -> bool:
        try:
            return time.time() - path.stat().st_mtime < max_age
        except OSError:
            return False

    async def _download_json(self, url: str) -> dict[str, Any]:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise RuntimeError("课程快照地址必须使用 HTTPS")
        headers = {
            "Accept": "application/json",
            "User-Agent": "YmaKmern/0.7 USTC-public-catalog-reader",
        }
        timeout = httpx.Timeout(self.config.timeout_seconds)
        ssl_context = ssl.create_default_context(cafile=certifi.where())
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=False, headers=headers,
            verify=ssl_context,
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                announced = _safe_int(response.headers.get("content-length"))
                if announced > MAX_DOWNLOAD_BYTES:
                    raise RuntimeError("课程快照超过大小限制")
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_DOWNLOAD_BYTES:
                        raise RuntimeError("课程快照超过大小限制")
        body = bytes(chunks)
        value = json.loads(body)
        if not isinstance(value, dict):
            raise RuntimeError("课程快照格式错误")
        return value

    @staticmethod
    def _valid_manifest(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and isinstance(value.get("semesters"), list)
            and bool(value.get("semesters"))
        )

    @staticmethod
    def _valid_dataset(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and isinstance(value.get("courses"), list)
            and isinstance(value.get("semester"), dict)
        )

    async def _load_manifest(self) -> tuple[dict[str, Any], bool]:
        if self._manifest is not None and self._is_fresh(
            self._manifest_cache_path, self._refresh_seconds,
        ):
            return self._manifest, False
        disk = self._read_json(self._manifest_cache_path)
        if disk is not None and not self._valid_manifest(disk):
            disk = None
        if disk is not None and self._is_fresh(
            self._manifest_cache_path, self._refresh_seconds,
        ):
            self._manifest = disk
            return disk, False
        try:
            remote = await self._download_json(self._manifest_url)
            if not self._valid_manifest(remote):
                raise RuntimeError("课程学期清单格式错误")
            self._write_json(self._manifest_cache_path, remote)
            self._manifest = remote
            self._last_health = ServiceHealth.HEALTHY
            return remote, False
        except Exception as exc:
            if disk is None:
                self._last_health = ServiceHealth.UNAVAILABLE
                raise RuntimeError(f"课程公开快照暂时不可用：{exc}") from exc
            logger.warning("catalog manifest refresh failed, using stale cache: %s", exc)
            self._manifest = disk
            self._last_health = ServiceHealth.DEGRADED
            return disk, True

    @staticmethod
    def _semester_entry(
        manifest: dict[str, Any], requested: str = "",
    ) -> dict[str, Any]:
        semesters = [
            s for s in manifest.get("semesters", []) if isinstance(s, dict)
        ]
        wanted = _normalise(requested)
        if wanted:
            for entry in semesters:
                if wanted in {
                    _normalise(entry.get("key")), _normalise(entry.get("name")),
                }:
                    return entry
                key = str(entry.get("key", ""))
                name = str(entry.get("name", ""))
                year = re.search(r"20\d{2}", wanted)
                if year and year.group(0) in (key + name):
                    season_map = {
                        "春": "spring", "秋": "fall", "夏": "summer",
                        "spring": "spring", "fall": "fall", "summer": "summer",
                    }
                    if any(
                        token in wanted and value in key.lower()
                        for token, value in season_map.items()
                    ):
                        return entry
            raise LookupError(f"没有找到学期：{requested}")
        default_key = str(manifest.get("defaultSemester", ""))
        for entry in semesters:
            if str(entry.get("key", "")) == default_key:
                return entry
        if not semesters:
            raise RuntimeError("课程学期清单为空")
        return semesters[0]

    async def _load_dataset(
        self, semester: str = "",
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        async with self._lock:
            manifest, manifest_stale = await self._load_manifest()
            entry = self._semester_entry(manifest, semester)
            key = str(entry.get("key", "")).strip()
            revision = str(entry.get("revision", "")).strip()
            memory = self._datasets.get(key)
            if memory and (not revision or memory[0] == revision):
                return memory[1], entry, manifest_stale or memory[2]

            path = self._semester_cache_path(key)
            disk = self._read_json(path)
            if disk is not None and not self._valid_dataset(disk):
                disk = None
            disk_revision = str((disk or {}).get("revision", "")).strip()
            if disk is not None and revision and disk_revision == revision:
                self._datasets[key] = (revision, disk, manifest_stale)
                return disk, entry, manifest_stale

            file_name = str(entry.get("file", "")).strip()
            if not file_name:
                raise RuntimeError("课程学期清单缺少数据文件")
            data_url = urljoin(self._manifest_url, file_name)
            if urlparse(data_url).netloc != urlparse(self._manifest_url).netloc:
                raise RuntimeError("课程学期清单引用了非同源数据文件")
            try:
                remote = await self._download_json(data_url)
                if not self._valid_dataset(remote):
                    raise RuntimeError("课程数据快照格式错误")
                remote_revision = str(remote.get("revision", "")).strip()
                if revision and remote_revision and remote_revision != revision:
                    raise RuntimeError("课程数据 revision 与学期清单不一致")
                self._write_json(path, remote)
                resolved_revision = revision or remote_revision
                self._datasets[key] = (
                    resolved_revision, remote, manifest_stale,
                )
                self._last_health = (
                    ServiceHealth.DEGRADED
                    if manifest_stale else ServiceHealth.HEALTHY
                )
                return remote, entry, manifest_stale
            except Exception as exc:
                if disk is None:
                    self._last_health = ServiceHealth.UNAVAILABLE
                    raise RuntimeError(f"课程数据快照暂时不可用：{exc}") from exc
                logger.warning("catalog dataset refresh failed, using stale cache: %s", exc)
                self._datasets[key] = (disk_revision, disk, True)
                self._last_health = ServiceHealth.DEGRADED
                return disk, entry, True

    @staticmethod
    def _format_schedule(course: dict[str, Any]) -> str:
        raw = str(course.get("rawSchedule", "")).strip()
        if raw:
            return raw
        items = []
        day_names = "一二三四五六日"
        for slot in (course.get("schedule") or [])[:6]:
            if not isinstance(slot, dict):
                continue
            day = _safe_int(slot.get("day"))
            day_text = f"周{day_names[day - 1]}" if 1 <= day <= 7 else "时间待定"
            periods = ",".join(str(v) for v in (slot.get("periods") or []))
            weeks = ",".join(str(v) for v in (slot.get("weeks") or []))
            room = str(slot.get("room", "")).strip()
            bits = [day_text]
            if periods:
                bits.append(f"第{periods}节")
            if weeks:
                bits.append(f"第{weeks}周")
            if room:
                bits.append(room)
            items.append(" ".join(bits))
        return "；".join(items) or "时间地点待定"

    @staticmethod
    def _score(course: dict[str, Any], query: str) -> int:
        q = _normalise(query)
        if not q:
            return 1
        cid = _normalise(course.get("id"))
        name = _normalise(course.get("courseName"))
        teacher = _normalise(course.get("teacher"))
        dept = _normalise((course.get("department") or {}).get("name"))
        score = 0
        if q == cid:
            score += 1000
        elif cid and cid in q:
            score += 700
        if name and name in q:
            score += 600 + min(len(name), 30)
        elif q in name:
            score += 500
        if teacher and teacher in q:
            score += 350
        elif q in teacher:
            score += 300
        if dept and dept in q:
            score += 150
        elif q in dept:
            score += 120
        if not score:
            tokens = [
                token for token in re.split(r"[\s，。！？、,:：;；]+", str(query))
                if len(token) >= 2
            ]
            haystack = " ".join((name, teacher, dept, cid))
            score += sum(25 for token in tokens if _normalise(token) in haystack)
        return score

    @classmethod
    def _render_course(
        cls,
        course: dict[str, Any],
        dataset: dict[str, Any],
        entry: dict[str, Any],
        stale: bool,
    ) -> dict[str, Any]:
        department = str(
            (course.get("department") or {}).get("name", "")
        ).strip()
        teacher = str(course.get("teacher", "")).strip() or "教师待定"
        course_id = str(course.get("id", "")).strip()
        name = str(course.get("courseName", "")).strip() or course_id
        credits = course.get("credits")
        hours = course.get("hours")
        enrolled = _safe_int(course.get("enrolled"))
        capacity = _safe_int(course.get("capacity"))
        details = [f"教师 {teacher}"]
        if department:
            details.append(department)
        if credits not in (None, ""):
            details.append(f"{credits} 学分")
        if hours not in (None, ""):
            details.append(f"{hours} 学时")
        details.append(cls._format_schedule(course))
        if capacity:
            details.append(f"已选 {enrolled}/{capacity}")
        if course.get("examType"):
            details.append(str(course["examType"]))
        semester_data = dataset.get("semester") or {}
        semester_key = str(
            entry.get("key") or semester_data.get("key") or ""
        )
        semester_name = str(
            entry.get("name") or semester_data.get("name") or semester_key
        )
        if semester_name:
            details.insert(0, semester_name)
        generated_at = str(dataset.get("generatedAt", "")).strip()
        if generated_at:
            details.append(f"快照生成 {generated_at}")
        if stale:
            details.append("当前使用旧缓存")
        return {
            "title": f"{name}（{course_id}）",
            "link": OFFICIAL_CATALOG_URL,
            "snippet": "；".join(details),
            "course_id": course_id,
            "course_name": name,
            "teacher": teacher,
            "department": department,
            "credits": credits,
            "hours": hours,
            "schedule": cls._format_schedule(course),
            "capacity": capacity,
            "enrolled": enrolled,
            "level": course.get("level", ""),
            "course_type": course.get("courseType", ""),
            "exam_type": course.get("examType", ""),
            "grading": course.get("grading", ""),
            "semester": semester_key,
            "semester_name": semester_name,
            "generated_at": generated_at,
            "revision": dataset.get("revision") or entry.get("revision", ""),
            "stale": bool(stale),
            "source_name": "USTC 开课公开缓存",
            "source_project": SNAPSHOT_PROJECT_URL,
        }

    async def search(
        self,
        keyword: str = "",
        semester: str = "",
        teacher: str = "",
        department: str = "",
        limit: int = 8,
    ) -> ServiceResult:
        start = time.time()
        try:
            dataset, entry, stale = await self._load_dataset(semester)
            courses = [
                course for course in dataset.get("courses", [])
                if isinstance(course, dict)
            ]
            teacher_n = _normalise(teacher)
            ranked = []
            for course in courses:
                if teacher_n and teacher_n not in _normalise(course.get("teacher")):
                    continue
                course_dept = _normalise(
                    (course.get("department") or {}).get("name")
                )
                if department and not _department_matches(department, course_dept):
                    continue
                score = self._score(course, keyword)
                if keyword and score <= 0:
                    continue
                ranked.append((score, course))
            ranked.sort(key=lambda item: (
                -item[0],
                str(item[1].get("courseName", "")),
                str(item[1].get("id", "")),
            ))
            bounded = max(1, min(_safe_int(limit) or 8, 20))
            result = [
                self._render_course(course, dataset, entry, stale)
                for _, course in ranked[:bounded]
            ]
            return ServiceResult.ok(
                result,
                source=(
                    "ustc_catalog_snapshot_stale"
                    if stale else "ustc_catalog_snapshot"
                ),
                latency_ms=(time.time() - start) * 1000,
            )
        except (RuntimeError, LookupError) as exc:
            return ServiceResult.fail(str(exc))

    async def get_course(
        self, course_id: str, semester: str = "", limit: int = 8,
    ) -> ServiceResult:
        return await self.search(
            keyword=course_id, semester=semester, limit=limit,
        )

    async def list_by_department(
        self, department: str, semester: str = "", limit: int = 10,
    ) -> ServiceResult:
        return await self.search(
            department=department, semester=semester, limit=limit,
        )

    async def list_by_grading(
        self, grading: str, semester: str = "", limit: int = 20,
    ) -> ServiceResult:
        """List unique courses by grading system, not review keywords.

        The snapshot contains one row per teaching section, so results are
        deduplicated by base course id before the requested limit is applied.
        """
        start = time.time()
        target = _canonical_grading(grading)
        if not target:
            return ServiceResult.fail("grading is required")
        try:
            dataset, entry, stale = await self._load_dataset(semester)
            grouped: dict[str, list[dict[str, Any]]] = {}
            for course in dataset.get("courses", []):
                if not isinstance(course, dict):
                    continue
                if _canonical_grading(course.get("grading")) != target:
                    continue
                identity = _base_course_id(course.get("id"))
                if not identity:
                    identity = _normalise(course.get("courseName"))
                grouped.setdefault(identity, []).append(course)

            ordered = sorted(
                grouped.items(),
                key=lambda item: (
                    str(item[1][0].get("courseName", "")), item[0]),
            )
            bounded = max(1, min(_safe_int(limit) or 20, 100))
            rendered = []
            for base_id, sections in ordered[:bounded]:
                item = self._render_course(sections[0], dataset, entry, stale)
                teachers = sorted({
                    teacher.strip()
                    for section in sections
                    for teacher in str(section.get("teacher", "")).split(",")
                    if teacher.strip()
                })
                item.update({
                    "base_course_id": base_id,
                    "section_count": len(sections),
                    "teachers": teachers,
                })
                rendered.append(item)
            payload = {
                "grading": target,
                "semester": str((dataset.get("semester") or {}).get("key", "")),
                "semester_name": str(
                    (dataset.get("semester") or {}).get("name", "")),
                "total_courses": len(ordered),
                "returned_courses": len(rendered),
                "courses": rendered,
                "source_note": (
                    "成绩等级制来自 USTC 公开开课缓存；评课社区不提供该筛选字段"
                ),
            }
            is_truncated = len(rendered) < len(ordered)
            return ServiceResult.ok(
                payload,
                source=(
                    "ustc_catalog_snapshot_stale"
                    if stale else "ustc_catalog_snapshot"
                ),
                latency_ms=(time.time() - start) * 1000,
                truncated=is_truncated,
            )
        except (RuntimeError, LookupError) as exc:
            return ServiceResult.fail(str(exc))

    async def list_semesters(self) -> ServiceResult:
        start = time.time()
        try:
            manifest, stale = await self._load_manifest()
            data = [{
                "key": str(entry.get("key", "")),
                "name": str(entry.get("name", "")),
                "revision": str(entry.get("revision", "")),
                "default": (
                    str(entry.get("key", ""))
                    == str(manifest.get("defaultSemester", ""))
                ),
                "stale": stale,
            } for entry in manifest.get("semesters", []) if isinstance(entry, dict)]
            return ServiceResult.ok(
                data,
                source=(
                    "ustc_catalog_manifest_stale"
                    if stale else "ustc_catalog_manifest"
                ),
                latency_ms=(time.time() - start) * 1000,
            )
        except RuntimeError as exc:
            return ServiceResult.fail(str(exc))

    async def get_personal_schedule(self, **kwargs) -> ServiceResult:
        return ServiceResult.fail(
            "公开开课缓存不包含个人选课记录；目前只能查询全校开课、教师、院系和上课时间"
        )

    def check_health(self) -> ServiceHealth:
        if self._last_health != ServiceHealth.UNKNOWN:
            return self._last_health
        if self._read_json(self._manifest_cache_path):
            return ServiceHealth.HEALTHY
        return ServiceHealth.UNKNOWN

    def invalidate_cache(self, key: Optional[str] = None):
        super().invalidate_cache(key)
        self._manifest = None
        self._datasets.clear()
