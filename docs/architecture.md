# Service architecture and IPC reference

For installation, use [the setup guide](setup.md).

## Runtime ownership

`navi.target` groups four systemd user services. Each service starts
`python3 -m Services <component>` from the checkout using the installer-selected
interpreter. This internal entry point requires systemd invocation/notification
variables; start services through systemd or NaviRemoteControl.

| Unit | Implementation | Responsibility |
| --- | --- | --- |
| `navi-face.service` | `Face.service`, `Face.Animation` | Renderer, bounded command submission, envelope receiver |
| `navi-listener.service` | `Listener.service`, `Listener.audio`, `Listener.capture`, `Listener.wakeword` | Exclusive continuous capture, local wake inference and buffered handoff |
| `navi-speaker.service` | `Audio.service`, `Audio.Playback` | Exclusive playback ownership and synchronous Piper pipeline |
| `navi-coordinator.service` | `Coordinator.service`, `Network.Client.ServerClient` | Conversation routing and instance-owned HTTP discovery state |

Each component has its own process and component lease. Listener also holds a
microphone lease, Speaker a playback lease, and Face a renderer lease. Kernel
locks release on crashes. Locks prevent duplicates within Navi's configured
runtime directory; they cannot prevent unrelated external audio tools.

Face never imports capture or playback. It samples only the IPC envelope
receiver. Renderer state, playback clocks, and HTTP URL caches belong to their
own component. There are no cross-component Python globals.

## Recovery and shutdown

systemd alone owns recovery: `Type=notify`, `Restart=on-failure`, three-second
restart delay, ten-second watchdog, and five starts per minute. All units use
`PartOf=navi.target` and `KillMode=control-group`, with bounded stop/abort times.
Stopping the target stops its components. Restarting one component does not
restart its peers. An installed pre-migration unit is disabled by setup; unit
conflicts prevent it from running concurrently during migration.

A component's event loop sends readiness/watchdog notifications. Face pumps
that loop from rendering, so a frozen renderer cannot keep sending heartbeats
from a separate thread. One worker per component keeps IPC health responsive
while bounded work runs. Job deadlines are Listener 12s, Speaker 180s, and
Coordinator 300s. Exceeding a deadline exits the component for systemd recovery.

The service tracks subprocesses and terminates/reaps them during shutdown;
systemd's cgroup cleanup handles abrupt failures. stderr drainers and network
discovery threads remain local, bounded implementation details. No thread or
CLI launches replacement services automatically.

## IPC version 1

Unix stream sockets live in `$XDG_RUNTIME_DIR/navi` (normally
`/run/user/<uid>/navi`) or `NAVI_RUNTIME_DIR`. The directory is owned by the user
and mode 0700; sockets are mode 0600. A component acquires its lease before
removing a stale socket. Use the same runtime directory for all services and
health/command clients. If overridden in services.env, export it in manual CLI
sessions as well.

One UTF-8 JSON object plus newline per request/reply, maximum 256 KiB, with a
two-second request-header deadline:

```json
{"v":1,"id":"request-id","op":"command","args":{"text":"listen"},"context":{}}
```

```json
{"v":1,"id":"request-id","ok":true,"result":{"accepted":true,"job_id":"request-id"}}
```

Errors return `ok:false,error:"..."`. Clients enforce an overall response
deadline. A component permits at most 16 simultaneous connections.

| Endpoint | Operation | Result |
| --- | --- | --- |
| Every component | `health` | Component, PID, uptime, ready/busy, current job, last job/error |
| Coordinator | `command`, text 1..4000 characters | Immediate acceptance; outcome in health/logs |
| Coordinator | `wake`, activation_id (32 hex characters) | Immediate acceptance of a single-use Listener activation |
| Listener | `record`, empty args or activation_id | Base64 WAV from the continuous stream; four seconds plus up to 1.5 seconds of wake pre-roll, maximum 176044 bytes |
| Listener | `playback_started`, empty args; `release`, activation_id | Immediate suppression/release controls, including while recording |
| Speaker | `speak`, text and optional source/trigger | Reply after playback completes, or failure |
| Face | `health` only | Render-loop responsiveness |

`context` propagates interaction ID, client name, and originating session ID.
Each process also logs its own session. Listener transfers audio bytes through
IPC from private memory; no shared audio pathname is
required. Coordinator retains the existing disk IP cache for discovery only.

Listener health adds an `audio` diagnostics object. Wake inference and capture
are independently checked by its heartbeat. Playback suppression uses direct
Speaker-to-Listener IPC and Speaker health, without depending on Face. See the
[wake-word integration guide](operations.md#wake-word-listening) for audio windows,
restart semantics, deployment, verification, and rollback.

Busy services reject additional work. Disconnects and restarts never replay a
request. If Coordinator fails, already accepted capture/playback may finish,
but the conversation does not resume. Health remembers only the current/most
recent job and resets on restart; logs retain history. `ready` in the version-1
wire protocol means responsive and idle, not proof that hardware works. The
controller displays it as `responsive`, separately from process state.

## Mouth envelopes

Speaker sends datagrams to `animation.sock`: version, session, monotonic
playback timestamp, level, completion. Face maintains a bounded 256-sample
buffer and applies the playback latency offset. Missing/full sockets drop
packets so Face failure cannot stall audio. New packets resume animation after
Face restarts; completion or 0.5 seconds without packets returns the mouth to
idle. An absent Speaker therefore cannot leave Face stuck speaking.

## Operations and troubleshooting

```bash
systemctl --user start navi.target
python3 -m Services.control status
python3 -m Services.control command --text listen
python3 -m Services.control restart listener
journalctl --user -u navi-listener.service -n 40 --no-pager
systemctl --user stop navi.target
```

NaviRemoteControl continues to use SSH, systemd, journalctl, and
`python3 -m Services.control`; there is no network management daemon. Use
`start client` for the whole group, `restart face` for one component, and
`diagnose client` for systemd/health details. See
[migration and recovery](history.md) for configuration changes.
