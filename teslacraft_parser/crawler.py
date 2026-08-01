from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from patchright.async_api import Page

from .browser import BrowserController
from .storage import RecordStore, append_error

ParserFunction = Callable[[Page, Any, str], Awaitable[dict[str, Any]]]
UrlFunction = Callable[[Any], str]


class CrawlCancelled(RuntimeError):
    """Raised after a user requests a controlled crawler cancellation."""


class CrawlControl:
    def __init__(self) -> None:
        self._resume = asyncio.Event()
        self._resume.set()
        self._cancelled = False
        self._cancel_signal = asyncio.Event()

    @property
    def paused(self) -> bool:
        return not self._resume.is_set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def pause(self) -> None:
        self._resume.clear()

    def resume(self) -> None:
        self._resume.set()

    def cancel(self) -> None:
        self._cancelled = True
        self._cancel_signal.set()
        self._resume.set()

    async def checkpoint(self) -> None:
        await self._resume.wait()
        if self._cancelled:
            raise CrawlCancelled("Остановлено пользователем")

    async def wait(self, seconds: float) -> None:
        """Wait between batches while allowing cancellation to interrupt the delay."""

        if seconds > 0 and not self._cancelled:
            with suppress(TimeoutError):
                await asyncio.wait_for(self._cancel_signal.wait(), timeout=seconds)
        await self.checkpoint()


@dataclass(slots=True)
class CrawlSummary:
    mode: str
    discovered: int = 0
    skipped: int = 0
    saved: int = 0
    failed: int = 0
    elapsed_seconds: float = 0


@dataclass(slots=True)
class ProgressEvent:
    mode: str
    processed: int
    total: int
    saved: int
    failed: int
    skipped: int
    elapsed_seconds: float
    message: str


ProgressCallback = Callable[[ProgressEvent], Any]


async def _emit_progress(
    callback: ProgressCallback | None,
    event: ProgressEvent,
) -> None:
    if callback is None:
        return
    result = callback(event)
    if inspect.isawaitable(result):
        await result


async def crawl_items(
    *,
    mode: str,
    controller: BrowserController,
    items: Iterable[Any],
    url_for: UrlFunction,
    parse: ParserFunction,
    store: RecordStore,
    error_path: Path,
    concurrency: int,
    delay_seconds: float,
    control: CrawlControl | None = None,
    on_progress: ProgressCallback | None = None,
) -> CrawlSummary:
    """Process stable-key jobs in ordered batches.

    Results are persisted after every batch. Existing JSONL keys are skipped,
    which makes relaunching after a crash safe and avoids duplicate records.
    """

    if concurrency < 1:
        raise ValueError("concurrency должна быть не меньше 1")

    partition_pending = getattr(store, "partition_pending", None)
    if callable(partition_pending):
        pending, skipped = partition_pending(items)
    else:
        pending = []
        skipped = 0
        for item in items:
            if item in store:
                skipped += 1
            else:
                pending.append(item)
    summary = CrawlSummary(
        mode=mode,
        discovered=len(pending),
        skipped=skipped,
    )
    if not pending:
        message = f"[{mode}] Новых элементов нет."
        print(message)
        await _emit_progress(
            on_progress,
            ProgressEvent(
                mode=mode,
                processed=0,
                total=0,
                saved=0,
                failed=0,
                skipped=summary.skipped,
                elapsed_seconds=0,
                message=message,
            ),
        )
        return summary

    if control is not None:
        await control.checkpoint()

    pages = [await controller.new_worker_page() for _ in range(min(concurrency, len(pending)))]
    started = time.monotonic()

    async def process(
        page: Page,
        item: Any,
    ) -> tuple[Any, str, dict[str, Any] | None, Exception | None]:
        url = url_for(item)
        try:
            await controller.navigate(page, url)
            return item, url, await parse(page, item, url), None
        except CrawlCancelled:
            # Cancellation is control flow, not a failed parsing result.  Let it
            # abort the whole batch so no partly completed batch is committed.
            raise
        except Exception as error:
            return item, url, None, error

    try:
        for offset in range(0, len(pending), len(pages)):
            if control is not None:
                await control.checkpoint()
            batch = pending[offset : offset + len(pages)]
            tasks = [
                asyncio.create_task(process(pages[index], item))
                for index, item in enumerate(batch)
            ]
            try:
                results = await asyncio.gather(*tasks)
            except CrawlCancelled:
                # gather() does not cancel sibling tasks when one task raises.
                # Stop and join every worker before closing its browser page.
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

            # A stop request can arrive after the last browser checkpoint.  Do
            # not persist that batch until we know cancellation was not raised.
            if control is not None:
                await control.checkpoint()

            completed_records: list[dict[str, Any]] = []
            for item, url, record, error in results:
                if error is not None or record is None:
                    summary.failed += 1
                    append_error(
                        error_path,
                        mode=mode,
                        key=item,
                        url=url,
                        error=error or "Парсер вернул пустой результат",
                    )
                    print(f"[{mode}] Ошибка {item}: {error}")
                    continue

                completed_records.append(record)
            summary.saved += store.append_many(completed_records)

            processed = min(offset + len(batch), len(pending))
            elapsed = time.monotonic() - started
            rate = processed / elapsed if elapsed else 0
            message = (
                f"[{mode}] {processed}/{len(pending)}, "
                f"сохранено: {summary.saved}, ошибок: {summary.failed}, "
                f"{rate:.1f} стр./с"
            )
            print(message)
            await _emit_progress(
                on_progress,
                ProgressEvent(
                    mode=mode,
                    processed=processed,
                    total=len(pending),
                    saved=summary.saved,
                    failed=summary.failed,
                    skipped=summary.skipped,
                    elapsed_seconds=elapsed,
                    message=message,
                ),
            )

            for index, page in enumerate(pages):
                if page.is_closed():
                    pages[index] = await controller.new_worker_page()

            if delay_seconds > 0 and processed < len(pending):
                if control is None:
                    await asyncio.sleep(delay_seconds)
                else:
                    await control.wait(delay_seconds)
    finally:
        for page in pages:
            if not page.is_closed():
                await page.close()

    summary.elapsed_seconds = time.monotonic() - started
    return summary
