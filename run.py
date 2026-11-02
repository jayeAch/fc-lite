#!/usr/bin/env python3
"""Scrape FastChannels sources straight into M3U + XMLTV files. No DB, no web app.

  python run.py                 # run every source with "enabled": true in config.json
  python run.py --init          # create/update config.json with defaults (also happens automatically)
  python run.py --list          # show every source, its state and why
  python run.py -s pluto,xumo   # run just these, ignoring their enabled flag

config.json:
  {
    "options": {"out": "playlists", "workers": 4, "combined": true, "base_url": ""},
    "sources": {"samsung": {"enabled": true, "resolve_at_build": false, "config": {}}, ...}
  }

Output: <out>/<source>.m3u, <out>/<source>.xml and (combined=true) <out>/all.m3u, <out>/all.xml.
"""
import argparse
import json
import logging
import re
import sys
import time
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).parent))
import prune  # noqa: E402
from app.scrapers import registry  # noqa: E402

log = logging.getLogger('run')
HTTP = re.compile(r'^https?://', re.I)
# ad-macro placeholder like =[CB] or /[DEVICE_ID]; not a bracketed param name like device[did]=
MACRO = re.compile(r'(?<![A-Za-z0-9_])\[[^\]\s]+\]')

DEFAULT_OPTIONS = {'out': 'playlists', 'workers': 4, 'combined': False, 'base_url': ''}

# seconds to pause between build-time resolves for rate-limited services
RESOLVE_DELAY = {'stirr': 1.5,'free_networks': 0.5}

# name: (enabled, resolve_at_build, note, default scraper config)
# Enabled by default = free, no login, no DRM, and the stream URL is playable as a plain URL
# (directly, or after a stable build-time resolve). Everything else is off.
PROFILES = {
    'adultswim':      (True,  True,  'direct HLS after build-time resolve', {}),
    'hallmark':       (False, False,  'direct HLS after build-time resolve', {}),
    'samsung':        (False, False, 'direct redirect URLs', {}),
    'vizio':          (True,  False, 'direct HLS', {}),
    'lg-channels':    (False, False,  'direct HLS after macro expansion', {}),
    'freelivesports': (False, False,  'direct HLS after macro expansion', {}),
    'free_networks':  (True,  True,  'mostly direct HLS; cbn/newsnation/ognetwork need per-play '
                                     'tokens so they are excluded',
                       {'disabled_networks': 'cbn,newsnation,ognetwork'}),
    # DRM
    'roku':           (False, False, 'token + DRM (Widevine/FairPlay variants)', {}),
    'vix':            (False, False, 'DRM', {}),
    'vidaa':          (True, False, 'DRM', {}),
    'pbs':            (False, False, 'DRM', {}),
    'sling':          (False, False, 'DRM + login', {}),
    'amazon_prime_free': (False, False, 'DRM + login', {}),
    'directv':        (False, False, 'DRM + login', {}),
    'fubo':           (False, False, 'DRM + login', {}),
    'philo':          (False, False, 'DRM + login', {}),
    # login
    'tubi':           (False, False, 'login required', {'username': '', 'password': ''}),
    'freecast':       (False, False, 'login required', {'username': '', 'password': ''}),
    'hdhomerun':      (False, False, 'local tuner; needs device_url', {'device_url': ''}),
    'custom':         (False, False, 'your own playlist; needs config', {}),
    # stream URL only obtained per play (session/token), needs resolver.py + options.base_url
    'pluto':          (False, False, 'token (per-play JWT)', {}),
    'plex':           (False, False, 'token (short-lived per-play URL)', {}),
    'stirr':          (True,  True,  'token (per-play API call)', {}),
    'xumo':           (False, False, 'token (per-play API call)', {}),
    'tcl':            (False, False, 'token (per-play API call)', {}),
    'whale':          (True,  True,  'token (per-play lookup)', {}),
    'zeam':           (False, False, 'token (per-play API call)', {}),
    'cspan':          (False, False, 'token (per-play API call)', {}),
    'ballysports':    (False, False, 'token (per-play API call)', {}),
    'localnow':       (False, False, 'token (per-play session) + needs dma/market', {}),
}


