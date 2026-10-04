"""Repeatable browser contract for Settings -> ChatGPT Plan.

This harness deliberately mocks every OpenAI-plan response.  It never starts a
real auth flow, reads a credential, or runs an inference.  Run it against a
built local Easel Web instance:

    python tests/browser_chatgpt_plan_settings_harness.py http://127.0.0.1:7860
"""

from __future__ import annotations

import json
import re
import sys
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect, sync_playwright


BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:7860"
DEVICE_COMMAND = "openclaw --profile easel models auth login --provider openai --method device-code"
INTERACTIVE_COMMAND = "openclaw --profile easel models auth login --provider openai --method siwc"
SENTINELS = (
    "TOKEN_SENTINEL_NOT_A_SECRET",
    "RAW_PROFILE_SENTINEL",
    "owner-sentinel@example.invalid",
)


def plan_status(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "available": True,
        "connected": False,
        "usable": False,
        "reauthRequired": False,
        "authMethod": "unknown",
        "billingSource": "unknown",
        "activeProfileHandle": "",
        "displayLabel": "",
        "selectedModel": "",
        "runtimeStatus": "missing",
        "errorCode": "",
        "recoveryAction": "",
        "deviceCodeWebSupported": False,
        "profiles": [],
    }
    body.update(overrides)
    return body


def usable_status(**overrides: Any) -> dict[str, Any]:
    handle = "plan_0123456789abcdef0123456789abcdef"
    body = plan_status(
        connected=True,
        usable=True,
        authMethod="siwc",
        billingSource="chatgpt_plan",
        activeProfileHandle=handle,
        displayLabel="ChatGPT Plan account",
        selectedModel="openai/gpt-6-sol",
        runtimeStatus="usable",
        profiles=[{
            "handle": handle,
            "displayLabel": "ChatGPT Plan account",
            "authMethod": "siwc",
            "usable": True,
        }],
    )
    body.update(overrides)
    return body


def available_models() -> dict[str, Any]:
    return {
        "status": "available",
        "errorCode": "",
        "models": [
            {
                "provider": "openai",
                "ref": "openai/gpt-6-sol",
                "id": "gpt-6-sol",
                "name": "GPT-6 Sol",
                "displayName": "GPT-6 Sol",
                "availability": "available",
            },
            {
                "provider": "openai",
                "ref": "openai/gpt-6-astra",
                "id": "gpt-6-astra",
                "name": "GPT-6 Astra",
                "displayName": "GPT-6 Astra",
                "availability": "unavailable",
            },
            {
                "provider": "anthropic",
                "ref": "anthropic/not-allowed",
                "id": "not-allowed",
                "name": "Must be filtered",
                "displayName": "Must be filtered",
                "availability": "available",
            },
        ],
    }


