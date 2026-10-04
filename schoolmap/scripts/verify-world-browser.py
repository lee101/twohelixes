import sys
from playwright.sync_api import sync_playwright

base=sys.argv[1] if len(sys.argv)>1 else 'http://localhost:7493'
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':900})
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    assert page.goto(base+'/schools/world').status==200
    page.wait_for_function("document.querySelector('#count').textContent.includes('102,178')",timeout=60000)
    assert page.locator('#region option').count()>70
    for key,query in [('au-nt','Acacia Hill'),('nz','Te Kura o Te Kao'),('au-vic','Parade College'),('ca','Tricentia'),('us-ca','Lincoln')]:
        page.locator('#region').select_option(key)
        page.wait_for_function("document.querySelector('#json').getAttribute('href').endsWith('/'+document.querySelector('#region').value)")
        page.locator('#search').fill(query)
        page.wait_for_timeout(1000)
        page.wait_for_function("document.querySelector('#results button')!==null")
        page.locator('#results button').first.click()
        page.locator('.leaflet-popup-content').wait_for()
        assert 'Location only' in page.locator('.leaflet-popup-content').inner_text()
        assert page.locator('#error').inner_text()==''
        print(key,'search, map and school popup passed')
    page.get_by_role('button',name='Toggle map fullscreen').click()
    page.wait_for_function('!!document.fullscreenElement')
    page.get_by_role('button',name='Toggle map fullscreen').click()
    for width,height in ((1366,768),(1280,600),(390,844),(667,375)):
        page.set_viewport_size({'width':width,'height':height});page.wait_for_timeout(300)
        assert page.evaluate('document.documentElement.scrollHeight<=innerHeight+1')
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    assert not errors,errors
    page.set_viewport_size({'width':1440,'height':900})
    page.screenshot(path='/tmp/world-schools.png')
    browser.close()
