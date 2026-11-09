"""
Persistent browser manager — connects to a long-running Chromium container over CDP.

The Chromium container runs with --remote-debugging-port=9222 and is accessible at
http://browser:9222 inside the Docker network.

Named contexts ("sessions") persist between requests — cookies, localStorage, and auth
all survive across calls.  The connection is re-established automatically if it drops.

CDP WebSocket resolution
------------------------
Playwright's connect_over_cdp() normally POSTs to /json/version/ to discover the
browser WebSocket URL.  Some Chromium builds (e.g. zenika/alpine-chrome) return HTTP
500 on that endpoint while /json/ or /json/list return perfectly valid JSON with a
``webSocketDebuggerUrl`` field.

_resolve_ws_url() probes several fallback endpoints and returns a ws:// URL that
Playwright can use directly — bypassing the broken /json/version/ endpoint.
"""
import asyncio
import base64
import socket
import subprocess
from typing import Optional
from urllib.parse import urlparse, urlunparse

from . import memory as mem

# CDP host from environment or default (override in tests)
import os

CDP_URL = os.environ.get("BROWSER_CDP_URL", "http://browser:9222")
_CONNECT_RETRIES = 10
_CONNECT_RETRY_DELAY = 3.0  # seconds between retries


def _browser_ip(hostname: str = "browser", port: int = 9222) -> str:
    """Resolve *hostname* to an IP address via DNS.

    Connecting to the raw IP bypasses Chromium's Host-header origin check (it only
    rejects named hostnames that aren't "localhost").  Returns the dotted-quad IPv4
    address, or the original hostname if resolution fails.
    """
    try:
        results = socket.getaddrinfo(hostname, port, socket.AF_INET, socket.SOCK_STREAM)
        ip = results[0][4][0]
        print(f"[browser_manager] resolved {hostname} → {ip}")
        return ip
    except Exception as e:
        print(f"[browser_manager] DNS resolution of {hostname!r} failed: {e} — using hostname")
        return hostname


def _rewrite_ws_url(ws_url: str, target_ip: str, target_port: int = 9222) -> str:
    """Replace the host:port portion of a CDP ws:// URL with *target_ip*:*target_port*.

    Chromium returns URLs like ``ws://localhost/devtools/browser/<UUID>`` or
    ``ws://0.0.0.0/devtools/…``.  This function rewrites them so that Playwright
    connects to the actual Docker-routable address instead of the loopback reported
    by Chromium.  Using a bare IP also skips Chromium's Host-header check.
    """
    parsed = urlparse(ws_url)
    rewritten = parsed._replace(netloc=f"{target_ip}:{target_port}")
    return urlunparse(rewritten)


