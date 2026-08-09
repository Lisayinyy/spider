#!/usr/bin/env python3
"""实时矢量化验收 —— 两种风格 + 横向斜带几何。

## 参考视频的真相（ref/vecframes/，逐帧 3x 放大 + 10% 网格读出）
用户要复刻的小红书笔记，作者的做法和我最初的实现完全不同：
  ① 手指拉开的是一条**贯穿画面的横向斜带**，不是方框取景窗。
     左手食指(27%,30%) 右手食指(77%,32%) = 斜带上边界
     左手拇指(28%,58%) 右手拇指(76%,57%) = 斜带下边界
     带厚 ≈ 28% 画面高；左右一直延伸出画面（x 覆盖 0..1）。
  ② 带内是**日漫赛璐璐**（大面积平色 + 硬边阴影 + 几乎无描边），
     不是 Kuwahara+Sobel 那种 toon filter 的细描边风格。
  ③ 带内**连背景一起换**（窗框、吊灯都变成矢量插画），带外完全是原片。
  ④ 头发穿过带边界是连续的 → 作者是整帧预转绘 + 手势遮罩擦除，不是实时。
     我们这套是真实时（比原作强），但呈现形式必须对齐。

## 为什么测试台必须是真实人像
上一版只在 __VEC_FEED__ 的合成渐变方块上测，9 条断言全绿，
但同一份代码喂真人照片时 darkFrac 0.77、mean 从 64 掉到 23，满脸黑斑。
合成渐变方块**没有真实结构边界**，Sobel 在它上面几乎不产生描线
（阈值从 0.02 扫到 0.90，darkFrac 恒 0.017，纯永真）。
→ 合成纹理只能证明"算法在跑"，证明不了"真人看起来像矢量插画"。

## 两种风格的判据完全不同（不能共用断言）
赛璐璐（uVecStyle=1，默认）：
  暗部来自**阴影色块**（纯黑头发/暗面），不是描边。实测 dark≈ink≈0.422，
  即所有暗像素都低于 48 —— 是色块不是线。所以它的验收判据是
  rough（色块平整度）和 uniq（色数），**不能用 ink 判**。
toon filter（uVecStyle=0）：
  暗部主要来自 Sobel 描线，判据是 ink 的存在与不泛滥。

真人像基线（ROI [.30,.22,.70,.78]，shots/testface.png）：
  raw(vecMix=0)              mean  64.2  dark .451  ink .429  rough .183  uniq 139
  赛璐璐默认 L3 R5 E.22      mean  95.1  dark .422  ink .422  rough .134  uniq 106
  赛璐璐 关描边(E=9)         mean 106.2  dark .422             rough .077  uniq  88
  赛璐璐 R=1 / R=5（关描边）  rough .101 uniq 192  /  rough .077 uniq  88
  赛璐璐 L=2 / L=16（关描边） uniq  84 mean 136.9  /  uniq 125 rough .182
  toon L6 E.14               mean  68.9  dark .244  ink .236  rough .179  uniq 115

## 修复历史（都是实测归因，不是读代码猜的）
1. posterize 在线性 RGB 上均匀量化 → 暗部全砸进同一档 = 死黑（mean 23.3）。
   改为亮度重映射后只量化明度。
2. 锯齿主凶是 posterize 硬台阶（rough +0.123），描边次之（+0.082）→ 软量化。
3. 色度必须按明度变化比例同步放大，否则读作灰绿。
4. 档内偏移写 0.62 会被 /L 放大成 L 相关的亮度抬升（L=3 抬 .040、L=6 抬 .020），
   实测 mean 随 L 单调变化就是它 → 改成档中心 0.5。
5. 赛璐璐"不暗"的唯一主因是 inkGain 把描边关了（逐项实测：
   关顶档提亮 mean 无变化 / 关高光 -0.9 / 关饱和差异 +0.1 / 关描边差异 -39.4）。
   → 赛璐璐必须自己有暗档：黑位 0.16→0.05、gamma 0.78→1.00。

破坏对照记录见文件末 DAMAGE 注释。
算法来源：GPUImage2 ToonFilter / KuwaharaFilter（Kyprianidis et al., GPU Pro 2010）。
统计全部在页面内完成，只回传数字（整幅像素数组回传 Python 会超时）。
"""
import threading, http.server, socketserver, functools, json, sys, os

