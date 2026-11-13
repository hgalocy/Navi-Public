# Navi

Navi runs as four independent systemd user services, communicating through
local IPC. NaviRemoteControl starts the group; NaviServer processes requests
separately using its existing API.

| Directory | Process | Owns |
| --- | --- | --- |
| `Listener/` | `navi-listener` | One continuous microphone stream, Hey Navi detection, buffered request audio, inference environment and models |
| `Audio/` | `navi-speaker` | Piper synthesis, USB speaker initialization, playback and mouth envelopes |
| `Coordinator/` | `navi-coordinator` | Commands, conversation routing and NaviServer requests |
| `Face/` | `navi-face` | Rendering, keyboard input and mouth animation |
| `Services/` | Shared infrastructure | IPC, leases, health, systemd lifecycle hooks and controls |

`Audio/` is the Speaker implementation; it is not a fifth service. systemd
supervises each process independently. The Listener does not depend on Face.

## Set up on Jetson

From the Navi checkout, as the desktop user, without sudo:

```bash
python3 Listener/setup_env.py
python3 setup_services.py --check --listener-python "$PWD/Listener/.venv/bin/python"
python3 setup_services.py --start --listener-python "$PWD/Listener/.venv/bin/python"
```

Listener setup prepares its pinned environment and verified feature models.
The service installer validates and installs the four services. See
[setup and upgrade](docs/setup.md) for prerequisites and migrating an older
installation.

In NaviRemoteControl, keep `[client] manager="systemd-user", name="navi.target"`:

```text
start client
status client
restart listener
diagnose speaker
stop client
```

Say **"Hey Navi, what's your name?"** without a deliberate pause. Listener keeps
1.5 seconds of pre-roll plus four seconds after detection. Typed `listen`
remains available for debugging. Speaker initializes its mute state once at
startup, not before each utterance.

## Guides

- [Setup and upgrade](docs/setup.md): dependencies, model preparation, services and deployment.
- [Architecture](docs/architecture.md): ownership, IPC, restart and failure behavior.
- [Operations](docs/operations.md): wake settings, audio troubleshooting, verification and rollback.
- [Migration and recovery](docs/history.md): older layouts, reliability behavior and local backup handling.

Training tools, recordings and datasets are not included or required at runtime.
Model provenance and evaluation limitations are recorded in the
[model manifest](Listener/models/hey_navi_v3.manifest.json).

## Privacy and network use

Listener continuously captures microphone audio locally for wake detection.
On activation or a typed `listen` command, Coordinator sends the buffered
request to NaviServer for processing. Server discovery scans the local IPv4
/24 subnet; requests use HTTP on port 5000. Use this configuration on a trusted
network. Conversation logs can contain response text, and runtime logs can
contain device names, paths and network addresses. Keep logs, recordings and
local settings out of public repositories.

## Model notice

The bundled wake-word model's manifest identifies it as a research/noncommercial
experiment and records upstream noncommercial training inputs. Preserve its
provenance and license notice when distributing the model. No repository-wide
source license is currently provided.

## Tests

```bash
python3 -B -m unittest discover -s tests -v
```

Tests use simulated hardware. Linux additionally runs real IPC, ownership and
independent-process restart tests. `python3 -B tests/systemd_smoke.py` is an
opt-in isolated systemd smoke test. Actual audible speech and wake accuracy
still need a Jetson check after deployment.
