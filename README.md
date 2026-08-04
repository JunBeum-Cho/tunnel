# SirTunnel

Expose a webserver running on your machine (or in a container) at a public
HTTPS URL, over a plain SSH remote forward. Caddy terminates TLS and gets the
certificate; SSH carries the traffic; `tunnel.py` is the small piece that tells
Caddy where to send it.

```
browser --https--> Caddy :443 --> 127.0.0.1:9001 --ssh--> your app :8080
                     ^
                     |  tunnel.py adds/removes this route via the admin API
```


# Running the server

```bash
./install.sh      # downloads Caddy, lets it bind :80/:443
./run_server.sh   # caddy run --config caddy_config.json
```

Caddy needs to bind port 443, either by running as root (not recommended), by
setting `CAP_NET_BIND_SERVICE` on the binary (what `install.sh` does), or by
changing `caddy_config.json` to a high port and forwarding to it.

Put `tunnel.py` somewhere clients can run it (the SSH command below uses
`/home/linuxuser/SirTunnel/tunnel.py`), and run one reaper — see
[Keeping tunnels up](#keeping-tunnels-up).


# Connecting a tunnel

The client opens a remote forward and then runs `tunnel.py` on the server with
the public hostname and the forwarded port:

```bash
AUTOSSH_GATETIME=0 autossh -M 0 -tt \
    -o ServerAliveInterval=20 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -R 9001:localhost:8080 \
    linuxuser@example.com \
    /home/linuxuser/SirTunnel/tunnel.py sub1.example.com 9001
```

Requests to `https://sub1.example.com` are now proxied to port 8080 on the
client. Caddy fetches the certificate on the first request.

From a Dockerfile, alongside the app:

```dockerfile
CMD sh -c "AUTOSSH_GATETIME=0 sshpass -p $SSH_PASSWORD autossh -M 0 -tt \
    -o StrictHostKeyChecking=no \
    -o UserKnownHostsFile=/dev/null \
    -o ServerAliveInterval=30 \
    -o ServerAliveCountMax=3 \
    -o ExitOnForwardFailure=yes \
    -R $SSH_PORT:localhost:$PORT \
    linuxuser@example.com \
    /home/linuxuser/SirTunnel/tunnel.py $SERVER_ENDPOINT $SSH_PORT & \
    doppler run -- bun run src/index.ts"
```

Every option there is load bearing:

| option | why |
| --- | --- |
| `-tt` | Forces a PTY even though the container has no terminal. Without it the server keeps running `tunnel.py` after the SSH connection dies, and its route lingers as a zombie. `-t` alone is **not** enough: it silently gives up when stdin is not a terminal. |
| `ExitOnForwardFailure=yes` | If a stale sshd still holds the remote port, the session would otherwise come up with no working forward — connected in appearance, dead in practice. |
| `ServerAliveInterval` / `CountMax` | Notices a peer that died silently (NAT state expiring, ISP handing out a new address) within ~60-90s, and keeps NAT state warm so an idle tunnel is not dropped in the first place. |
| `AUTOSSH_GATETIME=0` | autossh's default is 30: if the first connection dies within 30 seconds it gives up **permanently**. That is exactly what a container starting before the network is ready looks like. |

`StrictHostKeyChecking=no` with `UserKnownHostsFile=/dev/null` accepts any host
key, so the tunnel's contents are only as safe as the network path. Pinning the
server's key (and using a key instead of `sshpass`) closes that.


# Keeping tunnels up

A tunnel that has been up for days rarely fails because of the tunnel: the SSH
connection underneath dies quietly, nothing notices, and nothing restarts it.
The client options above cover the client side. Two things remain on the server.

**Let sshd reap dead sessions**, so a stale sshd does not sit on port `900x` and
block the reconnect. In `/etc/ssh/sshd_config`:

```
ClientAliveInterval 30
ClientAliveCountMax 3
```

Without this, sshd falls back to OS-level TCP keepalive and can hold the
forwarded port for a couple of hours.

**Run exactly one reaper**, which removes routes whose upstream port has been
unreachable for several consecutive probes:

```bash
sudo cp tunnel_cleanup.py /usr/local/bin/tunnel_cleanup.py
sudo chmod +x /usr/local/bin/tunnel_cleanup.py
sudo cp systemd/sirtunnel-reaper.service /etc/systemd/system/
sudo systemctl enable --now sirtunnel-reaper
```

One per server, not one per tunnel: this used to run inside every client, which
meant every client probed and deleted every other client's routes, and a single
slow probe could take down someone else's healthy tunnel.


# Debugging

```bash
tunnel_cleanup.py list          # routes, with dead ports and duplicate IDs flagged
tunnel_cleanup.py dedupe -n     # show routes sharing an @id, change nothing
tunnel_cleanup.py reap -n       # show what the reaper would remove
tunnel_cleanup.py delete <id>   # remove one route
tunnel_cleanup.py cleanup       # remove all routes
```

Things worth knowing when reading logs:

* Caddy reports an unknown `@id` as `500 unknown object ID`, not `404`. Such a
  line usually means "already gone", not "broken".
* `Nothing is listening on port N` at startup means the `-R` forward never came
  up. Check that the port in `-R` and the port passed to `tunnel.py` match.
* `Port N is not listening, leaving the route absent` means the forward went
  away underneath a still-running client. It deliberately does not re-create
  the route: a route pointing at a dead port only serves 502s.
* Routes are matched in order, so a stale route for a hostname shadows a newer
  one. `dedupe` collapses duplicates, keeping the most recent.


# How it works

`ssh -R 9001:localhost:8080` makes sshd listen on `127.0.0.1:9001` on the
server and forward anything arriving there to port 8080 on the client. The
`tunnel.py sub1.example.com 9001` part runs on the server and `PATCH`es a route
into Caddy's admin API (`127.0.0.1:2019`) that reverse-proxies
`sub1.example.com` to `:9001`, tagged with an `@id` of `sub1.example.com-9001`.

While it runs, it re-checks every ~5s that its route is still there and still
belongs to it, and restores it if Caddy lost it. When the SSH session ends the
process gets SIGHUP and deletes its route — unless a newer session has taken
the hostname over in the meantime, in which case it leaves that one alone.

Upstream project: https://github.com/anderspitman/SirTunnel