@dataclass
class MockPlanApi:
    statuses: list[dict[str, Any]] = field(default_factory=lambda: [plan_status()])
    models: dict[str, Any] = field(default_factory=available_models)
    connect_status: int = 200
    connect: dict[str, Any] = field(default_factory=lambda: {
        "jobId": "job123",
        "state": "running",
        "phase": "gateway_starting",
        "method": "siwc",
        "message": "",
        "errorCode": "",
    })
    jobs: list[dict[str, Any]] = field(default_factory=lambda: [{
        "jobId": "job123",
        "state": "running",
        "phase": "gateway_starting",
        "method": "siwc",
        "message": "",
        "errorCode": "",
    }])
    test_use: dict[str, Any] = field(default_factory=lambda: {
        "ok": True,
        "selectedModel": "openai/gpt-6-sol",
        "effectiveProvider": "openai",
        "testResult": "success",
        "errorCode": "",
    })
    calls: list[tuple[str, str, Any]] = field(default_factory=list)
    _status_index: int = 0
    _job_index: int = 0

    def _next_status(self) -> dict[str, Any]:
        result = self.statuses[min(self._status_index, len(self.statuses) - 1)]
        self._status_index += 1
        return deepcopy(result)

    def _next_job(self) -> dict[str, Any]:
        result = self.jobs[min(self._job_index, len(self.jobs) - 1)]
        self._job_index += 1
        return deepcopy(result)

    def route(self, route: Route) -> None:
        request = route.request
        path = urlparse(request.url).path
        method = request.method
        payload: Any = None
        if request.post_data:
            try:
                payload = request.post_data_json
            except Exception:
                payload = None
        self.calls.append((method, path, payload))

        if path == "/api/settings/openai-plan/status":
            route.fulfill(json=self._next_status())
        elif path == "/api/settings/openai-plan/connect":
            if self.connect_status == 409:
                route.fulfill(status=409, json={"detail": "已有 OpenAI 登录任务正在运行"})
            else:
                body = deepcopy(self.connect)
                if isinstance(payload, dict):
                    body["method"] = payload.get("method", body.get("method"))
                route.fulfill(json=body)
        elif re.fullmatch(r"/api/settings/openai-plan/job/[^/]+", path) and method == "GET":
            route.fulfill(json=self._next_job())
        elif re.fullmatch(r"/api/settings/openai-plan/job/[^/]+/cancel", path) and method == "POST":
            route.fulfill(json={
                "jobId": path.split("/")[-2],
                "state": "cancelled",
                "phase": "complete",
                "method": "siwc",
                "message": "",
                "errorCode": "auth_cancelled",
            })
        elif path == "/api/settings/openai-plan/models":
            route.fulfill(json=deepcopy(self.models))
        elif path == "/api/settings/openai-plan/test-use":
            route.fulfill(json=deepcopy(self.test_use))
        elif path == "/api/status":
            route.fulfill(json={"gateway": False, "skills": [], "personas": []})
        elif path == "/api/personas":
            route.fulfill(json=[])
        elif path == "/api/env/tools":
            route.fulfill(json={"tools": [], "python": ""})
        elif path == "/api/settings/local-agents":
            route.fulfill(json={
                "agents": [],
                "installedCount": 0,
                "usableWithoutKeyCount": 0,
                "usableWithoutKey": [],
            })
        elif path == "/api/settings/models":
            route.fulfill(json={
                "channels": {
                    "chat": {"rows": []},
                    "transcribe": {"rows": []},
                    "speech": {"rows": []},
                    "image": {"rows": []},
                    "video": {"rows": []},
                    "music": {"rows": []},
                },
                "primary": "",
            })
        else:
            # Keep unrelated page APIs on the isolated local backend so their
            # real response shapes cannot be broken by an over-broad mock.
            route.continue_()


def open_settings(page: Page) -> None:
    page.goto(BASE_URL, wait_until="domcontentloaded")
    expect(page.locator(".settings-gear")).to_be_visible()
    page.locator(".settings-gear").click()
    expect(page.locator(".settings-panel")).to_be_visible()
    expect(page.get_by_test_id("chatgpt-plan-card")).to_be_visible()


def with_scenario(browser: Any, api: MockPlanApi, check: Callable[[Page, MockPlanApi], None]) -> None:
    context = browser.new_context()
    page = context.new_page()
    page.set_default_timeout(5_000)
    page.set_default_navigation_timeout(15_000)
    console: list[str] = []
    page.on("console", lambda message: console.append(message.text))
    page.add_init_script("localStorage.setItem('easel_onboarding_seen', '1')")
    page.route("**/api/**", api.route)
    try:
        open_settings(page)
        check(page, api)
    except Exception:
        print("BROWSER_CONSOLE:", console)
        print("BROWSER_BODY:", page.locator("body").inner_text()[:1_500])
        raise
    setattr(api, "console", console)
    context.close()


