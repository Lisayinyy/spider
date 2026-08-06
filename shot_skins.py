#!/usr/bin/env python3
"""三套皮肤视觉验收截图：不启动相机（避免 uploadCam 覆盖注入纹理），
照 smoke_test D 段的路子：隐藏 boot + __SUITUP_TEST__ 注入假 cam/mask。
"""
import os, threading, http.server, socketserver, functools
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8899
Handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT)
socketserver.TCPServer.allow_reuse_address = True
httpd = socketserver.TCPServer(("127.0.0.1", PORT), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{PORT}/index.html"

with sync_playwright() as p:
    b = p.chromium.launch(args=[
        "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
        "--disable-gpu-sandbox"])
    ctx = b.new_context(viewport={"width": 1100, "height": 700})
    page = ctx.new_page()
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    # 不点 #go —— 相机循环会每帧覆盖注入纹理。直接走测试钩子。
    page.evaluate("() => { document.querySelector('#boot').classList.add('hide'); }")
    ok = page.evaluate("() => window.__SUITUP_TEST__()")
    assert ok is True, "__SUITUP_TEST__ 失败"
    page.wait_for_timeout(1200)
    page.keyboard.press("h")           # 收起面板，拍干净图
    page.wait_for_timeout(300)
    for skin, name in [(0, "classic"), (1, "gwen"), (2, "symbiote")]:
        page.keyboard.press(str(skin + 1))
        page.wait_for_timeout(900)
        page.screenshot(path=os.path.join(ROOT, f"shots/skin2_{name}.png"))
        print("saved", name)
    b.close()
httpd.shutdown()
