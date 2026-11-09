"""
Headless browser tools — backed by the persistent BrowserManager.

- navigate(): fast read-only page text extraction.
- browser_act(): interactive browser agent powered by browser-use.

Both functions return (result_str, screenshot_b64_or_None).

The browser manager connects to a long-running Chromium container over CDP
(http://browser:9222) and maintains named contexts so sessions survive
between requests.  If the container isn't ready yet the manager will retry
the connection automatically.
"""
import base64

from ..browser_manager import get_browser_manager


async def navigate(url: str, context_name: str = "default") -> tuple[str, str | None]:
    """
    Navigate to a URL in the persistent browser and return (text_content, screenshot_b64).
    screenshot_b64 is only set on error — success returns None.
    """
    mgr = get_browser_manager()
    return await mgr.navigate(url, context_name=context_name)


async def browser_act(task: str, context_name: str = "default") -> tuple[str, str | None]:
    """
    Execute a multi-step browser task using an LLM-driven agent in the persistent browser.
    Returns (result_str, screenshot_b64). Screenshot is the last frame taken by the agent,
    captured on both success and failure.
    """
    mgr = get_browser_manager()
    return await mgr.act(task, context_name=context_name)
