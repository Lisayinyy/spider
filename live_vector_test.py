#!/usr/bin/env python3
"""实时矢量化（live 模式）验收。

破坏对照记录（每条断言都实测过牙齿）：
  去 posterize（post = flat3）        → levels_controls_banding 红
  去 Sobel（vecCol = post）           → edge_threshold_works 红
  真正禁用 Kuwahara（四个分支全换成中心采样）
                                      → vectorize_changes_window + kuwahara_radius_flattens 红
  live 改回盖静态底图                 → live_mode_no_static_plate 红
  去掉 portal 闸门（vecAmt = uVecMix）→ outside_window_untouched 红

坑：只把 `vec3 flat3 = mA` 换成中心采样**不是**有效破坏 —— 后面三个
`if(vX < best) flat3 = mX` 会把值覆盖回去，测试照样全绿。做破坏对照时
必须确认被破坏的值没有在后续代码里被重新赋回。

覆盖的语义是「窗内真人被实时变成矢量插画」，这是参考视频的核心效果，
也是 vector_test.py（测静态底图路径）完全没有覆盖的部分。

算法来源：GPUImage2 ToonFilter / KuwaharaFilter
（Kyprianidis, Kang, Doellner, "Anisotropic Kuwahara Filtering on the GPU",
 GPU Pro p.247, 2010）。三段式：Kuwahara 保边平滑 → posterize 色阶量化 → Sobel 描边。

统计全部在页面内完成，只回传数字（整幅像素数组回传 Python 会超时）。
"""
import threading, http.server, socketserver, functools, json, sys
from playwright.sync_api import sync_playwright

