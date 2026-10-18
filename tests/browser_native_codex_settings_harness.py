"""Mock-browser smoke for the optional Native Codex settings surface.

All API responses are intercepted.  This harness never invokes Codex,
OpenClaw, authentication, or inference.
"""
from __future__ import annotations

import sys
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Route, expect, sync_playwright

from browser_chatgpt_plan_settings_harness import MockPlanApi


BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7871"


class MockHarnessApi(MockPlanApi):
    def __init__(self, *, installed: bool):
        super().__init__()
        self.installed = installed
        self.saved: list[dict[str, Any]] = []

    def route(self, route: Route) -> None:
        request = route.request
        path = urlparse(request.url).path
        if path == "/api/settings/harness":
            body = {
                "runtime": "openclaw",
                "models": {"openclaw": "", "codex": ""},
                "sandbox": "workspace-write",
                "agents": [
                    {"id": "openclaw", "label": "OpenClaw", "installed": True, "protocol": "gateway", "installHint": ""},
                    {"id": "codex", "label": "Codex (Native)", "installed": self.installed, "protocol": "app-server", "installHint": "Install and run codex login"},
                ],
            }
            if request.method == "POST":
                payload = request.post_data_json
                self.saved.append(payload)
                body.update({
                    "runtime": payload["runtime"],
                    "models": {"openclaw": "", "codex": payload.get("model", "")},
                    "sandbox": payload["sandbox"],
                })
            route.fulfill(json=body)
            return
        super().route(route)


def open_more(page, api: MockHarnessApi) -> None:
    page.add_init_script("localStorage.setItem('easel_onboarding_seen', '1')")
    page.route("**/api/**", api.route)
    page.goto(BASE_URL, wait_until="domcontentloaded")
    page.locator(".settings-gear").click()
    page.get_by_role("button", name="更多设置").click()
    expect(page.get_by_text("选择只影响之后新建的对话")).to_be_visible()


def run() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        context = browser.new_context()
        page = context.new_page()
        api = MockHarnessApi(installed=True)
        open_more(page, api)
        openclaw = page.locator('input[name="agent-runtime"][value="openclaw"]')
        codex = page.locator('input[name="agent-runtime"][value="codex"]')
        expect(openclaw).to_be_checked()
        expect(codex).to_be_enabled()
        codex.check()
        page.get_by_role("button", name="保存执行引擎").click()
        expect(page.get_by_text("已保存；仅新建对话使用该引擎，现有会话保持不变。")).to_be_visible()
        assert api.saved == [{"runtime": "codex", "model": "", "sandbox": "workspace-write"}]
        context.close()

        context = browser.new_context()
        page = context.new_page()
        api = MockHarnessApi(installed=False)
        open_more(page, api)
        expect(page.locator('input[name="agent-runtime"][value="codex"]')).to_be_disabled()
        expect(page.get_by_text("Codex CLI 未检测到。请在终端自行安装并执行 codex login；Easel 不会代装或读取登录凭据。")).to_be_visible()
        context.close()

        browser.close()
    print("native Codex settings mock-browser smoke: PASS")


if __name__ == "__main__":
    run()
