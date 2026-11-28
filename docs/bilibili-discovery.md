# Bilibili music discovery

Bilibili discovery is off by default. The daemon can read yesterday's visible
Shanghai history for the existing `[album_discovery].target_user`. The history
server must use `TZ=Asia/Shanghai`. The discovery job starts at 04:30 Shanghai
time and resumes unfinished work on the recommendation check interval.

```ini
[bilibili_discovery]
enabled = off
auto_download = off
history_api_url = https://history.example.invalid
history_api_token_file = /run/secrets/bili-history-token
start_time = 04:30
```

Set the history server URL and mount the existing `X-Auth-Token` secret file
when server authentication is enabled. The daemon uses the existing
`[album_discovery]` user, library key, received music directory and CN catalog.
Extraction uses the existing OpenAI album discovery workload and shared budget.
Review the `bilibili-discovery` job under `/admin/tasks` for progress and short
error reasons. History sync happens through the history service before the normal
daily fetch. The snapshot covers the rows currently visible at that service.

With `enabled = on`, current eligible local recordings can supplement the next
fresh AI daily playlist. A playlist already generated for that day retains its
identity and ordered entries. The model sees the source category and age.
Source eligibility lasts 14 days from readiness.

Both switches must be on for new Bilibili orders. Configure Oracle control and
delivery credentials and set the aggregate `aiDiscovery.maxAlbumsPerDay` in the
existing album discovery JSON. For a 13-album limit, legacy and Bilibili orders
share 13 slots. Keep the existing OpenWrt delivery and library indexing path.
The shared reconciliation job can account for accepted or ambiguous orders while
new Bilibili discovery is off.

History snapshots and evidence are kept in the existing database and its
backups. This version applies the discovery and recommendation windows during
processing and retains the underlying rows for inspection and order reconciliation.

## Isolated rehearsal

Run `python -m tests.bilibili_rehearsal` from the repository with the usual test
dependencies. It creates a temporary SQLite database and music library, blocks
network connections, and feeds synthetic history, catalog and extraction
responses through the daemon's discovery pass. The JSON output compares the
actual baseline and enhanced pools and checks the fixture playlist, play
counts and orders. The result measures candidate composition. Ranking and
provider token use are unmeasured in this rehearsal.