ROOT = '/Users/minimax/.mavis/agents/main/workspace/spiderman-ar'
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8985
H = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT)
socketserver.TCPServer.allow_reuse_address = True
srv = socketserver.TCPServer(('127.0.0.1', PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

# 对给定归一化 ROI 统计：唯一色数（5bit 量化，滤掉 8bit 抖动）、暗描边占比、
# 3x3 局部方差均值（衡量「平不平」）。
STAT = r'''(r)=>{
 const c=document.querySelector('canvas'),w=c.width,h=c.height;
 const t=document.createElement('canvas');t.width=w;t.height=h;
 t.getContext('2d').drawImage(c,0,0);
 const x0=Math.floor(r[0]*w),y0=Math.floor(r[1]*h),x1=Math.floor(r[2]*w),y1=Math.floor(r[3]*h);
 const d=t.getContext('2d').getImageData(x0,y0,x1-x0,y1-y0).data;
 const W=x1-x0,H=y1-y0;
 const set=new Set(); let dark=0,n=0;
 const g=(x,y)=>{const i=(y*W+x)*4;return d[i]*.299+d[i+1]*.587+d[i+2]*.114;};
 for(let y=0;y<H;y++)for(let x=0;x<W;x++){
   const i=(y*W+x)*4;
   set.add(((d[i]>>3)<<10)|((d[i+1]>>3)<<5)|(d[i+2]>>3));
   if(g(x,y)<48) dark++;
   n++;
 }
 let lv=0,lc=0;
 for(let y=1;y<H-1;y+=2)for(let x=1;x<W-1;x+=2){
   let m=0,m2=0;
   for(let j=-1;j<=1;j++)for(let i2=-1;i2<=1;i2++){const v=g(x+i2,y+j);m+=v;m2+=v*v;}
   m/=9;m2/=9;lv+=Math.max(0,m2-m*m);lc++;
 }
 return {uniq:set.size, darkFrac:dark/n, localVar:lv/lc, n:n};
}'''

# __VEC_FEED__ 里的窗 quad 是 [0.12,0.16]..[0.88,0.86]
ROI_IN  = [.35, .35, .65, .65]
ROI_OUT = [.005, .30, .09, .70]

with sync_playwright() as p:
    b = p.chromium.launch(args=['--use-gl=angle', '--use-angle=swiftshader',
                                '--enable-unsafe-swiftshader', '--disable-gpu-sandbox'])
    pg = b.new_context(viewport={'width': 1000, 'height': 680}).new_page()
    errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.goto(f'http://127.0.0.1:{PORT}/index.html', wait_until='domcontentloaded')
    pg.wait_for_timeout(2000)
    pg.evaluate("()=>document.querySelector('#boot').classList.add('hide')")
    shader_ok = pg.evaluate('()=>window.__SHADER_OK__')
    shader_err = pg.evaluate('()=>window.__SHADER_ERR__')

    # 注入一张连续渐变 + 高频细节的测试纹理当摄像头，并停掉主循环
    pg.evaluate('()=>window.__VEC_FEED__()'); pg.wait_for_timeout(300)

    pg.evaluate('()=>window.__VEC_SET__(0)'); pg.wait_for_timeout(300)
    off_in = pg.evaluate(STAT, ROI_IN); off_out = pg.evaluate(STAT, ROI_OUT)
    pg.evaluate('()=>window.__VEC_SET__(1)'); pg.wait_for_timeout(300)
    on_in = pg.evaluate(STAT, ROI_IN); on_out = pg.evaluate(STAT, ROI_OUT)

    # 色阶数必须真的控制色块数量：2 阶应比 12 阶明显更少的唯一色
    pg.evaluate('()=>window.__VEC_SET__(1,2)'); pg.wait_for_timeout(250)
    lo = pg.evaluate(STAT, ROI_IN)
    pg.evaluate('()=>window.__VEC_SET__(1,12)'); pg.wait_for_timeout(250)
    hi = pg.evaluate(STAT, ROI_IN)

    # Kuwahara 的牙齿：半径必须真的控制平滑程度。
    # 破坏对照实测「on vs off 的 localVar 下降」这条断言没有牙齿 —— 把 Kuwahara
    # 退化成中心单点采样后它照样绿，因为 posterize 自己也会压低局部方差。
    # 所以必须对比大半径 vs 极小半径（其余参数不变），才只测到 Kuwahara 这一段。
    # 描边必须关掉（阈值调到 9.0 = 永不触发），否则描线自己制造局部对比，
    # 把平滑效果完全掩盖 —— 实测带描边时大/小半径只差 13%，关掉后差 29%。
    # 半径扫描实测 localVar：0.5→175.9 / 1→172.9 / 3.2→123.0 / 7→123.3 / 16→141.1，
    # 谷底在 3.2~7（默认 3.2 正在谷底），过大反而回升（窗口跨越不同色块）。
    pg.evaluate('()=>window.__VEC_SET__(1,6,3.2,9.0)'); pg.wait_for_timeout(250)
    radBig = pg.evaluate(STAT, ROI_IN)
    pg.evaluate('()=>window.__VEC_SET__(1,6,1.0,9.0)'); pg.wait_for_timeout(250)
    radSmall = pg.evaluate(STAT, ROI_IN)

    # 描边阈值必须真的控制描线量：阈值极低应描出远多于阈值极高的黑边
    pg.evaluate('()=>window.__VEC_SET__(1,6,3.2,0.02)'); pg.wait_for_timeout(250)
    edgeLo = pg.evaluate(STAT, ROI_IN)
    pg.evaluate('()=>window.__VEC_SET__(1,6,3.2,0.90)'); pg.wait_for_timeout(250)
    edgeHi = pg.evaluate(STAT, ROI_IN)

    # live 模式下 2D 层必须让开窗内（否则实时矢量化被静态底图完全盖住 —— 这正是
    # 用户反馈「和参考视频不一样」的根因）
    pg.evaluate('()=>window.__VEC_SET__(1,6,3.2,0.11)'); pg.wait_for_timeout(200)
    vst = pg.evaluate('()=>window.__VECTOR_STATE__()')
    plate_area = vst['vectorRect'][2] * vst['vectorRect'][3]

    checks = {
        'shader_compiles': bool(shader_ok) and not shader_err,
        # 整条链生效：窗内唯一色数明显减少 + 出现描边。
        # 注意这一条不能单独证明 posterize —— 破坏 posterize 后它仍绿（Kuwahara
        # 自己也会合并颜色）。真正测 posterize 的是下面的 levels_controls_banding。
        'vectorize_changes_window': on_in['uniq'] < off_in['uniq'] * 0.90,
        # Sobel 生效：出现明显的暗描边（关闭时几乎没有）
        'sobel_draws_ink': on_in['darkFrac'] > 0.05 and off_in['darkFrac'] < 0.02,
        # Kuwahara 生效：大半径必须比极小半径更平（只测这一段，与 posterize 解耦）
        # 实测 123.0 vs 172.9（比值 0.71），阈值 0.85 留足余量且能抓住退化
        'kuwahara_radius_flattens': radBig['localVar'] < radSmall['localVar'] * 0.85,
        # posterize 生效：色阶数必须真的控制色块数量（破坏 posterize 后此条转红）
        'levels_controls_banding': lo['uniq'] < hi['uniq'],
        # 描边阈值有牙齿
        'edge_threshold_works': edgeLo['darkFrac'] > edgeHi['darkFrac'] + 0.03,
        # 只作用在窗内，窗外保持真人不动
        'outside_window_untouched': abs(on_out['uniq'] - off_out['uniq']) <= 2
                                    and on_out['darkFrac'] < 0.02,
        # live 模式下静态底图不得占据窗内
        'live_mode_no_static_plate': plate_area == 0,
        'no_pageerror': not errs,
    }
    out = {
        'off_in': off_in, 'on_in': on_in,
        'off_out': off_out, 'on_out': on_out,
        'levels': {'L2': lo['uniq'], 'L12': hi['uniq']},
        'radius': {'big': radBig['localVar'], 'small': radSmall['localVar']},
        'edge': {'thrLow': edgeLo['darkFrac'], 'thrHigh': edgeHi['darkFrac']},
        'plate_area': plate_area,
        'shader_err': shader_err,
        'errors': errs,
        'checks': checks,
    }
    print(json.dumps(out, ensure_ascii=False))
    pg.screenshot(path='/tmp/live_vec.png')
    b.close()
    assert all(checks.values()), {k: v for k, v in checks.items() if not v}
srv.shutdown()
