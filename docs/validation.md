# MVP validation record — 2026-09-09 UTC

This records actual cross-host execution, not a proposed test plan. No passwords,
private keys, bearer tokens, machine credential hashes, raw catalog host keys,
or runtime state are included here.

## Platforms, builds and topology

- **Azula:** x86_64 Linux/NixOS, coordinator and native client.
- **O’Brien:** Apple Silicon Mac mini, Darwin 25.2.0, outbound Drover node.
- Both built Herdr **0.9.0** from
  `herdrdev/herdr@b99002ac99b09e00b4ca692436cb15a6b0d676f1`, through the pinned
  Nix inputs; both also built the Drover package. No ambient 0.8 binary was used
  for delegated invocations.
- `nix build` and `nix flake check` passed on x86_64-linux and aarch64-darwin.
  **11 Python unit/integration tests** cover authentication, registration
  collision/validation, persistent identity/route allocation, control-stream
  correlation, disconnect/unknown-outcome semantics, revocation, forbidden RPC,
  real local Unix-socket framing, pinned-version rejection, atomic private files,
  and constrained OpenSSH configuration generation.
- Azula listened on **TCP 9840 (TLS/control)** and **TCP 9841 (system OpenSSH)**.
  The test used the hosts’ actual LAN route. Public DNS for Azula initially
  resolved to a beacon rather than Azula, so the test selected Azula's LAN
  address and issued a short-lived test certificate with the correct IP SAN.
  O’Brien explicitly trusted that certificate via `ca_file`; TLS verification
  was never disabled. This was ordinary HTTPS/WSS, not HTTP over SSH.
- O’Brien’s separate system sshd listened only on **127.0.0.1:22222**.
  Its outbound system `ssh -R` established **127.0.0.1:24000 on Azula**.
  Azula’s client connected through its constrained jump identity and that route;
  native client traffic did not connect directly to O’Brien's admin SSH port.
- Test bootstrap used `admin@obrien.fort.gisi.network`, separate from the
  loopback node endpoint. The test deliberately used a new named session under
  the existing admin account; the deployment default is a dedicated OS identity.
  No human's default Herdr session was adopted or modified.

## Registration and real structured RPC

O’Brien’s packaged `drover ... serve` logged:

```text
Herdr namespace probe: ... "type": "pong", "version": "0.9.0", "protocol": 22 ...
registered/control connected: obrien-mvp route: 24000
```

Azula’s authenticated `drover ... list` returned machine `obrien-mvp`,
`online: true`, `port: 24000`, SSH user `admin`, session `drover-mvp`.
No machine credential was present in the catalog.

Azula ran the packaged CLI:

```sh
drover --config /private/client.json rpc obrien-mvp ping
drover --config /private/client.json rpc obrien-mvp workspace.list
```

The correlated reply from the actual Mac Unix API included:

```json
{
  "result": {
    "type": "pong",
    "version": "0.9.0",
    "protocol": 22,
    "capabilities": {
      "live_handoff": true,
      "detached_server_daemon": true,
      "endpoint_protocol_generation": 1,
      "surface_interest": true,
      "health_check": true
    }
  }
}
```

`workspace.list` returned native `workspace_list` with workspace `w1`, active
tab `w1:t1`, one pane and one tab. This request traversed Azula's HTTPS handler,
O’Brien's existing outbound WebSocket, and O’Brien's local Unix socket. There
was no node HTTP/RPC listener and no shell-command RPC adapter.

## SSH, namespace identity and native Herdr

Through the real reverse-route/ProxyJump alias, the pinned remote invocation
`herdr --session drover-mvp status server --json` returned:

```json
{
  "status": "running",
  "running": true,
  "version": "0.9.0",
  "protocol": 22,
  "compatible": true,
  "socket": "/Users/admin/.config/herdr/sessions/drover-mvp/herdr.sock",
  "session": "drover-mvp",
  "restart_needed": false,
  "server_binary_stale": false
}
```

That socket is **exactly** the socket configured in the Drover node, not a
second HOME/XDG namespace. The API’s version/capability response matched it.

Native Herdr on Azula successfully executed:

```sh
herdr machine add drover-obrien-mvp --label obrien-mvp --remote-session drover-mvp
```

It reported `Saved SSH machine … Remote server is ready.` and its native
`machine list --json` showed the enabled target/session pair.

The packaged `drover ... client` was then run in a real pseudo-terminal
(140 columns × 40 rows). It stayed alive and emitted **19,877 bytes** of native
terminal rendering during a 10-second sample. Herdr's client log recorded two
successful endpoint handshakes at **04:40:44 and 04:40:45 UTC**, both
`generation=1 server_version=0.9.0` (local and saved remote endpoint). Earlier
native-only client samples also completed both handshakes. The test exercised
native bootstrap, saved profiles, endpoint attachment and terminal rendering;
it was not a human visual/UI acceptance test or a model/agent workload test.

Two concrete integration findings were fixed/documented:

1. A foreground `herdr server` reports `detached_server_daemon=false`; native
   saved-machine preparation rejects it. The empty test server was explicitly
   stopped once and started through Herdr's native detached bootstrap. Drover
   now uses that bootstrap only when its socket is absent, otherwise probes
   and refuses incompatible adoption. It never silently replaces live servers.
