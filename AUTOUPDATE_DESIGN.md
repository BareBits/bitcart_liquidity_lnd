# Auto-Update Design

Status: **draft / proposed**. This document specifies how the Liquidity
Helper plugin ships updates to deployments in the wild — including
deployments we have no direct access to. It is the agreed design; code
lands in later PRs, one phase at a time.

This spans **two repos**:

- `bitcart_liquidity_lnd` (this repo) — the plugin: detection, surfacing,
  the `/health` probe, settings.
- `deploy_bitcart_liquidity_lnd` — the deployment script: the host-side
  updater that actually applies updates and rolls them back.

---

## 1. The constraint that shapes everything

In a **Dockerized Bitcart** — which is essentially every real install —
**nothing running inside the containers can durably update the plugin.**
Plugin code is baked into the backend + admin images at build time; a
file written inside a running container is wiped on the next rebuild, and
the rebuild is itself a host-level operation. Bitcart's own install flow
confirms this: installing a plugin means uploading a `.bitcartcc` archive
in admin, which **rebuilds everything**. There is no built-in
remote/registry auto-update in Bitcart.

Therefore the actor that *applies* an update must live **on the host** for
any Docker deployment. The plugin process can *detect* an update and
*surface* it, but it cannot durably *apply* it.

The only deployment type where in-process apply is durable is
**manual / bare-metal** Bitcart (persistent `modules/` directory, backend
under systemd/supervisor). That path is **deferred** (see §8).

### Two facts Bitcart gives us for free

1. **Plugin load failures are isolated.** Bitcart's `load_plugins()`
   wraps each plugin's load in try/except; a plugin that throws on import
   logs an error and is skipped — the backend and *other* plugins keep
   running. So a crash-on-import in our shipped code does **not** take the
   node down, and "our plugin failed to load" is cleanly distinguishable
   from "the node is down." This is the signal our rollback logic keys on.

2. **A separate plugin is its own isolation domain.** Because load
   failures don't cascade, a tiny second plugin survives the main plugin
   crashing. (Relevant only to the deferred manual-install path in §8.)

---

## 2. Trust model

No code signing for now. The trust anchor is the **BareBits GitHub
repo**. Updates are pulled from a release channel (a branch). Whoever
controls the branch controls the update — same trust as a `git pull`.

Revisit signing (signed tags / pinned key) before the first deployment
that auto-applies without an operator in the loop at scale; tracked as
future work, not in this design.

---

## 3. Release channels

A channel **is a git branch**:

- `main` — stable.
- `testing` — development / early adopters.

Both the in-plugin version check and the host updater operate against the
configured channel branch. Switching channel = pointing the updater at a
different branch.

---

## 4. Settings

Three new settings, flowing through the **existing precedence chain**
(plugin UI > `LIQUIDITYHELPER_*` env > `user_config.py` > `config.py`
default):

| Setting | Env var | Default | Meaning |
|---|---|---|---|
| `AUTO_UPDATE_ENABLED` | `LIQUIDITYHELPER_AUTO_UPDATE_ENABLED` | `False` | Master switch for auto-apply. Off by default. |
| `UPDATE_CHANNEL` | `LIQUIDITYHELPER_UPDATE_CHANNEL` | `main` | `main` or `testing`. |
| `UPDATE_CHECK_INTERVAL` | `LIQUIDITYHELPER_UPDATE_CHECK_INTERVAL` | e.g. `21600` (6h) | How often the plugin polls GitHub for the version check. |

**Off-by-default behavior is a hard requirement.** With
`AUTO_UPDATE_ENABLED=False`, the plugin still *checks* for updates and,
when a newer version exists on the channel, it:

