"""来源 HTTP 客户端。

只与合成来源的真实协议对话：
- GET /facets、/catalog、/revisions
- 400 unsupported_filter 属于非法查询，客户端规划阶段就不会发出；
- 409 version_expired 与 429 unit_quota 是两类不同后果，分别抛出异常，
  调用方必须区别处理（旧版本作废 vs 单位配额冷却）。
所有方法都是纯网络/序列化逻辑，不做任何业务归并。
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Optional


class SourceError(RuntimeError):
    """来源返回的非 2xx 响应基类。"""

    def __init__(self, status: int, payload: Any):
        self.status = status
        self.payload = payload
        super().__init__(f"来源返回 HTTP {status}: {payload!r}")


class UnsupportedFilter(SourceError):
    """400：查询条件不在来源声明的合法筛选内，禁止重试或改写。"""


class VersionExpired(SourceError):
    """409：携带的查询版本已过期，旧页与新页不能拼接。"""

    @property
    def current_version(self) -> Optional[int]:
        return self.payload.get("current") if isinstance(self.payload, dict) else None


class UnitQuotaExceeded(SourceError):
    """429：该单位触发共享配额限速。Retry-After 单位为秒。"""

    def __init__(self, status: int, payload: Any, retry_after: float):
        super().__init__(status, payload)
        self.retry_after = retry_after

    @property
    def unit(self) -> Optional[str]:
        return self.payload.get("unit") if isinstance(self.payload, dict) else None


@dataclass(frozen=True)
class CatalogPage:
    """一次 /catalog 响应的结构化结果（原文仍单独落盘）。"""

    unit: str
    query: dict[str, str]
    version: int
    total: int
    cap: int
    page_size: int
    truncated: bool
    next: bool
    page: int
    items: list[dict[str, Any]]
    raw: bytes
    digest: str


class SourceClient:
    """同步 HTTP 客户端；timeout/clock 可注入，便于测试确定性等待。"""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = 10.0,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        user_agent: str = "scenario-catalog-collector/1.0 (+authorized-collection)",
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.clock = clock
        self.sleep = sleep
        self.user_agent = user_agent

    # ---- 底层 ----------------------------------------------------------
    def _get(self, path: str, params: Optional[dict[str, str]] = None) -> tuple[int, dict[str, str], bytes]:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = {"raw": raw.decode("utf-8", "replace")}
            headers = dict(exc.headers) if exc.headers else {}
            self._raise_for_status(exc.code, payload, headers, raw)

    @staticmethod
    def _raise_for_status(status: int, payload: Any, headers: dict[str, str], raw: bytes) -> None:
        if status == 400:
            raise UnsupportedFilter(status, payload)
        if status == 409:
            raise VersionExpired(status, payload)
        if status == 429:
            retry_after = float(headers.get("Retry-After", "0") or 0)
            raise UnitQuotaExceeded(status, payload, retry_after)
        raise SourceError(status, payload)

    # ---- 业务端点 ------------------------------------------------------
    def facets(self) -> dict[str, Any]:
        _, _, raw = self._get("/facets")
        return json.loads(raw.decode("utf-8"))

    def revisions(self) -> list[dict[str, Any]]:
        _, _, raw = self._get("/revisions")
        return json.loads(raw.decode("utf-8"))

    def catalog_page(
        self,
        unit: str,
        *,
        industry: Optional[str] = None,
        year: Optional[int] = None,
        page: int = 0,
        version: Optional[int] = None,
    ) -> CatalogPage:
        params: dict[str, str] = {"unit": unit, "page": str(page)}
        if industry is not None:
            params["industry"] = industry
        if year is not None:
            params["year"] = str(year)
        if version is not None:
            params["version"] = str(version)
        _, _, raw = self._get("/catalog", params)
        data = json.loads(raw.decode("utf-8"))
        # 来源契约规定页大小由 /facets 的 page_size 声明；catalog 不回传该字段，
        # 由调用方在引擎层注入，这里不做硬编码假设。
        return CatalogPage(
            unit=data["unit"],
            query=data.get("query", {}),
            version=int(data["version"]),
            total=int(data["total"]),
            cap=int(data["cap"]),
            page_size=0,
            truncated=bool(data["truncated"]),
            next=bool(data["next"]),
            page=page,
            items=data.get("items", []),
            raw=raw,
            digest=hashlib.sha256(raw).hexdigest(),
        )
