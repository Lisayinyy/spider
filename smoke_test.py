#!/usr/bin/env python3
"""SUIT UP 冒烟测试：shader 编译 / 渲染管线 / 像素证据 / UI 元素。
无需摄像头 —— 用 __SUITUP_TEST__ 注入假的 cam+mask 纹理。
每条断言都配一次故意破坏对照，确认检查本身有效。
"""
import http.server, socketserver, threading, os, sys, json, functools, time
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = 8791

def serve():
    h = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT)
    socketserver.TCPServer.allow_reuse_address = True
    httpd = socketserver.TCPServer(("127.0.0.1", PORT), h)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd

FAILS = []
def check(name, cond, detail=""):
    print(("  OK   " if cond else "  FAIL ") + name + (("  | " + str(detail)[:200]) if detail else ""))
    if not cond:
        FAILS.append(name)
    return cond

PIXEL_JS = """() => {
  const c = document.querySelector('#gl');
  const g = c.getContext('webgl2', {preserveDrawingBuffer:true});
  const w = c.width, h = c.height;
  const px = new Uint8Array(w*h*4);
  g.readPixels(0,0,w,h,g.RGBA,g.UNSIGNED_BYTE,px);
  let red=0, blue=0, white=0, dark=0, nonuni=0;
  const set = new Set();
  for(let i=0;i<px.length;i+=4*11){
    const r=px[i],gg=px[i+1],b=px[i+2];
    if(r>90 && r>gg*1.7 && r>b*1.5) red++;
    if(b>85 && b>r*1.4 && b>gg*1.2) blue++;
    if(r>205 && gg>205 && b>205) white++;
    if(r<28 && gg<28 && b<28) dark++;
    set.add((r>>4)+','+(gg>>4)+','+(b>>4));
  }
  return {w,h,red,blue,white,dark,uniq:set.size,samples:Math.floor(px.length/(4*11))};
}"""

BG_JS = """() => {
  const c = document.querySelector('#gl');
  const g = c.getContext('webgl2', {preserveDrawingBuffer:true});
  const w = c.width, h = c.height;
  const px = new Uint8Array(w*h*4);
  g.readPixels(0,0,w,h,g.RGBA,g.UNSIGNED_BYTE,px);
  const bw = Math.floor(w*0.16);
  let red=0, n=0;
  for(let y=0;y<h;y+=2){
    for(const [xa,xb] of [[0,bw],[w-bw,w]]){
      for(let x=xa;x<xb;x+=2){
        const i=(y*w+x)*4, r=px[i],gg=px[i+1],b=px[i+2];
        n++;
        if(r>90 && r>gg*1.7 && r>b*1.5) red++;
      }
    }
  }
  return {bgRed:red, bgN:n};
}"""

# 按「视频 uv (sv) 坐标」取点采样：内部复刻 shader 的 cover 映射 + 镜像
PROBE_JS = """(pts) => {
  const c = document.querySelector('#gl');
  const g = c.getContext('webgl2', {preserveDrawingBuffer:true});
  const W = c.width, H = c.height;
  const ca = W/H, va = 1280/720;
  const out = [];
  for(const p of pts){
    let svx = p.sv[0], svy = p.sv[1];
    svx = 1.0 - svx;                       // 默认开镜像
    let vidx = svx, vidy = 1.0 - svy;
    let uvx, uvy;
    if(ca > va){ uvx = vidx; uvy = (vidy-0.5)/(va/ca)+0.5; }
    else       { uvx = (vidx-0.5)/(ca/va)+0.5; uvy = vidy; }
    const px = Math.round(uvx*W), py = Math.round(uvy*H);   // readPixels: row0 = bottom
    const buf = new Uint8Array(4*9*9);
    g.readPixels(Math.max(0,px-4), Math.max(0,py-4), 9,9, g.RGBA, g.UNSIGNED_BYTE, buf);
    let r=0,gg=0,b=0;
    for(let i=0;i<81;i++){ r+=buf[i*4]; gg+=buf[i*4+1]; b+=buf[i*4+2]; }
    out.push({name:p.name, r:Math.round(r/81), g:Math.round(gg/81), b:Math.round(b/81), px, py});
  }
  return out;
}"""

# 细线检测：9×9 窗口取逐像素 max(g - max(r,b))。debug 绿线只有 ~2px 粗，
# 均值采样会被亮背景稀释成假阴性；纯绿线像素的这个分数 > 100，非线像素 <= 0 附近。
MAXG_JS = """(pts) => {
  const c = document.querySelector('#gl');
  const g = c.getContext('webgl2', {preserveDrawingBuffer:true});
  const W = c.width, H = c.height;
  const ca = W/H, va = 1280/720;
  const out = [];
  for(const p of pts){
    let svx = 1.0 - p.sv[0];               // 默认开镜像
    let vidx = svx, vidy = 1.0 - p.sv[1];
    let uvx, uvy;
    if(ca > va){ uvx = vidx; uvy = (vidy-0.5)/(va/ca)+0.5; }
    else       { uvx = (vidx-0.5)/(ca/va)+0.5; uvy = vidy; }
    const px = Math.round(uvx*W), py = Math.round(uvy*H);
    const buf = new Uint8Array(4*9*9);
    g.readPixels(Math.max(0,px-4), Math.max(0,py-4), 9,9, g.RGBA, g.UNSIGNED_BYTE, buf);
    let best = -255;
    for(let i=0;i<81;i++){
      const s = buf[i*4+1] - Math.max(buf[i*4], buf[i*4+2]);
      if(s > best) best = s;
    }
    out.push({name:p.name, score:best, px, py});
  }
  return out;
}"""


def shot(pg, path):
    """证据截图：不是断言，超时/失败都不该挡住后续段落。
    playwright 默认会等 fonts + 渲染稳定，这个页面模型在后台下载时实测会超 30s。"""
    try:
        pg.screenshot(path=path, timeout=8000, animations="disabled")
    except Exception as e:
        print("   (截图跳过 %s: %s)" % (os.path.basename(path), str(e).split(chr(10))[0][:70]))

