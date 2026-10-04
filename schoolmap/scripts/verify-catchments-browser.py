"""Read-only public map interaction regression checks."""
import sys
from playwright.sync_api import sync_playwright

base=sys.argv[1] if len(sys.argv)>1 else 'http://localhost:7492'
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':900})
    errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    assert page.goto(base+'/schools/nsw').status==200
    page.wait_for_function("document.querySelector('#results button') !== null")
    page.locator('#search').fill('Abbotsford')
    school=page.locator('#results button').first
    school.hover()
    page.wait_for_function("document.querySelector('#catchment-status').textContent.includes('Abbotsford Public School:')")
    assert page.evaluate("Object.values(map._layers).filter(l=>l.feature&&l.options.pane==='catchmentFocus').length")>0
    school.click()
    page.wait_for_function("document.querySelector('.leaflet-popup-content')?.textContent.includes('Abbotsford Public School')")
    page.wait_for_timeout(500)
    assert 'Abbotsford Public School' in page.locator('.leaflet-popup-content').inner_text()
    page.locator('#catchment-mode').select_option('overlay')
    page.wait_for_function("document.querySelector('#catchment-status').textContent.includes('source boundary') || document.querySelector('#catchment-status').textContent.includes('outlines in view')")
    page.wait_for_function("Object.values(map._layers).some(l=>l.feature&&l.options.pane==='catchmentAreas')",timeout=60000)
    page.evaluate("map.closePopup(); map.fire('click',{latlng:L.latLng(-33.852728,151.131206)})")
    page.wait_for_function("document.querySelector('.leaflet-popup-content')?.textContent.includes('Current intake zones at this point')")
    assert 'Abbotsford' in page.locator('.leaflet-popup-content').inner_text()
    page.locator('#catchment-year').select_option('7')
    page.wait_for_timeout(300)
    page.locator('#catchment-mode').select_option('off')
    assert page.evaluate("Object.values(map._layers).filter(l=>l.feature&&['catchmentAreas','catchmentFocus'].includes(l.options.pane)).length")==0
    page.locator('#catchment-year').select_option('')
    page.locator('#catchment-mode').select_option('overlay')
    page.evaluate('map.closePopup();map.setZoom(12)')
    page.wait_for_timeout(1500)
    page.screenshot(path='/tmp/catchments-desktop.png')
    page.get_by_role('button',name='Toggle map fullscreen').click()
    page.wait_for_function('!!document.fullscreenElement')
    page.get_by_role('button',name='Toggle map fullscreen').click()
    page.set_viewport_size({'width':390,'height':844})
    page.wait_for_timeout(300)
    assert page.evaluate('document.documentElement.scrollHeight<=innerHeight+1')
    assert not errors,errors
    print(base+': school hover, click, full geometry, subtle overlay, overlap lookup, filters, fullscreen and mobile passed')
    browser.close()
