import json
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
 b=p.chromium.launch(args=['--enable-webgl','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
 for width in (1440,390):
  page=b.new_page(viewport={'width':width,'height':950})
  page.goto('http://127.0.0.1:7487/app?sample=nsw_schools',wait_until='networkidle')
  payload=json.load(open('/vfast/data/code/twohelixes/interp/twohelixes/datasets/fixtures/nsw_agent_naplan_map.json'))
  page.evaluate('(p)=>window.__thShowResult(p)',payload)
  page.wait_for_function("document.querySelector('.js-plotly-plot')._fullLayout.map._subplot.map.areTilesLoaded()")
  stats=page.evaluate('''()=>{const p=document.querySelector('.js-plotly-plot'),m=p._fullLayout.map._subplot.map,c=m.getCanvas();const points=p.data.flatMap(t=>t.lat.map((lat,i)=>m.project([t.lon[i],lat])));return {w:c.clientWidth,h:c.clientHeight,points:points.map(p=>[p.x,p.y])}}''')
  assert all(0<=x<=stats['w'] and 0<=y<=stats['h'] for x,y in stats['points']),stats
  print(len(stats['points']),'points visible at width',width,flush=True)
  page.close()
 b.close()
