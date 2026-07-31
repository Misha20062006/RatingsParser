from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from patchright.async_api import BrowserContext, Page, Playwright, async_playwright

BASE_URL = "https://teslacraft.org"
ACCESS_CHECK_URL = f"{BASE_URL}/donate/"
SITE_READY_SELECTOR = ".rekt_titleContainer"


class TeslaCraftAccessError(RuntimeError):
    """The site did not become available after the Cloudflare wait window."""


class BrowserController:
    def __init__(
        self,
        *,
        profile_dir: Path,
        request_timeout_seconds: float = 60,
        captcha_timeout_seconds: float = 300,
        retries: int = 3,
        retry_delay_seconds: float = 1,
    ) -> None:
        self.profile_dir = profile_dir.resolve()
        self.request_timeout_ms = int(request_timeout_seconds * 1000)
        self.captcha_timeout_seconds = captcha_timeout_seconds
        self.retries = retries
        self.retry_delay_seconds = retry_delay_seconds
        self._playwright: Playwright | None = None
        self.context: BrowserContext | None = None
        self._access_page: Page | None = None
        self._access_lock = asyncio.Lock()

    async def __aenter__(self) -> BrowserController:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = await async_playwright().start()
        try:
            self.context = await self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=False,
                channel="chrome",
                no_viewport=True,
                args=[
                    "--disable-background-networking",
                    "--disable-component-update",
                    "--disable-extensions",
                    "--disable-notifications",
                    "--disable-sync",
                    "--mute-audio",
                    "--no-first-run",
                ],
            )
            await self.ensure_access()
        except BaseException:
            await self._shutdown()
            raise
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._shutdown()

    async def _shutdown(self) -> None:
        if self.context is not None:
            try:
                await self.context.close()
            except Exception:
                pass
            finally:
                self.context = None
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            finally:
                self._playwright = None
        self._access_page = None

    async def ensure_access(self) -> None:
        """Wait for a normal site page, allowing a human to pass the challenge.

        The unrestricted page is intentionally kept separate from worker pages:
        workers block heavy resources, whereas a Cloudflare challenge needs JS.
        """

        async with self._access_lock:
            if self.context is None:
                raise RuntimeError("Браузер ещё не запущен")

            if self._access_page is None or self._access_page.is_closed():
                self._access_page = await self.context.new_page()

            page = self._access_page
            print("Проверяю доступ к TeslaCraft…")
            with suppress(Exception):
                await page.goto(
                    ACCESS_CHECK_URL,
                    wait_until="domcontentloaded",
                    timeout=self.request_timeout_ms,
                )

            deadline = time.monotonic() + self.captcha_timeout_seconds
            notice_printed = False
            while time.monotonic() < deadline:
                try:
                    if await page.locator(SITE_READY_SELECTOR).count() > 0:
                        print("Доступ к TeslaCraft подтверждён.")
                        return
                except Exception:
                    pass

                if not notice_printed:
                    print(
                        "Если в Chrome открыта проверка Cloudflare, пройдите её "
                        "вручную. Профиль и cookies сохранятся для следующих запусков."
                    )
                    notice_printed = True
                await asyncio.sleep(2)

            raise TeslaCraftAccessError(
                "TeslaCraft не открылся за отведённое время. "
                "Проверьте окно Chrome, сеть и отсутствие VPN."
            )

    async def new_worker_page(self) -> Page:
        if self.context is None:
            raise RuntimeError("Браузер ещё не запущен")
        page = await self.context.new_page()

        async def route_handler(route: Any, request: Any) -> None:
            if request.resource_type == "document":
                await route.continue_()
            else:
                await route.abort()

        await page.route("**/*", route_handler)
        return page

    async def navigate(self, page: Page, url: str) -> None:
        last_error: BaseException | None = None
        for attempt in range(1, self.retries + 1):
            try:
                await page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=self.request_timeout_ms,
                )
                if await page.locator(SITE_READY_SELECTOR).count() == 0:
                    raise TeslaCraftAccessError(
                        "Вместо страницы TeslaCraft получена проверка Cloudflare"
                    )
                return
            except TeslaCraftAccessError as error:
                last_error = error
                await self.ensure_access()
            except Exception as error:
                last_error = error

            if attempt < self.retries:
                await asyncio.sleep(self.retry_delay_seconds * (2 ** (attempt - 1)))

        raise RuntimeError(
            f"Не удалось открыть {url} после {self.retries} попыток: {last_error}"
        )