async def _resolve_ws_url(http_base: str) -> str:
    """Resolve the CDP WebSocket URL from the Chromium HTTP debugging endpoint.

    Playwright's built-in discovery calls /json/version/, which some Chromium builds
    (e.g. zenika/alpine-chrome) answer with HTTP 500.  We therefore probe several
    alternative endpoints and extract the ``webSocketDebuggerUrl`` ourselves, then
    hand the raw ws:// URL directly to connect_over_cdp().

    Probe order (first success wins):
      1. GET {base}/json/version/   — standard endpoint, works on most Chrome builds
      2. GET {base}/json/browser/   — some builds expose the browser target here
      3. GET {base}/json            — returns a JSON array; use the first entry that
                                      has a webSocketDebuggerUrl
      4. GET {base}/json/list       — same array format, alternate path

    Host-header / WebSocket-origin fix
    ------------------------------------
    Chromium validates the ``Host`` header on CDP WebSocket upgrades and rejects
    connections whose Host is a non-IP non-localhost name (e.g. "browser").  To
    work around this we:

      1. Send ``Host: localhost`` on the HTTP probes so Chromium answers them.
      2. Resolve the Docker hostname "browser" to its container IP via DNS.
      3. Rewrite the returned ``ws://localhost/…`` URL to ``ws://<IP>:9222/…``.

    Connecting via the raw IP satisfies Chromium's origin check without needing to
    forge a Host header on the WebSocket upgrade (which Playwright doesn't allow).

    Args:
        http_base: The base HTTP URL, e.g. ``http://browser:9222``.

    Returns:
        A ``ws://`` WebSocket URL, or *http_base* as a last-resort fallback.
    """
    import httpx

    base = http_base.rstrip("/")
    ws_url: Optional[str] = None

    # Chromium rejects CDP HTTP requests whose Host header is a non-IP hostname
    # (e.g. "browser").  Sending Host: localhost satisfies its origin check.
    _cdp_headers = {"Host": "localhost"}

    # Resolve the container IP once; we'll rewrite every ws:// URL we find.
    browser_ip = _browser_ip("browser", 9222)

    async with httpx.AsyncClient(timeout=5.0) as client:
        # ── Probe 1: /json/version/ ──────────────────────────────────────────
        try:
            r = await client.get(f"{base}/json/version/", headers=_cdp_headers)
            if r.status_code == 200:
                data = r.json()
                ws_url = data.get("webSocketDebuggerUrl")
                if ws_url:
                    ws_url = _rewrite_ws_url(ws_url, browser_ip)
                    print(f"[browser_manager] ws URL from /json/version/: {ws_url}")
                    return ws_url
            else:
                print(
                    f"[browser_manager] /json/version/ returned {r.status_code}, "
                    "trying fallback endpoints…"
                )
        except Exception as e:
            print(f"[browser_manager] /json/version/ probe failed: {e}")

        # ── Probe 2: /json/browser/ ──────────────────────────────────────────
        try:
            r = await client.get(f"{base}/json/browser/", headers=_cdp_headers)
            if r.status_code == 200:
                data = r.json()
                ws_url = data.get("webSocketDebuggerUrl")
                if ws_url:
                    ws_url = _rewrite_ws_url(ws_url, browser_ip)
                    print(f"[browser_manager] ws URL from /json/browser/: {ws_url}")
                    return ws_url
        except Exception as e:
            print(f"[browser_manager] /json/browser/ probe failed: {e}")

        # ── Probe 3: /json and /json/list (array) ────────────────────────────
        for path in ("/json", "/json/list"):
            try:
                r = await client.get(f"{base}{path}", headers=_cdp_headers)
                if r.status_code == 200:
                    data = r.json()
                    # data may be a list of target descriptors or a dict
                    entries = data if isinstance(data, list) else [data]
                    for entry in entries:
                        ws_url = entry.get("webSocketDebuggerUrl") if isinstance(entry, dict) else None
                        if ws_url:
                            ws_url = _rewrite_ws_url(ws_url, browser_ip)
                            print(f"[browser_manager] ws URL from {path}: {ws_url}")
                            return ws_url
            except Exception as e:
                print(f"[browser_manager] {path} probe failed: {e}")

    # ── Last resort: return the http base and let Playwright try ─────────────
    print(
        f"[browser_manager] all ws-URL probes failed — falling back to {http_base} "
        "and letting Playwright attempt its own discovery"
    )
    return http_base

# ── 1Password helper ──────────────────────────────────────────────────────────

def get_1password_credential(item_name: str, field: str = "password") -> str:
    """Fetch a credential from the Ki vault in 1Password using the op CLI.

    Requires OP_SERVICE_ACCOUNT_TOKEN to be set in the environment and the
    1Password CLI ('op') to be installed.  The vault used is always 'Ki'.

    Args:
        item_name: The item name (or UUID) in 1Password.
        field:     The field label to retrieve (default 'password').
                   Pass 'otp', 'totp', or 'one-time password' to get the
                   current TOTP code via --otp (fetches a fresh code each call).

    Returns:
        The field value as a stripped string.

    Raises:
        Exception: If the CLI exits non-zero or the token is missing.
    """
    token = os.environ.get("OP_SERVICE_ACCOUNT_TOKEN", "")
    if not token:
        raise Exception(
            "OP_SERVICE_ACCOUNT_TOKEN is not set — cannot fetch 1Password credentials."
        )

    # TOTP: use --otp which returns the *current* 6-digit code (refreshed each call)
    if field.lower() in ("otp", "totp", "one-time password"):
        result = subprocess.run(
            ["op", "item", "get", item_name, "--otp", "--vault", "Ki"],
            capture_output=True,
            text=True,
            env={**os.environ, "OP_SERVICE_ACCOUNT_TOKEN": token},
        )
        if result.returncode != 0:
            raise Exception(f"1Password CLI error: {result.stderr.strip()}")
        return result.stdout.strip()

    result = subprocess.run(
        ["op", "item", "get", item_name, "--vault", "Ki", "--fields", field],
        capture_output=True,
        text=True,
        env={**os.environ, "OP_SERVICE_ACCOUNT_TOKEN": token},
    )
    if result.returncode != 0:
        raise Exception(f"1Password CLI error: {result.stderr.strip()}")
    return result.stdout.strip()


