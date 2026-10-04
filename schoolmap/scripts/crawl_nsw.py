#!/usr/bin/env python3
"""Resumable My School NSW crawl using the public browser's JSON responses.

Run with the Twohelixes Playwright environment. Raw responses stay on disk;
only successful, schema-checked responses count as collected.
"""
import argparse
import asyncio
import json
import math
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from playwright.async_api import async_playwright

BASE = 'https://myschool.edu.au'
SEARCH = BASE + '/content/acara-myschool/au/en/school-search/jcr:content/root/container/school_search_result.json?FormPosted=True&SchoolSearchQuery=&SchoolSector=&SchoolType=&State=NSW'
RESULTS = BASE + '/content/acara-myschool/au/en/school/naplan/results/jcr:content/root/container/globalconfig_1719137697.result_naplan.json'


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False))
    temp.replace(path)


def now():
    return datetime.now(timezone.utc).isoformat()


def retry_delay(value, at=None):
    """Retry-After can be seconds or an HTTP date; never shorten the source pause."""
    at = at or datetime.now(timezone.utc)
    try:
        return max(60, int(value))
    except (ValueError, TypeError):
        try:
            return max(60, math.ceil((parsedate_to_datetime(value)-at).total_seconds()))
        except (ValueError, TypeError, OverflowError):
            return 3600


class SourceCooldown(RuntimeError):
    pass


async def browser_get(page, url):
    # Use Chromium's network stack and the page's normal session, rather than
    # Playwright's separate APIRequestContext. Do not fabricate browser tokens.
    return await page.evaluate("""async url => {
        const r = await fetch(url, {credentials: 'same-origin', signal: AbortSignal.timeout(45000)});
        const allowed = ['retry-after', 'date', 'age', 'cache-control', 'cf-cache-status', 'server'];
        return {status:r.status, headers:Object.fromEntries(allowed.map(k=>[k,r.headers.get(k)]).filter(([k,v])=>v!==null)),
                data:r.ok ? await r.json() : null};
    }""", url)


