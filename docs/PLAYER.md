# P2 player contract

P2 and R2 are separate from legacy P1/R1, with nonzero eight-lowercase-hex
correlation IDs and at most 91 UTF-8 bytes per frame. No malformed or reserved
P2/R2 request enters the model. List offsets are even, two entries per page;
revision `00000000` is valid only for first-page refresh. Track and revision IDs
are owner-scoped hashes with durable full-digest collision detection.

Request grammar: `P2|CID8|VERB|PAYLOAD`.
- `q/p/s/x/b/z`: empty payload; status/play-resume/pause/next/previous/stop.
- `l`: `REV8,OFFSET`; `d/k`: `REV8,ID8` for title details/select-play.
- `v`: canonical unsigned decimal 0..100, absolute volume.
- `j`: canonical unsigned decimal 0..863999, absolute seconds; duration must be
  observed and value must not exceed duration minus one second.
- `e`: `flat`, `bass` or `soft`, only if advertised by observed service status.

Replies: `R2|CID8|m|REV8,ID8,RUN,PAUSED,VOL,POS,DURATION;TITLE`,
`R2|CID8|l|REV8,OFFSET,TOTAL;ID8=TITLE;ID8=TITLE`,
`R2|CID8|d|REV8,ID8;TITLE`, or `e/u` for refusal/unknown.
Unknown observations use `?`; title clipping preserves UTF-8 scalars and shows
an ellipsis. Catalog capacity is 999; the compact UI may cache only 26 rows.

## Operator-owned private Music API

No server is included. The adapter connects only to loopback, default port 8899.
`home_music_port` may select another local port; `home_library_root` selects an
absolute canonical trusted root (default neutral example `/srv/pocket/music`).
Source directories are root/source; playlists are root/source.m3u, except
`all_sets.m3u`. Only the fixed generic APPROVED_SOURCES set is accepted; this
package deliberately excludes personal source labels from the demonstrated
installation. An operator must align approved sources and local API data.

Required GET `/api/library`: `{ok:true,count:N,tracks:[{path,label,source,ext}]}`;
GET `/api/status`: `ok`, boolean `running/paused`, optional finite numeric
`volume/position/duration`, `track/track_label`, `mode/playlist/queue`, and
`eq.presets`. Paths stay on the host and must name canonical regular approved
audio files with no symlink components. Responses use bounded Content-Length
JSON; duplicate JSON fields and nonfinite values are refused by P2.

Fixed POST routes: `/api/play-file?path=ENCODED_CATALOG_PATH`, `/api/vol/N`,
`/api/seek/N`, `/api/eq/preset?value=flat|bass|soft`, `/api/play/SOURCE`,
`/api/resume`, `/api/pause`, `/api/next`, `/api/prev`, `/api/stop`.
The catalog supplies paths; inbound requests cannot supply URLs, paths, shell
text or arbitrary filters. Queue/context checks precede transport operations.
POST bodies are not trusted as observations; a fresh status GET is required.

Seek can resume a paused track. EQ can restart playback; paused-state handling
belongs to the existing controller. Host sink selection and actual sound are
outside this client. Another privileged controller or filesystem writer can
change state between validation and POST: the existing API has no atomic
conditional mutation. This package does not claim transactional concurrency.
