#!/usr/bin/env python3
"""Tiny play-time resolver for sources whose stream URLs are tokenized/short-lived
(pluto://, tubi://, roku://, plex://, ...). Redirects /play/<source>/<id> to the
fresh upstream URL using the scraper's own resolve().

  python resolver.py [--port 5524] [--dir playlists] [--config config.json]

Set options.base_url in config.json to http://<this-host>:5524, enable the token sources,
and re-run run.py so the M3Us point here.
"""
import argparse
import json
import logging
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from app.scrapers import registry  # noqa: E402
from run import load_config, source_config  # noqa: E402

log = logging.getLogger('resolver')
_scrapers, _raw, _lock = {}, {}, threading.Lock()


def get_scraper(name, config):
    with _lock:
        if name not in _scrapers:
            cls = registry.get_all()[name]
            sc = cls(source_config(config, name))
            try:
                sc.pre_run_setup()
            except Exception as e:
                log.warning('[%s] pre_run_setup: %s', name, e)
            _scrapers[name] = sc
        return _scrapers[name]


def raw_url(name, ch_id, pl_dir):
    path = pl_dir / f'{name}.raw.json'
    mtime = path.stat().st_mtime if path.exists() else 0
    cached = _raw.get(name)
    if not cached or cached[0] != mtime:
        _raw[name] = (mtime, json.loads(path.read_text()) if path.exists() else {})
    return _raw[name][1].get(ch_id)


def make_handler(pl_dir, config):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            parts = self.path.split('?')[0].strip('/').split('/')
            if len(parts) != 3 or parts[0] != 'play':
                return self.send_error(404)
            _, name, ch_id = parts
            raw = raw_url(name, ch_id, pl_dir)
            if not raw or name not in registry.get_all():
                return self.send_error(404, 'unknown channel')
            try:
                url = get_scraper(name, config).resolve(raw)
            except Exception as e:
                log.error('[%s] resolve %s failed: %s', name, ch_id, e)
                return self.send_error(502, 'resolve failed')
            self.send_response(302)
            self.send_header('Location', url)
            self.end_headers()

        def log_message(self, fmt, *args):
            log.info(fmt, *args)
    return H


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=5524)
    ap.add_argument('--dir', default='playlists')
    ap.add_argument('--config', default='config.json')
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = load_config(a.config)
    a.dir = a.dir if a.dir != 'playlists' else config.get('options', {}).get('out', 'playlists')
    ThreadingHTTPServer(('0.0.0.0', a.port), make_handler(Path(a.dir), config)).serve_forever()


if __name__ == '__main__':
    main()
