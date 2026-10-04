import json,sys
from playwright.sync_api import sync_playwright
base=sys.argv[1] if len(sys.argv)>1 else 'http://127.0.0.1:7487'
with sync_playwright() as p:
 b=p.chromium.launch(args=['--enable-webgl','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
 page=b.new_page(viewport={'width':1440,'height':1100})
 errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 r=page.goto(base+'/app?sample=nsw_schools&example=nsw-map',wait_until='networkidle');assert r.status==200
 page.wait_for_selector('.maplibregl-canvas',timeout=60000)
 page.wait_for_timeout(4000)
 stats=page.evaluate('''()=>{const p=document.querySelector('.js-plotly-plot');const m=p._fullLayout.map._subplot.map;return {traces:p.data.map(t=>({type:t.type,n:t.lat?.length})),tilesLoaded:m.areTilesLoaded(),canvas:!!m.getCanvas(),sources:Object.keys(m.getStyle().sources),center:m.getCenter()}}''')
 print(json.dumps(stats),flush=True)
 assert sum(t.get('n',0) for t in stats['traces'])==3402
 assert stats['tilesLoaded']
 assert not errors,errors
 page.screenshot(path='/tmp/nsw-agent-map-desktop.png',full_page=True)
 page.set_viewport_size({'width':390,'height':844});page.wait_for_timeout(1500)
 page.screenshot(path='/tmp/nsw-agent-map-mobile.png',full_page=True)
 assert not errors,errors
 b.close()
