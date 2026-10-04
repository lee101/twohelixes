import json
from pathlib import Path
from playwright.sync_api import sync_playwright
import argparse
p=argparse.ArgumentParser();p.add_argument('--base',default='http://127.0.0.1:7487');args=p.parse_args()
with sync_playwright() as p:
 b=p.chromium.launch(headless=True,args=['--enable-webgl','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
 page=b.new_page(viewport={'width':1440,'height':1000})
 errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 r=page.goto(args.base+'/schools/nsw',wait_until='networkidle');assert r.status==200
 page.wait_for_function("document.querySelector('#count').textContent.includes('3,402')")
 assert page.locator('.leaflet-tile-loaded').count()>0
 page.locator('#search').fill('Abbotsford')
 page.locator('#results button').first.click()
 assert '350 Great North Rd' in page.locator('.leaflet-popup-content').inner_text()
 assert 'Reading' in page.locator('.leaflet-popup-content').inner_text()
 page.wait_for_timeout(2500)
 page.screenshot(path='/tmp/nsw-map-desktop.png')
 page.locator('#search').fill('')
 page.locator('#measure').select_option('reading_y3')
 page.locator('#availability').select_option('scored')
 assert '3,402' not in page.locator('#count').inner_text()
 assert not page.locator('#legend').is_hidden()
 page.set_viewport_size({'width':390,'height':844})
 page.screenshot(path='/tmp/nsw-map-mobile.png',full_page=True)
 for url in ['/datasets/nsw_schools','/datasets/nsw_naplan_results','/v1/samples/nsw_schools/download.csv']:
  if url.endswith('.csv'):
   r=page.request.get(args.base+url);assert r.status==200 and 'school_name' in r.text()
  else:
   r=page.goto(args.base+url,wait_until='networkidle');assert r.status==200
 assert not errors,errors
 print(json.dumps({'base':args.base,'map':'passed','popup':'address and real domain means','mobile':'passed','errors':errors}))
 b.close()