def assert_no_sensitive_surface(page: Page, console: list[str]) -> None:
    body = page.locator("body").inner_text()
    storage = page.evaluate("JSON.stringify({local: {...localStorage}, session: {...sessionStorage}})")
    observed = "\n".join([body, storage, *console])
    for sentinel in SENTINELS:
        assert sentinel not in observed, f"sensitive sentinel leaked: {sentinel}"


def run_matrix() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)

        # 1. Disconnected -> primary SIWC action.
        with_scenario(browser, MockPlanApi(), lambda page, _api: (
            expect(page.get_by_role("button", name="Continue with ChatGPT (Beta)")).to_be_visible()
        ))

        # Platform credentials may block activation, but must not block auth.
        platform_only = plan_status(
            billingSource="platform_api",
            errorCode="billing_ambiguity",
            recoveryAction="review_openai_billing_sources",
        )

        def platform_only_check(page: Page, api: MockPlanApi) -> None:
            expect(page.get_by_test_id("plan-risk")).to_contain_text("你仍可登录 ChatGPT Plan")
            expect(page.get_by_role("button", name="Continue with ChatGPT (Beta)")).to_be_enabled()
            expect(page.get_by_role("button", name="Test & use")).to_have_count(0)
            assert "/api/settings/openai-plan/models" not in [path for _, path, _ in api.calls]

        with_scenario(browser, MockPlanApi(statuses=[platform_only]), platform_only_check)

        # Billing ambiguity without a plan profile also permits auth while
        # catalog and activation remain fail closed.
        disconnected_ambiguous = plan_status(
            billingSource="unknown",
            errorCode="billing_ambiguity",
            recoveryAction="review_openai_billing_sources",
        )

        def disconnected_ambiguous_check(page: Page, api: MockPlanApi) -> None:
            expect(page.get_by_test_id("plan-risk")).to_be_visible()
            expect(page.get_by_role("button", name="Continue with ChatGPT (Beta)")).to_be_enabled()
            expect(page.get_by_role("button", name="Test & use")).to_have_count(0)
            assert "/api/settings/openai-plan/models" not in [path for _, path, _ in api.calls]

        with_scenario(browser, MockPlanApi(statuses=[disconnected_ambiguous]), disconnected_ambiguous_check)

        # Both OpenClaw-managed login methods remain available in the
        # Platform-only state; neither path reads or changes a credential.
        def platform_siwc(page: Page, api: MockPlanApi) -> None:
            page.get_by_role("button", name="Continue with ChatGPT (Beta)").click()
            expect(page.get_by_role("button", name="取消登录")).to_be_visible()
            page.wait_for_timeout(1_300)
            assert any(
                method == "POST" and path.endswith("/connect")
                and isinstance(payload, dict) and payload.get("method") == "siwc"
                for method, path, payload in api.calls
            )
            assert any(method == "GET" and "/job/" in path for method, path, _ in api.calls)
            page.get_by_role("button", name="取消登录").click()

        with_scenario(browser, MockPlanApi(statuses=[platform_only]), platform_siwc)

        def platform_oauth(page: Page, api: MockPlanApi) -> None:
            page.get_by_text("兼容登录方式", exact=True).click()
            oauth = page.get_by_role("button", name="使用 OAuth 登录")
            expect(oauth).to_be_enabled()
            oauth.click()
            expect(page.get_by_role("button", name="取消登录")).to_be_visible()
            assert any(
                method == "POST" and path.endswith("/connect")
                and isinstance(payload, dict) and payload.get("method") == "oauth"
                for method, path, payload in api.calls
            )
            page.get_by_role("button", name="取消登录").click()

        with_scenario(browser, MockPlanApi(statuses=[platform_only]), platform_oauth)

        # 2. Running -> poll plus an explicit cancel action.
        def running(page: Page, api: MockPlanApi) -> None:
            page.get_by_role("button", name="Continue with ChatGPT (Beta)").click()
            expect(page.get_by_test_id("plan-connecting")).to_contain_text("正在启动本机 OpenClaw Gateway")
            expect(page.get_by_role("button", name="取消登录")).to_be_visible()
            page.get_by_role("button", name="取消登录").click()
            expect(page.get_by_test_id("plan-message")).to_contain_text("已取消")
            assert any(path.endswith("/cancel") for _, path, _ in api.calls)

        with_scenario(browser, MockPlanApi(), running)

        browser_opened_api = MockPlanApi(jobs=[{
            "jobId": "job123",
            "state": "running",
            "phase": "browser_opened",
            "method": "siwc",
            "message": "",
            "errorCode": "",
        }])

        def browser_opened(page: Page, _api: MockPlanApi) -> None:
            page.get_by_role("button", name="Continue with ChatGPT (Beta)").click()
            expect(page.get_by_test_id("plan-connecting")).to_contain_text("已打开安全登录页面")
            page.get_by_role("button", name="取消登录").click()

        with_scenario(browser, browser_opened_api, browser_opened)

        terminal_opened_api = MockPlanApi(jobs=[{
            "jobId": "job123",
            "state": "running",
            "phase": "terminal_opened",
            "method": "siwc",
            "message": "",
            "errorCode": "",
        }])

        def terminal_opened(page: Page, _api: MockPlanApi) -> None:
            page.get_by_role("button", name="Continue with ChatGPT (Beta)").click()
            expect(page.get_by_test_id("plan-connecting")).to_contain_text(
                "已打开 OpenClaw 登录窗口，请在该窗口/浏览器中完成 ChatGPT 登录。"
            )
            page.get_by_role("button", name="取消登录").click()

        with_scenario(browser, terminal_opened_api, terminal_opened)

        manual_api = MockPlanApi(jobs=[{
            "jobId": "job123",
            "state": "interaction_required",
            "phase": "complete",
            "method": "siwc",
            "message": "",
            "errorCode": "gateway_unavailable",
            "terminalCommand": INTERACTIVE_COMMAND,
        }])

        def manual_terminal(page: Page, _api: MockPlanApi) -> None:
            page.get_by_role("button", name="Continue with ChatGPT (Beta)").click()
            expect(page.get_by_test_id("interactive-login-command")).to_be_visible()
            expect(page.get_by_label("OpenClaw interactive login command")).to_have_value(INTERACTIVE_COMMAND)

        with_scenario(browser, manual_api, manual_terminal)

        # 3. Successful auth refreshes status and then fetches models.
        success_api = MockPlanApi(
            statuses=[plan_status(), usable_status()],
            jobs=[{
                "jobId": "job123",
                "state": "success",
                "phase": "complete",
                "method": "siwc",
                "message": "Connected",
                "errorCode": "",
            }],
        )

        def auth_success(page: Page, api: MockPlanApi) -> None:
            page.get_by_role("button", name="Continue with ChatGPT (Beta)").click()
            expect(page.get_by_test_id("plan-model-select")).to_be_visible(timeout=5_000)
            paths = [path for _, path, _ in api.calls]
            assert paths.count("/api/settings/openai-plan/status") >= 2
            assert "/api/settings/openai-plan/models" in paths

        with_scenario(browser, success_api, auth_success)

        # A successful login can still return mixed billing proof.  It must
        # show the signed-in-but-not-activated state without fetching models.
        mixed_after_login = usable_status(
            usable=False,
            billingSource="mixed",
            errorCode="platform_fallback_present",
        )
        mixed_success_api = MockPlanApi(
            statuses=[platform_only, mixed_after_login],
            jobs=[{
                "jobId": "job123",
                "state": "success",
                "phase": "complete",
                "method": "siwc",
                "message": "Connected",
                "errorCode": "",
            }],
        )

        def auth_success_mixed(page: Page, api: MockPlanApi) -> None:
            page.get_by_role("button", name="Continue with ChatGPT (Beta)").click()
            expect(page.get_by_test_id("plan-status-copy")).to_contain_text("已登录 · 尚未安全启用")
            expect(page.get_by_test_id("plan-risk")).to_be_visible()
            expect(page.get_by_role("button", name="Test & use")).to_have_count(0)
            assert "/api/settings/openai-plan/models" not in [path for _, path, _ in api.calls]

        with_scenario(browser, mixed_success_api, auth_success_mixed)

        # 4. Auth failure is bounded and retryable.
        fail_api = MockPlanApi(jobs=[{
            "jobId": "job123",
            "state": "fail",
            "phase": "complete",
            "method": "siwc",
            "message": SENTINELS[0],
            "errorCode": "auth_failed",
        }])

        def auth_fail(page: Page, _api: MockPlanApi) -> None:
            page.get_by_role("button", name="Continue with ChatGPT (Beta)").click()
            expect(page.get_by_test_id("plan-message")).to_contain_text("登录失败")
            expect(page.get_by_role("button", name="Continue with ChatGPT (Beta)")).to_be_enabled()
            assert SENTINELS[0] not in page.locator("body").inner_text()

        with_scenario(browser, fail_api, auth_fail)

        # 5. Reauth state offers the safe primary reconnect path.
        reauth = plan_status(
            connected=True,
            reauthRequired=True,
            authMethod="siwc",
            billingSource="chatgpt_plan",
            runtimeStatus="missing",
            errorCode="profile_unusable",
            displayLabel="ChatGPT Plan account",
        )
        with_scenario(browser, MockPlanApi(statuses=[reauth]), lambda page, _api: (
            expect(page.get_by_test_id("plan-status-copy")).to_contain_text("需要重新登录"),
            expect(page.get_by_role("button", name="重新登录 ChatGPT")).to_be_visible(),
        ))

        # 6. Exclusive usable plan gets only the allowlisted OpenAI catalog.
        def usable(page: Page, _api: MockPlanApi) -> None:
            select = page.get_by_test_id("plan-model-select")
            expect(select).to_be_visible()
            expect(select.locator("option")).to_have_count(2)
            assert "anthropic/not-allowed" not in select.inner_text()
            expect(page.get_by_role("button", name="Test & use")).to_be_enabled()

        with_scenario(browser, MockPlanApi(statuses=[usable_status()]), usable)

        # 7. Mixed/platform fallback is explicit and fail closed.
        mixed = usable_status(
            usable=False,
            billingSource="mixed",
            errorCode="platform_fallback_present",
        )

        def mixed_check(page: Page, api: MockPlanApi) -> None:
            expect(page.get_by_test_id("plan-risk")).to_contain_text("意外 API 计费")
            expect(page.get_by_test_id("plan-status-copy")).to_contain_text("已登录 · 尚未安全启用")
            expect(page.get_by_role("button", name="Continue with ChatGPT (Beta)")).to_have_count(0)
            expect(page.get_by_role("button", name="Test & use")).to_have_count(0)
            page.get_by_text("兼容登录方式", exact=True).click()
            expect(page.get_by_role("button", name="使用 OAuth 登录")).to_be_disabled()
            assert "/api/settings/openai-plan/models" not in [path for _, path, _ in api.calls]

        with_scenario(browser, MockPlanApi(statuses=[mixed]), mixed_check)

        # A mixed profile may reauthenticate, but still cannot activate or
        # fetch the executable catalog until exclusive billing is proven.
        mixed_reauth = usable_status(
            usable=False,
            reauthRequired=True,
            billingSource="mixed",
            runtimeStatus="missing",
            errorCode="platform_fallback_present",
        )

        def mixed_reauth_check(page: Page, api: MockPlanApi) -> None:
            expect(page.get_by_role("button", name="重新登录 ChatGPT")).to_be_enabled()
            expect(page.get_by_test_id("plan-risk")).to_be_visible()
            expect(page.get_by_role("button", name="Test & use")).to_have_count(0)
            page.get_by_text("兼容登录方式", exact=True).click()
            expect(page.get_by_role("button", name="使用 OAuth 登录")).to_be_enabled()
            assert "/api/settings/openai-plan/models" not in [path for _, path, _ in api.calls]

        with_scenario(browser, MockPlanApi(statuses=[mixed_reauth]), mixed_reauth_check)

        # 8. Ambiguous billing source is also fail closed.
        ambiguous = usable_status(
            usable=False,
            billingSource="unknown",
            errorCode="billing_ambiguity",
        )
        with_scenario(browser, MockPlanApi(statuses=[ambiguous]), lambda page, _api: (
            expect(page.get_by_test_id("plan-risk")).to_contain_text("无法确认唯一订阅计费来源"),
            expect(page.get_by_role("button", name="Test & use")).to_have_count(0),
        ))

        # 9. Catalog empty/stale/unavailable each has bounded recovery copy.
        for catalog_status, copy in (
            ("empty", "当前账号未返回可用模型"),
            ("stale", "模型目录已过期"),
            ("unavailable", "模型目录暂不可用"),
        ):
            api = MockPlanApi(
                statuses=[usable_status()],
                models={"status": catalog_status, "models": [], "errorCode": f"catalog_{catalog_status}"},
            )
            with_scenario(browser, api, lambda page, _api, copy=copy: (
                expect(page.get_by_test_id("plan-catalog-state")).to_contain_text(copy)
            ))

        # 10. Test & use succeeds only on ok=true and refreshes selected state.
        test_ok_api = MockPlanApi(statuses=[usable_status(), usable_status(selectedModel="openai/gpt-6-sol")])

        def test_ok(page: Page, api: MockPlanApi) -> None:
            page.get_by_role("button", name="Test & use").click()
            expect(page.get_by_test_id("plan-message")).to_contain_text("已验证并启用 openai/gpt-6-sol")
            call = next(payload for method, path, payload in api.calls if method == "POST" and path.endswith("/test-use"))
            assert call == {
                "profileHandle": "plan_0123456789abcdef0123456789abcdef",
                "model": "openai/gpt-6-sol",
            }

        with_scenario(browser, test_ok_api, test_ok)

        # 11. A failed proof never presents the target model as enabled.
        test_fail_api = MockPlanApi(
            statuses=[usable_status()],
            test_use={
                "ok": False,
                "selectedModel": "",
                "effectiveProvider": "",
                "testResult": "fail",
                "errorCode": "model_test_unproven",
            },
        )

        def test_fail(page: Page, _api: MockPlanApi) -> None:
            page.get_by_role("button", name="Test & use").click()
            expect(page.get_by_test_id("plan-message")).to_contain_text("无法证明指定模型成功调用")
            assert "已验证并启用" not in page.get_by_test_id("plan-message").inner_text()

        with_scenario(browser, test_fail_api, test_fail)

        # 12. Unknown response fields must not reach DOM/storage/console.
        sentinel = usable_status()
        sentinel.update({
            "accessToken": SENTINELS[0],
            "rawProfileId": SENTINELS[1],
            "email": SENTINELS[2],
        })

        # Keep this scenario open long enough to inspect its page-side surfaces.
        context = browser.new_context()
        page = context.new_page()
        console: list[str] = []
        page.on("console", lambda message: console.append(message.text))
        page.add_init_script("localStorage.setItem('easel_onboarding_seen', '1')")
        sentinel_api = MockPlanApi(statuses=[sentinel])
        page.route("**/api/**", sentinel_api.route)
        open_settings(page)
        assert_no_sensitive_surface(page, console)
        context.close()

        # 13. Close/reopen resumes jobId-only session state; 409 is explicit.
        def resume(page: Page, _api: MockPlanApi) -> None:
            page.get_by_role("button", name="Continue with ChatGPT (Beta)").click()
            expect(page.get_by_role("button", name="取消登录")).to_be_visible()
            page.locator(".settings-close").click()
            stored = page.evaluate("sessionStorage.getItem('easel_openai_plan_auth_job')")
            parsed = json.loads(stored)
            assert set(parsed) == {"jobId", "method"}
            assert "profile" not in stored.lower()
            page.locator(".settings-gear").click()
            expect(page.get_by_role("button", name="取消登录")).to_be_visible()
            page.get_by_role("button", name="取消登录").click()

        with_scenario(browser, MockPlanApi(), resume)

        def conflict(page: Page, _api: MockPlanApi) -> None:
            page.get_by_role("button", name="Continue with ChatGPT (Beta)").click()
            expect(page.get_by_test_id("plan-message")).to_contain_text("已有登录任务正在运行")

        with_scenario(browser, MockPlanApi(connect_status=409), conflict)

        # 14. Device code is terminal-only copy; no device-code POST/scraping.
        def device_code(page: Page, api: MockPlanApi) -> None:
            page.get_by_text("兼容登录方式", exact=True).click()
            expect(page.get_by_test_id("device-code-command")).to_have_value(DEVICE_COMMAND)
            assert not any(
                isinstance(payload, dict) and payload.get("method") == "device-code"
                for method, path, payload in api.calls
                if method == "POST" and path.endswith("/connect")
            )

        with_scenario(browser, MockPlanApi(), device_code)

        # Existing Settings, dark-mode persistence and IME Enter remain intact.
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(5_000)
        page.set_default_navigation_timeout(15_000)
        page.add_init_script("localStorage.setItem('easel_onboarding_seen', '1')")
        page.add_init_script("if (!localStorage.getItem('easel_theme')) localStorage.setItem('easel_theme', 'light')")
        regression_api = MockPlanApi()
        page.route("**/api/**", regression_api.route)
        page.goto(BASE_URL, wait_until="domcontentloaded")
        expect(page.locator("html")).to_have_attribute("data-theme", "light")
        page.locator(".theme-toggle").click()
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")

        page.locator(".settings-gear").click()
        page.locator(".settings-nav .snav").nth(1).click()
        expect(page.locator(".st-env")).to_be_visible()
        page.locator(".settings-nav .snav").nth(0).click()
        expect(page.get_by_test_id("chatgpt-plan-card")).to_be_visible()
        page.locator(".settings-close").click()

        page.locator(".sidebar-nav .nav-item").nth(1).click()
        composer = page.locator("textarea.chat-input").first
        composer.fill("候选文本")
        composer.dispatch_event("compositionstart")
        composer.dispatch_event("keydown", {
            "key": "Enter",
            "code": "Enter",
            "keyCode": 229,
            "which": 229,
            "isComposing": True,
        })
        expect(composer).to_have_value("候选文本")
        composer.dispatch_event("compositionend")
        context.close()

        # The new card must stay within the viewport across the supported layout range.
        for width in (320, 768, 1024, 1440):
            context = browser.new_context(viewport={"width": width, "height": 900})
            page = context.new_page()
            page.set_default_timeout(5_000)
            page.set_default_navigation_timeout(15_000)
            page.add_init_script("localStorage.setItem('easel_onboarding_seen', '1')")
            page.route("**/api/**", MockPlanApi().route)
            open_settings(page)
            card = page.get_by_test_id("chatgpt-plan-card")
            box = card.bounding_box()
            assert box is not None
            assert box["x"] >= -1
            assert box["x"] + box["width"] <= width + 1
            if width >= 768:
                dimensions = card.evaluate("el => ({ scrollWidth: el.scrollWidth, clientWidth: el.clientWidth })")
                assert dimensions["scrollWidth"] <= dimensions["clientWidth"] + 1, (width, dimensions)
            context.close()

        browser.close()
    print("PASS: ChatGPT Plan auth/activation matrix + Settings/dark-mode/IME/responsive regressions")


if __name__ == "__main__":
    run_matrix()
