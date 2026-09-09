# Drover

**Working single-operator MVP:** [build, configuration, deployment and security](docs/operations.md) · [real Azula/O’Brien validation](docs/validation.md).

```sh
nix build
nix develop -c python -m unittest -v
nix run -- --config /etc/drover/coordinator.json coordinator
nix run -- --config /etc/drover/node.json serve
nix run -- --config /etc/drover/client.json list
nix run -- --config /etc/drover/client.json rpc machine-a ping
nix run -- --config /etc/drover/client.json client
```

The sections below define the architecture; the operations guide documents the
implemented JSON configuration and refined command names. The control listener
(default `127.0.0.1:9840`) and system OpenSSH rendezvous listener (default
`127.0.0.1:9841`) are independently configurable. No transport requires 443.

Drover is a self-hosted coordination plane for a fleet of [Herdr](https://herdr.dev) servers.

It lets machines register themselves with one coordinator, makes outbound-only machines addressable through stable reverse-SSH routes and structured RPC, and starts a native Herdr client connected to every authorized machine. The coordinator is both the registry and the rendezvous/jumpbox; clients and servers require no direct route to one another.

Drover is not an agent orchestrator. It does not define jobs, settlements, artifacts, worktrees, or agent lifecycles. Herdr owns agents and terminals. Drover owns machine identity, discovery, authentication, and connectivity.

## Shape

```text
                           Drover coordinator
                    registration · leases · auth
                       discovery · rendezvous
                         ▲                 ▲
          register/lease│                 │discover
                 RPC    │                 │SSH routes
                         │                 │
              ┌──────────┴───┐       ┌─────┴──────────────┐
              │ Drover server│       │ Drover CLI         │
              │ machine A    │       │                    │
              │              │       │ `drover client`    │
              │ Herdr server │◄──────┤ native Herdr 0.9  │
              │ SSH endpoint │       │ multi-machine UI   │
              │ RPC endpoint │       └────────────────────┘
              └──────────────┘
```

Each registered machine keeps its own Herdr server, sessions, panes, agents, credentials, and filesystem. The coordinator does not proxy or reinterpret agent state unless routing requires it. A client discovers machines through Drover and then uses Herdr's native multi-machine interface to interact with them.

## Components

### Coordinator

The coordinator is the fleet directory and trust root.

It owns:

- stable machine IDs and labels;
- machine registration and credential rotation;
- online leases and heartbeats;
- authorization and revocation;
- connection coordinates for SSH and RPC;
- rendezvous and constrained SSH jump access for outbound-only machines;
- bounded machine capability and version metadata.

It does not own:

- agents or prompts;
- terminal contents;
- Golem jobs;
- task settlement;
- artifacts;
- repositories or worktrees;
- model-provider credentials.

A minimal API will resemble:

```text
POST   /v1/machines/register
POST   /v1/machines/{id}/heartbeat
POST   /v1/machines/{id}/rotate
DELETE /v1/machines/{id}
GET    /v1/machines
GET    /v1/machines/{id}
POST   /v1/machines/{id}/rpc
```

The initial routing shape is explicit: each machine receives a stable coordinator-loopback reverse-SSH port, and authorized clients reach it through the coordinator's constrained SSH jump service. A later multiplexed relay may replace port allocation without changing machine identity or the public machine contract.

### Server

A Drover server runs on every participating machine. It:

1. enrolls with a coordinator;
2. persists its machine identity;
3. maintains registration and an online lease over an outbound authenticated control stream;
4. starts or adopts the one Herdr server Drover owns on that machine;
5. maintains an outbound reverse-SSH tunnel to its coordinator-assigned route;
6. exposes a local SSH endpoint through that tunnel for native Herdr clients;
7. exposes structured RPC backed by Herdr's local socket API over the coordinator control stream;
8. reconnects both transports with bounded backoff when necessary.

No participating server requires an inbound public port or a direct route from a client.

The Herdr namespace visible through SSH must be the same namespace the Drover server owns. Remote attachment must never accidentally start a second, empty Herdr server under a different `HOME`, XDG directory, session, or Unix socket.

The clean default is a dedicated OS identity whose conventional Herdr paths are Drover's private Herdr paths. Drover should not adopt or mutate a human user's ambient Herdr session.

### CLI

The CLI addresses machines by stable coordinator name:

```bash
drover azula agent start "Review the current migration and report blockers"
drover frankenstein agent list
drover joker agent prompt reviewer "Check the failing integration test"
drover azula pane read w1:p1 --source recent --lines 80
```

These commands use Drover's RPC channel. The server translates structured requests into the local Herdr socket API and returns Herdr's structured response. Drover does not infer task completion or wrap these calls in a job protocol.

Start the federated terminal client with:

```bash
drover client
```

`drover client`:

1. authenticates to the coordinator;
2. retrieves every machine visible to the caller;
3. reconciles stable SSH aliases and Herdr saved-machine profiles;
4. starts the native Herdr client;
5. delegates independent endpoint reconnects to native Herdr. The MVP reconciles
   registrations at launch; continuous catalog reconciliation remains follow-up.

Herdr owns the resulting multi-machine UI, including the combined agent list, machine-scoped navigation, terminal rendering, notifications, cached offline state, and independent reconnects.

## Example lifecycle

Start a coordinator:

```bash
drover coordinator start \
  --listen 127.0.0.1:9840 \
  --state ~/.local/state/drover-coordinator
```

Enroll and run a machine:

```bash
drover serve https://drover.example \
  --name azula \
  --herdr-session drover
```

On a client:

```bash
drover login https://drover.example
drover client
```

Then use either native Herdr navigation or direct RPC:

```bash
drover azula agent start "Inspect the repository"
```

Command names are provisional; the component boundaries are not.

## Two access channels

Drover deliberately exposes two distinct access channels.

### SSH-compatible interactive access

Native Herdr clients expect ordinary SSH semantics for saved remote machines. Drover therefore uses real OpenSSH on macOS and Linux rather than attempting to reproduce Herdr's private remote bootstrap protocol.

Every server receives a stable reverse-SSH port bound only to coordinator loopback:

```text
azula        → coordinator 127.0.0.1:24017
frankenstein → coordinator 127.0.0.1:24018
joker        → coordinator 127.0.0.1:24019
```

Azula maintains, under Drover supervision:

```bash
ssh -NT \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=15 \
  -o ServerAliveCountMax=3 \
  -R 127.0.0.1:24017:127.0.0.1:22 \
  drover-tunnel@coordinator.example
```

The coordinator tunnel identity may open only its assigned remote listener and receives no shell or PTY. The listener is not publicly reachable.

Clients reach that listener through a separately authenticated, constrained coordinator jump identity:

```sshconfig
Host drover-coordinator
  HostName coordinator.example
  Port 9841
  User drover-client

Host drover-azula
  HostName 127.0.0.1
  Port 24017
  User drover
  ProxyJump drover-coordinator
  HostKeyAlias drover-machine-azula
```

The jump identity may open only `direct-tcpip` channels to registered coordinator-loopback machine ports; it receives no shell or PTY. The inner client-to-machine SSH handshake remains end-to-end through the coordinator.

Herdr can save the stable alias normally:

```bash
herdr machine add drover-azula \
  --label Azula \
  --remote-session drover
```

The outer reverse tunnel authenticates the machine to the coordinator. The client jump connection authenticates access to the rendezvous. The SSH connection carried through both independently authenticates the human client to the machine.

Server machines must provide a localhost SSH endpoint. Linux uses OpenSSH `sshd`; macOS uses its built-in Remote Login/OpenSSH service. Drover does not expose that endpoint directly to the internet.

### Structured RPC

Automation should not parse or interpolate remote shell commands. Drover RPC carries an allowlisted method and structured arguments. Each server's persistent authenticated HTTPS/WebSocket control stream carries registration, leases, heartbeats, and correlated RPC in both directions. No inbound machine RPC port is required. The machine server invokes the corresponding local Herdr CLI or socket operation.

```text
drover azula agent start "prompt"
    ↓ HTTPS
coordinator resolves machine `azula`
    ↓ persistent outbound node control stream
azula's Drover server
    ↓ local Unix socket
Herdr agent.start / agent.prompt
    ↓
Drover returns the native structured result
```

Interactive terminal traffic does not traverse this RPC protocol; it uses the reverse-SSH and jump path above.

RPC request IDs provide correlation. They do not imply durable jobs, exactly-once execution, or semantic settlement. Retry behavior must be explicit per operation.

## Authentication

Registration authentication and interactive SSH authentication are separate concerns.

An initial trusted deployment may use:

- one coordinator enrollment secret;
- a machine-generated persistent keypair;
- a coordinator-assigned stable machine ID and route;
- a machine key authorized only to establish its reverse tunnel;
- an operator SSH key already authorized on participating machines;
- a bearer or client credential for coordinator discovery and RPC.

A hardened deployment may add:

- one-time enrollment tokens;
- short-lived machine certificates;
- renewable tunnel credentials;
- explicit machine and client revocation;
- scoped visibility and RPC permissions;
- key rotation and audit records.

A machine route belongs to the enrolled machine identity. Reconnecting may reclaim it; an unrelated registration may not.

Secrets, private keys, and agent credentials are never returned in the machine catalog.

## Relationship to Herdr

Herdr is the runtime and interface. Drover is connective tissue around multiple Herdr servers.

Drover relies on Herdr for:

- persistent terminal sessions;
- panes, tabs, and workspaces;
- agent startup and prompting;
- agent lifecycle observation;
- terminal input and reads;
- saved-machine federation;
- the combined multi-machine client UI.

Drover adds:

- a shared inventory of machines;
- enrollment and identity;
- automatic Herdr machine-profile reconciliation;
- stable connection routes;
- outbound-only machine reachability through the coordinator jumpbox;
- a machine-addressed structured RPC surface.

Drover should use Herdr's documented CLI and socket APIs. It should not duplicate Herdr's terminal renderer, agent detection, machine sidebar, or reconnect UX.

Herdr versions must be provisioned deliberately. Background client connections must never surprise-upgrade or replace a server that owns running agents.

## Relationship to Golem

Drover and [Golem](https://github.com/gisikw/golem) have different responsibilities.

Golem provides a durable delegated-job protocol:

- dispatch idempotency;
- job states;
- ordered steering and answers;
- harness-specific lifecycle evidence;
- semantic settlement;
- artifacts and worktree facts;
- replayable events and recovery policy.

Drover provides a machine coordination plane:

- registration;
- discovery;
- connectivity;
- authentication;
- native Herdr RPC and client federation.

Drover does not require `golemd` on participating machines. A Golem deployment may later use Drover as a remote transport, but neither project should absorb the other's semantics.

Golem's existing restricted SSH attach server is also separate. It exposes one verified tmux-backed worker pane and intentionally rejects command execution, forwarding, SFTP, and arbitrary shell access. It should not be weakened or repurposed as Drover's machine-level Herdr endpoint.

## Non-goals

Drover is not:

- a Golem replacement;
- a task scheduler;
- a durable job queue;
- a settlement or evaluation protocol;
- an artifact store;
- a model router;
- a hosted agent runtime;
- a terminal renderer;
- a second multi-machine UI;
- a transparent arbitrary-command shell API;
- a multi-tenant public relay product in its first form.

In particular, Herdr `idle`, `done`, `blocked`, and `working` remain Herdr lifecycle states. Drover does not relabel them as task verdicts.

## Initial scope

The first useful version should assume:

- one operator;
- macOS and Linux servers;
- macOS and Linux clients;
- one publicly reachable coordinator providing authenticated HTTPS/WebSocket and SSH services;
- outbound coordinator access from every client and server, with no direct client-to-server route;
- preinstalled, compatible Herdr versions;
- one stable coordinator-loopback reverse-SSH port per machine;
- a local OpenSSH server on each participating machine;
- existing operator SSH authentication;
- one Drover-owned named Herdr session per machine;
- no public arbitrary byte relay beyond constrained SSH forwarding;
- no multi-user policy system.

A practical implementation order is:

1. local server wrapping one Herdr socket;
2. machine-addressed structured RPC;
3. coordinator registration and leases;
4. persistent outbound control streams and coordinator-routed RPC;
5. stable reverse-SSH port allocation and supervised tunnels;
6. constrained coordinator jump access;
7. client machine discovery, generated SSH aliases, and Herdr profile reconciliation;
8. macOS/Linux deployment and real multi-machine integration tests;
9. credential rotation and revocation.

## Design invariants

1. **One registered machine identity maps to one Drover-owned Herdr namespace.**
2. **The coordinator does not become the authority for agent or task state.**
3. **Interactive SSH and structured RPC are distinct capabilities.**
4. **RPC methods and arguments are structured and explicitly authorized.**
5. **Machine disappearance means offline, not completed or failed.**
6. **No automatic server replacement may kill running Herdr panes.**
7. **Native Herdr clients remain native clients; Drover does not fork or reimplement the UI.**
8. **Golem semantics do not leak into Drover.**
9. **Clients and servers require only outbound access to the coordinator; nodes expose no direct public listener.**
10. **Reverse listeners remain coordinator-loopback-only and are reachable only through constrained jump authentication.**
11. **The reverse-SSH design must remain replaceable by a later relay without changing machine identity.**
12. **A useful single-operator system comes before a speculative cloud product.**

## Status

Implemented in Python/asyncio with SQLite, authenticated TLS/WebSocket control,
allowlisted native Unix-socket RPC, system OpenSSH reverse-tunnel supervision,
operator-installed constrained rendezvous authorization, and native Herdr 0.9.0
profile/client delegation. Nix pins the complete toolchain, including Herdr.

Validated O’Brien → Azula registration, RPC, reverse forwarding/ProxyJump, native
saved-machine preparation and client endpoint handshakes, transport reconnect,
and negative SSH authorization. See the [evidence and remaining gaps](docs/validation.md).

Herdr 0.9 saved-machine federation requires its detached daemon: Drover starts
it through the native `remote-client-bridge` bootstrap with EOF, or adopts an
already compatible socket. It refuses to replace running incompatible servers.
SSH authorization installation and two-plane revocation remain explicit operator
actions; there are no managed service units or Golem semantics in Drover.