# ── config ──────────────────────────────────────────────────────────────────

def registered():
    return {k: v for k, v in registry.get_all().items() if k == v.source_name}


def init_config(path: Path, reg) -> dict:
    cfg = json.loads(path.read_text()) if path.exists() else {}
    opts = cfg.setdefault('options', {})
    for k, v in DEFAULT_OPTIONS.items():
        opts.setdefault(k, v)
    srcs = cfg.setdefault('sources', {})
    added = []
    for name in sorted(reg):
        if name in srcs:
            continue
        en, rab, note, scfg = PROFILES.get(name, (False, False, 'unclassified', {}))
        srcs[name] = {'enabled': en, 'resolve_at_build': rab, 'note': note, 'config': dict(scfg)}
        if name in RESOLVE_DELAY:
            srcs[name]['resolve_delay'] = RESOLVE_DELAY[name]
        added.append(name)
    if added or not path.exists():
        path.write_text(json.dumps(cfg, indent=2) + '\n', encoding='utf-8')
        log.info('config %s: added %s', path, ', '.join(added) or '(nothing)')
    return cfg


def source_config(cfg: dict, name: str) -> dict:
    return (cfg.get('sources', {}).get(name) or {}).get('config') or {}


def load_config(path) -> dict:
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else {}


# ── output ──────────────────────────────────────────────────────────────────

def q(s) -> str:
    return escape(str(s or ''), {'"': '&quot;'})


def xt(dt) -> str:
    if isinstance(dt, (int, float)):
        dt = datetime.fromtimestamp(dt, timezone.utc)
    elif isinstance(dt, str):
        dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime('%Y%m%d%H%M%S +0000')


def cid(source: str, ch_id) -> str:
    return f"{source}.{re.sub(r'[^A-Za-z0-9._-]+', '_', str(ch_id))}"


def stream_line(source, ch, base_url):
    """Direct URL when playable as-is; resolver URL otherwise; None if neither possible."""
    url = ch.stream_url or ''
    if HTTP.match(url) and not MACRO.search(url):
        return url
    return f"{base_url.rstrip('/')}/play/{source}/{ch.source_channel_id}" if base_url else None


def drm_props(cls, name, ch, cfg, base_url):
    """#KODIPROP lines for a DRM (DASH) channel; [] for everything else.

    base_url set  -> license_key points at resolver.py's /license/<source>/<id> proxy,
                     which does the per-channel URL + headers at play time.
    base_url empty -> license URL and headers are resolved now and baked into the M3U
                      (works when the license call needs no per-session secret)."""
    if not cls.license_url or ch.stream_type != 'dash':
        return []
    cid_ = str(ch.source_channel_id)
    props = {'inputstream': 'inputstream.adaptive',
             'inputstream.adaptive.manifest_type': 'mpd',
             'inputstream.adaptive.license_type': 'com.widevine.alpha'}
    if base_url:
        key = f"{base_url.rstrip('/')}/license/{name}/{quote(cid_, safe='')}||R{{SSM}}|"
    else:
        sc_cfg = source_config(cfg, name)
        try:
            lic = cls.get_license_url(sc_cfg, cid_)
            _, hdrs = cls.prepare_license_request(b'', sc_cfg, cid_)
        except Exception as e:
            log.warning('[%s] license setup for %s failed: %s', name, cid_, e)
            return []
        if not lic:
            return []
        hdr = '&'.join(f"{k}={quote(str(v), safe='')}" for k, v in (hdrs or {}).items())
        key = f'{lic}|{hdr}|R{{SSM}}|'
    props['inputstream.adaptive.license_key'] = key
    props.update(cls.kodi_props)  # a scraper's own overrides win
    return [f'#KODIPROP:{k}={v}' for k, v in props.items()]