from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8985
H = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ROOT)
socketserver.TCPServer.allow_reuse_address = True
srv = socketserver.TCPServer(('127.0.0.1', PORT), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()

# uniq  唯一色数（4bit 量化，滤掉 8bit 抖动）—— 色块合并程度
# ink   描线/纯暗色块占比（亮度 < 48）
# dark  偏暗占比（亮度 < 60）—— 「整体压黑」这个病
# mean  平均亮度 —— 矢量插画应比原片更亮
# rough 相邻像素亮度差 > 14 的占比 —— 色块边缘的锯齿毛刺
STAT = r'''(r)=>{
 const c=document.querySelector('canvas'),w=c.width,h=c.height;
 const t=document.createElement('canvas');t.width=w;t.height=h;
 t.getContext('2d').drawImage(c,0,0);
 const x0=Math.floor(r[0]*w),y0=Math.floor(r[1]*h),x1=Math.floor(r[2]*w),y1=Math.floor(r[3]*h);
 const W=x1-x0,H=y1-y0;
 const d=t.getContext('2d').getImageData(x0,y0,W,H).data;
 const set=new Set(); let dark=0,ink=0,n=0,sum=0,rough=0;
 const g=(x,y)=>{const i=(y*W+x)*4;return d[i]*.299+d[i+1]*.587+d[i+2]*.114;};
 for(let y=1;y<H-1;y++)for(let x=1;x<W-1;x++){
   const i=(y*W+x)*4, l=g(x,y);
   set.add(((d[i]>>4)<<8)|((d[i+1]>>4)<<4)|(d[i+2]>>4));
   if(l<60) dark++;
   if(l<48) ink++;
   sum+=l; n++;
   if(Math.abs(l-g(x+1,y))+Math.abs(l-g(x,y+1))>14) rough++;
 }
 return {uniq:set.size, darkFrac:dark/n, ink:ink/n, mean:sum/n, rough:rough/n, n:n};
}'''

# 用参考帧 f05 实测的指尖坐标走真实 onGesture 管线，验斜带外推几何
BAND = r'''()=>{
  function hand(thumb, index, wrist){
    const lm=Array.from({length:21},()=>({x:wrist[0],y:wrist[1]}));
    lm[0]={x:wrist[0],y:wrist[1]};
    lm[4]={x:thumb[0],y:thumb[1]};
    lm[8]={x:index[0],y:index[1]};
    lm[9]={x:wrist[0],y:wrist[1]-0.12};
    for(const [tip,pip] of [[12,10],[16,14],[20,18]]){
      lm[pip]={x:wrist[0],y:wrist[1]-0.10};
      lm[tip]={x:wrist[0],y:wrist[1]-0.02};
    }
    return lm;
  }
  const A=hand([.28,.58],[.27,.30],[.24,.72]);
  const B=hand([.76,.57],[.77,.32],[.80,.72]);
  let st; for(let i=0;i<40;i++) st=window.__GESTURE_TEST__([A,B]);
  return st;
}'''

ROI_IN = [.35, .35, .65, .65]
ROI_OUT = [.005, .30, .09, .70]
ROI_FACE = [.30, .22, .70, .78]

with sync_playwright() as p:
    b = p.chromium.launch(args=['--use-gl=angle', '--use-angle=swiftshader',
                                '--enable-unsafe-swiftshader', '--disable-gpu-sandbox'])
    pg = b.new_context(viewport={'width': 1000, 'height': 700}).new_page()
    errs = []; pg.on('pageerror', lambda e: errs.append(str(e)))
    pg.goto(f'http://127.0.0.1:{PORT}/index.html', wait_until='domcontentloaded')
    pg.wait_for_function('()=>window.__SHADER_OK__!==undefined', timeout=25000)
    pg.evaluate("()=>document.querySelector('#boot').classList.add('hide')")
    shader_ok = pg.evaluate('()=>window.__SHADER_OK__')
    shader_err = pg.evaluate('()=>window.__SHADER_ERR__')

    # ---------- ① 斜带几何：手势必须外推成贯穿画面的斜带 ----------
    band = pg.evaluate(BAND)
    q = band['quad']
    xs = [pt[0] for pt in q]; ys = [pt[1] for pt in q]
    top_mid = (q[0][1] + q[1][1]) / 2
    bot_mid = (q[2][1] + q[3][1]) / 2
    band_thick = abs(bot_mid - top_mid)
    band_tilt = q[1][1] - q[0][1]

    # ---------- ② 空间闸门（合成纹理：窗外区域恒定，适合证明没污染） ----------
    pg.evaluate('()=>window.__VEC_FEED__()'); pg.wait_for_timeout(300)
    pg.evaluate('()=>window.__VEC_SET__(0)'); pg.wait_for_timeout(250)
    off_in = pg.evaluate(STAT, ROI_IN); off_out = pg.evaluate(STAT, ROI_OUT)
    pg.evaluate('()=>window.__VEC_SET__(1)'); pg.wait_for_timeout(250)
    on_in = pg.evaluate(STAT, ROI_IN); on_out = pg.evaluate(STAT, ROI_OUT)
    vst = pg.evaluate('()=>window.__VECTOR_STATE__()')
    plate_area = vst['vectorRect'][2] * vst['vectorRect'][3]

    # ---------- ③ 真人像：两种风格各测自己的语义 ----------
    pg.evaluate("""async()=>{
      const im=new Image(); im.src='shots/testface.png';
      await im.decode(); window.__FEED_IMG__=im;
    }""")
    pg.evaluate('()=>window.__VEC_FEED_IMG__()')
    # 关掉赛博背景和战衣，孤立出矢量化这一层
    CEL = {'vecMix': 1, 'vecStyle': 1, 'vecLevels': 3, 'vecRad': 5.0,
           'vecEdge': 0.22, 'cyber': 0, 'suitAmt': 0}

    def meas(**patch):
        qq = dict(CEL); qq.update(patch)
        pg.evaluate('(p)=>window.__VEC_PARAM__(p)', qq)
        pg.wait_for_timeout(170)
        return pg.evaluate(STAT, ROI_FACE)

    raw = meas(vecMix=0)
    cel = meas()                                        # 赛璐璐默认（用户看到的）
    cel_noedge = meas(vecEdge=9.0)
    cel_edgelo = meas(vecEdge=0.05)
    cel_r1 = meas(vecEdge=9.0, vecRad=1.0)              # Kuwahara 孤立
    cel_r5 = meas(vecEdge=9.0, vecRad=5.0)
    cel_l2 = meas(vecEdge=9.0, vecLevels=2)             # posterize 孤立
    cel_l16 = meas(vecEdge=9.0, vecLevels=16)
    toon = meas(vecStyle=0, vecLevels=6, vecEdge=0.14)  # toon filter 档
    meas(); pg.screenshot(path='/tmp/live_vec_cel.png')

    checks = {
        'shader_compiles': bool(shader_ok) and not shader_err,

        # --- 斜带几何（参考视频的核心形似）---
        # 必须贯穿画面：左端推到 x<0、右端推到 x>1
        'band_spans_screen': min(xs) < -0.05 and max(xs) > 1.05,
        # 厚度贴近参考帧实测 0.28H（手更张开会更厚，给宽区间但排除退化）
        'band_thickness_sane': 0.12 < band_thick < 0.55,
        # 倾斜必须跟着手的姿态，不是锁死水平
        'band_tilts_with_hands': abs(band_tilt) > 0.02,
        # 上边必须在下边之上（角点环绕顺序没搞反）
        'band_order_correct': top_mid < bot_mid,

        # --- 空间闸门 ---
        'outside_window_untouched': abs(on_out['uniq'] - off_out['uniq']) <= 2
                                    and on_out['darkFrac'] < 0.02,
        'live_mode_no_static_plate': plate_area == 0,
        'vectorize_changes_window': on_in['uniq'] < off_in['uniq'] * 0.90,

        # --- 赛璐璐（默认风格）---
        # ① 必须比原片亮（矢量插画不能死黑；旧版这里 mean 只有 23.3）
        'cel_brighter_than_raw': cel['mean'] > raw['mean'] * 1.25,
        # ② 必须保留阴影色块 —— 赛璐璐的暗部是画上去的暗面，不是没有暗部。
        #    L=2 时暗档消失（实测 dark=0），所以这条能抓住"阴影被抹平"。
        'cel_keeps_shadow': cel['darkFrac'] > 0.30,
        # ③ 色块必须合并
        'cel_colors_merged': cel['uniq'] < raw['uniq'] * 0.85,
        # ④ 色块必须比原片更平整（赛璐璐的核心观感）
        'cel_flatter_than_raw': cel['rough'] < raw['rough'] * 0.85,
        # ④b 软量化的牙齿只在 toon 档测得到 —— 赛璐璐的 qSoft 本来就只有 0.035
        #     （接近硬台阶，那正是它想要的刀切色块），把它置 0 对赛璐璐
        #     rough 只从 0.134 动到 0.129，任何合理阈值都抓不住。
        #     实测 toon 档：qSoft 0.11 -> rough 0.179，置 0 -> rough 0.199。
        'toon_soft_quantization_helps': toon['rough'] < 0.19,
        # ⑤ 赛璐璐几乎不靠描边：关掉描边后 rough 应明显更低，
        #    但 darkFrac 几乎不变（证明暗部来自色块而非描线）。
        #    这是赛璐璐与 toon 的分界，也是参考帧"没有描边"的量化表达。
        'cel_shadow_not_from_ink': abs(cel_noedge['darkFrac'] - cel['darkFrac']) < 0.03,
        # ⑥ 描边阈值仍要有牙齿（只是很淡）
        'cel_edge_has_effect': cel_edgelo['rough'] < cel_noedge['rough'] * 1.6
                               and abs(cel_edgelo['mean'] - cel_noedge['mean']) > 5,
        # ⑦ Kuwahara 有牙齿：大半径更平且色数更少
        'cel_kuwahara_flattens': cel_r5['rough'] < cel_r1['rough'] * 0.85
                                 and cel_r5['uniq'] < cel_r1['uniq'] * 0.80,
        # ⑧ posterize 有牙齿：色阶数控制色数
        'cel_levels_control_colors': cel_l2['uniq'] < cel_l16['uniq'] * 0.90,

        # --- toon filter（第二风格，按 V 可切）---
        # 它的暗部主要来自 Sobel 描线，判据用 ink
        'toon_ink_present': toon['ink'] > 0.10,
        'toon_ink_not_flooding': toon['ink'] < 0.32,
        # 两种风格必须真的不一样（否则 uVecStyle 是死开关）
        'styles_differ': abs(toon['mean'] - cel['mean']) > 15
                         and toon['rough'] > cel['rough'] * 1.15,

        'no_pageerror': not errs,
    }
    out = {
        'band': {'quad': q, 'x_range': [round(min(xs), 3), round(max(xs), 3)],
                 'thickness': round(band_thick, 3), 'tilt': round(band_tilt, 3),
                 'portal_target': band['target']},
        'synth': {'off_in': off_in, 'on_in': on_in,
                  'off_out': off_out, 'on_out': on_out, 'plate_area': plate_area},
        'face': {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv)
                     for kk, vv in v.items()}
                 for k, v in [('raw', raw), ('cel', cel), ('cel_noedge', cel_noedge),
                              ('cel_edgelo', cel_edgelo), ('cel_r1', cel_r1),
                              ('cel_r5', cel_r5), ('cel_l2', cel_l2),
                              ('cel_l16', cel_l16), ('toon', toon)]},
        'shader_err': shader_err, 'errors': errs, 'checks': checks,
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    b.close()
    assert all(checks.values()), {k: v for k, v in checks.items() if not v}
