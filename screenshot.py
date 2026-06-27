"""Take screenshots of all three chart tabs for visual verification."""
import asyncio
from playwright.async_api import async_playwright

CHROMIUM = "/usr/bin/chromium-browser"
URL = "http://localhost:8000"


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, executable_path=CHROMIUM)
        page = await browser.new_page(viewport={"width": 1600, "height": 900})
        await page.goto(URL, wait_until="networkidle")

        for tab in ["Fastest 5", "Recent 3", "PR vs Recent"]:
            await page.get_by_role("button", name=tab).click()
            fname = tab.lower().replace(" ", "_") + ".png"
            await page.screenshot(path=fname, full_page=False)
            print(f"Saved {fname}")

        await browser.close()


asyncio.run(main())