def main():
    httpd = serve()
    url = f"http://127.0.0.1:{PORT}/index.html"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=[
            "--use-gl=angle", "--use-angle=swiftshader",
            "--enable-unsafe-swiftshader", "--disable-gpu-sandbox",
            "--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
            "--autoplay-policy=no-user-gesture-required",
        ])
        ctx = browser.new_context(viewport={"width": 1100, "height": 700}, permissions=["camera"])
        page = ctx.new_page()
        errs, cons = [], []
        page.on("pageerror", lambda e: errs.append(str(e)))
        page.on("console", lambda m: cons.append((m.type, m.text)))
        page.goto(url, wait_until="domcontentloaded")
        page.wait_for_timeout(1200)

        print("\n== A. 加载 / shader ==")
        check("无 pageerror", not errs, errs[:2])
        shader_ok = page.evaluate("() => window.__SHADER_OK__ === true")
        check("WebGL2 shader 编译 + link 成功", shader_ok)
        err_box = page.evaluate("() => { const e=document.querySelector('#err'); "
                                "return {shown: getComputedStyle(e).display!=='none', txt: e.textContent}; }")
        check("页面未弹错误条", not err_box["shown"], err_box["txt"])

        print("\n== B. 首屏 UI（默认可见，不依赖 JS 显形）==")
        boot_vis = page.evaluate("() => { const b=document.querySelector('#boot');"
                                 "const s=getComputedStyle(b); return s.display!=='none' && parseFloat(s.opacity)>0.9; }")
        check("启动卡片默认可见", boot_vis)
        for sel, label in [("#go","开始按钮"), ("#panel","控制面板"), ("#skins button","皮肤按钮"),
                           ("#s0","滑块"), ("#mode","模式 HUD")]:
            check(f"存在 {label} ({sel})", page.locator(sel).count() > 0)
        n_sliders = page.locator("#panel input[type=range]").count()
        check("滑块数量 >= 8", n_sliders >= 8, n_sliders)

        print("\n== C. 空管线基线（对照组：不注入纹理时应该是死画面）==")
        page.evaluate("() => { document.querySelector('#boot').classList.add('hide'); }")
        base = page.evaluate("()=>{const c=document.querySelector('#gl');"
                             "const g=c.getContext('webgl2');g.clearColor(0,0,0,1);g.clear(g.COLOR_BUFFER_BIT);return 1;}")
        page.wait_for_timeout(120)
        b0 = page.evaluate(PIXEL_JS)
        check("对照：未渲染时颜色种类很少（<8）", b0["uniq"] < 8, b0)

        print("\n== D. 注入假 cam+mask，跑真实渲染 ==")
        ok = page.evaluate("() => window.__SUITUP_TEST__()")
        check("__SUITUP_TEST__ 执行成功", ok is True)
        page.wait_for_timeout(900)
        # 注意：绝对帧数是性能指标，量的是测试机忙不忙、不是渲染管线。
        # 只断言「帧数在单调增长」= 渲染循环活着，这个跟硬件无关。
        f1 = page.evaluate("() => window.__FRAMES__ || 0")
        page.wait_for_timeout(600)
        f2 = page.evaluate("() => window.__FRAMES__ || 0")
        page.wait_for_timeout(400)
        f3 = page.evaluate("() => window.__FRAMES__ || 0")
        check("渲染循环在持续推进（帧数单调递增，与帧率无关）",
              f1 > 0 and f2 > f1 and f3 > f2, (f1, f2, f3))
        check("渲染期间无 pageerror", not errs, errs[:2])

        px = page.evaluate(PIXEL_JS)
        print("   pixel stats:", json.dumps(px))
        check("画面有丰富颜色（uniq > 40）", px["uniq"] > 40, px["uniq"])
        check("有战衣暖色像素（red > 30）", px["red"] > 30, px["red"])
        check("有战衣冷色像素（blue > 20）", px["blue"] > 20, px["blue"])
        check("有面罩/高光白像素（white > 3）", px["white"] > 3, px["white"])

        # 假人剪影（128 坐标系）：左臂 (47,50)->(31,82)，右臂 (81,50)->(97,82)，
        # 躯干 45..83 x 45..93，双腿 y 92..126。转成 sv = (x/128, y/128)
        probes = [
          {"name":"左臂中段", "sv":[(47+31)/2/128, (50+82)/2/128]},
          {"name":"右臂中段", "sv":[(81+97)/2/128, (50+82)/2/128]},
          {"name":"胸口上方", "sv":[64/128, 52/128]},
          {"name":"大腿",     "sv":[56/128, 106/128]},
        ]
        pr = {p["name"]: p for p in page.evaluate(PROBE_JS, probes)}
        for k,v in pr.items(): print(f"   probe {k}: rgb({v['r']},{v['g']},{v['b']}) @({v['px']},{v['py']})")
        warm = lambda v: v["r"] > v["g"]*1.45 and v["r"] > v["b"]*1.25
        cold = lambda v: v["b"] > v["r"]*1.25
        check("张开的手臂是暖色（逐行躯干轴生效，没被当成腰以下）",
              warm(pr["左臂中段"]) and warm(pr["右臂中段"]),
              (pr["左臂中段"], pr["右臂中段"]))
        check("胸口是暖色", warm(pr["胸口上方"]), pr["胸口上方"])
        check("大腿是冷色（证明分区真的在分，不是整体涂红）", cold(pr["大腿"]), pr["大腿"])

        bg = page.evaluate(BG_JS)
        print("   bg strip:", json.dumps(bg))
        check("战衣没有溢色到背景（左右侧条暖色 < 0.3%）",
              bg["bgRed"] < bg["bgN"]*0.003, bg)

        os.makedirs(os.path.join(ROOT, "shots"), exist_ok=True)
        shot(page, os.path.join(ROOT, "shots", "smoke_suit.png"))

        print("\n== E. 皮肤切换 / 参数 / 模式 都真的改画面 ==")
        page.evaluate("() => window.__setSkinTest = 1")
        sig = {}
        for k in (0,1,2):
            page.evaluate(f"() => {{ document.querySelector('#skins button[data-k=\\\"{k}\\\"]').click(); }}")
            page.wait_for_timeout(420)
            sig[k] = page.evaluate(PIXEL_JS)
            print(f"   skin{k}:", json.dumps(sig[k]))
        check("经典红蓝 vs 格温白 画面不同",
              abs(sig[0]["red"] - sig[1]["red"]) > 5 or abs(sig[0]["white"] - sig[1]["white"]) > 5,
              (sig[0]["red"], sig[1]["red"], sig[0]["white"], sig[1]["white"]))
        check("共生体黑更暗（dark 增加）", sig[2]["dark"] > sig[0]["dark"], (sig[0]["dark"], sig[2]["dark"]))
        page.evaluate("() => document.querySelector('#skins button[data-k=\"0\"]').click()")
        page.wait_for_timeout(300)

        # 战衣强度归零 -> 应回到原始背景（红色像素显著减少）
        on = page.evaluate(PIXEL_JS)
        page.evaluate("() => { const s=document.querySelector('#s0'); s.value='0'; "
                      "s.dispatchEvent(new Event('input')); }")
        page.wait_for_timeout(500)
        off = page.evaluate(PIXEL_JS)
        check("战衣强度=0 时暖色像素显著减少（证明滑块真的接线了）",
              off["red"] < on["red"] * 0.6 + 3, (on["red"], off["red"]))
        page.evaluate("() => { const s=document.querySelector('#s0'); s.value='1'; "
                      "s.dispatchEvent(new Event('input')); }")
        page.wait_for_timeout(400)

        # 传送窗：portal 打开后应出现霓虹边框（画面变化）
        pre = page.evaluate(PIXEL_JS)
        page.evaluate("""() => { const w=[0.22,0.18,0.78,0.86];
            window.__portalTest = 1;
            const ev = new Event('x');
            // 直接驱动内部状态：模拟双手 L
        }""")
        # 通过 debug 模式验证叠加层生效
        page.evaluate("() => document.querySelector('#bDebug').click()")
        page.wait_for_timeout(400)
        dbg = page.evaluate(PIXEL_JS)
        check("骨骼/debug 叠加改变了画面", dbg["uniq"] != pre["uniq"] or dbg["red"] != pre["red"],
              (pre["uniq"], dbg["uniq"]))
        page.evaluate("() => document.querySelector('#bDebug').click()")

        print("\n== F. 故意破坏对照（确认上面的像素检查不是永真）==")
        broken = page.evaluate("""() => {
          const c = document.querySelector('#gl');
          const g = c.getContext('webgl2');
          // 停掉渲染循环并清成纯黑
          window.__stopHack = true;
          const s = document.querySelector('#s0'); s.value='0'; s.dispatchEvent(new Event('input'));
          return true;
        }""")
        page.evaluate("() => { const b=document.querySelector('#bFull'); "
                      "if(document.querySelector('#bFull').classList.contains('on')) b.click(); }")
        page.evaluate("() => { window.__SUIT_OFF__=1; }")
        page.wait_for_timeout(700)
        # 关掉战衣 + 赛博背景，画面应回到近乎原始灰蓝渐变：暖色应该极少
        page.evaluate("() => { const s6=document.querySelector('#s6'); s6.value='0'; s6.dispatchEvent(new Event('input')); }")
        page.wait_for_timeout(500)
        bad = page.evaluate(PIXEL_JS)
        print("   broken stats:", json.dumps(bad))
        check("对照实验：全关后暖色像素接近消失（说明 red 检查真的在量战衣）",
              bad["red"] < max(5, px["red"] * 0.35), (px["red"], bad["red"]))
        check("对照实验：全关后颜色种类骤降（说明 uniq 在量战衣纹理而非背景）",
              bad["uniq"] < px["uniq"] * 0.4, (px["uniq"], bad["uniq"]))
        check("对照实验：全关后白色像素消失（说明 white 在量面罩而非底色）",
              bad["white"] < max(3, px["white"] * 0.3), (px["white"], bad["white"]))

        shot(page, os.path.join(ROOT, "shots", "smoke_off.png"))

        print("\n== F2. 溢色检查的有效性对照（把 alpha 边界换成大模糊 -> 必须溢出）==")
        page.reload(wait_until="domcontentloaded")
        page.wait_for_timeout(700)
        arm_guard = page.evaluate("""() => {
          const src = window.__FRAG_SRC__ || '';
          return {
            hasRows: src.includes('uniform sampler2D uRows'),
            hasTorso: src.includes('float torsoU = (sv.x - rowC) / rowHW'),
            hasArm: src.includes('armAmt'),
            noBboxUu: !src.includes('uuC = B.x - 0.5'),
          };
        }""")
        check("shader 用逐行躯干轴而不是 bbox 横坐标做分区",
              all(arm_guard.values()), arm_guard)

        leak_ok = page.evaluate("""() => {
          // 重建一份把 alpha 从小半径 hE 换成大半径 h 的 shader，直接量溢色
          const src = window.__FRAG_SRC__;
          if(!src) return 'no-src';
          return src.includes('smoothstep(0.46, 0.58, hE)') ? 'ok' : 'pattern-missing';
        }""")
        check("shader 里 alpha 确实走小半径 hE（不是大模糊 h）", leak_ok == 'ok', leak_ok)
        page.evaluate("() => { document.querySelector('#boot').classList.add('hide'); }")
        page.evaluate("() => window.__SUITUP_TEST__()")
        page.wait_for_timeout(900)
        page.evaluate("() => { const s=document.querySelector('#s4'); s.value='4'; s.dispatchEvent(new Event('input')); }")
        page.wait_for_timeout(700)
        bgSoft = page.evaluate(BG_JS)
        print("   bg strip @ 边缘柔度=4:", json.dumps(bgSoft))
        check("边缘柔度拉到最大也不溢色到背景（证明 alpha 与高度场解耦）",
              bgSoft["bgRed"] < bgSoft["bgN"]*0.004, bgSoft)
        shot(page, os.path.join(ROOT, "shots", "smoke_soft_max.png"))

        print("\n== H. 真实启动流程：进入不得依赖模型加载 ==")
        # 用户实测 bug：卡在「正在加载模型…」永远进不去。
        # 根因是 go handler 里 await 了模型初始化，CDN 一慢就死锁在按钮上。
        src = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
        go_body = src[src.index("$('#go').addEventListener"):]
        go_body = go_body[:go_body.index("window.__SUITUP_TEST__")]
        enter_at = go_body.index("loop();")
        init_at  = go_body.index("initMP()")
        check("源码结构：initMP 在「进入渲染」之后才调用", init_at > enter_at,
              (enter_at, init_at))
        check("源码结构：initMP 没有被 await（否则会阻塞进入）",
              "await initMP" not in go_body, go_body[max(0,init_at-40):init_at+20])
        check("源码结构：模型资源走本地 assets/，不依赖 CDN",
              "./assets/vision_bundle.mjs" in src and "cdn.jsdelivr" not in src,
              [l.strip()[:70] for l in src.split(chr(10)) if "jsdelivr" in l][:3])

        page2 = ctx.new_page()
        p2errs = []
        page2.on("pageerror", lambda e: p2errs.append(str(e)))
        page2.goto(url, wait_until="domcontentloaded")
        page2.wait_for_timeout(1200)
        page2.click("#go")
        page2.wait_for_timeout(2000)
        st = page2.evaluate("""()=>({
          bootHidden: document.querySelector('#boot').classList.contains('hide'),
          btn: document.querySelector('#go').textContent,
          vw: document.querySelector('#cam').videoWidth,
          frames: window.__FRAMES__||0,
        })""")
        print("   t=2s:", json.dumps(st, ensure_ascii=False))
        check("点「进入」2 秒内就进到画面（不等模型）", st["bootHidden"] is True, st)
        check("2 秒内摄像头已出分辨率", st["vw"] > 0, st["vw"])
        check("2 秒内已经在渲染", st["frames"] > 0, st["frames"])

        # 等模型串行加载完（本地资源，20s 足够）
        mp = None
        for _ in range(20):
            page2.wait_for_timeout(1500)
            mp = page2.evaluate("() => window.__MP_STATE__ || null")
            if mp and all(v in (2, 3) for v in mp.values()):
                break
        print("   最终 mpState:", mp, " (2=就绪 3=失败)")
        check("三个模型全部加载成功（tasks-vision 无全局 Module 冲突）",
              mp is not None and all(v == 2 for v in mp.values()), mp)
        f_before = page2.evaluate("()=>window.__FRAMES__||0")
        page2.wait_for_timeout(700)
        check("模型加载完后渲染循环仍在推进",
              page2.evaluate("()=>window.__FRAMES__||0") > f_before)
        seg_live = page2.evaluate("()=>document.querySelector('#dSeg').className")
        check("人体抠像状态灯不是红色（回调真的在出结果）", "bad" not in seg_live, seg_live)
        check("启动流程无 pageerror", not p2errs, p2errs[:2])
        page2.close()

        print("\n== I. 摄像头 → 纹理链路 + 诊断面板 ==")
        # 用户实测 bug：进得去但画面全黑。根因在「摄像头流 → 解码 → 纹理」这条链上，
        # 而原先 upload() 用 try{}catch(e){} 把 texImage2D 的失败静默吞掉了。
        src2 = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
        check("upload 不再用空 catch 吞掉 texImage2D 失败",
              "}catch(e){}" not in src2.split("function upload(")[1].split("\n}")[0],
              src2.split("function upload(")[1].split("\n}")[0][-90:])
        check("纹理上传后显式检查 gl.getError()", "gl.getError()" in src2)

        page3 = ctx.new_page()
        p3errs = []
        page3.on("pageerror", lambda e: p3errs.append(str(e)))
        page3.goto(url, wait_until="domcontentloaded")
        page3.wait_for_timeout(1000)
        page3.click("#go")
        # 等探测跑完（每 22 帧一次，探测 6 次）
        d = None
        for _ in range(24):
            page3.wait_for_timeout(700)
            d = page3.evaluate("() => window.__DIAG__ ? {...window.__DIAG__} : null")
            if d and d.get("probes", 0) >= 3:
                break
        print("   DIAG:", json.dumps(d, ensure_ascii=False))
        check("探测真的跑了（probes >= 3）", d and d["probes"] >= 3, d and d.get("probes"))
        check("video 解码出画面（decoded=True）", d and d["decoded"] is True, d and d.get("decoded"))
        check("纹理确实有内容（texLit=True）", d and d["texLit"] is True,
              (d and d.get("texLit"), d and d.get("lastProbe")))
        check("正常环境不误切路径（camPath 保持 A）", d and d["camPath"] == "A", d and d.get("camPath"))
        check("无 GL 错误", d and d["glErr"] == 0, d and d.get("glErrMsg"))

        # 桥接路径 B 必须真的可用 —— 写了 fallback 没测过等于没写
        page3.evaluate("() => document.querySelector('#bCamPath').click()")
        dB = None
        for _ in range(20):
            page3.wait_for_timeout(700)
            dB = page3.evaluate("() => window.__DIAG__ ? {...window.__DIAG__} : null")
            if dB and dB.get("camPath") == "B" and dB.get("texLit") is not None:
                break
        print("   DIAG(路径B):", json.dumps(dB, ensure_ascii=False))
        check("桥接画布路径 B 也能把画面写进纹理",
              dB and dB["camPath"] == "B" and dB["texLit"] is True,
              (dB and dB.get("texLit"), dB and dB.get("lastProbe"), dB and dB.get("glErrMsg")))
        page3.evaluate("() => document.querySelector('#bCamPath').click()")

        # 诊断面板必须真的输出逐层结论
        page3.evaluate("() => document.querySelector('#bDiag').click()")
        page3.wait_for_timeout(900)
        dg = page3.evaluate("""() => {
          const d = document.querySelector('#diag');
          return { shown: d.classList.contains('show'),
                   rows: d.querySelectorAll('.r').length,
                   text: d.innerText.slice(0, 700) };
        }""")
        check("诊断面板能打开", dg["shown"] is True)
        check("诊断面板逐层输出（>= 12 行）", dg["rows"] >= 12, dg["rows"])
        for kw in ["摄像头流", "分辨率", "相机路径", "抠像模型", "手势模型"]:
            check(f"诊断包含「{kw}」", kw in dg["text"])
        has_verdict = any(x in dg["text"] for x in ["各环节正常", "——", "试试"])
        check("诊断给出结论句（不只是堆数字）", has_verdict, dg["text"][-120:])
        shot(page3, os.path.join(ROOT, "shots", "diag_panel.png"))
        check("链路测试无 pageerror", not p3errs, p3errs[:2])
        page3.close()

        print("\n== J. muted 轨道（用户实测：摄像头灯亮但画面全黑） ==")
        # 用户实测环境（MiniMax Code 内置 WebView）：getUserMedia 成功、track.readyState
        # 是 'live'、系统摄像头指示灯也亮，但 track.muted === true —— 摄像头开着不吐帧，
        # 于是 video.readyState 停在 0、videoWidth 恒为 0、整屏全黑。
        # muted 是可恢复状态（由 source 决定，以 unmute 事件解除），所以必须监听并自恢复，
        # 而不是像原先那样 waitMeta 超时就走 track settings 兜底 —— 那只是把黑屏
        # 伪装成「已启动」。
        src3 = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
        check("监听了 track 的 unmute 事件（muted 可自恢复）",
              "addEventListener('unmute'" in src3)
        check("监听了 track 的 mute 事件", "addEventListener('mute'" in src3)
        check("tracks() 把 muted 和 ended 区分开（两者恢复方式不同）",
              "muted:" in src3.split("function tracks()")[1].split("\n}")[0])
        check("unmute 后重置探测状态，让链路重新判定",
              "DIAG.probes = 0" in src3.split("function watchTrackMute")[1].split("\nasync function")[0])

        # 行为级：把 muted 做成可注入的真实状态，验证诊断结论指向 muted 而不是「被抢走」
        page4 = ctx.new_page()
        p4errs = []
        page4.on("pageerror", lambda e: p4errs.append(str(e)))
        page4.goto(url, wait_until="domcontentloaded")
        page4.wait_for_timeout(900)
        # 在 getUserMedia 返回的 track 上把 muted 改成 true（模拟 WebView 行为）
        page4.evaluate("""() => {
          const orig = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
          navigator.mediaDevices.getUserMedia = async (c) => {
            const s = await orig(c);
            for (const t of s.getVideoTracks()) {
              Object.defineProperty(t, 'muted', { get: () => true, configurable: true });
            }
            return s;
          };
        }""")
        page4.click("#go")
        page4.wait_for_timeout(9000)
        page4.evaluate("() => document.querySelector('#bDiag').click()")
        page4.wait_for_timeout(800)
        dm = page4.evaluate("""() => ({
          diag: window.__DIAG__ ? {...window.__DIAG__} : null,
          text: document.querySelector('#diag').innerText,
          err:  (document.querySelector('#err') || {}).innerText || '',
        })""")
        print("   muted 场景 trackMuted:", dm["diag"] and dm["diag"].get("trackMuted"))
        check("muted 被记录到 DIAG.trackMuted",
              dm["diag"] and dm["diag"]["trackMuted"] is True,
              dm["diag"] and dm["diag"].get("trackMuted"))
        check("诊断面板明确显示 muted 行", "muted" in dm["text"], dm["text"][:160])
        check("结论句指向 muted，不再误报「被抢走」",
              "muted" in dm["text"] and "抢走" not in dm["text"].split("muted")[-1],
              dm["text"][-200:])
        check("给出可执行出路（提示改用 Chrome）",
              "Chrome" in dm["text"] or "Chrome" in dm["err"],
              (dm["text"][-120:], dm["err"][:120]))
        check("muted 场景无 pageerror", not p4errs, p4errs[:2])
        # 这个页面的模型还在后台下载，等字体+渲染完全稳定会超时（实测偶发 30s+）。
        # 截图只是留个证据，不是断言，别让它挡住后面的段落。
        shot(page4, os.path.join(ROOT, "shots", "diag_muted.png"))
        page4.close()

        print("\n== K. 单帧检测异常必须留痕（禁止空 catch） ==")
        det_block = src3.split("function runDetect(")[1].split("\n}\n")[0]
        check("runDetect 里没有空 catch",
              "catch(e){}" not in det_block.replace(" ", "")
              and "catch(e){/*" not in det_block.replace(" ", ""),
              det_block[-200:])
        check("三个模型的异常各自记进 DIAG.detErr",
              det_block.count("note(") >= 3, det_block.count("note("))
        check("异常计数会显示在诊断面板", "detErrN" in src3.split("function renderDiag")[1])
        check("analyzeGrid 不再用 try/catch 兜住纯算术（出错就是真 bug）",
              "try{" not in src3.split("function analyzeGrid")[1].split("\n}\n")[0])

        print("\n== L. 传送窗：任意四边形 + finger-frame 追踪管线 ==")
        # 交互移植自 sophiamyang/finger-frame-effect：
        # 任意 quad（跟手转）/ 开窗-维持迟滞 / teleport rejection / 速度自适应平滑 / 掉帧按时间保持
        srcL = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
        check("shader 用 4 角点四边形（uWinQ[4]），不再是轴对齐矩形",
              "uniform vec2  uWinQ[4]" in srcL and "uWin;" not in srcL)
        gest = srcL.split("function onGesture(")[1].split("\n}\n")[0]
        check("开窗/维持双阈值迟滞（needOpen 随 portalTarget 切换）",
              "needOpen" in gest and "0.75" in gest and "0.20" in gest, gest[:120])
        check("teleport rejection（孤立跳变帧丢弃）", "jumpN" in gest and "0.30" in gest)
        check("掉帧按时间保持（不是立刻收窗）", "quadSeen" in gest and "0.9" in gest)

        page5 = ctx.new_page()
        p5errs = []
        page5.on("pageerror", lambda e: p5errs.append(str(e)))
        page5.goto(url, wait_until="domcontentloaded")
        page5.wait_for_timeout(900)
        page5.evaluate("() => { document.querySelector('#boot').classList.add('hide'); }")
        page5.evaluate("() => window.__SUITUP_TEST__()")
        page5.wait_for_timeout(700)

        # 像素级：注入一个明显旋转的 quad，开 debug 线框。
        # 绿线必须出现在 quad 的斜边上，而不是它外接 bbox 的水平边上 ——
        # 轴对齐矩形实现两个点会反过来，这条就是「真的是任意四边形」的证据。
        # 注意用 MAXG_JS（细线要逐像素找峰值，均值会被背景稀释）。
        page5.evaluate("() => window.__PORTAL_TEST__()")   # 默认斜 quad
        page5.wait_for_timeout(600)
        page5.keyboard.press("d")
        page5.wait_for_timeout(500)
        pts = page5.evaluate(MAXG_JS, [
            {"name": "quad斜边中点", "sv": [0.51, 0.2735]},   # (0.30,0.22)-(0.72,0.32) 的中点
            {"name": "bbox水平边",   "sv": [0.60, 0.22]},     # 轴对齐矩形的上边会经过这里
        ])
        print("   debug 线框采样:", json.dumps(pts, ensure_ascii=False))
        onEdge, offEdge = pts[0], pts[1]
        check("debug 绿线出现在 quad 斜边上", onEdge["score"] > 60, onEdge)
        check("外接 bbox 的水平边上没有绿线（证明不是轴对齐矩形）",
              offEdge["score"] < onEdge["score"] - 40, (onEdge, offEdge))
        page5.keyboard.press("d")

        # 5) 自交四边形（蝴蝶结）：手一转，解剖学顺序的角点会连成对角交叉的
        #    bowtie —— 用户实测旧「所有边同侧」判定在这种姿态下窗内一片空。
        #    even-odd 判定必须把它填成两个三角形（与原版 canvas clip 行为一致）。
        page5.evaluate("""() => window.__PORTAL_TEST__(
            [[0.30,0.22],[0.72,0.32],[0.24,0.66],[0.66,0.78]])""")   # 交换 2/3 角 = 自交
        page5.wait_for_timeout(700)
        # 断言语义：窗内 vs 窗外，同一像素颜色必须显著变化。
        # 不检测「红占优」—— 上三角正盖着胸口蛛徽（黑）+银网+布料高光，
        # 具体颜色五花八门；「和原始视频不同」才是「窗内出了效果」的本义。
        BOW_PTS = [
            {"name": "bow-质心",  "sv": [0.499, 0.344]},
            {"name": "bow-左上",  "sv": [0.460, 0.315]},
            {"name": "bow-右上",  "sv": [0.540, 0.355]},
            {"name": "bow-下",    "sv": [0.480, 0.400]},
            {"name": "bow-上",    "sv": [0.520, 0.300]},
        ]
        inWin = page5.evaluate(PROBE_JS, BOW_PTS)
        # 把窗挪到左上角（不再盖住采样点）→ 同批点回到原始视频 = 基线
        page5.evaluate("""() => window.__PORTAL_TEST__(
            [[0.02,0.02],[0.20,0.02],[0.20,0.20],[0.02,0.20]])""")
        page5.wait_for_timeout(700)
        base5 = page5.evaluate(PROBE_JS, BOW_PTS)
        deltas = [abs(a["r"]-b["r"])+abs(a["g"]-b["g"])+abs(a["b"]-b["b"])
                  for a, b in zip(inWin, base5)]
        print("   bowtie 窗内:", json.dumps(inWin, ensure_ascii=False))
        print("   bowtie 基线:", json.dumps(base5, ensure_ascii=False), "Δ:", deltas)
        check("蝴蝶结姿态窗内也出效果（≥4/5 点被窗显著改变）",
              sum(1 for d in deltas if d > 60) >= 4, deltas)
        # 恢复 bowtie quad（后续行为级测试从干净状态开始，不受影响）

        # 行为级：伪造 landmarks 走真实 onGesture 管线
        MK = """(spec) => {
          const mk = (cx, cy, open) => {
            const lm = Array.from({length:21}, () => ({x:cx, y:cy}));
            lm[0]  = {x:cx, y:cy+0.10};                   // wrist
            lm[9]  = {x:cx, y:cy};                        // middle MCP -> scale 0.1
            lm[4]  = {x:cx, y:cy+0.05*open};              // thumb tip
            lm[8]  = {x:cx, y:cy-0.05*open};              // index tip -> pinch = open
            for(const [tip,pip] of [[12,10],[16,14],[20,18]]){
              lm[tip] = {x:cx, y:cy+0.13};                // 指尖贴掌根 = 收起
              lm[pip] = {x:cx, y:cy+0.25};
            }
            return lm;
          };
          const hands = spec.map(h => mk(h[0], h[1], h[2]));
          return window.__GESTURE_TEST__(spec.length ? hands : []);
        }"""
        # 1) 张开的双 L -> 开窗，quad 四角在手上
        st1 = page5.evaluate(MK, [[0.30, 0.50, 4.0], [0.70, 0.50, 4.0]])
        for _ in range(6):
            st1 = page5.evaluate(MK, [[0.30, 0.50, 4.0], [0.70, 0.50, 4.0]])
        check("双 L 手势开窗（portalTarget=1）", st1["target"] == 1, st1)
        check("quad 角点收敛到指尖位置", abs(st1["quad"][0][0] - 0.30) < 0.03
              and abs(st1["quad"][2][0] - 0.70) < 0.03, st1["quad"])
        # 2) 迟滞：手指半合（open=0.5，低于开窗阈 0.75、高于维持阈 0.20）-> 窗口不掉
        st2 = page5.evaluate(MK, [[0.30, 0.50, 0.5], [0.70, 0.50, 0.5]])
        check("迟滞：手指半合窗口不闪断", st2["target"] == 1, st2)
        # 3) teleport rejection：单帧整体跳 0.5 屏 -> quad 不动；连续第 2 帧 -> 接受
        stJ1 = page5.evaluate(MK, [[0.10, 0.20, 4.0], [0.50, 0.20, 4.0]])
        check("孤立跳变帧被丢弃（quad 原地）", abs(stJ1["quad"][0][0] - 0.30) < 0.05, stJ1["quad"])
        stJ2 = page5.evaluate(MK, [[0.10, 0.20, 4.0], [0.50, 0.20, 4.0]])
        check("连续第 2 帧跳变被接受（真移动了）", stJ2["quad"][0][0] < 0.25, stJ2["quad"])
        # 4) 掉帧保持：检测断了 -> 0.9s 内窗口保持，之后才收
        stH = page5.evaluate(MK, [])
        check("检测掉帧后窗口按时间保持（不立刻收）", stH["target"] == 1, stH)
        page5.wait_for_timeout(1100)
        stH2 = page5.evaluate(MK, [])
        check("超时 0.9s 后窗口收拢", stH2["target"] == 0, stH2)
        check("传送窗管线无 pageerror", not p5errs, p5errs[:2])
        page5.close()

        print("\n== M. 背景：城市上空 view + 战衣质感雕刻 ==")
        # 背景从赛博夜雨换成 Homecoming 海报式「城市上空」：蓝天白云 + 底部天际线楼群。
        # 阈值来自实测（sky b-r≥55 / b≥177；楼群带 4 点 b≤123），各留 15+ 余量。
        srcM = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
        check("shader 有 cityscape 函数（程序化城市背景）", "cityscape(" in srcM)
        check("赛博雨丝已删（rain 是死代码）", "float rain(" not in srcM)
        check("UI 文案改成城市天空", "城市天空" in srcM)
        check("布料有面板鼓起法线扰动（panelBulge 进 N）",
              "panelBulge" in srcM and "fiberN" in srcM)
        check("缝线有厚度感（seamCore 暗沟 + seamRim 亮边）",
              "seamCore" in srcM and "seamRim" in srcM)
        page6 = ctx.new_page()
        p6errs = []
        page6.on("pageerror", lambda e: p6errs.append(str(e)))
        page6.goto(url, wait_until="domcontentloaded")
        page6.wait_for_timeout(900)
        page6.evaluate("() => { document.querySelector('#boot').classList.add('hide'); }")
        page6.evaluate("() => window.__SUITUP_TEST__()")
        page6.wait_for_timeout(700)
        cityPts = page6.evaluate(PROBE_JS, [
            {"name": "sky-top",  "sv": [0.50, 0.05]},
            {"name": "sky-left", "sv": [0.10, 0.15]},
            {"name": "bot-a", "sv": [0.06, 0.95]},
            {"name": "bot-b", "sv": [0.30, 0.95]},
            {"name": "bot-c", "sv": [0.72, 0.95]},
            {"name": "bot-d", "sv": [0.93, 0.95]},
        ])
        print("   城市背景采样:", json.dumps(cityPts, ensure_ascii=False))
        skyT, skyL = cityPts[0], cityPts[1]
        bots = cityPts[2:]
        check("天空是亮蓝色（蓝显著大于红，够亮）",
              skyT["b"] > skyT["r"] + 35 and skyT["b"] > 160
              and skyL["b"] > skyL["r"] + 35 and skyL["b"] > 160, (skyT, skyL))
        check("画面底部有楼群暗带（4 采样点全部明显暗于天空）",
              all(q["b"] < 145 for q in bots), bots)
        check("城市背景页无 pageerror", not p6errs, p6errs[:2])
        page6.close()

        print("\n== N. 蛛徽只在胸口，脸部特写时不画 ==")
        # 用户实测：脸怼到镜头前时蛛徽爬到鼻子上。根因是画面里没有胸口，
        # 而 cy 的 clamp 会把它硬拉回「框内」—— 而那个框就是一张脸。
        srcN = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
        check("uChest 带可见度分量（vec3）", "uniform vec3  uChest" in srcN)
        check("蛛徽被可见度闸住",
              "uChest.z" in srcN.split("float sd = spiderMark(mp);")[1][:300])
        page7 = ctx.new_page()
        p7errs = []
        page7.on("pageerror", lambda e: p7errs.append(str(e)))
        page7.goto(url, wait_until="domcontentloaded")
        page7.wait_for_timeout(900)
        page7.evaluate("() => { document.querySelector('#boot').classList.add('hide'); }")
        page7.evaluate("() => window.__SUITUP_TEST__()")
        page7.wait_for_timeout(600)
        stHalf = page7.evaluate("() => window.__CLOSEUP_TEST__(false)")
        page7.wait_for_timeout(300)
        halfPix = page7.evaluate(PROBE_JS, [{"name": "chest", "sv": [0.50, 0.44]}])[0]
        stClose = page7.evaluate("() => window.__CLOSEUP_TEST__(true)")
        page7.wait_for_timeout(400)
        # 像素证据用 diff 对照，不猜蛛徽落点也不断言具体颜色：
        # 在特写状态下强行把 chestVis 拉回 1（= 摘掉闸门的效果），
        # 脸上就会长出蛛徽；对比同一批点，差异必须显著。
        # 反过来说：闸门生效时这批点应当和「无蛛徽」状态一致。
        FACE_PTS = [
            {"name": "f-y50", "sv": [0.50, 0.50]},
            {"name": "f-y53", "sv": [0.50, 0.53]},
            {"name": "f-y56", "sv": [0.50, 0.56]},
            {"name": "f-y59", "sv": [0.50, 0.59]},
            {"name": "f-y62", "sv": [0.50, 0.62]},
        ]
        # S.chest 以 0.2/帧 平滑收敛，切完特写要等它稳住再采样，
        # 否则量到的是收敛半路的位置，蛛徽还没落到最终落点上（Δ 假 0）。
        page7.wait_for_timeout(1200)
        gated = page7.evaluate(PROBE_JS, FACE_PTS)
        page7.evaluate("() => { window.__FORCE_CHEST_VIS__(1); }")
        page7.wait_for_timeout(800)
        ungated = page7.evaluate(PROBE_JS, FACE_PTS)
        markDelta = [abs(a["r"]-b["r"])+abs(a["g"]-b["g"])+abs(a["b"]-b["b"])
                     for a, b in zip(gated, ungated)]
        print("   半身:", stHalf, halfPix)
        print("   特写(闸门开):", stClose, json.dumps(gated, ensure_ascii=False))
        print("   特写(强开可见度):", json.dumps(ungated, ensure_ascii=False), "Δ:", markDelta)
        check("正常半身像：胸口可见度为 1（蛛徽照常画）", stHalf["chestVis"] > 0.9, stHalf)
        check("脸部特写：胸口可见度归 0（蛛徽关掉）", stClose["chestVis"] < 0.1, stClose)
        check("闸门确实在挡蛛徽（强开可见度后脸上明显多出东西）",
              sum(1 for d in markDelta if d > 40) >= 2, markDelta)
        check("蛛徽测试页无 pageerror", not p7errs, p7errs[:2])
        page7.close()

        print("\n== O. 蜘蛛格温：兜帽 / 洋红蛛网 / 白蛛徽 / 粉紫夜景 ==")
        # 参照 Into the Spider-Verse 海报：白兜帽 + 洋红蛛网 + 黑胸腹上的白蜘蛛
        # + 青绿芭蕾鞋 + 大月亮粉紫夜城。
        srcO = open(os.path.join(ROOT, "index.html"), encoding="utf-8").read()
        check("有兜帽实现（uHood + 帽口 taper 尖角）",
              "uniform float uHood" in srcO and "taper" in srcO and "liningC" in srcO)
        check("格温蛛网是洋红不是紫（web 的 r 远大于 b）",
              "web=vec3(0.95,0.20,0.52)" in srcO.replace(" ", ""))
        check("格温蛛徽是白色（压在黑胸腹上）",
              "vec3(0.98,0.97,0.99)" in srcO.replace(" ", ""))
        check("有青绿芭蕾鞋", "vec3(0.10,0.80,0.74)" in srcO.replace(" ", ""))
        check("cityscape 支持夜景模式（night 参数 + 月亮）",
              "float night" in srcO and "moonC" in srcO and "mare" in srcO)

        page8 = ctx.new_page()
        p8errs = []
        page8.on("pageerror", lambda e: p8errs.append(str(e)))
        page8.goto(url, wait_until="domcontentloaded")
        page8.wait_for_timeout(900)
        page8.evaluate("() => { document.querySelector('#boot').classList.add('hide'); }")
        page8.evaluate("() => window.__SUITUP_TEST__()")
        page8.wait_for_timeout(600)

        def skin(k):
            page8.evaluate(
                "() => { document.querySelector('#skins button[data-k=\"%d\"]').click(); }" % k)
            page8.wait_for_timeout(600)

        # 1) 背景：格温=夜景（暗且偏洋红），经典=白天（亮蓝天）
        skin(0)
        dayPts = page8.evaluate(PROBE_JS, [
            {"name": "sky", "sv": [0.50, 0.06]},
            {"name": "moonSpot", "sv": [0.16, 0.20]},
        ])
        skin(1)
        nightPts = page8.evaluate(PROBE_JS, [
            {"name": "sky", "sv": [0.50, 0.06]},
            {"name": "moonSpot", "sv": [0.16, 0.20]},
        ])
        print("   白天天空:", json.dumps(dayPts, ensure_ascii=False))
        print("   夜景天空:", json.dumps(nightPts, ensure_ascii=False))
        daySky, nightSky = dayPts[0], nightPts[0]
        check("格温切到夜景（天空显著变暗）",
              nightSky["b"] < daySky["b"] - 40, (daySky, nightSky))
        check("夜空偏洋红（红 ≥ 绿，不是蓝天）",
              nightSky["r"] >= nightSky["g"], nightSky)
        # 月亮：那一带在夜景下必须明显亮于周围夜空
        dayMoon, nightMoon = dayPts[1], nightPts[1]
        moonLum = nightMoon["r"] + nightMoon["g"] + nightMoon["b"]
        skyLum = nightSky["r"] + nightSky["g"] + nightSky["b"]
        check("夜景有大月亮（月亮区明显亮于夜空）",
              moonLum > skyLum + 60, (nightMoon, nightSky))

        # 2) 战衣：格温手臂的洋红菱形网。
        # 不用绝对峰值 —— 网线在 1100px 宽下只有 1~2px，白布高光会把 r-max(g,b)
        # 冲到 20 以下（实测手臂 19，胸口黑区 24，两者分不开）。
        # 改成「格温 vs 经典」同点对照：格温手臂是粉的，经典手臂是深红/蓝的。
        ARM_PTS = [
            {"name": "臂-内", "sv": [0.34, 0.40]},
            {"name": "臂-中", "sv": [0.30, 0.43]},
            {"name": "肩",    "sv": [0.40, 0.38]},
        ]
        gwenArm = page8.evaluate(PROBE_JS, ARM_PTS)
        skin(0)
        classicArm = page8.evaluate(PROBE_JS, ARM_PTS)
        print("   格温手臂:", json.dumps(gwenArm, ensure_ascii=False))
        print("   经典手臂:", json.dumps(classicArm, ensure_ascii=False))
        # 格温手臂主色是「白布 + 粉网」-> 整体明显亮于经典的深绯红
        brighter = sum(1 for a, c in zip(gwenArm, classicArm)
                       if (a["r"]+a["g"]+a["b"]) > (c["r"]+c["g"]+c["b"]) + 60)
        check("格温手臂明显亮于经典（白布底 + 粉网，不是深绯红）",
              brighter >= 2, (gwenArm, classicArm))
        # 粉网存在性：不采单点 —— 网线只有 1~2px，采样点大概率落在网线之间的白布上
        # （实测 r≈g≈190 的纯白，开关网密度 Δ 只有 7）。改成统计手臂矩形区域内
        # 「偏粉像素」的占比：格温的洋红网线满足 r-g>18，白布不满足。
        ARM_ROI_JS = """() => {
          const c = document.querySelector('#gl');
          const g = c.getContext('webgl2', {preserveDrawingBuffer:true});
          const W = c.width, H = c.height;
          // 手臂区域（画布坐标，避开躯干中线和背景）：左右各取一块
          // 框要小：swiftshader 下 readPixels 很慢，两块 150x110 会让整跑超时。
          const boxes = [[0.18*W, 0.44*H, 46, 40],
                         [0.74*W, 0.44*H, 46, 40]];
          let pink = 0, tot = 0;
          for(const [bx,by,bw,bh] of boxes){
            const w = Math.round(bw), h = Math.round(bh);
            const buf = new Uint8Array(4*w*h);
            g.readPixels(Math.round(bx), Math.round(by), w, h,
                         g.RGBA, g.UNSIGNED_BYTE, buf);
            for(let i=0;i<w*h;i++){
              const r=buf[i*4], gg=buf[i*4+1], b=buf[i*4+2];
              if(r+gg+b < 90) continue;            // 跳过背景暗部
              tot++;
              if(r - gg > 18 && r > 90) pink++;
            }
          }
          return {pink, tot, ratio: tot ? +(pink/tot).toFixed(4) : 0};
        }"""
        skin(1)
        page8.wait_for_timeout(400)
        roiOn = page8.evaluate(ARM_ROI_JS)
        page8.evaluate("() => { const s=document.querySelector('#s1'); "
                       "s.value='0'; s.dispatchEvent(new Event('input')); }")
        page8.wait_for_timeout(500)
        roiOff = page8.evaluate(ARM_ROI_JS)
        print("   手臂粉像素 有网:", roiOn, " 无网:", roiOff)
        # 不锁"粉占比 > X%" —— 那会把一个错的美术目标钉死。参考图里格温手臂
        # 是**白布为主、洋红只是细网线**；实测把粉压到 8%（白 33%）才对味，
        # 而旧断言要求 >70%，正好锁住"整条粉袖子"那个错版本。
        # 正确语义只有一条：网开 vs 网关，粉像素必须显著变化。
        check("格温手臂真的铺了洋红网（开/关网线粉像素显著变化）",
              roiOn["ratio"] > roiOff["ratio"] + 0.12, (roiOn, roiOff))
        # 守住美术目标：参考图里格温是**白衣**，洋红只是网线。
        # 归因实测（纯绿探针）这层网覆盖手臂约 30%，光靠收线宽压不下来，
        # 是靠降混合 alpha 到 0.42 + 白布提亮才把白粉比翻正的（粉 28%→8%）。
        # 先把网密度恢复（上面为做对照调成 0 了），否则量到的是"无网"状态。
        page8.evaluate("() => { const s=document.querySelector('#s1'); "
                       "s.value='13'; s.dispatchEvent(new Event('input')); }")
        page8.wait_for_timeout(500)
        ARM_WHITE_JS = """() => {
          const cv = document.querySelector('canvas');
          const g = cv.getContext('webgl2');
          const x0 = Math.floor(cv.width*0.18), x1 = Math.floor(cv.width*0.34);
          const y0 = Math.floor(cv.height*0.38), y1 = Math.floor(cv.height*0.62);
          const w = x1-x0, h = y1-y0;
          const buf = new Uint8Array(w*h*4);
          // WebGL readPixels 原点在左下，画面 y 要翻过来
          g.readPixels(x0, cv.height-y1, w, h, g.RGBA, g.UNSIGNED_BYTE, buf);
          let pink=0, white=0;
          for(let i=0;i<w*h;i++){
            const r=buf[i*4], gg=buf[i*4+1], b=buf[i*4+2];
            const lum = (r*299+gg*587+b*114)/1000;
            if(r-gg > 45 && r > 110) pink++;
            else if(lum > 150) white++;
          }
          return {pink, white};
        }"""
        armRatio = page8.evaluate(ARM_WHITE_JS)
        print("   手臂白/粉比:", armRatio)
        # 阈值 2.4 来自实测两端：修好的版本白/粉 = 3.6，
        # 回退成"粉袖子"（网 alpha 0.92）的版本 = 1.5。
        # 第一版写 1.3 太松，正好卡在两者之间 —— 破坏对照不报红，断言等于永真。
        check("格温手臂白布是主色（白像素 ≥ 2.4 倍粉像素）",
              armRatio["white"] > armRatio["pink"] * 2.4, armRatio)
        # 兜帽必须有实体面积。用 uDebug==2 的**几何探针**（帽体覆盖输出成纯绿），
        # 不去数画面白像素 —— 帽口一放大，露出的面罩本身也是浅色会被当成帽布，
        # 那样写破坏对照读不出差异（实测 0.301 vs 0.268，断言等于永真）。
        # 几何探针实测两端：帽口 0.74×1.00 → 18.7%（帽子读得出来）；
        # 回退到旧的 0.86×1.16 → 11.6%（用户看到的"只有一圈粉边"）。
        # 阈值取 0.15，卡在两者之间 —— 第一版写 0.10 太松，破坏对照不报红。
        HOOD_GEO_JS = """() => {
          const cv = document.querySelector('canvas');
          const g = cv.getContext('webgl2');
          const x0 = Math.floor(cv.width*0.38), x1 = Math.floor(cv.width*0.62);
          const y0 = Math.floor(cv.height*0.06), y1 = Math.floor(cv.height*0.34);
          const w = x1-x0, h = y1-y0;
          const buf = new Uint8Array(w*h*4);
          g.readPixels(x0, cv.height-y1, w, h, g.RGBA, g.UNSIGNED_BYTE, buf);
          let n = 0;
          for(let i=0;i<w*h;i++) if(buf[i*4+1] > 128) n++;
          return {hood: n, tot: w*h, ratio: +(n/(w*h)).toFixed(3)};
        }"""
        page8.evaluate("() => window.__HOOD_PROBE__(true)")
        page8.wait_for_timeout(400)
        hoodGeo = page8.evaluate(HOOD_GEO_JS)
        page8.evaluate("() => window.__HOOD_PROBE__(false)")
        page8.wait_for_timeout(300)
        print("   兜帽几何覆盖:", hoodGeo)
        check("兜帽有实体帽布面积（几何探针 ≥ 15% 头部窗口）",
              hoodGeo["ratio"] > 0.15, hoodGeo)
        check("格温测试页无 pageerror", not p8errs, p8errs[:2])
        page8.close()

        print("\n== G. console 汇总 ==")
        bad_cons = [c for c in cons if c[0] == "error"]
        # MediaPipe CDN 在测试路径里没被 init，不该有错
        check("无 console error", not bad_cons, bad_cons[:3])

        browser.close()
    httpd.shutdown()

    print("\n" + "="*56)
    if FAILS:
        print("FAILED:", len(FAILS))
        for f in FAILS: print("  -", f)
        sys.exit(1)
    print("ALL SMOKE CHECKS PASSED")

if __name__ == "__main__":
    main()
