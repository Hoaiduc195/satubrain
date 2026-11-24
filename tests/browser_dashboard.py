"""Exercise dashboard controls with Playwright Chromium at desktop and phone widths."""

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread

from playwright.sync_api import expect, sync_playwright

from satubrain.dashboard import make_dashboard
from satubrain.scope import Workspace
from satubrain.store import Store


def run():
    project, branch, task = "github.com/example/parcel", "feat/bulk-import", "DEMO-17"
    handoff = "Import tests passed. Verify cancellation on large files next."
    prefix = {"project": project, "branch": branch}
    with TemporaryDirectory() as folder:
        store = Store(Path(folder) / "example.sqlite3")
        store.save(**prefix, task=task, agent="codex", kind="handoff", text=handoff)
        store.save(**prefix, task=task, agent="claude", kind="decision", text="Run imports in a transaction.")
        store.save(**prefix, task="DEMO-12", agent="copilot", kind="note", text="Check the retry limit.")
        server, key = make_dashboard(store, Workspace(project, branch), task)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/#{key}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={"width": 960, "height": 590})
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(url)
                expect(page.get_by_role("heading", name=task, exact=True)).to_be_visible()
                expect(page.locator(".entry")).to_have_count(2)
                assert "#" not in page.url
                page.get_by_role("searchbox").fill("cancellation")
                expect(page.locator(".entry")).to_have_count(1)
                page.get_by_role("button", name=handoff, exact=False).click()
                expect(page.locator("#detail-text")).to_have_text(handoff)
                page.locator("#details").get_by_role("button", name="Close").click()
                page.get_by_role("button", name="Add memory").click()
                memory = "Cancellation test passed on the large <fixture>."
                page.get_by_label("Memory", exact=True).fill(memory)
                page.get_by_role("button", name="Save memory").click()
                expect(page.locator(".entry")).to_have_count(3)
                page.get_by_role("button", name=memory, exact=False).click()
                expect(page.locator("#detail-text")).to_have_text(memory)
                assert page.locator("#detail-text *").count() == 0
                page.get_by_role("button", name="Delete memory").click()
                page.get_by_role("button", name="Confirm deletion").click()
                expect(page.locator(".entry")).to_have_count(2)
                page.reload()
                expect(page.locator(".entry")).to_have_count(2)
                mobile = browser.new_page(viewport={"width": 375, "height": 812}, is_mobile=True, has_touch=True)
                mobile.goto(url)
                expect(mobile.get_by_role("heading", name=task, exact=True)).to_be_visible()
                mobile.get_by_role("button", name="DEMO-12", exact=False).click()
                expect(mobile.get_by_text("Check the retry limit", exact=False)).to_be_visible()
                assert mobile.evaluate("document.documentElement.scrollWidth === window.innerWidth")
                assert not errors, errors
                browser.close()
        finally:
            server.shutdown()
            thread.join()
            server.server_close()


if __name__ == "__main__":
    run()
