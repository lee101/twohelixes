"""Isolated local ingest benchmark; never connects to a running site or model.

This measures Python routing, normalisation and SQLite persistence, not HTTP
or production PostgreSQL capacity. All rows must be retained for a valid run.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import statistics
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--threads', type=int, default=4)
    parser.add_argument('--batches', type=int, default=25, help='batches per thread')
    parser.add_argument('--events', type=int, default=100, help='events per batch')
    args = parser.parse_args()
    if not 1 <= args.threads <= 32 or not 1 <= args.batches <= 1000 or not 1 <= args.events <= 2000:
        parser.error('threads 1..32, batches 1..1000, events 1..2000 required')
    with tempfile.TemporaryDirectory(prefix='th-ingest-bench-') as directory:
        os.environ['TWOHELIXES_DATA_DIR'] = directory
        os.environ.pop('TWOHELIXES_PG_DSN', None)
        os.environ['TWOHELIXES_ANALYTICS_SAMPLE_AFTER_RPS'] = '1000000000'
        from twohelixes import config, router, store
        from twohelixes.routes import analytics

        config.DOTENV_CANDIDATES = ()
        config._dotenv.cache_clear()
        analytics._schedule_dashboards = lambda _claimed: None
        store.init()
        router.build()
        owner = store.create_user('benchmark@test.local')
        site_id = store.new_id()
        with store.transaction() as conn:
            conn.execute(
                'INSERT INTO analytics_sites (id,user_id,domain,name,write_key,created_at)'
                ' VALUES (?,?,?,?,?,?)',
                (site_id, owner['id'], 'bench.test', 'Local benchmark', 'thw_local', time.time()),
            )
        headers = json.dumps({'user-agent': 'Mozilla/5.0 Chrome/120'})

        def worker(index):
            latencies = []
            try:
                for batch in range(args.batches):
                    body = json.dumps({'site_id': site_id, 'events': [
                        {'event': 'purchase', 'client_id': f'{index}-{batch}-{event}',
                         'session_id': f'{index}-{batch}-{event}', 'page_path': '/pricing',
                         'props': {'plan': 'pro', 'value': 20}}
                        for event in range(args.events)
                    ]})
                    started = time.perf_counter()
                    status, *_ = router.handle('POST', '/v1/collect', '', body, headers)
                    if int(status) != 204:
                        raise RuntimeError(f'ingest failed with status {status}')
                    latencies.append((time.perf_counter() - started) * 1000)
                return latencies
            finally:
                store.close()

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=args.threads) as pool:
            latencies = sorted(value for result in pool.map(worker, range(args.threads)) for value in result)
        elapsed = time.perf_counter() - started
        expected = args.threads * args.batches * args.events
        stored = store.one('SELECT COUNT(*) AS n FROM analytics_events WHERE event_name = ?', ('purchase',))['n']
        if stored != expected:
            raise RuntimeError(f'data loss: expected {expected}, stored {stored}')
        print(json.dumps({
            'backend': 'isolated SQLite, Python router (not HTTP)',
            'threads': args.threads, 'batches': len(latencies), 'events_per_batch': args.events,
            'accepted_events': stored, 'elapsed_seconds': round(elapsed, 3),
            'events_per_second': round(stored / elapsed),
            'batch_p50_ms': round(statistics.median(latencies), 2),
            'batch_p99_ms': round(latencies[min(len(latencies) - 1, int(len(latencies) * .99))], 2),
        }, indent=2))
        store.close()


if __name__ == '__main__':
    main()
