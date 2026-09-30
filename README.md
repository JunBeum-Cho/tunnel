# SirTunnel

Expose services over an SSH remote forward. Caddy terminates HTTPS; `tunnel.py`
registers each hostname in its local admin API.

```text
browser → HTTPS :443 → Caddy → 127.0.0.1:9001 → SSH → your app :8080
```

## Repository layout

```text
run_server.sh          # server entry point: start/status/stop/restart
server.py              # Caddy supervision and automatic route reaping
caddy_config.json      # base Caddy configuration
tunnel.py              # server-side process for each SSH tunnel
tunnel_common.py       # shared API calls, route checks and process locks
tunnel_cleanup.py      # route cleanup used by server.py; inspection commands
install.sh             # one-time Caddy installation
tests/                 # route safety and server recovery regression tests
README.md              # setup and operation
LICENSE                # project license
```

`server.py`, `tunnel_common.py` and `tunnel_cleanup.py` are required by
`run_server.sh`. Keep them together with `tunnel.py` in the same directory.
The downloaded `caddy` binary and `.runtime/` are local files excluded from Git.

## Run the server

Python 3.8+ and Caddy are required. Install Caddy once, then use the same entry
point for all server operations:

```bash
./install.sh
./run_server.sh
```

`install.sh` defaults to Caddy 2.11.4, verifies the release checksum, and installs
the binary atomically. On Linux it gives the binary permission to bind ports
80/443 using `setcap`. `CADDY_VERSION` can select another release.

`run_server.sh` starts a **detached supervisor** and returns once Caddy's admin
API is ready. Closing the terminal or SSH session does not stop it. Starting it
again reports the existing instance; it does not launch another Caddy/reaper.

```bash
./run_server.sh status       # supervisor/Caddy PID, readiness, restart count
./run_server.sh stop         # stop reaping, then gracefully stop Caddy
./run_server.sh restart      # stop and start through the same entry point
./run_server.sh --foreground # run in this terminal; Ctrl+C stops the server

tail -F .runtime/server.log
```

The supervisor:

- Restarts Caddy after an exit, with a retry delay increasing from 1 to 30 seconds.
- Checks the admin API every 10 seconds, with a 3-second request timeout. Six
  consecutive failures trigger a check of the HTTPS listener. If it completes
  a TLS handshake and returns an HTTP response, Caddy keeps serving and the
  restart is deferred. If both checks fail, Caddy is restarted. A listening TCP
  port alone does not count as a healthy server. During an admin-only outage,
  `status` reports `ready: false` and explains the deferred restart in `error`;
  route reaping pauses until the API recovers.
- Runs one reaper every 30 seconds, removing routes only after three consecutive
  failed port checks and a final check immediately before removal.
- Restores Caddy's saved dynamic configuration on restart, using `--resume`.
  A changed base `caddy_config.json` takes precedence over saved configuration.
  Invalid saved configuration falls back to the base file; live tunnel clients
  then register their routes again.
- Captures Caddy, watchdog and reaper messages in `.runtime/server.log`, rotating
  at 10 MiB and keeping five backups.

Runtime files and autosave are in `.runtime/` and ignored by Git. Caddy's existing
certificate data directory is preserved. Use the same Unix user and working
copy to start and stop the server. `.runtime` contains a control socket restricted
to that user.

This handles Caddy failures while the supervisor is running. Host reboot, loss
of the VPS/network, or termination of the supervisor itself still requires the
host's boot/process manager to invoke `run_server.sh` again. Restarting Caddy also
interrupts existing connections briefly; this is a single-server setup.

### Moving from the previous version

1. Stop the Caddy process launched by the old `run_server.sh` before starting
   the new version. The supervisor refuses to compete with an existing admin API.
2. If the separate reaper service was installed, disable it:

   ```bash
   sudo systemctl disable --now sirtunnel-reaper
   ```

3. Deploy **all four Python files together**: `server.py`, `tunnel.py`,
   `tunnel_cleanup.py`, `tunnel_common.py`, plus `run_server.sh` and the JSON config.
4. Start `./run_server.sh`. Reconnect existing SSH clients so they load the new
   route locking and port leases. Existing Python processes keep their old code.

Deploy clients gradually if needed, but old clients do not participate in the
shared lock until restarted. Mixing old and new route writers retains the old
race conditions during migration.

## Connect a service

Use one unique hostname and one unique remote port per service:

```bash
AUTOSSH_GATETIME=0 autossh -M 0 -tt \
    -o ServerAliveInterval=20 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -R 9001:127.0.0.1:8080 \
    linuxuser@example.com \
    /home/linuxuser/SirTunnel/tunnel.py sub1.example.com 9001
```

The server-side `tunnel_common.py` must be beside `tunnel.py`. Start the app and
confirm its HTTP readiness before starting autossh. In a container, supervise
both processes and configure the container's restart policy: the app continuing
to run after autossh exits does not restore its public endpoint.

| Setting | Purpose |
| --- | --- |
| `-tt` | Allocate a PTY even without local stdin, so SSH session loss delivers SIGHUP to the remote process. |
| `ExitOnForwardFailure=yes` | Fail when the remote port cannot bind, rather than leave a session with no forward. |
| `ServerAliveInterval=20`, `ServerAliveCountMax=3` | Detect an unresponsive server and keep idle connections active. |
| `AUTOSSH_GATETIME=0` | Keep retrying even when the first attempt fails quickly. |

