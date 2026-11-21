"""Playwright 会话层：持久化登录态、弹窗清理与稳健点击。

一个 context + 多个 page：正文抓取用主 page，资源/notebook 下载用独立 page 并行，
避免正文进度被大文件下载阻塞。
"""
from __future__ import annotations

from playwright.async_api import BrowserContext, Page, Playwright
from playwright.async_api import TimeoutError as PWTimeout
from playwright.async_api import async_playwright

from config import (
    DASHBOARD_URL,
    NAV_TIMEOUT,
    RENDER_TIMEOUT,
    UI_TIMEOUT,
    Credentials,
    Settings,
)
from logs import log

# 只屏蔽纯统计脚本，不影响业务接口
_BLOCK_PATTERNS = (
    "*://*.google-analytics.com/*",
    "*://*.googletagmanager.com/*",
    "*://*.doubleclick.net/*",
    "*://*.googlesyndication.com/*",
    "*://*.hotjar.com/*",
    "*://*.facebook.net/*",
    "*://*.segment.io/*",
    "*://*.sentry.io/*",
    "*://*.fullstory.com/*",
    "*://*.mixpanel.com/*",
    "*://*.amplitude.com/*",
)

_DISMISS_MODALS_JS = """() => {
    document.querySelectorAll('.chakra-portal').forEach(p => {
        if (p.querySelector('.chakra-modal__content, .chakra-modal__overlay')) p.remove();
    });
    document.querySelectorAll('.chakra-modal__overlay').forEach(e => e.remove());
    document.body.style.overflow = '';
    document.body.style.pointerEvents = '';
    document.body.removeAttribute('data-scroll-lock');
}"""


async def dismiss_modals(page: Page) -> None:
    """清掉残留的 chakra 弹层（课程评分弹窗、workspace 提示等）。"""
    try:
        await page.evaluate(_DISMISS_MODALS_JS)
    except Exception:
        pass


async def safe_click(locator, page: Page, timeout: int = UI_TIMEOUT) -> None:
    """点击前清理遮挡层；被拦截时清一次再 force 点击。"""
    await dismiss_modals(page)
    try:
        await locator.click(timeout=timeout)
    except Exception:
        await dismiss_modals(page)
        await locator.click(timeout=timeout, force=True)


async def wait_until_quiet(page: Page, timeout: int = 15_000) -> None:
    """等待网络基本静默；超时不算错误（Udacity 有长连接埋点）。"""
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout)
    except PWTimeout:
        pass


async def _block_route(route) -> None:
    try:
        await route.abort()
    except Exception:
        pass


class BrowserSession:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self._pages: list[Page] = []

    async def __aenter__(self) -> "BrowserSession":
        self._playwright = await async_playwright().start()
        launch_kwargs = dict(
            user_data_dir=str(self.settings.profile),
            headless=self.settings.headless,
            accept_downloads=True,
            downloads_path=str(self.settings.assets_root),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            self.context = await self._playwright.chromium.launch_persistent_context(
                channel=self.settings.channel, **launch_kwargs)
        except Exception as exc:  # 本机没装 Chrome 时退回自带 chromium
            log.warning("启动 %s 失败（%s），改用内置 chromium", self.settings.channel, exc)
            self.context = await self._playwright.chromium.launch_persistent_context(**launch_kwargs)

        log.debug("Chrome 已启动：headless=%s, channel=%s", self.settings.headless, self.settings.channel)
        self.context.set_default_timeout(UI_TIMEOUT)
        self.context.set_default_navigation_timeout(NAV_TIMEOUT)
        if self.settings.block_analytics:
            for pattern in _BLOCK_PATTERNS:
                await self.context.route(pattern, _block_route)
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self.context:
            try:
                await self.context.close()
            except Exception:
                pass
        if self._playwright:
            await self._playwright.stop()

    async def new_page(self) -> Page:
        page = await self.context.new_page()
        page.set_default_timeout(UI_TIMEOUT)
        page.set_default_navigation_timeout(NAV_TIMEOUT)
        self._pages.append(page)
        return page

    # ------------------------------------------------------------------ 登录
    async def login(self, page: Page, credentials: Credentials) -> None:
        await page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=NAV_TIMEOUT)
        await wait_until_quiet(page, 6_000)
        if "auth.udacity.com" not in page.url:
            log.info("已登录（复用 %s 的会话）", self.settings.profile.name)
            return

        if self.settings.headless:
            log.warning("headless 模式下无法人工完成登录（可能需验证码）；若登录失败请改用 --headed 先登录一次")

        log.info("需要登录，使用 %s", credentials.email)
        email_input = page.locator('input[name="email"]').first
        try:
            await email_input.wait_for(state="visible", timeout=RENDER_TIMEOUT)
        except PWTimeout:
            await page.screenshot(path=str(self.settings.out / "_login_debug.png"))
            raise RuntimeError("登录页未出现邮箱输入框，截图见 _login_debug.png")

        await email_input.fill(credentials.email)
        await page.locator('input[name="password"]').first.fill(credentials.password)
        await page.locator('form:has(input[name="email"]) button[type="submit"]').first.click(
            no_wait_after=True)
        try:
            await page.wait_for_url(lambda url: "auth.udacity.com" not in url, timeout=RENDER_TIMEOUT)
        except PWTimeout:
            pass
        await wait_until_quiet(page, 8_000)
        if "auth.udacity.com" in page.url:
            raise RuntimeError("登录失败：请检查 login.txt 中的邮箱与密码")
        log.info("登录成功")