def _build_totp_controller():
    """Build a browser-use Controller with a get_totp_code action.

    The action fetches a fresh TOTP code from 1Password immediately when the
    browser agent calls it — guaranteeing the code is current at the moment of
    entry regardless of how long the preceding steps took.

    Returns None if browser-use is not installed or Controller cannot be built.
    """
    try:
        from browser_use import Controller

        controller = Controller()

        @controller.action(
            "Fetch a fresh TOTP / one-time password from 1Password. "
            "Call this immediately before you need to type a 2FA or authentication code — "
            "TOTP codes expire every 30 seconds so fetch at the last possible moment."
        )
        def get_totp_code(item_name: str) -> str:
            """item_name: the 1Password item name, e.g. 'HEY', 'GitHub', 'Gmail'"""
            print(f"[browser_manager] fetching TOTP for {item_name!r} from 1Password")
            code = get_1password_credential(item_name, field="otp")
            print(f"[browser_manager] TOTP fetched for {item_name!r} ({len(code)} chars)")
            return code

        print("[browser_manager] TOTP controller built — get_totp_code action registered")
        return controller
    except Exception as e:
        print(f"[browser_manager] could not build TOTP controller: {e}")
        return None


# ── Browser manager ───────────────────────────────────────────────────────────

class BrowserManager:
    """Manages a persistent Playwright connection to a remote Chromium container.

    Usage::

        mgr = BrowserManager()
        page = await mgr.get_or_create_page("gmail")
        await mgr.navigate("https://mail.google.com", context_name="gmail")
        text = await mgr.get_text(context_name="gmail")
        await mgr.close_context("gmail")

    Named *contexts* map onto Playwright BrowserContexts — each has its own
    cookies and localStorage so sessions remain isolated.  Within each context
    a single Page is reused (lazy-created on first use).
    """

    def __init__(self, cdp_url: str = CDP_URL) -> None:
        self._cdp_url = cdp_url
        self._playwright = None
        self._browser = None
        # context_name → playwright BrowserContext
        self._contexts: dict[str, object] = {}
        # context_name → playwright Page (one per context)
        self._pages: dict[str, object] = {}
        self._lock = asyncio.Lock()
        self._current_act_task: asyncio.Task | None = None

    def cancel(self) -> None:
        """Cancel any in-progress act() call. Called by /kill."""
        if self._current_act_task and not self._current_act_task.done():
            self._current_act_task.cancel()
            print("[browser_manager] cancelled running browser agent task")

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _ensure_connected(self) -> None:
        """Connect to the Chromium CDP endpoint if not already connected."""
        if self._browser is not None:
            try:
                # Quick connectivity check — is_connected() returns False if the
                # underlying WebSocket has closed.
                if self._browser.is_connected():
                    return
            except Exception:
                pass
            # Connection dead — clean up before reconnecting
            print("[browser_manager] connection lost, reconnecting…")
            self._contexts.clear()
            self._pages.clear()
            self._browser = None

        from playwright.async_api import async_playwright

        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        for attempt in range(1, _CONNECT_RETRIES + 1):
            try:
                # Resolve the actual ws:// endpoint before handing to Playwright.
                # Some Chromium builds (e.g. zenika/alpine-chrome) return HTTP 500
                # on /json/version/ — the endpoint Playwright probes internally —
                # while /json/ or /json/list expose the real WebSocket URL just fine.
                ws_url = await _resolve_ws_url(self._cdp_url)
                print(
                    f"[browser_manager] connecting to {ws_url} "
                    f"(attempt {attempt}/{_CONNECT_RETRIES})"
                )
                self._playwright = await async_playwright().start()
                self._browser = await self._playwright.chromium.connect_over_cdp(ws_url)
                print("[browser_manager] connected to Chromium")
                return
            except Exception as e:
                print(f"[browser_manager] connect attempt {attempt} failed: {e}")
                if self._playwright:
                    try:
                        await self._playwright.stop()
                    except Exception:
                        pass
                    self._playwright = None
                if attempt < _CONNECT_RETRIES:
                    await asyncio.sleep(_CONNECT_RETRY_DELAY)
        raise RuntimeError(
            f"[browser_manager] could not connect to Chromium at {self._cdp_url} "
            f"after {_CONNECT_RETRIES} attempts"
        )

    async def get_or_create_context(self, name: str = "default") -> object:
        """Return an existing BrowserContext or create a new one."""
        async with self._lock:
            await self._ensure_connected()
            if name not in self._contexts:
                ctx = await self._browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                    ),
                )
                self._contexts[name] = ctx
                print(f"[browser_manager] created context: {name!r}")
            return self._contexts[name]

    async def get_or_create_page(self, context_name: str = "default") -> object:
        """Return the page for a context, creating it if needed."""
        ctx = await self.get_or_create_context(context_name)
        async with self._lock:
            if context_name not in self._pages:
                page = await ctx.new_page()
                self._pages[context_name] = page
                print(f"[browser_manager] created page for context: {context_name!r}")
            return self._pages[context_name]

    # ── Public operations ─────────────────────────────────────────────────────

    async def navigate(
        self, url: str, context_name: str = "default", timeout: int = 30_000
    ) -> tuple[str, Optional[str]]:
        """Navigate to *url* in the named context and return (text, screenshot_b64|None).

        The screenshot is only captured on error; on success it is None.
        """
        try:
            page = await self.get_or_create_page(context_name)
            await page.goto(url, timeout=timeout, wait_until="domcontentloaded")
            # Strip noise elements
            await page.evaluate("""() => {
                for (const sel of ['script','style','nav','footer','header','aside','noscript']) {
                    for (const el of document.querySelectorAll(sel)) el.remove();
                }
            }""")
            text = await page.inner_text("body")
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            result = "\n".join(lines)
            if len(result) > 4000:
                result = result[:4000] + "\n[truncated]"
            return result or "(no text content)", None
        except Exception as e:
            print(f"[browser_manager] navigate error {url}: {e}")
            return f"Error navigating to {url}: {e}", None

    async def act(
        self, task: str, context_name: str = "default"
    ) -> tuple[str, Optional[str]]:
        """Execute a multi-step browser task using browser-use inside the named context.

        Uses BrowserSession to inject our persistent Playwright page into the
        browser-use Agent so that cookies, localStorage, and auth state are fully
        preserved across calls.

        LLM: browser_use.llm.anthropic.chat.ChatAnthropic — the native class
        shipped with browser-use; no LangChain dependency required.

        Returns (result_str, screenshot_b64|None).
        """
        try:
            from browser_use import Agent as BrowserAgent
            from browser_use.browser.session import BrowserSession
            from browser_use.llm.anthropic.chat import ChatAnthropic
            import inspect as _inspect

            # ── Instantiate the LLM ─────────────────────────────────────────────
            llm = ChatAnthropic(
                model="claude-sonnet-4-5",
                api_key=os.environ["ANTHROPIC_API_KEY"],
                temperature=0.0,
            )
            print("[browser_manager] LLM: browser_use ChatAnthropic(model=claude-sonnet-4-5)")

            # ── Build BrowserSession ────────────────────────────────────────────
            ws_url = await _resolve_ws_url(self._cdp_url)
            _bs_sig = _inspect.signature(BrowserSession.__init__)
            _bs_params = set(_bs_sig.parameters)
            if "cdp_url" in _bs_params:
                _bs_kwarg = "cdp_url"
            elif "wss_url" in _bs_params:
                _bs_kwarg = "wss_url"
            else:
                _bs_kwarg = next(
                    (p for p in _bs_params if "cdp" in p.lower() or ("url" in p.lower() and p != "self")),
                    None,
                )
            if _bs_kwarg:
                browser_session = BrowserSession(**{_bs_kwarg: ws_url})
                print(f"[browser_manager] BrowserSession({_bs_kwarg}={ws_url!r})")
            else:
                print(f"[browser_manager] BrowserSession params={list(_bs_params)}, using no cdp kwarg")
                browser_session = BrowserSession()

            # ── Introspect Agent so we only pass supported kwargs ───────────────
            agent_sig = _inspect.signature(BrowserAgent.__init__)
            agent_params = set(agent_sig.parameters)

            base_kwargs: dict = dict(
                task=task,
                llm=llm,
                browser_session=browser_session,
            )

            # Build TOTP controller — registered action lets the agent fetch a
            # fresh code from 1Password at the exact moment it needs to type it.
            _controller = _build_totp_controller()

            optional_kwargs: dict = dict(
                max_failures=5,
                use_vision=False,
                generate_gif=False,
            )
            if _controller is not None:
                optional_kwargs["controller"] = _controller

            agent_kwargs = {
                **base_kwargs,
                **{k: v for k, v in optional_kwargs.items() if k in agent_params},
            }

            # ── Run the agent ───────────────────────────────────────────────────
            print(f"[browser_manager] act starting: {task[:120]}")
            agent = BrowserAgent(**agent_kwargs)
            self._current_act_task = asyncio.ensure_future(agent.run())
            try:
                history = await self._current_act_task
            except asyncio.CancelledError:
                print("[browser_manager] act cancelled by /kill")
                return "Cancelled.", None
            finally:
                self._current_act_task = None
            result = history.final_result()
            print(f"[browser_manager] act done: {str(result)[:120]}")

            # ── Track LLM token usage ───────────────────────────────────────────
            try:
                _b_input = history.total_input_tokens() if hasattr(history, 'total_input_tokens') else 0
                _b_output = history.total_output_tokens() if hasattr(history, 'total_output_tokens') else 0
                if _b_input or _b_output:
                    await mem.log_token_usage("claude-sonnet-4-5", _b_input, _b_output, use_case="browser_agent")
                    print(f"[browser_manager] logged browser LLM usage: {_b_input} in / {_b_output} out")
            except Exception as _usage_err:
                print(f"[browser_manager] could not log browser LLM usage: {_usage_err}")

            return result or "(task completed, no output)", None

        except Exception as e:
            print(f"[browser_manager] act error: {e}")
            screenshot = await self._try_screenshot(context_name)
            return f"Browser agent error: {e}", screenshot

    async def get_text(self, context_name: str = "default") -> str:
        """Return the current page's body text (stripped)."""
        try:
            page = await self.get_or_create_page(context_name)
            text = await page.inner_text("body")
            lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
            return "\n".join(lines) or "(no text)"
        except Exception as e:
            return f"Error getting page text: {e}"

    async def screenshot(self, context_name: str = "default") -> Optional[str]:
        """Capture and return a base64-encoded PNG screenshot of the current page."""
        return await self._try_screenshot(context_name)

    async def click(self, selector: str, context_name: str = "default") -> str:
        """Click an element matching *selector* on the current page."""
        try:
            page = await self.get_or_create_page(context_name)
            await page.click(selector, timeout=10_000)
            return f"Clicked: {selector}"
        except Exception as e:
            return f"Click failed ({selector}): {e}"

    async def type_text(
        self, selector: str, text: str, context_name: str = "default"
    ) -> str:
        """Type *text* into an element matching *selector*."""
        try:
            page = await self.get_or_create_page(context_name)
            await page.fill(selector, text)
            return f"Typed into: {selector}"
        except Exception as e:
            return f"Type failed ({selector}): {e}"

    async def evaluate(
        self, js: str, context_name: str = "default"
    ) -> str:
        """Evaluate a JavaScript expression on the current page and return its string repr."""
        try:
            page = await self.get_or_create_page(context_name)
            result = await page.evaluate(js)
            return str(result)
        except Exception as e:
            return f"JS evaluate failed: {e}"

    async def close_context(self, name: str) -> None:
        """Close and remove a named context (and its page)."""
        async with self._lock:
            page = self._pages.pop(name, None)
            if page:
                try:
                    await page.close()
                except Exception:
                    pass
            ctx = self._contexts.pop(name, None)
            if ctx:
                try:
                    await ctx.close()
                except Exception:
                    pass
            print(f"[browser_manager] closed context: {name!r}")

    async def close_all(self) -> None:
        """Close all contexts and disconnect from Chromium."""
        for name in list(self._contexts.keys()):
            await self.close_context(name)
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
        print("[browser_manager] disconnected")

    # ── Private helpers ───────────────────────────────────────────────────────

    async def _try_screenshot(self, context_name: str) -> Optional[str]:
        """Attempt to capture a PNG screenshot of the current browser page.

        Tries three layers in order, returning the first that succeeds:

        Layer 1 — Managed page (self._pages):
            Used when navigate() / get_or_create_page() opened the page
            directly through this manager.

        Layer 2 — CDP active targets:
            When browser-use runs its own Playwright session the page is not
            in self._pages.  We ask the connected browser for all active
            Page targets and screenshot whichever one is currently visible /
            most recently created.  This is the common path during act().

        Layer 3 — Raw CDP /json endpoint:
            Last resort — probe the browser's HTTP endpoint for any target
            with a webSocketDebuggerUrl and connect to it directly.

        Returns base64-encoded PNG, or None if all layers fail.
        """
        # ── Layer 1: managed page ──────────────────────────────────────────
        if context_name in self._pages:
            try:
                raw = await self._pages[context_name].screenshot(type="png")
                result = base64.b64encode(raw).decode("ascii")
                print(f"[browser_manager] screenshot via Layer 1 (managed page): {len(result)} chars")
                return result
            except Exception as ss_err:
                print(f"[browser_manager] Layer 1 screenshot failed: {ss_err}")

        # ── Layer 2: browser-use / CDP active targets ──────────────────────
        # When browser-use manages its own BrowserSession the page is not
        # tracked in self._pages.  Ask the connected browser for all open
        # pages and screenshot the first visible / active one.
        try:
            await self._ensure_connected()
            if self._browser is not None:
                contexts = self._browser.contexts
                # Collect all pages across all contexts
                all_pages = []
                for ctx in contexts:
                    all_pages.extend(ctx.pages)

                if all_pages:
                    # Prefer the last page (most recently opened / active)
                    page = all_pages[-1]
                    raw = await page.screenshot(type="png")
                    result = base64.b64encode(raw).decode("ascii")
                    print(
                        f"[browser_manager] screenshot via Layer 2 (CDP contexts, "
                        f"{len(all_pages)} page(s) found): {len(result)} chars"
                    )
                    return result
                else:
                    print("[browser_manager] Layer 2: browser connected but no pages found")
        except Exception as ss_err:
            print(f"[browser_manager] Layer 2 screenshot failed: {ss_err}")

        # ── Layer 3: raw CDP /json probe ───────────────────────────────────
        # Connect a fresh Playwright instance directly to whichever page
        # target is listed at the browser's HTTP debugging endpoint.
        try:
            import httpx as _httpx
            from playwright.async_api import async_playwright as _apw

            cdp_base = self._cdp_url.rstrip("/")
            _cdp_headers = {"Host": "localhost"}
            browser_ip = _browser_ip("browser", 9222)

            async with _httpx.AsyncClient(timeout=5.0) as client:
                for path in ("/json/list", "/json"):
                    try:
                        r = await client.get(f"{cdp_base}{path}", headers=_cdp_headers)
                        if r.status_code != 200:
                            continue
                        targets = r.json()
                        if not isinstance(targets, list):
                            targets = [targets]
                        # Find a page target (not a worker / service-worker)
                        page_targets = [
                            t for t in targets
                            if isinstance(t, dict)
                            and t.get("type") == "page"
                            and t.get("webSocketDebuggerUrl")
                        ]
                        if page_targets:
                            # Use the last one (most recently opened)
                            ws = _rewrite_ws_url(page_targets[-1]["webSocketDebuggerUrl"], browser_ip)
                            print(f"[browser_manager] Layer 3: attaching to page target {ws!r}")
                            async with _apw() as pw:
                                browser = await pw.chromium.connect_over_cdp(ws)
                                all_pages = [p for ctx in browser.contexts for p in ctx.pages]
                                if all_pages:
                                    raw = await all_pages[-1].screenshot(type="png")
                                    result = base64.b64encode(raw).decode("ascii")
                                    print(f"[browser_manager] Layer 3 screenshot: {len(result)} chars")
                                    return result
                    except Exception as _probe_err:
                        print(f"[browser_manager] Layer 3 probe {path} failed: {_probe_err}")
        except Exception as ss_err:
            print(f"[browser_manager] Layer 3 screenshot failed: {ss_err}")

        print("[browser_manager] all screenshot layers failed — returning None")
        return None


# ── Module-level singleton ────────────────────────────────────────────────────

_manager: Optional[BrowserManager] = None


def get_browser_manager() -> BrowserManager:
    """Return the module-level BrowserManager singleton, creating it if needed."""
    global _manager
    if _manager is None:
        _manager = BrowserManager()
    return _manager