def m3u_entries(source, channels, base_url, cls=None, cfg=None):
    lines, skipped = [], 0
    for ch in channels:
        url = stream_line(source, ch, base_url)
        if not url:
            skipped += 1
            continue
        attrs = [f'tvg-id="{q(cid(source, ch.source_channel_id))}"', f'tvg-name="{q(ch.name)}"']
        if ch.logo_url:
            attrs.append(f'tvg-logo="{q(ch.logo_url)}"')
        if ch.category:
            attrs.append(f'group-title="{q(ch.category)}"')
        if ch.language:
            attrs.append(f'tvg-language="{q(ch.language)}"')
        if ch.number is not None:
            attrs.append(f'tvg-chno="{q(ch.number)}"')
        if ch.gracenote_id:
            attrs.append(f'tvc-guide-stationid="{q(ch.gracenote_id)}"')
        lines.append(f'#EXTINF:-1 {" ".join(attrs)},{ch.name}')
        if cls is not None:
            lines += drm_props(cls, source, ch, cfg or {}, base_url)
        lines.append(url)
    return lines, skipped


def count_channels(m3u) -> int:
    return sum(1 for l in m3u if l.startswith('#EXTINF'))


def xmltv_entries(source, channels, programs):
    chans = [f'<channel id="{q(cid(source, c.source_channel_id))}"><display-name>{q(c.name)}</display-name>'
             + (f'<icon src="{q(c.logo_url)}"/>' if c.logo_url else '') + '</channel>' for c in channels]
    ids = {str(c.source_channel_id) for c in channels}
    progs = []
    for p in programs:
        if str(p.source_channel_id) not in ids or not p.start_time or not p.end_time:
            continue
        try:
            s = (f'<programme start="{xt(p.start_time)}" stop="{xt(p.end_time)}" '
                 f'channel="{q(cid(source, p.source_channel_id))}"><title>{q(p.title)}</title>')
        except Exception:
            continue
        if p.episode_title:
            s += f'<sub-title>{q(p.episode_title)}</sub-title>'
        if p.description:
            s += f'<desc>{q(p.description)}</desc>'
        if p.category:
            s += f'<category>{q(p.category)}</category>'
        if p.poster_url:
            s += f'<icon src="{q(p.poster_url)}"/>'
        if p.season and p.episode:
            try:
                s += f'<episode-num system="xmltv_ns">{int(p.season)-1}.{int(p.episode)-1}.</episode-num>'
            except (TypeError, ValueError):
                pass
        if p.rating:
            s += f'<rating><value>{q(p.rating)}</value></rating>'
        progs.append(s + '</programme>')
    return chans, progs


def write_m3u(path, entries):
    path.write_text('\n'.join(['#EXTM3U'] + entries) + '\n', encoding='utf-8')


def write_xmltv(path, chans, progs):
    head = '<?xml version="1.0" encoding="UTF-8"?>\n<tv generator-info-name="fastchannels-lite">'
    path.write_text('\n'.join([head] + chans + progs + ['</tv>']) + '\n', encoding='utf-8')


# ── scraping ────────────────────────────────────────────────────────────────

def needs_resolve(url) -> bool:
    return bool(url) and (not HTTP.match(url) or MACRO.search(url))


def resolve_now(name, sc, channels, delay=0.0, passes=3):
    """Bake final URLs into channels. Retries failures (e.g. HTTP 429) after a growing pause."""
    pending = [c for c in channels if needs_resolve(c.stream_url)]
    for attempt in range(passes):
        for c in pending:
            try:
                c.stream_url = sc.resolve(c.stream_url) or c.stream_url
            except Exception as e:
                log.warning('[%s] resolve %s failed: %s', name, c.source_channel_id, e)
            if delay:
                time.sleep(delay)
        pending = [c for c in pending if needs_resolve(c.stream_url)]
        if not pending:
            return
        if attempt < passes - 1:
            wait = 15 * (attempt + 1)
            log.info('[%s] %d unresolved, retrying in %ds', name, len(pending), wait)
            time.sleep(wait)
    log.warning('[%s] %d channels still unresolved', name, len(pending))


