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
 pg=b.new_context(viewport={'width':1100,'height':700}).new_page();errs=[];pg.on('pageerror',lambda e:errs.append(str(e)))
 pg.goto(f'http://127.0.0.1:{PORT}/index.html',wait_until='domcontentloaded');pg.wait_for_timeout(1400)
 pg.evaluate("()=>document.querySelector('#boot').classList.add('hide')");pg.evaluate('()=>window.__VECTOR_TEST__()');pg.wait_for_timeout(250)
 state=pg.evaluate('()=>window.__VECTOR_STATE__()')
 with_holes=pg.evaluate(JS)
 pg.evaluate('()=>window.__VECTOR_REDRAW__(false)');pg.wait_for_timeout(80)
 no_holes=pg.evaluate(JS)
 moved=pg.evaluate('()=>window.__VECTOR_MOVE_WINDOW__()');pg.wait_for_timeout(80)
 out={'state':state,'with_holes':with_holes,'no_holes':no_holes,'moved':moved,'errors':errs}
 # 有牙齿的语义：底图真加载；窗内是成片；窗外透明；有手时挖出透明孔；窗口移动不拖动角色底图
 rect_delta=max(abs(a-b) for a,b in zip(state['vectorRect'],moved['vectorRect']))
 checks={
   'plate_loaded': state['plateReady'] and state['plateSize'][0]>2000,
   'strip_has_art': with_holes['center']['alpha']>245 and with_holes['center']['cyan']>.08 and with_holes['center']['white']>.18,
   'outside_clear': with_holes['outside']['alpha']<1,
   'real_hand_cutout': no_holes['leftHand']['alpha']-with_holes['leftHand']['alpha']>25,
   'character_locked_to_face': rect_delta<1,
   'no_pageerror': not errs,
 }
 out['rect_delta']=rect_delta;out['checks']=checks
 print(json.dumps(out,ensure_ascii=False))
 assert all(checks.values()), checks
 b.close()
srv.shutdown()
