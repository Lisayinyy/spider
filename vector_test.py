#!/usr/bin/env python3
"""格温二维矢量条带验收。统计均在页面内完成，只回传数字。"""
import threading,http.server,socketserver,functools,json
from playwright.sync_api import sync_playwright
ROOT='.'; PORT=8920
H=functools.partial(http.server.SimpleHTTPRequestHandler,directory=ROOT)
socketserver.TCPServer.allow_reuse_address=True
srv=socketserver.TCPServer(('127.0.0.1',PORT),H)
threading.Thread(target=srv.serve_forever,daemon=True).start()
JS=r'''()=>{
 const c=document.querySelector('#vector'),x=c.getContext('2d'),w=c.width,h=c.height,d=x.getImageData(0,0,w,h).data;
 function roi(x0,y0,x1,y1){let n=0,a=0,lit=0,cyan=0,white=0,mean=0;for(let y=Math.floor(y0*h);y<Math.floor(y1*h);y++)for(let x=Math.floor(x0*w);x<Math.floor(x1*w);x++){let i=(y*w+x)*4,r=d[i],g=d[i+1],b=d[i+2],aa=d[i+3];n++;a+=aa;mean+=(r+g+b)/3;if(aa>20)lit++;if(b>140&&g>100&&b>r*1.15)cyan++;if(r>220&&g>220&&b>225)white++;}return{n,alpha:a/n,mean:mean/n,lit:lit/n,cyan:cyan/n,white:white/n};}
 return {center:roi(.35,.30,.65,.68), outside:roi(.02,.10,.12,.20), leftHand:roi(.13,.25,.22,.72), size:[w,h]};
}'''
with sync_playwright() as p:
 b=p.chromium.launch(args=['--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader','--disable-gpu-sandbox'])
 # 真机是 Retina：必须在 dpr=2 下验收，dpr=1 会掩盖非整数拉伸与抖动问题
 pg=b.new_context(viewport={'width':1100,'height':700},device_scale_factor=2).new_page();errs=[];pg.on('pageerror',lambda e:errs.append(str(e)))
 pg.goto(f'http://127.0.0.1:{PORT}/index.html',wait_until='domcontentloaded');pg.wait_for_timeout(1400)
 pg.evaluate("()=>document.querySelector('#boot').classList.add('hide')");pg.evaluate('()=>window.__VECTOR_TEST__()');pg.wait_for_timeout(250)
 state=pg.evaluate('()=>window.__VECTOR_STATE__()')
 with_holes=pg.evaluate(JS)
 pg.evaluate('()=>window.__VECTOR_REDRAW__(false)');pg.wait_for_timeout(80)
 no_holes=pg.evaluate(JS)
 moved=pg.evaluate('()=>window.__VECTOR_MOVE_WINDOW__()');pg.wait_for_timeout(80)
 out={'state':state,'with_holes':with_holes,'no_holes':no_holes,'moved':moved,'errors':errs}
 # ---- 清晰度 / 稳定性：这三项才覆盖用户实机反馈的「糊」 ----
 import random as _rnd
 _rnd.seed(7)
 # (1) backing store 必须精确等于物理像素（非整数拉伸是主观模糊的直接来源）
 dprInfo=pg.evaluate("()=>({dpr:devicePixelRatio,cw:document.querySelector('#vector').width,"
                     "ch:document.querySelector('#vector').height,iw:innerWidth,ih:innerHeight})")
 dprExact=abs(dprInfo['cw']-dprInfo['iw']*dprInfo['dpr'])<=1 and abs(dprInfo['ch']-dprInfo['ih']*dprInfo['dpr'])<=1
 # (2) 静止的人 + landmark 噪声 → 底图尺寸/位置必须完全不动
 pg.evaluate('()=>window.__VECTOR_TEST__()');pg.wait_for_timeout(120)
 jw=[];jy=[]
 for _ in range(40):
   _s=pg.evaluate('(v)=>window.__VECTOR_SET_EYES__(v)',0.080*(1+_rnd.uniform(-0.02,0.02)))
   jw.append(_s['vectorRect'][2]);jy.append(_s['vectorRect'][1])
 jitW=max(abs(jw[i]-jw[i-1]) for i in range(1,len(jw)))
 jitY=max(abs(jy[i]-jy[i-1]) for i in range(1,len(jy)))
 # 但真人靠近时必须还能跟上（防止「锁死」冒充「稳定」）
 near=pg.evaluate('(v)=>window.__VECTOR_SET_EYES__(v)',0.070)['vectorRect'][2]
 far =pg.evaluate('(v)=>window.__VECTOR_SET_EYES__(v)',0.115)['vectorRect'][2]
 # (3) mip 必须真生效。注意：dpr=2 时 drawW≈2062 已超过 1376 级，只能取原图，
 #     此时「走不走 mip」结果完全相同 —— 破坏对照实测全绿，该条件下这条断言没有牙齿。
 #     所以专门另开一个 dpr=1 的页面来测：那里 drawW≈1031，必须命中 1376 级而不是 2752。
 pg1=b.new_context(viewport={'width':1100,'height':700},device_scale_factor=1).new_page()
 pg1.goto(f'http://127.0.0.1:{PORT}/index.html',wait_until='domcontentloaded');pg1.wait_for_timeout(1400)
 pg1.evaluate("()=>document.querySelector('#boot').classList.add('hide')")
 pg1.evaluate('()=>window.__VECTOR_TEST__()');pg1.wait_for_timeout(200)
 mst=pg1.evaluate('()=>window.__VECTOR_STATE__()')
 mipW,drawW=mst['vectorMip']
 mipRatio=drawW/mipW if mipW else 0
 pg1.close()
 # (4) 手孔不能大到把插画挖穿：透明面积占窗内中段 < 1.6%
 holes=pg.evaluate('''()=>{const c=document.querySelector('#vector'),x=c.getContext('2d'),w=c.width,h=c.height;
   const d=x.getImageData(0,0,w,h).data;let n=0,cl=0;
   for(let y=Math.floor(.26*h);y<Math.floor(.70*h);y++)for(let X=Math.floor(.30*w);X<Math.floor(.70*w);X++){
     const a=d[(y*w+X)*4+3];n++;if(a<40)cl++;}
   return cl/n;}''')
 pg.evaluate('()=>window.__VECTOR_REDRAW__(false)');pg.wait_for_timeout(80)
 holesNo=pg.evaluate('''()=>{const c=document.querySelector('#vector'),x=c.getContext('2d'),w=c.width,h=c.height;
   const d=x.getImageData(0,0,w,h).data;let n=0,cl=0;
   for(let y=Math.floor(.26*h);y<Math.floor(.70*h);y++)for(let X=Math.floor(.30*w);X<Math.floor(.70*w);X++){
     const a=d[(y*w+X)*4+3];n++;if(a<40)cl++;}
   return cl/n;}''')
 holeArea=holes-holesNo
 out.update({'dprInfo':dprInfo,'jitW':jitW,'jitY':jitY,'near':near,'far':far,
             'mipW':mipW,'drawW':drawW,'mipRatio':mipRatio,'holeArea':holeArea})
 # 有牙齿的语义：底图真加载；窗内是成片；窗外透明；有手时挖出透明孔；窗口移动不拖动角色底图
 rect_delta=max(abs(a-b) for a,b in zip(state['vectorRect'],moved['vectorRect']))
 checks={
   'plate_loaded': state['plateReady'] and state['plateSize'][0]>2000,
   'strip_has_art': with_holes['center']['alpha']>245 and with_holes['center']['cyan']>.08 and with_holes['center']['white']>.18,
   'outside_clear': with_holes['outside']['alpha']<1,
   'real_hand_cutout': no_holes['leftHand']['alpha']-with_holes['leftHand']['alpha']>25,
   'character_locked_to_face': rect_delta<1,
   'no_pageerror': not errs,
   # 真机「糊」的三条防线，均配破坏对照验证过有牙齿
   'dpr_exact': dprExact,
   'plate_no_jitter': jitW<0.6 and jitY<0.6,
   'plate_still_tracks': far-near > 300,
   'mip_downscale': 0.5 <= mipRatio <= 1.05,
   'hand_hole_small': holeArea < 0.016,
 }
 out['rect_delta']=rect_delta;out['checks']=checks
 print(json.dumps(out,ensure_ascii=False))
 assert all(checks.values()), checks
 b.close()
srv.shutdown()