srv.shutdown()
print('live_vector_test OK')

# ---------------------------------------------------------------------------
# DAMAGE（破坏对照，每条都实测过）
#   BAND_EXTEND 改成 0（不外推，退回方框取景窗）
#       → band_spans_screen 红
#   bandFromFingers 返回 [tL,tR,bR,bL] 换成 [bL,bR,tR,tL]（上下颠倒）
#       → band_order_correct 红
#   ext() 里改成统一水平外推（忽略每条边自己的方向）
#       → band_tilts_with_hands 红
#   赛璐璐黑位改回 0.16 / gamma 改回 0.78（即不给赛璐璐独立暗档）
#       → cel_keeps_shadow 红
#   档内偏移改回 0.62
#       → cel_edge_has_effect 红（实测：L=3 时该偏移把亮度整体抬高，
#         关描边/低阈值两版的 mean 差从 14.2 缩到 4.9，跌破 >5 的判据）
#   inkGain 赛璐璐档改回 0.85（跟 toon 一样重描边）
#       → cel_shadow_not_from_ink + styles_differ 红
#   去掉软量化（qSoft=0）
#       → toon_soft_quantization_helps 红（rough 0.179 -> 0.199）
#         注意：这条在**赛璐璐档没有牙齿** —— 赛璐璐 qSoft 本来就是 0.035
#         （刀切色块正是它要的），置 0 后 rough 只从 0.134 动到 0.129。
#         如实记录，不要把 cel_flatter_than_raw 当成软量化的防线。
#   真正禁用 Kuwahara（四象限加权混合那一行换成中心采样）
#       → cel_kuwahara_flattens + vectorize_changes_window 红
#   去 Sobel（vecCol = post）
#       → toon_ink_present 红
#   uVecStyle 上传改成常量 1（两种风格塌成一个）
#       → toon_ink_not_flooding 红（实测：toon 档也走赛璐璐的黑位 0.05/gamma 1.0，
#         暗色块把 ink 推到 0.42 > 0.32 上限。styles_differ 反而仍绿，
#         因为 vecLevels/vecEdge 的差异还在撑着 mean 和 rough 的距离 ——
#         所以 styles_differ 不能当作 uVecStyle 的唯一防线。）
#   live 改回盖静态底图
#       → live_mode_no_static_plate 红
#   去掉 portal 闸门（vecAmt = uVecMix）
#       → outside_window_untouched 红
# ---------------------------------------------------------------------------