2. Herdr’s background health probes sometimes use ordinary SSH defaults rather
   than its managed `-F` file. The real OS account's SSH config must also include
   Drover's generated aliases. The client’s Herdr HOME remains separate, with
   inherited HERDR/XDG variables removed to avoid the harness’s ambient 0.8
   namespace.

## Failure and authorization tests

- Stopped/restarted the real Azula coordinator and killed the tunnel-side SSH
  connection. Within the bounded reconnect window, the existing O’Brien node
  reclaimed **the same identity and route 24000**. Both the structured ping and
  SSH status query succeeded again; Herdr retained the same socket/session.
- A jump-identity shell command failed with exit **255**, session channel refused.
- Jump forwarding to unassigned `127.0.0.1:22` failed with exit **255** and
  `administratively prohibited`.
- O’Brien’s tunnel key attempting reverse listener `127.0.0.1:24001` failed with
  exit **255**, `remote port forwarding failed`.
- `ss` on Azula showed public/control listeners 9840/9841 but reverse listener
  **127.0.0.1:24000 only**. Root `lsof` on O’Brien confirmed its node sshd bound
  **127.0.0.1:22222 only**.
- Final generated configuration additionally disables Unix-stream forwarding
  and user RC execution. `sshd -t` passed; `sshd -T` for the tunnel identity
  confirmed `maxsessions 0`, `allowtcpforwarding remote`,
  `allowstreamlocalforwarding no`, `permitopen none`, `permituserrc no`.

The other coordinator/node direction was not tested; the required O’Brien →
Azula direction was proven. No agent prompt/start with a live model was issued.

## Fort firewall change and deployment

Fort Nix commit **`0f7474da`**, pushed to its normal `origin/main`, changes only:

```text
clusters/bedlam/hosts/azula/manifest.nix
config.networking.firewall.allowedTCPPorts = [ 9840 9841 ];
```

Host evaluation returned `[22,80,443,9840,9841]`: the first three were pre-existing
services, not Drover requirements. Only Azula changed. No O’Brien firewall
opening, route range, node listener exposure, or managed Drover service unit was
added. Matching narrow live firewall rules initially enabled the experiment.

After the push, Fort's normal GitOps agent fetched `0f7474da` at **04:44:27 UTC**,
activated it at **04:44:35**, reloaded `firewall.service` at **04:44:36**, and
completed successfully at **04:44:37**. The live firewall then contained the
normal generated accept rules for 9840/9841. Thus the final firewall change was
actually deployed, not only evaluated. NixOS activation also removed the
undeclared temporary SSH accounts, illustrating why production deployments must
provision identities declaratively even while Drover service units are deferred.

## Secret scans and explicit Fort-only exception

Drover's tracked files/index/staged diff and **all reachable history blobs** were
checked against an external private denylist containing the supplied bootstrap
password, generated API/node tokens, and generated private-key body material.
Gitleaks scans covered its working tree and full history. No credentials or
host-specific runtime state were added to Drover.

Fort Nix's current tracked files, index and new outgoing commit range passed
exact credential checks; Gitleaks found no leaks in the new firewall commit.
However, its **pre-existing history** contains the supplied O’Brien bootstrap
password in old `BRIEF.md` revisions (including history around `67eded8`;
subsequently removed by `a23735f`). No password is reproduced here.

The operator explicitly approved a **Fort-only pre-existing-history exception**
and normal push of the clean firewall change. Shared history was not rewritten.
**Rotate that exposed password** as the actual remediation. Any history cleanup
must be coordinated and is optional follow-up: rewriting alone does not revoke
credentials or erase existing clones. No such exception applies to Drover.

## Cleanup and deployment follow-up

- Stopped both temporary Drover processes, Azula's test rendezvous sshd and
  O’Brien's loopback-only test sshd; closed reverse forwarding.
- Stopped only the test-owned Herdr session/client daemon; did not kill ambient
  Herdr, Golem, agents or unrelated SSH services.
- Removed O’Brien's temporary authorized key and node key file, test session,
  runtime secrets, source checkout, package links and temporary Herdr symlink.
  Nix store build outputs remain available for normal garbage collection.
- Removed Azula's temporary SSH include, rendezvous state/host key and test
  service accounts (the latter had already been removed by Fort activation).
- Confirmed Azula no longer had listeners on 9840, 9841 or 24000 after cleanup.
- The intentional, now-deployed **Azula 9840/9841 firewall openings remain**.
  They have no Drover listener until deployment. Remove the five-line Fort change
  if these ports are no longer wanted.
- Production needs dedicated declared identities, persistent private state,
  deployment TLS/SSH keys, operator-installed forwarding authorization and a
  supervisor. Managed Drover units were intentionally deferred.

## Honest MVP limits

Single operator; shared fleet client credential; manual SSH authorization and
revocation; no automatic machine-key rotation; launch-time rather than live
catalog reconciliation. `online` means the control stream is alive, not that
SSH attachment has passed a probe. RPC is bounded/serial at the node and has no
retry/exactly-once contract. No multi-tenant policy, durable job protocol, visual
UI acceptance suite, or reverse-direction cross-host proof is claimed.