An unhealthy remote tunnel exits with a failure code. A successful remote command
exit would also make autossh exit instead of reconnecting. Keep autossh running
on the **client**; the Caddy supervisor cannot create an SSH connection from a
client that has stopped.

### Release dead SSH sessions on the VPS

Set the following in the server's `sshd_config` (or an included config file),
validate with `sudo sshd -t`, and reload sshd using the host's service manager:

```text
ClientAliveInterval 30
ClientAliveCountMax 3
```

These probes let sshd disconnect unreachable clients and release their remote
ports. A listening SSH port alone does not prove that the app behind it is ready.
The reaper cannot replace sshd's connection checks. See the
[OpenSSH settings](https://man.openbsd.org/sshd_config#ClientAliveInterval).

## Route safety and recovery

All cooperating route writers use the same file lock at
`/var/tmp/sirtunnel-routes.lock`. Caddy ETags also protect updates against changes
made outside these scripts. A failed API read, invalid JSON or failed lock
acquisition aborts the operation; it is never treated as an empty route list.

Registration replaces stale routes and adds the new route in one PATCH. A live
hostname on a different remote port rejects the new claim. A port lease prevents
two current tunnel processes from using the same remote port. Reusing a port
removes its previous hostname's tunnel route, preventing the previous domain
from exposing a different service. Routes outside SirTunnel are preserved.

Tunnel clients check their own route and SSH listener roughly every 5–7 seconds.
They restore missing or damaged routes, leave routes untouched on ambiguous API
errors, and exit after three consecutive missing-listener checks. Shutdown only
removes routes whose ID, owner, hostname and port all match the exiting process.
The reaper rechecks ownership after probes; new sessions do not inherit an old
session's failed-probe counts.

An unchanged registration is not written again. New routes use a five-minute
`stream_close_delay`, so adding or removing another tunnel does not immediately
close existing WebSockets during a Caddy configuration reload. This is a bounded
grace period: an upgraded connection still using the old configuration can close
after five minutes, and a Caddy process restart interrupts it. WebSocket clients
should reconnect. Old Caddy versions that reject this option still register
routes but log a warning; run `./install.sh` to get the supported version. Existing
SSH sessions must reconnect to load the new route option. See
[Caddy's streaming connection behavior](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy#streaming).

The remote tunnel process exits with a nonzero status on SIGHUP/SIGTERM/SIGINT,
so `autossh` can reconnect after a remote termination rather than treating the
exit as a successful end of the session. The client must still run `autossh`
with the options shown above.

The `sirtunnel` server listens on HTTPS port 443 only. Caddy creates the HTTP
redirect server when a hostname is registered. See
[Caddy's automatic HTTPS behavior](https://caddyserver.com/docs/automatic-https).

## Inspect and clean routes

```bash
./tunnel_cleanup.py list
./tunnel_cleanup.py dedupe -n
./tunnel_cleanup.py delete sub1.example.com-9001
./tunnel_cleanup.py cleanup          # asks before removing all SirTunnel routes
```

`run_server.sh` runs the reaper automatically and owns its process lock. No
separate reaper service or watcher is needed. The commands above inspect routes
or perform explicit maintenance; they do not start the server. Dry-run does not
change Caddy configuration.

## Optional environment settings

| Variable | Default |
| --- | --- |
| `SIRTUNNEL_RUNTIME_DIR` | `.runtime` beside the scripts |
| `SIRTUNNEL_CADDY_BIN` | `caddy` beside the scripts |
| `SIRTUNNEL_CADDY_CONFIG` | `caddy_config.json` beside the scripts |
| `SIRTUNNEL_CADDY_API` | `http://127.0.0.1:2019` (supervisor; clients can pass the API URL in Python) |
| `SIRTUNNEL_LOCK_FILE` | `/var/tmp/sirtunnel-routes.lock` |
| `SIRTUNNEL_HEALTH_INTERVAL` | `10` seconds |
| `SIRTUNNEL_HEALTH_FAILURES` | `6` |
| `SIRTUNNEL_REAPER_INTERVAL` | `30` seconds |
| `SIRTUNNEL_REAPER_STRIKES` | `3` |
| `SIRTUNNEL_START_TIMEOUT` | `30` seconds |

Every route writer must use the same `SIRTUNNEL_LOCK_FILE`; changing it for just
one process defeats synchronization. If using another admin address, update
Caddy's `admin.listen` and give that address to the tunnel clients/cleanup utility
as well. The public listeners must match your firewall and DNS configuration.

## Verification

```bash
python3 -m unittest discover -s tests -v
SIRTUNNEL_TEST_CADDY_BIN=/absolute/path/to/caddy \
    python3 -m unittest discover -s tests -v
```

The first command checks concurrency, owner-safe cleanup, API failures, reused
ports and reaper state. The second additionally uses a real Caddy instance on
isolated high ports to check HTTPS forwarding, HTTP redirects, detached startup,
duplicate starts, SIGHUP, SIGKILL recovery with saved routes, watchdog recovery
from SIGSTOP, WebSocket continuity across route registration/removal, an admin-only
failure with HTTPS still serving, and shutdown. It needs `openssl` and does not
touch production listeners or certificate storage.

Upstream project: https://github.com/anderspitman/SirTunnel
