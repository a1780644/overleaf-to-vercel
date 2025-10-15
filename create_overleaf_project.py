#python create_overleaf_project.py
import asyncio
import os
import re
import json
from pathlib import Path
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ========= 自定义 =========
PROJECT_NAME = "aaa2"          # 项目名   
HEADLESS = True                # 目标：后台执行, False or True
SLOW_MO = 120                  # 放慢操作（毫秒）
STATE_PATH = Path("overleaf_state.json")  # 会话文件
# ========================

def is_editor_url(url: str) -> bool:
    return re.search(r"/project/[^/?#]+$", url or "") is not None

async def accept_cookies(page):
    for sel in [
        'button:has-text("Accept all cookies")',
        'button:has-text("Essential cookies only")',
        'button:has-text("Accept")',
        'button[aria-label*="accept" i]',
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible():
                await btn.click(timeout=1500)
                await page.wait_for_timeout(200)
                return
        except Exception:
            pass

async def robust_login(page, email, password):
    """智能登录（用于 bootstrap）"""
    await page.goto("https://www.overleaf.com/login", wait_until="domcontentloaded")
    try:
        await page.get_by_text("Log in with email", exact=False).click(timeout=2500)
    except Exception:
        pass

    await accept_cookies(page)

    await page.fill('input[name="email"]', email)
    await page.fill('input[name="password"]', password)

    clicked = False
    for sel in ['button[type="submit"]','button:has-text("Log in")','button:has-text("Sign in")']:
        try:
            await page.locator(sel).first.click(timeout=4000)
            clicked = True
            break
        except Exception:
            pass
    if not clicked:
        try:
            await page.keyboard.press("Enter")
        except Exception:
            pass

    # ---------- 修改点 #1：安全等待 + 错误探测 + 兜底尝试 ----------
    # 先等待自然导航到来
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass

    # 在最多 20 秒内轮询结果：成功 / 常见错误 / 继续等
    for _ in range(20):
        if "overleaf.com/project" in (page.url or ""):
            return True

        # 常见错误提示（邮箱/密码错误、需要重试等）
        try:
            if await page.locator('text=/incorrect email or password|try again|couldn\\\'t log you in/i').first.is_visible():
                return False
        except Exception:
            pass

        await page.wait_for_timeout(1000)

    # 仍未到 /project：再尝试一次跳转，但吞掉并发导航导致的异常
    try:
        await page.goto("https://www.overleaf.com/project", wait_until="domcontentloaded", timeout=15000)
    except Exception:
        pass

    return "overleaf.com/project" in (page.url or "")
    # ---------- 修改点 #1 结束 ----------

async def wait_editor_page(context, fallback_page, timeout_ms=30000):
    deadline = asyncio.get_event_loop().time() + timeout_ms / 1000
    while asyncio.get_event_loop().time() < deadline:
        for p in context.pages[::-1]:
            if is_editor_url(p.url or ""):
                await p.wait_for_load_state("domcontentloaded")
                return p
        await fallback_page.wait_for_timeout(120)
    raise PlaywrightTimeoutError("新项目未进入编辑器页")

async def click_and_maybe_new_page(context, click_coro, timeout_sec=3.0):
    page_future = asyncio.create_task(context.wait_for_event("page"))
    try:
        await click_coro
    finally:
        pass
    try:
        new_page = await asyncio.wait_for(page_future, timeout=timeout_sec)
        return new_page
    except asyncio.TimeoutError:
        if not page_future.done():
            page_future.cancel()
        return None
    except Exception:
        if not page_future.done():
            page_future.cancel()
        return None

async def create_blank_project(context, page, project_name: str):
    """仪表盘点击 New project → Blank project → 取名弹窗创建；返回编辑器页"""
    if "overleaf.com/project" not in (page.url or ""):
        await page.goto("https://www.overleaf.com/project", wait_until="domcontentloaded")

    # 1) 点击左上角绿色 New project（多个选择器兜底）
    await page.wait_for_selector('button:has-text("New project"), [data-testid="new-project"]', timeout=20000)
    btn = page.locator('button:has-text("New project"), [data-testid="new-project"]').first
    await btn.click()

    # 2) 点击下拉中的 Blank project
    new_page = await click_and_maybe_new_page(
        context,
        page.locator('text=/^\\s*Blank\\s*project\\s*$/i').first.click()
    )
    host = new_page if new_page else page

    # 3) 处理“New project”取名弹窗（id="blank-project-modal"）
    modal_sel = '#blank-project-modal'
    found_modal_host = None
    for _ in range(40):  # ~5s
        for p in context.pages[::-1]:
            try:
                if await p.locator(modal_sel).first.is_visible():
                    found_modal_host = p
                    break
            except Exception:
                pass
        if found_modal_host:
            break
        await host.wait_for_timeout(120)

    if found_modal_host:
        await found_modal_host.locator(f'{modal_sel} input#project-name').fill(project_name)
        create_btn = found_modal_host.locator(f'{modal_sel} button:has-text("Create")').first
        await create_btn.wait_for(state="visible", timeout=3000)
        await found_modal_host.wait_for_timeout(150)
        await create_btn.click(timeout=3000)

    # 4) 等进入 /project/<id>（编辑器）
    editor = await wait_editor_page(context, host, timeout_ms=40000)
    return editor

async def open_share_and_get_edit_link(context, page):
    print("🔍 正在打开分享窗口…")

    # 打开 Share 按钮（兼容新版UI的各种按钮形态）
    for sel in [
        'button:has-text("Share")',
        '[aria-label="Share"]',
        '[data-testid="share-button"]',
        'button:has-text("Invite")'
    ]:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible():
                await btn.click(timeout=4000)
                break
        except Exception:
            pass

    # 容忍新版UI标题不一致的情况
    modal_found = False
    for selector in [
        'text=/Share( Project| your project)?/i',
        '[role="dialog"] >> text=/Share/i',
        '[aria-label*="Share" i]',
        '.modal-content:has-text("Share")',
        '.ol-ShareModal',
    ]:
        try:
            if await page.locator(selector).first.is_visible():
                modal_found = True
                break
        except Exception:
            pass

    if not modal_found:
        await page.wait_for_timeout(3000)
        try:
            await page.locator('button:has-text("Share")').first.click(timeout=2000)
        except Exception:
            pass
        await page.wait_for_timeout(1000)

    print("✅ 分享窗口检测完毕，准备开启共享…")

    # 确保 Link sharing 开启
    try:
        t = page.get_by_text("Turn on link sharing", exact=False)
        if await t.is_visible():
            await t.click(timeout=3000)
            await page.wait_for_timeout(300)
    except Exception:
        pass

    # A) 从 <code> 读取链接
    edit_link = None
    code_loc = page.locator(
        'xpath=//strong[contains(normalize-space(),"Anyone with this link can edit this project")]'
        '/following::div[contains(@class,"access-token")][1]/code'
    ).first
    try:
        await code_loc.wait_for(state="visible", timeout=5000)
        text = await code_loc.inner_text()
        if text and "overleaf.com" in text and "/read/" not in text:
            edit_link = text.strip()
    except Exception:
        pass

    # B) 如果没找到，再尝试从复制按钮或输入框读取
    if not edit_link:
        try:
            await context.grant_permissions(["clipboard-read", "clipboard-write"], origin="https://www.overleaf.com")
        except Exception:
            pass
        try:
            copy_btn = page.locator(
                'xpath=//strong[contains(normalize-space(),"Anyone with this link can edit this project")]'
                '/following::button[@aria-label="Copy" or contains(@class,"copy-button")][1]'
            ).first
            if await copy_btn.is_visible():
                await copy_btn.click(timeout=2000)
                await page.wait_for_timeout(150)
                clip = await page.evaluate("navigator.clipboard.readText && navigator.clipboard.readText()")
                if isinstance(clip, str) and "overleaf.com" in clip and "/read/" not in clip:
                    edit_link = clip.strip()
        except Exception:
            pass

    if not edit_link:
        # C) input / textarea 兜底
        try:
            field = page.locator(
                'xpath=//strong[contains(normalize-space(),"Anyone with this link can edit this project")]'
                '/following::input[1] | '
                '//strong[contains(normalize-space(),"Anyone with this link can edit this project")]'
                '/following::textarea[1]'
            ).first
            if await field.is_visible():
                val = await field.input_value()
                if val and "overleaf.com" in val and "/read/" not in val:
                    edit_link = val.strip()
        except Exception:
            pass

    if not edit_link:
        raise RuntimeError("❌ 未抓到可编辑链接")
    return edit_link


async def bootstrap_state_if_needed(p, email, password):
    """若会话文件不存在：自动开一个可视化浏览器登录并保存会话，然后关闭"""
    if STATE_PATH.exists():
        return True

    print("ℹ️ 未找到会话文件，正在启动一次可视化登录以保存会话…")
    browser = await p.chromium.launch(headless=False, slow_mo=120)
    context = await browser.new_context()
    page = await context.new_page()

    ok = await robust_login(page, email, password)
    if not ok:
        # ---------- 修改点 #2：失败现场尽量保留 ----------
        try:
            await page.screenshot(path="login_failed.png", full_page=True)
            print("⚠️ 登录似乎未成功，已保存截图 login_failed.png")
        except Exception:
            pass
        try:
            html = await page.content()
            Path("login_failed.html").write_text(html, encoding="utf-8")
        except Exception:
            pass

        # 可选：失败时不关窗口（设置 KEEP_OPEN_ON_FAIL=1）
        if os.getenv("KEEP_OPEN_ON_FAIL", "0") == "1":
            print("🟡 KEEP_OPEN_ON_FAIL=1：失败时保留窗口以便查看页面提示。按 Ctrl+C 结束脚本。")
            return False
        # ---------- 修改点 #2 结束 ----------

        await context.close()
        await browser.close()
        return False

    # 成功：保存会话
    await context.storage_state(path=str(STATE_PATH))
    print(f"✅ 会话已保存到 {STATE_PATH}")
    await context.close()
    await browser.close()
    return True

async def run():
    load_dotenv()
    email = os.getenv("OVERLEAF_EMAIL")
    password = os.getenv("OVERLEAF_PASSWORD")
    if not email or not password:
        print("请在 .env 中设置 OVERLEAF_EMAIL / OVERLEAF_PASSWORD")
        return

    async with async_playwright() as p:
        # 如果没有会话，先自动用可视化方式登录保存一次
        ok = await bootstrap_state_if_needed(p, email, password)
        if not ok:
            print("❌ 自动登录失败，请检查账号/密码或打开 login_failed.png")
            return

        # 使用已保存的会话，进入 headless 流程
        browser = await p.chromium.launch(headless=HEADLESS, slow_mo=SLOW_MO)
        context = await browser.new_context(storage_state=str(STATE_PATH))
        page = await context.new_page()

        # 去仪表盘
        await page.goto("https://www.overleaf.com/project", wait_until="domcontentloaded")

        # 新建空白项目（含“取名”弹窗）
        editor = await create_blank_project(context, page, PROJECT_NAME)
        print("✅ 已进入编辑器：", editor.url)

        # 打开分享并拿到“可编辑链接”
        link = await open_share_and_get_edit_link(context, editor)
        print("✅ 可编辑链接：", link)

        # ====== 写入静态产物：public/overleaf.json ======
        out = Path("public/overleaf.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps({"edit_link": link}, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        print("✅ 已写入 public/overleaf.json")
        # ================================================

        # 如需自动关闭，取消注释
        # await context.close()
        # await browser.close()

if __name__ == "__main__":
    asyncio.run(run())
