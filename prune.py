#!/usr/bin/env python3
"""Delete playlist files that no enabled source uses.

  python prune.py              # delete
  python prune.py --dry-run    # just list what would go

Keeps <name>.m3u / <name>.xml / <name>.raw.json for every source with "enabled": true,
plus all.m3u / all.xml when options.combined is true. Anything else matching those
patterns (disabled sources, removed scrapers, stray files) is deleted.
Other files in the folder are never touched. run.py calls this after every full run.
"""
import argparse
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

log = logging.getLogger('prune')
_PATTERN = re.compile(r'^(?P<name>.+?)\.(?:m3u|xml|raw\.json)$')


def prune(out_dir, enabled, combined=True, dry_run=False):
    """Remove unused playlist files in out_dir. Returns the list of removed (or would-be) paths."""
    out_dir = Path(out_dir)
    if not out_dir.is_dir():
        return []
    keep = set(enabled) | ({'all'} if combined else set())
    removed = []
    for f in sorted(out_dir.iterdir()):
        m = _PATTERN.match(f.name)
        if not f.is_file() or not m or m.group('name') in keep:
            continue
        removed.append(f)
        if not dry_run:
            f.unlink()
    for f in removed:
        log.info('%s %s', 'would delete' if dry_run else 'deleted', f)
    return removed


def main():
    import run  # late import: run.py imports this module
    ap = argparse.ArgumentParser()
    ap.add_argument('-c', '--config', default='config.json')
    ap.add_argument('--dry-run', action='store_true')
    a = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(message)s')
    cfg = run.init_config(Path(a.config), run.registered())
    opts = {**run.DEFAULT_OPTIONS, **cfg.get('options', {})}
    enabled = {k for k, e in cfg['sources'].items() if e.get('enabled')}
    removed = prune(opts['out'], enabled, opts['combined'], a.dry_run)
    if not removed:
        log.info('nothing to prune')


if __name__ == '__main__':
    main()
