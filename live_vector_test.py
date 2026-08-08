#!/usr/bin/env python3
"""实时矢量化（live 模式）验收 —— 主测试台是**真实人像**，不是合成方块。

为什么换测试台（重要）：
  上一版这个文件只在 __VEC_FEED__ 的合成渐变方块上测，9 条断言全绿，
  但同一份代码喂真人照片时 darkFrac 高达 0.77（77% 画面变暗）、mean 从
  64.4 掉到 23.3，满脸黑斑，完全不可看。
  归因：合成渐变方块**没有真实结构边界**，Sobel 在它上面几乎不产生描线
  （实测阈值从 0.02 扫到 0.90，darkFrac 恒为 0.017，纯永真），
  所以「描边有牙齿」这条在合成图上根本测不出来。
  → 结论：合成纹理只能证明"算法在跑"，证明不了"真人看起来像矢量插画"。
    真人像断言（下面 REAL 段）才是这个效果的验收。

真人像基线（ROI [.30,.22,.70,.78]，shots/testface.png，默认参数
L=6 R=5.0 E=0.14）：
  raw(vecMix=0)   mean 64.2  ink 0.429  rough 0.183  uniq 139
  default         mean 71.6  ink 0.234  rough 0.179  uniq 108
  noedge(E=9)     mean 128.4 ink 0.000  rough 0.091  uniq 131
  edge_lo(E=.03)  mean 60.7  ink 0.329  rough 0.151  uniq  66
  noedge R=1      rough 0.132  uniq 245     noedge R=5   rough 0.091  uniq 132
  noedge L=2      uniq 118                  noedge L=24  uniq 148

修复前后对照（就是用户反馈的「糊 / 丑」）：
  旧（线性 RGB 均匀 posterize + 过饱和外推 + E=0.11 死黑描边）
      mean 23.3  dark 0.853   ← 死黑一坨
  新（亮度重映射 + 软量化 + 色度按明度同步放大 + 淡描边）
      mean 71.6  dark 0.242   ← 明亮平色块

破坏对照记录（每条断言都实测过牙齿，见文件末 DAMAGE 注释）

算法来源：GPUImage2 ToonFilter / KuwaharaFilter
（Kyprianidis, Kang, Doellner, "Anisotropic Kuwahara Filtering on the GPU",
 GPU Pro p.247, 2010）。三段式：Kuwahara 保边平滑 → posterize 色阶量化 → Sobel 描边。

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

# 对给定归一化 ROI 统计：
#   uniq     唯一色数（4bit 量化，滤掉 8bit 抖动）—— 衡量 posterize 的色块合并
#   ink      描线占比（亮度 < 48）—— 只算真正的暗描线
#   dark     偏暗占比（亮度 < 60）—— 衡量「整体压黑」这个病
#   mean     平均亮度 —— 矢量插画应比原片更亮，不能更暗
#   rough    相邻像素亮度差 > 14 的占比 —— 衡量色块边缘的锯齿毛刺
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
 let lv=0,lc=0;
 for(let y=1;y<H-1;y+=2)for(let x=1;x<W-1;x+=2){
   let m=0,m2=0;
   for(let j=-1;j<=1;j++)for(let i2=-1;i2<=1;i2++){const v=g(x+i2,y+j);m+=v;m2+=v*v;}
   m/=9;m2/=9;lv+=Math.max(0,m2-m*m);lc++;
 }
 return {uniq:set.size, darkFrac:dark/n, ink:ink/n, mean:sum/n,
         rough:rough/n, localVar:lv/lc, n:n};
}'''

# 合成纹理（__VEC_FEED__）的窗 quad 是 [0.12,0.16]..[0.88,0.86]
ROI_IN = [.35, .35, .65, .65]
ROI_OUT = [.005, .30, .09, .70]
# 真人像（__VEC_FEED_IMG__）的窗几乎铺满，ROI 取脸+上身
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

    # ---------- 第一段：合成纹理，验「空间闸门」和「链路在跑」 ----------
    # 合成纹理的唯一强项是它窗外区域是恒定的，适合证明窗外没被污染。
    pg.evaluate('()=>window.__VEC_FEED__()'); pg.wait_for_timeout(300)
    pg.evaluate('()=>window.__VEC_SET__(0)'); pg.wait_for_timeout(250)
    off_in = pg.evaluate(STAT, ROI_IN); off_out = pg.evaluate(STAT, ROI_OUT)
    pg.evaluate('()=>window.__VEC_SET__(1)'); pg.wait_for_timeout(250)
    on_in = pg.evaluate(STAT, ROI_IN); on_out = pg.evaluate(STAT, ROI_OUT)

    # live 模式下 2D 静态底图必须让开窗内（否则实时矢量化被死图完全盖住 ——
    # 这正是用户反馈「和参考视频不一样」的根因）
    vst = pg.evaluate('()=>window.__VECTOR_STATE__()')
    plate_area = vst['vectorRect'][2] * vst['vectorRect'][3]

    # ---------- 第二段（主）：真人像，验「看起来像矢量插画」 ----------
    pg.evaluate("""async()=>{
      const im=new Image(); im.src='shots/testface.png';
      await im.decode(); window.__FEED_IMG__=im;
    }""")
    pg.evaluate('()=>window.__VEC_FEED_IMG__()')
    # 关掉赛博背景和战衣，孤立出矢量化这一层（否则城市贴图会盖住人）
    BASE = {'vecMix': 1, 'vecLevels': 6, 'vecEdge': 0.14, 'vecRad': 5.0,
            'cyber': 0, 'suitAmt': 0}

    def meas(**patch):
        q = dict(BASE); q.update(patch)
        pg.evaluate('(p)=>window.__VEC_PARAM__(p)', q)
        pg.wait_for_timeout(160)
        return pg.evaluate(STAT, ROI_FACE)

    f_raw = meas(vecMix=0)          # 原始照片
    f_def = meas()                  # 默认参数（就是用户会看到的）
    f_noedge = meas(vecEdge=9.0)    # 关描边
    f_edgelo = meas(vecEdge=0.03)   # 描边阈值极低
    f_r1 = meas(vecEdge=9.0, vecRad=1.0)    # 关描边下比半径（Kuwahara 孤立）
    f_r5 = meas(vecEdge=9.0, vecRad=5.0)
    f_l2 = meas(vecEdge=9.0, vecLevels=2)   # 关描边下比色阶（posterize 孤立）
    f_l24 = meas(vecEdge=9.0, vecLevels=24)
    meas()                          # 回到默认再截图
    pg.screenshot(path='/tmp/live_vec_face.png')

    checks = {
        'shader_compiles': bool(shader_ok) and not shader_err,

        # --- 空间闸门（合成纹理）---
        # 只作用在窗内，窗外保持真人不动
        'outside_window_untouched': abs(on_out['uniq'] - off_out['uniq']) <= 2
                                    and on_out['darkFrac'] < 0.02,
        # live 模式下静态底图不得占据窗内
        'live_mode_no_static_plate': plate_area == 0,
        # 链路整体生效：窗内色数明显减少
        'vectorize_changes_window': on_in['uniq'] < off_in['uniq'] * 0.90,

        # --- 真人像：这是修复的验收标准 ---
        # ① 不得整体压黑。旧版这里是 mean 23.3 / dark 0.853，是「丑」的主因。
        #    矢量插画必须比原片更亮（抬黑位 + 提中调），且暗占比明显低于原片。
        'face_not_darkened': f_def['mean'] > f_raw['mean'] * 1.05
                             and f_def['darkFrac'] < f_raw['darkFrac'] * 0.70,
        # ② 色块必须真的合并（矢量感）。实测 139 -> 108。
        'face_colors_merged': f_def['uniq'] < f_raw['uniq'] * 0.85,
        # ③ 色块边缘不得比原片更毛刺。软量化之前这里是 0.287 vs 原片 0.183。
        'face_not_rougher': f_def['rough'] <= f_raw['rough'] * 1.02,
        # ④ 描边必须存在，但不能泛滥成满脸黑斑。
        #    旧版 darkFrac 0.769 就是泛滥；关描边时 ink 必须归零证明是描边画的。
        'face_ink_present': f_def['ink'] > 0.10,
        'face_ink_not_flooding': f_def['ink'] < 0.32,
        'face_ink_is_from_sobel': f_noedge['ink'] < 0.01,
        # ⑤ 描边阈值有牙齿（真人像上才测得出，合成方块上恒 0.017 是永真）
        'face_edge_threshold_works': f_edgelo['ink'] > f_def['ink'] * 1.25,
        # ⑥ Kuwahara 有牙齿：关掉描边后，大半径必须更平且色数更少
        'face_kuwahara_flattens': f_r5['rough'] < f_r1['rough'] * 0.85
                                  and f_r5['uniq'] < f_r1['uniq'] * 0.80,
        # ⑦ posterize 有牙齿：关掉描边后色阶数必须控制色数
        'face_levels_control_colors': f_l2['uniq'] < f_l24['uniq'] * 0.90,

        'no_pageerror': not errs,
    }
    out = {
        'synth': {'off_in': off_in, 'on_in': on_in,
                  'off_out': off_out, 'on_out': on_out,
                  'plate_area': plate_area},
        'face': {k: {kk: (round(vv, 4) if isinstance(vv, float) else vv)
                     for kk, vv in v.items()}
                 for k, v in [('raw', f_raw), ('default', f_def),
                              ('noedge', f_noedge), ('edge_lo', f_edgelo),
                              ('rad1', f_r1), ('rad5', f_r5),
                              ('lv2', f_l2), ('lv24', f_l24)]},
        'shader_err': shader_err,
        'errors': errs,
        'checks': checks,
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    b.close()
    assert all(checks.values()), {k: v for k, v in checks.items() if not v}
srv.shutdown()
print('live_vector_test OK')

# ---------------------------------------------------------------------------
# DAMAGE（破坏对照，每条都实测过；命令见 git 历史里的 /tmp/damage_live.py）
#   把 posterize 换回线性均匀量化 floor(flat3*L+.5)/L
#       → face_not_darkened 红（mean 掉到 23.3 / dark 0.853）
#   去掉亮度重映射（lo=0, hi=1, gamma=1）
#       → face_not_darkened 红
#   去掉软量化（lq = (fi+0.62)/L 硬台阶）
#       → face_not_rougher 红（rough 0.287 > 原片 0.183）
#   描边恢复成 lineC=post*0.14 + smoothstep(E, E*2.1) 且 E=0.11
#       → face_ink_not_flooding 红
#   去 Sobel（vecCol = post）
#       → face_ink_present + face_edge_threshold_works 红
#   真正禁用 Kuwahara（四象限全换成中心采样，注意必须连
#     加权混合那一行一起换 —— 只改 mA 没用）
#       → face_kuwahara_flattens + vectorize_changes_window 红
#   live 改回盖静态底图
#       → live_mode_no_static_plate 红
#   去掉 portal 闸门（vecAmt = uVecMix）
#       → outside_window_untouched 红
# ---------------------------------------------------------------------------
