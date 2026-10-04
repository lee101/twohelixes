"""Real NSW map colour controls, marker pixels and full-directory coverage.
Run: .venv-13/bin/python bench/visualbench_nsw.py --base http://127.0.0.1:7487
"""
import argparse
import io
import json
from pathlib import Path
from PIL import Image
from playwright.sync_api import sync_playwright
from visualbench import VIEWPORTS

parser=argparse.ArgumentParser()
parser.add_argument('--base',default='http://127.0.0.1:7487')
parser.add_argument('--out',type=Path,default=Path('visualbench/nsw-colour'))
args=parser.parse_args();args.out.mkdir(parents=True,exist_ok=True)
report=[]
with sync_playwright() as p:
 browser=p.chromium.launch(args=['--enable-webgl','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
 for width in ('desktop','mobile'):
  viewport=VIEWPORTS[width]
  page=browser.new_page(viewport={k:viewport[k] for k in ('width','height')})
  errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
  page.goto(args.base+'/datasets/nsw_schools',wait_until='networkidle')
  page.locator('iframe[title="NSW school map"]').scroll_into_view_if_needed()
  frame=next(f for f in page.frames if '/schools/nsw' in f.url)
  frame.wait_for_function('markers.size===schools.length')
  assert frame.locator('#measure').input_value()=='reading_y3'
  for measure in ('reading_y3','numeracy_y3','sector'):
   frame.locator('#measure').select_option(measure)
   frame.locator('#map').scroll_into_view_if_needed()
   frame.wait_for_timeout(500)
   stats=frame.evaluate('''()=>({measure:el('measure').value,total:markers.size,expected:schools.length,
     scored:schools.filter(s=>numeric(s[el('measure').value])).length,
     coloured:[...markers.values()].filter(m=>m.options.fillColor!=='#85929b').length,
     colors:[...new Set([...markers.values()].map(m=>m.options.fillColor))],
     radii:[...new Set([...markers.values()].map(m=>m.options.radius))],last:[...markers.values()].at(-1).options.fillColor,status:el('colour-status').textContent})''')
   assert stats['total']==stats['expected'],stats
   assert stats['coloured']==(stats['total'] if measure=='sector' else stats['scored']),stats
   assert max(stats['radii'])-min(stats['radii'])<=1,stats
   assert len(stats['colors'])>=3 and stats['last']!='#85929b',stats
   # Check rendered pixels, not just the configured marker colours.
   png=frame.locator('#map').screenshot()
   rgb=Image.open(io.BytesIO(png)).convert('RGB')
   pixels=rgb.getcolors(rgb.width*rgb.height)
   visible=[]
   for colour in stats['colors']:
    if colour=='#85929b':continue
    target=tuple(bytes.fromhex(colour[1:])) if colour.startswith('#') else tuple(map(int,colour[4:-1].split(',')))
    count=sum(n for n,c in pixels if max(abs(a-b) for a,b in zip(c,target))<=2)
    if count>=3:visible.append(colour)
   assert len(visible)>=2,{'marker_pixels_missing':stats,'visible':visible}
   stats['visible_colours']=visible
   (args.out/f'dataset-{width}-{measure}.png').write_bytes(png)
   page.locator('iframe[title="NSW school map"]').screenshot(path=str(args.out/f'embedded-{width}-{measure}.png'))
   report.append({'view':width,'surface':'dataset',**stats})
  page.goto(args.base+'/app?sample=nsw_schools&example=nsw-naplan-map',wait_until='networkidle')
  page.wait_for_selector('.maplibregl-canvas')
  page.locator('.chart-controls-panel').evaluate('(el)=>el.open=true')
  for colour in ('sector','reading_y3','numeracy_y3'):
   with page.expect_response(lambda r:'/v1/schools/nsw/map-config' in r.url) as response:
    page.locator('[data-control="colour"]').select_option(colour)
   assert response.value.status==200,response.value.text()
   page.wait_for_timeout(800)
   stats=page.evaluate('''()=>{const p=document.querySelector('.js-plotly-plot');return {total:p.data.reduce((n,t)=>n+t.lat.length,0),names:p.data.map(t=>t.name),colors:p.data.map(t=>t.marker.color),sizes:p.data.map(t=>t.marker.size)}}''')
   assert stats['total']==3402,stats
   if colour=='sector':assert len(set(stats['colors']))==3,stats
   else:
    assert stats['names'][0]=='No selected score',stats
    assert 0<stats['sizes'][-1]-stats['sizes'][0]<=2,stats
    assert isinstance(stats['colors'][-1],list) and len(set(stats['colors'][-1]))>1,stats
   page.locator('.js-plotly-plot').screenshot(path=str(args.out/f'agent-{width}-{colour}.png'))
   report.append({'view':width,'surface':'agent','measure':colour,**stats})
  assert not errors,errors
  page.close()
 browser.close()
(args.out/'report.json').write_text(json.dumps(report,indent=2))
print(json.dumps({'passed':len(report),'screenshots':str(args.out),'checks':'actual marker pixels, control changes, 3402 locations, neutral markers below scores'}))
