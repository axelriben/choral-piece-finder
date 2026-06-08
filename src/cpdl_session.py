"""
src/cpdl_session.py — reusable CPDL HTTP session with automatic Cloudflare bypass.

CPDL (www2.cpdl.org) is protected by Cloudflare's "managed challenge" — the
JS-powered "Just a moment…" interstitial that blocks plain HTTP clients with a
403.  This module solves that in two layers:

  1. Playwright launches a real Chrome window, navigates to the CPDL main page,
     and waits until the Cloudflare challenge resolves.  It then captures the
     resulting cf_clearance cookie — Cloudflare's token for previously-verified
     browsers.

  2. That cookie is injected into a curl_cffi session configured with
     impersonate="chrome", which mirrors Chrome's TLS fingerprint and HTTP/2
     behaviour.  Plain requests/httpx would re-trigger the challenge even with
     the correct cookie because their TLS fingerprint is recognizably non-browser.

CPDLSession tracks cookie age and proactively re-runs the Playwright step when
cookies approach expiry (default: refresh after 20 minutes, comfortably inside
Cloudflare's ~30–60 min window).  This fixes the "tail of the crawl gets 403s"
problem that occurred with the original single-cookie-on-startup approach.

Usage:
    from cpdl_session import get_cpdl_session

    session = get_cpdl_session()
    resp = session.get("https://www2.cpdl.org/wiki/api.php", params={...})
"""

import logging
import time
from typing import Any

from curl_cffi import requests as cffi_requests
from playwright.sync_api import sync_playwright

log = logging.getLogger(__name__)

_USER_AGENT = (
    "ChoralPieceFinder/0.1 (university IR lab; axel.riben2@gmail.com; "
    "https://github.com/axelriben/choral-piece-finder) python-requests/2.x"
)

# Cloudflare interstitial page titles (any language substring)
_CF_INTERSTITIALS = ("just a moment", "vänta", "einen moment")

# Module-level singleton — one Playwright run per process
_session_instance: "CPDLSession | None" = None


