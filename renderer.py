import asyncio
import io
import logging
import os
import re
from typing import Optional

from PIL import Image
from playwright.async_api import async_playwright

logger = logging.getLogger("Cub.Renderer")


def extract_html(model_output: str) -> str:
    """
    Gemini is instructed to return only HTML, but this safely removes a
    markdown fence if a model adds one anyway.
    """
    text = model_output.strip()

    fenced = re.match(
        r"^```(?:html)?\s*(.*?)\s*```$",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced:
        text = fenced.group(1).strip()

    start = text.lower().find("<!doctype html")
    if start == -1:
        start = text.lower().find("<html")

    if start > 0:
        text = text[start:]

    if "<html" not in text.lower():
        raise ValueError("Gemini did not return a valid HTML document.")

    return text


class HtmlImageRenderer:
    def __init__(
        self,
        chromium_path: Optional[str] = None,
        max_concurrency: int = 2,
    ):
        self.chromium_path = (chromium_path or "").strip()
        self.max_concurrency = max_concurrency
        self.semaphore = asyncio.Semaphore(max_concurrency)
        self.playwright = None
        self.browser = None

    async def start(self):
        if self.browser is not None:
            return

        self.playwright = await async_playwright().start()

        launch_args = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-extensions",
            ],
        }

        if self.chromium_path and os.path.exists(self.chromium_path):
            launch_args["executable_path"] = self.chromium_path

        self.browser = await self.playwright.chromium.launch(**launch_args)
        logger.info("Chromium renderer started.")

    async def close(self):
        if self.browser is not None:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None

        if self.playwright is not None:
            try:
                await self.playwright.stop()
            except Exception:
                pass
            self.playwright = None

    async def render_png(self, html_document: str) -> bytes:
        if self.browser is None:
            raise RuntimeError("Renderer is not started.")

        html_document = extract_html(html_document)

        async with self.semaphore:
            context = await self.browser.new_context(
                viewport={"width": 1200, "height": 900},
                device_scale_factor=1.25,
            )

            page = await context.new_page()

            # Only allow the page itself + MathJax CDN.
            async def route_handler(route):
                url = route.request.url

                allowed = (
                    url.startswith("about:blank")
                    or url.startswith("data:")
                    or url.startswith("https://cdn.jsdelivr.net/")
                )

                if allowed:
                    await route.continue_()
                else:
                    await route.abort()

            await page.route("**/*", route_handler)

            try:
                await page.set_content(
                    html_document,
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )

                # Wait for MathJax, but don't fail forever if a network hiccup occurs.
                try:
                    await page.wait_for_function(
                        """
                        () => window.MathJax &&
                              MathJax.startup &&
                              MathJax.startup.promise
                        """,
                        timeout=25_000,
                    )

                    await page.evaluate(
                        """
                        async () => {
                            await MathJax.startup.promise;
                            if (MathJax.typesetPromise) {
                                await MathJax.typesetPromise();
                            }
                            if (document.fonts) {
                                await document.fonts.ready;
                            }
                        }
                        """
                    )
                except Exception as exc:
                    logger.warning("MathJax wait warning: %s", exc)

                await page.wait_for_timeout(250)

                # Prefer a solution sheet if Gemini created one; otherwise body.
                target = page.locator(
                    ".solution-sheet, .sheet, main, body"
                ).first

                png = await target.screenshot(
                    type="png",
                    animations="disabled",
                )

                return png

            finally:
                await context.close()


def image_dimensions(png_bytes: bytes):
    with Image.open(io.BytesIO(png_bytes)) as im:
        return im.width, im.height