def scrape(name, cls, cfg, out_dir):
    entry = cfg.get('sources', {}).get(name, {})
    base_url = cfg.get('options', {}).get('base_url', '')
    sc = cls(source_config(cfg, name))
    try:
        sc.pre_run_setup()
    except Exception as e:
        log.warning('[%s] pre_run_setup failed: %s', name, e)
    channels, programs = sc.run()
    if not channels:
        raise RuntimeError('no channels returned')
    if cls.license_url and not base_url:
        log.info('[%s] DRM source, no base_url: license URL/headers baked into M3U', name)
    if entry.get('resolve_at_build'):
        resolve_now(name, sc, channels, float(entry.get('resolve_delay', 0)))
    m3u, skipped = m3u_entries(name, channels, base_url, cls, cfg)
    chans, progs = xmltv_entries(name, channels, programs)
    write_m3u(out_dir / f'{name}.m3u', m3u)
    write_xmltv(out_dir / f'{name}.xml', chans, progs)
    raw = {str(c.source_channel_id): c.stream_url for c in channels
           if stream_line(name, c, 'x') != c.stream_url}
    raw_path = out_dir / f'{name}.raw.json'
    if raw:
        raw_path.write_text(json.dumps(raw), encoding='utf-8')  # consumed by resolver.py
    elif raw_path.exists():
        raw_path.unlink()
    return {'m3u': m3u, 'chans': chans, 'progs': progs, 'skipped': skipped,
            'n_channels': count_channels(m3u), 'n_programs': len(progs)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('-s', '--sources', help='comma list; runs these regardless of enabled flag')
    ap.add_argument('-c', '--config', default='config.json')
    ap.add_argument('--init', action='store_true', help='create/update config.json and exit')
    ap.add_argument('--list', action='store_true')
    ap.add_argument('-v', '--verbose', action='store_true')
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')

    reg = registered()
    cfg_path = Path(a.config)
    cfg = init_config(cfg_path, reg)  # adds any sources missing from the file; never overwrites yours
    if a.init:
        return
    srcs = cfg['sources']
    if a.list:
        for k in sorted(srcs):
            e = srcs[k]
            print(f"{'ON ' if e.get('enabled') else 'off'} {k:18} {'' if k in reg else '[not importable] '}{e.get('note', '')}")
        return

    if a.sources:
        names = [s.strip() for s in a.sources.split(',') if s.strip()]
    else:
        names = sorted(k for k, e in srcs.items() if e.get('enabled'))
    bad = [n for n in names if n not in reg]
    if bad:
        log.error('not available (missing from registry or failed to import): %s', ', '.join(bad))
        names = [n for n in names if n in reg]
    if not names:
        sys.exit('nothing to run; set "enabled": true for a source in config.json')

    opts = {**DEFAULT_OPTIONS, **cfg.get('options', {})}
    out_dir = Path(opts['out'])
    out_dir.mkdir(parents=True, exist_ok=True)

    if not a.sources:  # delete output no enabled source uses (disabled/removed sources)
        prune.prune(out_dir, {k for k, e in srcs.items() if e.get('enabled')}, opts['combined'])

    results, failed = {}, list(bad)
    with ThreadPoolExecutor(max_workers=int(opts['workers'])) as ex:
        futs = {ex.submit(scrape, n, reg[n], cfg, out_dir): n for n in names}
        for f in as_completed(futs):
            n = futs[f]
            try:
                r = results[n] = f.result()
                log.info('[%s] %d channels, %d programmes%s', n, r['n_channels'], r['n_programs'],
                         f", {r['skipped']} skipped (need resolver + options.base_url)" if r['skipped'] else '')
            except Exception as e:
                failed.append(n)
                log.error('[%s] FAILED: %s', n, e)

    if opts['combined'] and results and not a.sources:
        m3u, chans, progs = [], [], []
        for n in sorted(results):
            m3u += results[n]['m3u']; chans += results[n]['chans']; progs += results[n]['progs']
        write_m3u(out_dir / 'all.m3u', m3u)
        write_xmltv(out_dir / 'all.xml', chans, progs)
        log.info('combined: %d channels from %d sources -> %s', count_channels(m3u), len(results), out_dir / 'all.m3u')
    if failed:
        log.warning('failed: %s', ', '.join(sorted(failed)))
    sys.exit(1 if not results else 0)


if __name__ == '__main__':
    main()