class CPDLSession:
    """HTTP session for CPDL with transparent Cloudflare cookie management.

    Wraps a curl_cffi session (Chrome TLS impersonation) and re-acquires
    Cloudflare cookies via Playwright whenever they become stale.
    """

    # The main CPDL homepage used for the Playwright challenge pass.
    # www.cpdl.org redirects to the same backend as www2.cpdl.org and the
    # resulting cf_clearance cookie is scoped to .cpdl.org, covering all subdomains.
    _CF_HOMEPAGE = "https://www.cpdl.org/wiki/index.php/Main_Page"

    def __init__(
        self,
        cookie_lifetime_minutes: int = 20,
        backup_servers: list[str] | None = None,
        rate_limit_per_sec: float = 1.0,
    ) -> None:
        self.cookie_lifetime_minutes = cookie_lifetime_minutes
        # Primary server first; failover attempts use the rest in order.
        self.backup_servers: list[str] = backup_servers or ["www2.cpdl.org", "www1.cpdl.org"]
        self.rate_limit_per_sec = rate_limit_per_sec

        self._cffi_session: cffi_requests.Session | None = None
        self._cookie_acquired_at: float | None = None
        self._last_request_time: float = 0.0

    # ------------------------------------------------------------------
    # Cookie management
    # ------------------------------------------------------------------

    @property
    def _cookies_stale(self) -> bool:
        if self._cookie_acquired_at is None:
            return True
        age_min = (time.monotonic() - self._cookie_acquired_at) / 60
        return age_min >= self.cookie_lifetime_minutes

    def _refresh_cookies(self) -> None:
        """Run Playwright, pass the Cloudflare challenge, load cookies into the session."""
        if self._cookie_acquired_at is not None:
            age_min = (time.monotonic() - self._cookie_acquired_at) / 60
            log.info("Refreshing Cloudflare cookies (cookie age: %.1f min)", age_min)
        else:
            log.info("Acquiring Cloudflare cookies via browser (first request)…")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                channel="chrome",
                headless=False,
                args=["--disable-blink-features=AutomationControlled"],
            )
            ctx = browser.new_context(user_agent=_USER_AGENT)
            # Hide navigator.webdriver — the primary automation signal Cloudflare checks.
            ctx.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )
            page = ctx.new_page()
            page.goto(self._CF_HOMEPAGE, timeout=60_000)

            # Poll until the interstitial disappears (usually 5–10 s)
            for _ in range(30):
                title = page.title().lower()
                if not any(s in title for s in _CF_INTERSTITIALS):
                    break
                log.info("  waiting for Cloudflare challenge… (%s)", page.title())
                time.sleep(2)
            else:
                log.warning("Cloudflare challenge may not have resolved; proceeding anyway")

            cookies = ctx.cookies()
            browser.close()

        log.info("Browser cookies obtained (%d cookies)", len(cookies))

        session = cffi_requests.Session(impersonate="chrome")
        session.headers.update(
            {
                "User-Agent": _USER_AGENT,
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        for ck in cookies:
            session.cookies.set(ck["name"], ck["value"], domain=ck.get("domain", ""))

        self._cffi_session = session
        self._cookie_acquired_at = time.monotonic()

    def _ensure_fresh(self) -> None:
        """Refresh cookies if stale; called before every outbound request."""
        if self._cookies_stale:
            self._refresh_cookies()

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _rate_limit(self) -> None:
        """Sleep if necessary to honour rate_limit_per_sec."""
        if self.rate_limit_per_sec <= 0:
            return
        min_gap = 1.0 / self.rate_limit_per_sec
        elapsed = time.monotonic() - self._last_request_time
        if elapsed < min_gap:
            time.sleep(min_gap - elapsed)
        self._last_request_time = time.monotonic()

    # ------------------------------------------------------------------
    # Backup server failover
    # ------------------------------------------------------------------

    def _with_failover(self, method: str, url: str, **kwargs: Any):
        """Dispatch *method* to *url*, retrying with backup server domains on failure."""
        primary = self.backup_servers[0] if self.backup_servers else None
        try:
            return getattr(self._cffi_session, method)(url, **kwargs)
        except Exception as primary_exc:
            for backup in self.backup_servers[1:]:
                if primary and primary in url:
                    fallback_url = url.replace(primary, backup, 1)
                    try:
                        log.warning(
                            "Primary server %s unreachable; retrying with %s",
                            primary,
                            backup,
                        )
                        return getattr(self._cffi_session, method)(fallback_url, **kwargs)
                    except Exception:
                        continue
            raise primary_exc

    # ------------------------------------------------------------------
    # Public request methods
    # ------------------------------------------------------------------

    def get(self, url: str, **kwargs: Any):
        """Send a GET request through the authenticated CPDL session."""
        self._ensure_fresh()
        self._rate_limit()
        return self._with_failover("get", url, **kwargs)

    def head(self, url: str, **kwargs: Any):
        """Send a HEAD request through the authenticated CPDL session."""
        self._ensure_fresh()
        self._rate_limit()
        return self._with_failover("head", url, **kwargs)

    def post(self, url: str, **kwargs: Any):
        """Send a POST request through the authenticated CPDL session."""
        self._ensure_fresh()
        self._rate_limit()
        return self._with_failover("post", url, **kwargs)


def get_cpdl_session(
    cookie_lifetime_minutes: int = 20,
    backup_servers: list[str] | None = None,
    rate_limit_per_sec: float = 1.0,
) -> CPDLSession:
    """Return the module-level CPDLSession, creating it on first call.

    The singleton is intentional: Playwright runs exactly once per process
    (or once per *cookie_lifetime_minutes* window), and all callers share the
    same authenticated session.
    """
    global _session_instance
    if _session_instance is None:
        _session_instance = CPDLSession(
            cookie_lifetime_minutes=cookie_lifetime_minutes,
            backup_servers=backup_servers,
            rate_limit_per_sec=rate_limit_per_sec,
        )
    return _session_instance
