"""HTTP client: retries, rate limiting, and honest TLS.

The single most damaging shortcut a corporate scraper can take is falling back
to ``verify=False`` when a TLS-inspecting proxy breaks the handshake. It turns
a fixable trust-store problem into a permanent silent downgrade. This module
refuses to do that. On a certificate failure it raises ``TlsTrustError``, which
carries the exact remediation, and the run degrades that source instead.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .config import Config

log = logging.getLogger(__name__)


class FetchError(RuntimeError):
    """A request failed after exhausting retries."""


class TlsTrustError(FetchError):
    """Certificate verification failed. Never bypassed - always surfaced."""


@dataclass(slots=True)
class Response:
    url: str
    status: int
    text: str
    elapsed: float
    from_cache: bool = False


class _RateLimiter:
    """Process-wide minimum spacing between requests to one host."""

    def __init__(self, per_second: float) -> None:
        self._min_interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        if self._min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            sleep_for = self._next_allowed - now
            if sleep_for > 0:
                time.sleep(sleep_for)
                now = time.monotonic()
            self._next_allowed = now + self._min_interval


class Client:
    """A polite, retrying, verification-preserving HTTP client."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._limiter = _RateLimiter(cfg.rate_limit_per_sec)
        self._session = requests.Session()

        retry = Retry(
            total=cfg.max_retries,
            connect=cfg.max_retries,
            read=cfg.max_retries,
            status=cfg.max_retries,
            backoff_factor=cfg.backoff_base,
            # 403 is deliberately absent: on these sites it means "your client is
            # not welcome", and hammering it is both futile and rude.
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD"}),
            raise_on_status=False,
            respect_retry_after_header=True,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=8, pool_maxsize=16)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        self._session.headers.update(
            {
                "User-Agent": cfg.user_agent,
                "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
            }
        )

        if cfg.ca_bundle:
            self._session.verify = cfg.ca_bundle
        if cfg.proxy:
            self._session.proxies = {"http": cfg.proxy, "https": cfg.proxy}

    # ---- core ----------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Response:
        """GET a URL, or raise ``FetchError``/``TlsTrustError``."""
        self._limiter.wait()
        started = time.monotonic()
        try:
            resp = self._session.get(
                url,
                params=params,
                headers=headers,
                timeout=timeout or self.cfg.request_timeout,
                allow_redirects=True,
            )
        except requests.exceptions.SSLError as exc:
            raise TlsTrustError(self._tls_help(url, exc)) from exc
        except requests.exceptions.ProxyError as exc:
            raise FetchError(
                f"Proxy refused the connection to {url}. "
                f"Check the 'proxy' setting or HTTPS_PROXY. Original error: {exc}"
            ) from exc
        except requests.exceptions.ConnectionError as exc:
            raise FetchError(f"Could not connect to {url}: {exc}") from exc
        except requests.exceptions.Timeout as exc:
            raise FetchError(
                f"{url} did not respond within {timeout or self.cfg.request_timeout}s"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise FetchError(f"Request to {url} failed: {exc}") from exc

        elapsed = time.monotonic() - started

        if resp.status_code == 403:
            raise FetchError(
                f"{url} returned 403 Forbidden. This site rejects the client as "
                f"configured; it usually needs a different User-Agent or is only "
                f"reachable through its own JSON API."
            )
        if resp.status_code == 404:
            raise FetchError(f"{url} returned 404 Not Found - the page has moved.")
        if resp.status_code >= 400:
            raise FetchError(f"{url} returned HTTP {resp.status_code}.")

        log.debug("GET %s -> %s in %.2fs (%d bytes)",
                  resp.url, resp.status_code, elapsed, len(resp.content))
        return Response(url=resp.url, status=resp.status_code, text=resp.text, elapsed=elapsed)

    def get_json(self, url: str, **kwargs: object) -> object:
        resp = self.get(url, **kwargs)  # type: ignore[arg-type]
        import json

        try:
            return json.loads(resp.text)
        except ValueError as exc:
            raise FetchError(f"{url} did not return valid JSON: {exc}") from exc

    def close(self) -> None:
        self._session.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- helpers -------------------------------------------------------

    @staticmethod
    def _tls_help(url: str, exc: Exception) -> str:
        return (
            f"TLS certificate verification failed for {url}.\n"
            f"  Underlying error: {exc}\n"
            "  This normally means a corporate TLS-inspecting proxy is re-signing "
            "traffic with a root certificate this Python does not trust.\n"
            "  Fix it by trusting that root, not by disabling verification:\n"
            "    1. Export your organisation's root CA bundle to a .pem file.\n"
            "    2. Set ca_bundle in config.toml, or the REQUESTS_CA_BUNDLE "
            "environment variable, to that file.\n"
            "    3. Re-run 'sro-tracker doctor' to confirm.\n"
            "  Certificate verification is never disabled by this application."
        )
