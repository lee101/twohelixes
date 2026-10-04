import json
import unittest
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from crawl_nsw import browser_get, retry_delay
import crawl_nsw
from playwright.async_api import async_playwright

class RetryTests(unittest.TestCase):
    def test_seconds_dates_and_invalid_headers(self):
        at=datetime(2026,9,15,tzinfo=timezone.utc)
        self.assertEqual(retry_delay('7200',at),7200)
        self.assertEqual(retry_delay('Tue, 15 Sep 2026 01:00:00 GMT',at),3600)
        self.assertEqual(retry_delay('not a delay',at),3600)
        self.assertEqual(retry_delay(None,at),3600)

class BrowserTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_page_session_and_rate_limit_response(self):
        async with async_playwright() as p:
            browser=await p.chromium.launch()
            context=await browser.new_context()
            page=await context.new_page()
            await page.route('http://crawl.test/**',lambda route:route.fulfill(status=200,body='<html></html>',content_type='text/html'))
            await page.goto('http://crawl.test/')
            await context.add_cookies([{'name':'session','value':'test-session','url':'http://crawl.test/'}])
            async def result(route):
                self.assertIn('session=test-session',route.request.headers.get('cookie',''))
                await route.fulfill(status=200,json={'response':{'SchoolInfo':{'SML_ID':44003}}})
            await page.route('http://crawl.test/result',result)
            data=await browser_get(page,'http://crawl.test/result')
            self.assertEqual(data['status'],200)
            self.assertEqual(data['data']['response']['SchoolInfo']['SML_ID'],44003)
            await page.route('http://crawl.test/limited',lambda route:route.fulfill(status=429,body='Slow down',headers={'Retry-After':'3600','Age':'123','Set-Cookie':'private=x'}))
            limited=await browser_get(page,'http://crawl.test/limited')
            self.assertEqual(limited['status'],429)
            self.assertEqual(limited['headers']['retry-after'],'3600')
            self.assertNotIn('set-cookie',limited['headers'])
            self.assertIsNone(limited['data'])
            await browser.close()

class CrawlCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_cooldown_retries_same_session_without_navigation(self):
        elapsed = 0.0
        started = datetime(2026,9,15,tzinfo=timezone.utc)
        request_times = []
        result = {'response':{'SchoolInfo':{'SML_ID':44003},'Numbers':[]}}

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return (started+timedelta(seconds=elapsed)).astimezone(tz)

        async def sleep(seconds):
            nonlocal elapsed
            self.assertGreaterEqual(seconds,0)
            elapsed += seconds

        with TemporaryDirectory() as temporary:
            root = Path(temporary)/'raw'
            (root/'search').mkdir(parents=True)
            (root/'search/0001.json').write_text(json.dumps({
                'url':crawl_nsw.SEARCH,
                'data':{'response':{'docs':[{'State':'NSW','SML_ID':44003}], 'numFound':1}}
            }))
            result_path = root/'results/44003.json'
            result_url = crawl_nsw.RESULTS+'?sml_id=44003&year=2025'
            cooldowns = []

            async def evaluate(script, url):
                self.assertEqual(url,result_url)
                request_times.append(elapsed)
                self.assertFalse(result_path.exists())
                if len(request_times) == 1:
                    return {'status':429,'headers':{'retry-after':'3600'},'data':None}
                cooldown = json.loads((root/'rate-limit.json').read_text())
                cooldowns.append(cooldown)
                self.assertEqual(cooldown['status'],'waiting')
                self.assertEqual(cooldown['retry_after_seconds'],3600)
                self.assertEqual(cooldown['last_url'],result_url)
                self.assertEqual(cooldown['response_headers'],{'retry-after':'3600'})
                self.assertEqual(cooldown['transport'],'browser_page')
                self.assertEqual(datetime.fromisoformat(cooldown['resume_at']),started+timedelta(seconds=request_times[0]+3600))
                self.assertGreaterEqual(elapsed-request_times[0],3600)
                status = json.loads((root/'crawl-status.json').read_text())
                self.assertEqual(status['status'],'waiting_for_retry')
                self.assertFalse(status['complete'])
                self.assertEqual(status.get('successful',0),0)
                return {'status':200,'headers':{},'data':result}

            async def storage_state(*, path):
                Path(path).write_text('{}')

            page = SimpleNamespace(
                goto=AsyncMock(return_value=SimpleNamespace(status=200)),
                wait_for_timeout=AsyncMock(),
                locator=MagicMock(return_value=SimpleNamespace(is_visible=AsyncMock(return_value=False))),
                evaluate=AsyncMock(side_effect=evaluate))
            context = SimpleNamespace(new_page=AsyncMock(return_value=page),storage_state=AsyncMock(side_effect=storage_state))
            browser = SimpleNamespace(new_context=AsyncMock(return_value=context),close=AsyncMock())
            playwright = MagicMock()
            playwright.__aenter__.return_value = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)))
            args = SimpleNamespace(output=root,year=2025,workers=1,interval=21,limit=None,refresh=False,headed=False,snapshot_command=None)
            loop = SimpleNamespace(time=lambda:elapsed)
            with patch.object(crawl_nsw,'async_playwright',return_value=playwright), patch.object(crawl_nsw,'datetime',Clock), patch.object(crawl_nsw.asyncio,'get_running_loop',return_value=loop), patch.object(crawl_nsw.asyncio,'sleep',side_effect=sleep):
                exit_code = await crawl_nsw.crawl(args)

            self.assertEqual(exit_code,0)
            page.goto.assert_awaited_once_with(crawl_nsw.BASE+'/school/44003/naplan/results',wait_until='networkidle',timeout=60000)
            browser.new_context.assert_awaited_once_with(storage_state=None)
            context.new_page.assert_awaited_once_with()
            self.assertEqual(page.evaluate.await_count,2)
            self.assertEqual(len(cooldowns),1)
            self.assertGreaterEqual(request_times[1]-request_times[0],3600)
            cached = json.loads(result_path.read_text())
            self.assertEqual(cached['url'],result_url)
            self.assertEqual(cached['data'],result)
            status = json.loads((root/'crawl-status.json').read_text())
            self.assertTrue(status['complete'])
            self.assertEqual(status['status'],'complete')
            self.assertEqual(status['attempted'],1)
            self.assertEqual(status['successful'],1)
            self.assertEqual(status['errors'],{})
            browser.close.assert_awaited_once_with()

            with patch.object(crawl_nsw,'async_playwright',return_value=playwright), patch.object(crawl_nsw,'datetime',Clock), patch.object(crawl_nsw.asyncio,'get_running_loop',return_value=loop), patch.object(crawl_nsw.asyncio,'sleep',side_effect=sleep), patch.object(crawl_nsw,'browser_get',wraps=crawl_nsw.browser_get) as resumed_get:
                resumed_exit_code = await crawl_nsw.crawl(args)

            self.assertEqual(resumed_exit_code,0)
            resumed_get.assert_not_awaited()
            self.assertEqual(page.evaluate.await_count,2)
            self.assertEqual(page.goto.await_count,2)
            page.goto.assert_awaited_with(crawl_nsw.BASE+'/school/44003/naplan/results',wait_until='networkidle',timeout=60000)
            browser.new_context.assert_awaited_with(storage_state=str(root/'browser-state.json'))
            self.assertEqual(json.loads(result_path.read_text()),cached)
            resumed_status = json.loads((root/'crawl-status.json').read_text())
            self.assertTrue(resumed_status['complete'])
            self.assertEqual(resumed_status['status'],'complete')
            self.assertEqual(resumed_status['attempted'],1)
            self.assertEqual(resumed_status['successful'],1)
            self.assertEqual(resumed_status['errors'],{})
