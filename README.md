# fastchannels-lite

FastChannels scrapers only, no DB/web UI. Everything is driven by `config.json`.

    pip install -r requirements.txt
    python run.py            # runs every source with "enabled": true
    python run.py --list     # states + reasons
    python run.py -s pluto   # run specific sources regardless of the flag

First run writes `config.json` (new sources found later are appended; your edits are never overwritten).

## config.json
    {
      "options": {"out": "playlists", "workers": 4, "combined": true, "base_url": ""},
      "sources": {
        "samsung": {"enabled": true,  "resolve_at_build": false, "note": "...", "config": {}},
        "pluto":   {"enabled": false, "resolve_at_build": false, "note": "token ...", "config": {}}
      }
    }

- `enabled` true/false per source. Disabled sources have their old output files deleted on the next full run.
- `resolve_at_build` bakes the final stream URL into the M3U (only for sources whose resolve is a stable lookup).
- `config` is passed to the scraper (logins, region, etc).
- `combined` also writes `all.m3u` / `all.xml`.

Defaults: ON = free, no login, no DRM, direct stream URLs (adultswim, hallmark, samsung, vizio,
lg-channels, freelivesports, free_networks minus cbn/newsnation/ognetwork). Everything else OFF
(DRM, login, per-play token).

## Token sources (pluto, plex, xumo, ...)
Enable them, run `python resolver.py` somewhere that stays on, set `options.base_url` to its address
(e.g. `http://192.168.1.50:5524`), and re-run `run.py`. Their M3U lines then point at the resolver,
which 302s to a fresh upstream URL per play. Without `base_url` those channels are skipped.

Termux/minimal systems need `pip install tzdata` (in requirements.txt).