async def crawl(args):
    root = args.output
    root.mkdir(parents=True, exist_ok=True)
    errors = {}
    rate_path = root/'rate-limit.json'
    async def report_wait(resume):
        status_path = root/'crawl-status.json'
        status = json.loads(status_path.read_text()) if status_path.exists() else {}
        status.update(updated_at=now(), complete=False, status='waiting_for_retry', resume_at=resume.isoformat(), transport='browser_page', score_coverage_target_pct=80)
        save(status_path, status)
        if args.snapshot_command:
            proc = await asyncio.create_subprocess_exec(str(args.snapshot_command))
            if await proc.wait():
                print('Snapshot publication failed; crawl remains resumable', flush=True)

    if rate_path.exists():
        resume = datetime.fromisoformat(json.loads(rate_path.read_text())['resume_at'])
        if datetime.now(timezone.utc) < resume:
            print(f'Honouring source Retry-After; next attempt at {resume.isoformat()}', flush=True)
            await report_wait(resume)
        while datetime.now(timezone.utc) < resume:
            remaining = (resume-datetime.now(timezone.utc)).total_seconds()
            if remaining > 0:
                await asyncio.sleep(min(30, remaining))
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headed)
        state = root / 'browser-state.json'
        context = await browser.new_context(storage_state=str(state) if state.exists() else None)
        page = await context.new_page()
        async def refresh_session():
            navigation = await page.goto(BASE + '/school/44003/naplan/results', wait_until='networkidle', timeout=60000)
            if navigation and navigation.status == 429:
                from datetime import timedelta
                delay = retry_delay(navigation.headers.get('retry-after'))
                resume = datetime.now(timezone.utc)+timedelta(seconds=delay)
                save(rate_path, {'status':'waiting', 'retry_after_seconds':delay, 'resume_at':resume.isoformat(), 'transport':'browser_navigation'})
                await report_wait(resume)
                raise SourceCooldown('Browser navigation rate limited; pause saved for the next service run')
            await page.wait_for_timeout(3000)
            label = page.locator('.tou-checkbox-inline')
            if await label.is_visible():
                await label.click()
                await page.get_by_role('button', name='Accept', exact=True).click()
            await context.storage_state(path=str(state))
            state.chmod(0o600)
        await refresh_session()
        semaphore = asyncio.Semaphore(args.workers)
        rate_lock = asyncio.Lock()
        last_request = 0.0
        consecutive_limits = 0
        cooldown_until = 0.0
        rate_path = root/'rate-limit.json'
        if rate_path.exists():
            resume = datetime.fromisoformat(json.loads(rate_path.read_text())['resume_at'])
            wait = max(0, (resume - datetime.now(timezone.utc)).total_seconds())
            cooldown_until = asyncio.get_running_loop().time() + wait

        async def fetch(url, path, validate):
            nonlocal last_request, cooldown_until, consecutive_limits
            if path.exists() and not args.refresh:
                cached = json.loads(path.read_text())
                if cached.get('url') == url:
                    validate(cached['data'])
                    return cached['data']
            async with semaphore:
                attempt = 0
                while True:
                    try:
                        async with rate_lock:
                            loop = asyncio.get_running_loop()
                            while loop.time() < cooldown_until:
                                await asyncio.sleep(min(30, cooldown_until - loop.time()))
                            await asyncio.sleep(max(0, args.interval - (loop.time() - last_request)))
                            last_request = loop.time()
                        response = await browser_get(page, url)
                        if response['status'] == 429:
                            from datetime import timedelta
                            delay = retry_delay(response['headers'].get('retry-after'))
                            consecutive_limits += 1
                            cooldown_until = max(cooldown_until, asyncio.get_running_loop().time() + delay)
                            save(root/'rate-limit.json', {'status': 'waiting', 'retry_after_seconds': delay,
                                'resume_at': (datetime.now(timezone.utc)+timedelta(seconds=delay)).isoformat(),
                                'last_url': url, 'response_headers': response['headers'], 'consecutive_limits': consecutive_limits,
                                'transport': 'browser_page'})
                            await report_wait(datetime.now(timezone.utc)+timedelta(seconds=delay))
                            print(f'{now()} Rate limited; pausing ALL workers for {delay} seconds', flush=True)
                            continue
                        if response['status'] in (401, 403):
                            raise RuntimeError(f"access/rate limit HTTP {response['status']}")
                        if not 200 <= response['status'] < 300:
                            raise ValueError(f"HTTP {response['status']}")
                        data = response['data']
                        consecutive_limits = 0
                        validate(data)
                        save(path, {'url': url, 'retrieved_at': now(), 'data': data})
                        return data
                    except SourceCooldown:
                        raise
                    except Exception as e:
                        attempt += 1
                        if attempt >= 4 or 'access/rate limit' in str(e):
                            raise
                        await asyncio.sleep(2 ** attempt)

        def check_search(data):
            result = data['response']
            assert isinstance(result['docs'], list) and isinstance(result['numFound'], int)
            assert all(d['State'] == 'NSW' and d.get('SML_ID') for d in result['docs'])

        first = await fetch(SEARCH, root/'search/0001.json', check_search)
        total = first['response']['numFound']
        size = len(first['response']['docs'])
        if not size:
            raise ValueError('Search returned no schools')
        async def search_page(number):
            data = await fetch(SEARCH+f'&pageNumber={number}', root/f'search/{number:04}.json', check_search)
            if data['response']['start'] != (number - 1)*size or data['response']['numFound'] != total:
                raise ValueError('Search pagination changed; rerun with --refresh')
            return data['response']['docs']
        pages = await asyncio.gather(*(search_page(n) for n in range(2, math.ceil(total/size)+1)))
        records = first['response']['docs'] + [d for page_rows in pages for d in page_rows]
        if len(records) != total or len({r['SML_ID'] for r in records}) != total:
            raise ValueError('Directory incomplete or duplicated')
        save(root/'directory.json', {'retrieved_at': now(), 'total': total, 'schools': records, 'source': SEARCH})
        print(f'Discovered {total} NSW schools/campuses', flush=True)
        result_schools = [r for r in records if str(r.get('HeadCampus_ID') or r['SML_ID']) == str(r['SML_ID'])]
        selected = result_schools[:args.limit] if args.limit else result_schools
        completed = 0

        async def school(row):
            nonlocal completed
            sid = str(row['SML_ID'])
            def validate(data):
                r = data['response']
                # Subcampus pages can legitimately carry their head school's profile.
                profile = r.get('SchoolInfo') or r.get('Profile')
                if not profile or str(profile.get('SML_ID')) not in {sid, str(row.get('HeadCampus_ID'))}:
                    raise ValueError('School identity mismatch or empty response')
                if not isinstance(r.get('Numbers', []), list):
                    raise ValueError('Invalid NAPLAN result schema')
            try:
                await fetch(RESULTS+f'?sml_id={sid}&year={args.year}', root/f'results/{sid}.json', validate)
            except SourceCooldown:
                raise
            except Exception as e:
                errors[sid] = str(e)
            completed += 1
            status = {'updated_at': now(), 'assessment_year': args.year, 'discovered': total,
                      'result_school_count': len(result_schools),
                      'attempted': completed, 'selected': len(selected), 'successful': completed-len(errors),
                      'errors': errors, 'complete': completed == len(result_schools) and not errors,
                      'status': 'complete' if completed == len(result_schools) and not errors else 'collecting',
                      'transport': 'browser_page', 'score_coverage_target_pct': 80}
            save(root/'crawl-status.json', status)
            print(f"{now()} {completed}/{len(selected)} attempted; {len(errors)} errors", flush=True)
            if args.snapshot_command and (completed % 25 == 0 or completed == len(selected)):
                proc = await asyncio.create_subprocess_exec(str(args.snapshot_command))
                if await proc.wait():
                    print('Snapshot publication failed; crawl remains resumable', flush=True)

        await asyncio.gather(*(school(row) for row in selected))
        await browser.close()
    return 1 if errors else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1]/'data/raw/nsw')
    parser.add_argument('--year', type=int, default=2025)
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--interval', type=float, default=21, help='Minimum seconds between requests, shared across workers (under 180/hour)')
    parser.add_argument('--limit', type=int)
    parser.add_argument('--refresh', action='store_true')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--snapshot-command', type=Path, help='Executable to build/publish checked snapshots at checkpoints')
    args = parser.parse_args()
    if args.workers != 1 or args.interval < .1:
        parser.error('Browser collection requires one worker and at least 0.1 seconds between requests')
    raise SystemExit(asyncio.run(crawl(args)))