- shows a warning on the plugin dashboard ("Liquidity Helper vX is
  available; automatic updates are off"), and
- emails the site operator **if SMTP is configured** (reusing the
  existing owner-notification path in `bitcart_plugin/`).

Detection is read-only and runs everywhere regardless of deployment type;
it carries no isolation or fund risk.

---

## 5. The `/health` endpoint

A new endpoint: `GET /plugins/liquidityhelper/health` → JSON. It is the
single contract shared by detection, surfacing, and the host updater's
success probe. It reports at least:

- `running_version` — the version the live plugin loaded from.
- `latest_version` / `update_available` — result of the last channel check.
- `auto_update_enabled`, `update_channel` — the plugin's **effective**
  settings (so the host updater can read them — see §6).
- `worker_alive` — whether the worker tick loop spawned (and, ideally, a
  `last_tick_at` heartbeat written to `liquidityhelper.sqlite`, so an
  engine that imports cleanly but dies mid-run is still caught).

Success semantics (used by the updater in §6):

- **Healthy** = endpoint returns `200` and `worker_alive` is true within a
  timeout window after restart.
- **Failed start** = endpoint is absent / `500`, or `worker_alive` never
  becomes true. Because Bitcart keeps the rest of the node up when our
  plugin fails to import, an absent endpoint is an unambiguous
  "our plugin failed to load."

We can reuse the existing `compute_health_warnings` machinery as the body
of the dashboard warning; `/health` is the machine-readable sibling.

---

## 6. Host-side updater (the apply + rollback actor)

Lives in `deploy_bitcart_liquidity_lnd`, an evolution of today's
`update_liquidityhelper.sh`. It runs from the host cron, **completely
outside the containers**, so it survives any plugin crash by construction
(satisfies the isolation requirement directly). It is plain
`bash + git + docker + curl` — **no Python, no imports of plugin code** —
so it is more reliable than the thing it updates. Keep it small and
rarely-changed.

### Effective-config source

The updater needs `enabled` + `channel`. Source of truth is the plugin's
**effective** settings, so:

1. **Query `/health`** on localhost for `auto_update_enabled` +
   `update_channel` (honors live UI toggles).
2. **Fall back to the compose env file** (`LIQUIDITYHELPER_*`) if the
   plugin is down / unreachable.

### Algorithm

```
0. lock           single-flight lockfile; bail if another run is active
1. read config    /health → enabled + channel; fall back to env file
2. gate           enabled == False  → exit 0 (detection/warn is the plugin's job)
3. fetch          git fetch origin <channel>
4. candidate      newest commit on origin/<channel> that is NOT in BANNED
5. up-to-date?    candidate == deployed → exit 0
6. snapshot       record LAST_GOOD commit; tag/keep the current good IMAGE
7. apply          checkout candidate → sync_plugin_code → build NEW images → restart
8. health-check   poll /health until Healthy, up to ~5 min
9a. Healthy       LAST_GOOD = candidate; persist; done
9b. Failed start  BANNED += candidate; roll back to LAST_GOOD; re-health-check; alert
```

### Rollback

Rollback must be fast and must not depend on rebuilding old source (old
source could itself fail to build). So at step 6 we **keep the previous
good image** (retag it `…:lastgood`); rollback at 9b is **retag + restart**
(seconds), not a rebuild. Source is also reset to `LAST_GOOD` so the next
cron run has a consistent baseline.

### Ban-list semantics ("only newer, non-banned")

State lives in a host file under the deploy dir, e.g.
`update_state/{last_good, banned}`:

- A commit that fails its health-check is appended to `BANNED` and we roll
  back to `LAST_GOOD`.
- Because `origin/<channel>` still points at the bad commit, the next run
  sees the candidate is banned (step 4) and **stays on `LAST_GOOD`**. It
  only moves again when a **newer** commit lands on the channel. That is
  exactly "ban it; only future, newer versions get downloaded."

### Safety rails

- **Build-then-swap.** Never tear down running containers until the new
  image has built *and* passed health. A failed `docker build` leaves the
  running node untouched — never brick a fund-moving node on a bad build.
- **Run live with real funds; no probation mode** (per decision). The only
  automatic rollback trigger is **failed start** (§5). A version that
  starts cleanly is trusted to operate.
- **DB schema + rollback hazard.** If a new version migrates
  `liquidityhelper.sqlite` and then fails to start, rolling back to older
  code can hit a newer schema. Mitigation policy (to confirm during
  implementation): snapshot the SQLite DBs at step 6 and restore on
  rollback, *or* require migrations to stay one-version backward
  compatible. Restoring the snapshot loses only the few minutes of state
  in the failed window.

---

## 7. Deployment coverage

| Deployment | Detect + warn | Auto-apply |
|---|---|---|
| **Our deploy repo** (Docker + host cron) | in-plugin | host updater (evolve existing cron) |
| **Standard bitcart-docker** + our plugin via `.bitcartcc` | in-plugin | host updater, shipped as an **optional standalone companion** — a one-line installer that drops the same cron + scripts. Opt-in. |
| **Manual / bare-metal** Bitcart | in-plugin | **deferred** (§8) — detect + warn only for now |

The in-plugin detection/surfacing layer is universal: every deployment at
least *learns* an update exists, even where we can't auto-apply.

---

## 8. Deferred: manual / bare-metal in-process updater

For non-Docker installs, an in-process updater *can* durably swap files
and restart. The intended shape (NOT in scope now):

- A **separate, tiny updater-plugin** (its own `modules/.../plugin.py`),
  isolated from the main plugin by Bitcart's per-plugin try/except, so it
  keeps running when the engine fails to import.
- **A/B slot + trial-boot confirmation:** stage the new version, mark it
  "trial," restart; the engine clears the trial flag once it loads
  cleanly. On the next start, an unconfirmed trial ⇒ revert to the
  previous slot and ban the bad version — boot-time rollback without an
  external actor.

Until built, manual installs get **detect + warn only**.

---

## 9. Build phases (each a self-contained PR)

1. **In-plugin detect + surface** (this repo): the three settings, the
   GitHub channel version check, the `/health` endpoint, dashboard warning
   + operator email. Ships value alone, zero risk.
2. **Harden host updater** (deploy repo): channel support, `/health`-gated
   success, fast image rollback, ban-list, lockfile.
3. **Standalone host companion** (deploy repo): one-line installer so
   plain bitcart-docker users get the updater without the full deploy
   script. **Implemented** — `install_updater.sh` (+ `tests/
   install_updater_test.sh`, 15 assertions): locates bitcart-docker,
   resolves the host, clones the plugin source + this repo into `/opt`,
   and installs the cron. Idempotent; changes nothing about the running
   stack; auto-updates stay off until enabled.
4. **Manual-install in-process A/B path** (this repo): the deferred §8
   work — last, fewest users, most complex. **Deferred** (not built).

---

## 10. Phase 1 — implemented

Phase 1 (in-plugin detect + surface) is built. What landed:

- **`config.py`** — new "Automatic updates" group: `AUTO_UPDATE_ENABLED`
  (default `False`), `UPDATE_CHANNEL` (`main`), `UPDATE_CHECK_INTERVAL_SECONDS`
  (6h). They auto-appear in the admin UI and inherit the full precedence
  chain (UI > env > user_config > default) via the existing
  config→schema generator. The running version is read from
  `manifest.json` — the single version source shared with the host updater.
- **`update_check.py`** (new, self-contained, never raises) — version
  helpers, the GitHub channel fetch, the DB-backed cache + worker
  heartbeat, the "update available" health-warning builder, and the
  operator-email body.
- **`liquidityhelper.py`** — the tick loop records a fast local
  **heartbeat** each iteration; `compute_health_warnings` appends the
  cached update warning (no network); `_send_admin_email` + a new
  `run_update_check_loop` were added.
- **`bitcart_plugin/health_endpoint.py`** (new) — the unauthenticated
  `/plugins/liquidityhelper/health` probe.
- **`bitcart_plugin/owner_notifications.py`** — `make_admin_notifier`
  (emails the first superuser / site operator).
- **`plugin.py`** — registers the health router (no auth), wires the
  admin notifier, and spawns/stops `run_update_check_loop`.
- **`tests/update_check_tests.py`** (new) — unit coverage for the above.

**One deviation from the first sketch:** the network update CHECK runs in
its **own background loop** (`run_update_check_loop`), NOT inline in the
tick loop. Only the cheap local heartbeat sits on the tick path. This
keeps a slow GitHub fetch from ever delaying a liquidity tick (a regression
the debug-mode tick-timing test caught) and better matches the "updater is
isolated" principle. The check self-throttles to `UPDATE_CHECK_INTERVAL_SECONDS`;
the loop just wakes every ~5 min to pick up live channel/interval changes.

**Note on `/health` auth:** intentionally unauthenticated so the plain
host updater can curl it without a bearer token. It exposes only
low-sensitivity version/liveness/channel data — no secrets, no fund data.

## 11. Phase 2 — implemented (deploy repo)

Phase 2 (harden the host updater) is built in `deploy_bitcart_liquidity_lnd`
(`update_liquidityhelper.sh` rewritten; `deploy.sh` cron updated; new
`tests/update_liquidityhelper_test.sh`). What landed:

- **Single orchestrator.** `update_liquidityhelper.sh` now owns the whole
  flow; the cron no longer chains `&& ./update.sh` (the script runs the
  rebuild itself so it can health-gate and roll it back). Plain
  bash + git + docker + curl + flock — no Python, no plugin imports.
- **Lockfile** (`flock`) so overlapping cron runs can't collide.
- **Effective config** from the plugin's `/health` (enabled + channel),
  falling back to the compose env file (`LIQUIDITYHELPER_*`) when the
  plugin is unreachable.
- **Off-by-default gate.** If `AUTO_UPDATE_ENABLED` isn't true, the cron
  does nothing. **This gate covers bitcart-core updates too** — with
  auto-updates off, the chained core `update.sh` no longer runs either,
  so a fund-moving node gets *no* surprise rebuilds until the operator
  opts in. (Documented prominently in `deploy.sh`.)
- **Channel = branch.** `main`/`testing` are whitelisted; anything else
  falls back to the currently checked-out branch (never interpolate
  arbitrary input into git/URLs).
- **Ban-list** (`update_state/banned`). The candidate is the channel tip;
  a banned commit is skipped, so only a *newer, non-banned* commit is ever
  applied.
- **Health gate.** After rebuild+restart it polls `/health` until
  `ok && worker_alive` (timeout ~5 min) — i.e. the worker tick loop
  actually started.
- **Rollback on failed start.** Bans the candidate, resets plugin source
  to the previous good commit, and rebuilds from that source. Outcome is
  written to `update_state/last_result`.

**Deviation from §6's sketch — rollback by source, not image retag.**
The sketch preferred retagging a cached previous image for a seconds-fast
rollback. In reality the rebuild is owned by bitcart-docker's `update.sh`
(a black box that rebuilds from `compose/plugins` and also updates core),
and its image-tagging isn't a stable contract to couple to. So rollback
**reverts the plugin source to the last good commit and rebuilds** — slower
(a rebuild) but fully decoupled and robust. The "run live with real funds,
roll back only on failed start" decision means this is the sole automatic
rollback trigger; we do **not** auto-revert a bitcart-core update (out of
scope — logged loudly for manual intervention if core breaks the plugin).

All six scenarios (gate-off, core-only, apply, fail→ban→rollback,
banned-skip, env-fallback) are covered by the new bash test — 20 assertions,
green.

## 12. Alignment with `DESIGN.md`

- **Nothing should ever crash the script.** Detection/version-check runs
  in its own try/except on the existing loop; a check failure is a logged
  no-op. The apply/rollback brain is host-side and cannot affect the
  running engine.
- **Persist as little as possible.** New persistent state is minimal: a
  `last_tick_at` heartbeat in the existing `liquidityhelper.sqlite`, and
  host-side `last_good` / `banned` files (outside the plugin).
- **Assume no filesystem access except the SQLite DBs.** Honored — the
  plugin never writes its own code; all file/image manipulation is the
  host updater's job.
