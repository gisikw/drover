# Operating the MVP

## Build and trust model

`nix build` builds Drover and the **exact Herdr 0.9.0 source revision** in
`flake.lock` (Linux and Darwin). `nix develop -c python -m unittest -v` runs tests.
`nix run -- --help` shows the CLI. The flake wrapper supplies an absolute
`DROVER_HERDR`; development invocation must explicitly set this to a built 0.9
binary. Ambient Herdr (including 0.8) is never selected. Python/aiohttp, OpenSSH,
Herdr's Rust/Zig toolchains and transitive sources are locked by the flakes.

This is a **single trusted operator** service, not an internet multi-tenant
relay. Enrollment and client capabilities are separate random bearer secrets.
Generate each with `openssl rand -hex 32`; export `DROVER_ENROLLMENT_TOKEN` and
`DROVER_CLIENT_TOKEN` from a mode-0600 file outside the repository. Never put
secrets in Nix expressions (the store is public). TLS uses system trust or the
explicit `ca_file` PEM trust root; there is no insecure TLS switch. Plain HTTP
is accepted only for loopback development. Private coordinator state and node
identity files must be backed up securely, not checked into Git.

The SQLite registry persists immutable machine names (the MVP's stable IDs),
credential hashes, namespace metadata and never-reused loopback route ports.
Only one coordinator process may own a database. Run it under a dedicated OS
account with a private state directory. The node saves its credential atomically
with mode 0600. If enrollment succeeds but its response/identity is lost, revoke
that registration explicitly before re-enrolling; do not retry mutations blindly.

## Coordinator: independent TLS/control and OpenSSH listeners

Private `coordinator.json`:

```json
{
  "listen": "0.0.0.0:9840",
  "state": "/var/lib/drover/registry",
  "tls_cert": "/run/secrets/drover-cert.pem",
  "tls_key": "/run/secrets/drover-key.pem",
  "first_port": 24000
}
```

```sh
nix run -- --config /etc/drover/coordinator.json coordinator
```

Default control listen is `127.0.0.1:9840`. SSH is separately configured via
`ssh_config.py`, default `127.0.0.1:9841`. Neither channel relies on 443. Open
**only the selected coordinator ports** in the relevant host firewall. Do not
open the reverse-port range, node SSH port, or Herdr socket to the network.
Ordinary TLS termination by a reverse proxy is also supported: bind Drover's
HTTP listener to loopback, preserve WebSocket upgrades and Authorization, and
set a proxy idle timeout longer than the 15-second heartbeat.

## Node: one OS identity, one native Herdr namespace

Provision a dedicated OS identity such as `drover`, conventional HOME, a pinned
Herdr in that identity's `~/.local/bin/herdr`, and a loopback-only OpenSSH daemon.
The binary symlink should point into the pinned Nix store, not an updater or
ambient shell executable. macOS can run its system `/usr/sbin/sshd` with a
separate configuration; no change to Remote Login's public binding is required.
Linux uses system OpenSSH `sshd` in the same way. Operator-admin access used to
bootstrap accounts is separate from Drover's actual route.

The local sshd should have at least:

```text
ListenAddress 127.0.0.1
Port 22222
HostKey /var/lib/drover-node/ssh_host_ed25519_key
AuthorizedKeysFile .ssh/drover_authorized_keys
AllowUsers drover
PasswordAuthentication no
KbdInteractiveAuthentication no
AllowTcpForwarding no
AllowStreamLocalForwarding no
AllowAgentForwarding no
X11Forwarding no
```

Use normal OS-appropriate PAM/account settings. The inner endpoint permits
normal Herdr remote command execution under this dedicated identity; it is not
a forced-command pane attachment server. Authorize only the operator's public
key. Create host keys with `ssh-keygen`, transfer their public halves through
an authenticated administrative channel, and verify fingerprints out of band.
Never use `StrictHostKeyChecking=no` or unauthenticated keyscan as deployment
trust establishment.

Private node config:

```json
{
  "url": "https://coordinator.example:9840",
  "name": "machine-a",
  "ssh_user": "drover",
  "session": "drover",
  "host_key": "COPY THE NODE OPENSSH ED25519 PUBLIC HOST KEY HERE",
  "socket": "/home/drover/.config/herdr/sessions/drover/herdr.sock",
  "identity_file": "/home/drover/.local/state/drover/identity.json",
  "ssh_config": "/home/drover/.ssh/drover-tunnel.conf",
  "tunnel_alias": "drover-coordinator-tunnel",
  "local_ssh_port": 22222
}
```

On macOS the socket is `/Users/drover/.config/herdr/sessions/drover/herdr.sock`.
`serve` checks that the SSH username, OS account, HOME, and conventional session
socket agree. Do not supply XDG/HERDR socket overrides or run in a human's
ambient Herdr environment. On first start it delegates daemon bootstrap to
Herdr's **native `remote-client-bridge` with EOF stdin**, then adopts its socket.
This does not relay terminal data. Herdr owns the daemon and surviving panes;
Drover shutdown does not kill it. An existing incompatible or foreground server
is rejected, never replaced. Herdr 0.9's saved-machine path requires
`detached_server_daemon=true`; foreground `herdr server` reports false. This
concrete integration requirement refines the original generic “start/adopt”
architecture, not the ownership boundary.

Tunnel config (absolute paths recommended):

```sshconfig
Host drover-coordinator-tunnel
  HostName coordinator.example
  Port 9841
  User drover-tunnel
  IdentityFile /home/drover/.ssh/tunnel_ed25519
  UserKnownHostsFile /home/drover/.ssh/coordinator_known_hosts
  IdentitiesOnly yes
  StrictHostKeyChecking yes
```

```sh
nix run -- --config /etc/drover/node.json serve
```

Enrollment may finish before SSH authorization is installed; the outbound
control stream becomes online and SSH retries with jittered 1–30-second backoff.
Install its public tunnel key for the assigned route as described next. The
catalog's `online` means live control connection, **not** verified SSH readiness.

## Constrained rendezvous authorization

Create distinct non-root OS accounts `drover-tunnel` and `drover-jump` on the
coordinator. Accounts must permit public-key login (not OS-locked); disable
password login in sshd. They get no session channels (`MaxSessions 0`), PTYs,
agent forwarding, X11, or tunnels. Use separate keys for each node tunnel.

`ssh_config.py` reads the registry and this private operator config:

```json
{
  "database": "/var/lib/drover/registry/registry.sqlite",
  "output": "/var/lib/drover-ssh",
  "ssh_listen": "0.0.0.0:9841",
  "tunnel_user": "drover-tunnel",
  "jump_user": "drover-jump",
  "tunnel_keys": {"machine-a": "COPY NODE TUNNEL PUBLIC KEY HERE"},
  "client_key": "COPY OPERATOR PUBLIC KEY HERE"
}
```

```sh
./result/bin/drover-ssh-config /etc/drover/ssh.json
# Install output under root ownership. All parent directories must be trusted,
# not /tmp. Root-owned directory 0755 and public *_keys files 0644 allow sshd's
# privilege-separated account to read them. Keep private host_key at 0600.
# Create /var/lib/drover-ssh/host_key with ssh-keygen -t ed25519.
sshd -t -f /var/lib/drover-ssh/sshd_config
sshd -D -e -f /var/lib/drover-ssh/sshd_config
```

The node key gets `restrict,port-forwarding,permitlisten="127.0.0.1:PORT"` and
its user can forward **remote only**. The jump key gets explicit `permitopen`
entries and its user can forward **local only**. `GatewayPorts no` is mandatory.
Generated public files must not be writable by either SSH account. Render again
when adding/revoking machines. This deliberate operator installation step avoids
making the web coordinator a root-capable OS-account/sshd manager in the MVP.

## Discovery, RPC and native client

Basic client config: `{"url":"https://coordinator.example:9840"}` (optionally
`"ca_file":"/path/to/private-ca.pem"`). With `DROVER_CLIENT_TOKEN` set:

```sh
nix run -- --config /etc/drover/client.json list
nix run -- --config /etc/drover/client.json rpc machine-a ping
nix run -- --config /etc/drover/client.json rpc machine-a workspace.list
nix run -- --config /etc/drover/client.json rpc machine-a agent.prompt '{"target":"w1:p1","text":"Review changes"}'
```

Parameters are Herdr's native JSON API parameters; consult the **pinned**
`herdr api schema --json`. Drover's allowlist includes agent start/prompt/read,
workspace/tab/pane inspection, and ping/snapshot. It also forwards the existing
Herdr 0.9.0/protocol 22 `workspace.create`, `workspace.close`, `agent.send_keys`,
`pane.send_input`, and `pane.process_info` APIs. Schemas are pinned in upstream
`src/api/schema/{agents,workspaces,panes}.rs` at
`b99002ac99b09e00b4ca692436cb15a6b0d676f1`. It excludes server stop, plugin
installation, event subscriptions, and worktree operations.

**This allowlist is not a shell sandbox.** Workspace environment and terminal
input, as well as the native SSH endpoint, grant arbitrary execution at the
effective authority of the enrolled Unix account, including its existing sudo,
container, filesystem and network powers. No Familiar job semantics, settlement,
ledger, or callback route are added to Drover.
RPC callers may pin the catalog enrollment generation with the HTTP header
`If-Match: "<port>"`, where `port` is the never-reused port from enrollment.
The coordinator verifies that the registry row still matches the authenticated
live WebSocket and checks this precondition before forwarding. Mismatch returns
412 without delivery; successful replies acknowledge the same value in `ETag`.
Clients requiring identity fencing must require that acknowledgment on their
read-only ping before sending mutations (old coordinators lack it). Revocation
and re-enrollment of a name cannot redirect a fenced request to the replacement.
This is generic machine routing, not an agent-job protocol. Header-less legacy
clients retain their existing behavior.

Replies preserve Herdr's structured result/error. Frames/bodies are bounded at
1 MiB; at most 128 correlated requests may be outstanding. RPC timeout is 30
seconds; disconnect/timeout means **outcome unknown**, never automatic retry,
completion or settlement. Node requests execute serially; this is not a job queue.

For `client`, add these paths to its config:

```json
{
  "url": "https://coordinator.example:9840",
  "client_home": "/home/operator/.local/state/drover/client-home",
  "jump_config": "/home/operator/.ssh/drover-jump.conf",
  "jump_alias": "drover-jump",
  "identity_key": "/home/operator/.ssh/operator_ed25519",
  "known_hosts": "/home/operator/.ssh/drover_known_hosts",
  "generated_ssh_config": "/home/operator/.ssh/drover-machines.conf"
}
```

Configure `drover-jump` like the tunnel alias, but with `User drover-jump` and
the operator key. **Include the generated machines config in the real OS
account's `~/.ssh/config` as well**:

```sshconfig
Include /home/operator/.ssh/drover-machines.conf
```

Herdr 0.9 uses its managed `-F` config for bootstrap, but some background endpoint
health probes use ordinary SSH defaults (the passwd account home). Both must
resolve the same aliases. Drover creates the separate `client_home` config for
Herdr and clears inherited HERDR/XDG variables, preventing accidental contact
with a caller's ambient 0.8/golem namespace.

`drover ... client` writes aliases/pinned host keys, adds missing online profiles,
and starts native Herdr. The MVP reconciles on invocation, **not continuously**;
restart the client command after catalog changes. Existing Herdr connections
reconnect independently. Registration of a saved profile is noninteractive:
Drover never approves a remote upgrade or server replacement. The UI remains
unmodified Herdr, including onboarding. Paths in config should be absolute and
without whitespace. Generated files and client_home are Drover-owned, not shared
with another application.

## Lifecycle, revocation, limitations

WebSocket ping/pong provides leases (15-second heartbeat); disconnection makes a
machine offline and fails outstanding RPC with unknown outcome. Both transports
reconnect. Revoked credentials (401/403) stop the node rather than retry forever.
SIGINT cancels tunnel supervision; SIGTERM is handled by the CLI. Herdr survives.
For service use, run under a supervisor that kills the entire Drover process
group, not unrelated Herdr processes.

`DELETE /v1/machines/NAME` with the client bearer revokes control access and closes
the stream. **SSH revocation is separate:** regenerate keys and terminate that
node's existing tunnel connections and affected jump connections. Removing a
key or reloading sshd alone does not invalidate an already authenticated session.
Rotate enrollment/client bearer secrets via the process environment and restart;
per-machine rotation is explicit revoke/re-enroll in this MVP (new route, no
reuse). Reverify host keys when identities change. Clients must reconcile/drop
stale saved profiles explicitly. There is no per-user ACL, automatic SSH key
issuance, managed service units, durable RPC, or fleet-wide settlement semantics.

Protect the coordinator as a trust root. A compromised coordinator can change
the catalog's host key pins; TLS identity, enrollment-secret control and careful
operator installation of SSH keys are essential. A compromised enrolled node
cannot claim another name without that machine's token or bind another SSH route.
The single client credential intentionally grants full allowlisted fleet RPC.
