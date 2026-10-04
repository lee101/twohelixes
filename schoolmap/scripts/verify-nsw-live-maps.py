from playwright.sync_api import sync_playwright
import json
with sync_playwright() as p:
 b=p.chromium.launch(args=['--enable-webgl','--use-gl=angle','--use-angle=swiftshader','--enable-unsafe-swiftshader'])
 for example,count in [('nsw-map',3402),('nsw-naplan-map',6)]:
  page=b.new_page(viewport={'width':1440,'height':1050});errors=[]
  page.on('pageerror',lambda e:errors.append(str(e)))
  page.goto('https://twohelixes.com/app?sample=nsw_schools&example='+example,wait_until='networkidle')
  page.wait_for_selector('.maplibregl-canvas',timeout=45000)
  page.wait_for_function("document.querySelector('.js-plotly-plot')._fullLayout.map._subplot.map.areTilesLoaded()",timeout=45000)
  stats=page.evaluate('''()=>{const p=document.querySelector('.js-plotly-plot');return {points:p.data.reduce((n,t)=>n+(t.lat?.length||0),0),colors:p.data.map(t=>t.marker.color),source:document.querySelector('select.control-input').selectedOptions[0].textContent,labels:p.data[0].text.slice(0,2)}}''')
  assert stats['points']==count,stats
  assert 'NSW' in stats['source'],stats
  if count==6:assert isinstance(stats['colors'][0],list) and min(stats['colors'][0])==328
  assert not errors,errors
  page.screenshot(path='/tmp/live-'+example+'-desktop.png',full_page=True)
  page.set_viewport_size({'width':390,'height':844});page.wait_for_timeout(1200)
  page.screenshot(path='/tmp/live-'+example+'-mobile.png',full_page=True)
  assert not errors,errors
  print(json.dumps({'example':example,'points':count,'tiles':'loaded','source':stats['source'],'errors':errors}),flush=True)
  page.close()
 b.close()
