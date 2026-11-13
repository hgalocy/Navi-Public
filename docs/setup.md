# Set up Navi's four components

This is Navi's only supported runtime. `navi.target` starts all four services;
Coordinator handles conversations and systemd handles supervision.

Run this **on Jetson**, as the same user NaviRemoteControl connects to, from a
terminal in the Jetson desktop. Update the Jetson Navi checkout first so it
contains `setup_services.py`, `Services/`, `Listener/`, `Audio/`, `Coordinator/`,
`Face/`, and `systemd/`.

Close manually launched Navi clients and wake-word/recording tools first.

Listener enables local **Hey Navi** detection by default. Its runtime code,
models, pinned dependencies and setup live in `Listener/`. The other services
keep their existing interpreter. See [operations](operations.md) for wake-word
settings, verification and rollback.
Setup preserves the installed Listener interpreter on subsequent runs. On a
fresh installation it uses `Listener/.venv/bin/python` if that environment
exists, otherwise the main Python interpreter. To choose a different environment,
use `--listener-python /path/to/python` in both check/install runs.

```bash
cd ~/Navi
python3 Listener/setup_env.py
python3 setup_services.py --check --listener-python "$PWD/Listener/.venv/bin/python"
python3 setup_services.py --start --listener-python "$PWD/Listener/.venv/bin/python"
```

**That installs and starts Face, Listener, Speaker, and Coordinator together.**
Use the interpreter/environment you normally run Navi with. For example:

```bash
python3 setup_services.py --python "$PWD/.venv/bin/python" --start
```

Listener setup requires Jetson Linux ARM64 with Python 3.8, verifies the tracked
v3 model, prepares `Listener/.venv` with pinned openWakeWord 0.6.0 dependencies,
and downloads the two checksum-verified ONNX feature models. It can reuse
verified feature files from an older PoC installation, but production loads
only `Listener/models/`. `python3 Listener/setup_env.py --check` validates an
existing environment without installing or opening the microphone.

The service installer checks Python 3.8+, the existing client packages, ALSA tools, Piper
and its voice files, and the systemd user connection. Missing prerequisites
produce an error before installation; it does not download packages or models.

## Upgrade from the previous layout

The component layout changes import and model paths in older installations.
Stop Navi before updating the production checkout; do not move source out from
under running processes.

```bash
systemctl --user stop navi.target
# Update the checkout, preserving local changes first.
python3 Listener/setup_env.py
python3 setup_services.py --check --listener-python "$PWD/Listener/.venv/bin/python"
python3 setup_services.py --start --listener-python "$PWD/Listener/.venv/bin/python"
```

Do not rename an installed virtual environment: its scripts contain absolute
paths. The setup helper creates the new Listener environment without moving or
deleting the old one. Keep old environments, shared models and systemd backups
until the new installation passes the live checks in [operations](operations.md).
Git may leave ignored files in the old PoC directory on Jetson; that leftover
environment is not needed by the new services once their interpreter is updated.
Updating source files does not install or restart production services.

The service installer:

1. Validates and installs the five units for the current user, using this
   checkout's actual path and your selected Python interpreter.
2. Creates `~/.config/navi/services.env`, capturing desktop settings from your
   terminal. Existing settings and custom systemd drop-ins are preserved.
3. Backs up changed files under `~/.config/navi/backups/` and reloads systemd.
4. Stops and disables the old `navi-client.service` if it exists.
5. With `--start`, starts/restarts all four components and checks their health.

No sudo is needed. The script does not modify NaviServer, wake-word files, or
models. systemd remains the supervisor; Coordinator routes conversations.
Responsive health confirms the component loops, not a recording or speech test.

## Control it from Windows

In NaviRemoteControl's existing `[client]` configuration, use:

```toml
manager = "systemd-user"
name = "navi.target"
```

Keep the Jetson SSH settings. Set `repository` and `python` to the paths the
installer prints. Relaunch NaviRemoteControl, then use:

```text
status client
start client
stop client
restart client
```

`client` selects all four components together. Use `restart face` or
`diagnose listener` when you need one component. NaviServer is separate:
use `start server` for conversation processing.

## Optional settings

| Command/option | Effect |
| --- | --- |
| `python3 setup_services.py --check` | Check prerequisites and validate staged units without changing installed files or services. |
| `python3 setup_services.py` | Install and stop/disable the legacy service, leaving startup to `start client` in NaviRemoteControl. Existing component processes are not restarted. |
| `python3 setup_services.py --start --enable` | Also enable `navi.target` when this user's systemd manager starts, only after health checks pass. Does not configure lingering or desktop login. |
| `--mic-device plughw:1,0` | Set the microphone ALSA device. |
| `--speaker-device plughw:CARD=Device,DEV=0` | Select the USB speaker by ALSA card name, independent of card number. |
| `--speaker-mixer Speaker --speaker-volume 46%` | Restore and verify this playback mixer before speech (adjust the volume for your output hardware). |
| `--speaker-mixer ''` | Disable mixer management for an explicitly chosen route without this control. |
| `--listener-python /path/to/python` | Use an independent openWakeWord interpreter for Listener. |
| `--wake-threshold 0.95` | Configure the wake score threshold (default 0.95). |
| `--wake-enabled 0` | Keep continuous capture and typed `listen`, with wake inference disabled. |
| `--display :0 --xauthority "$HOME/.Xauthority"` | Supply actual Jetson desktop values when installing over SSH. Do not guess them. |

Rerunning is supported: unchanged files stay unchanged; changed files are backed
up. Explicit options override the matching settings; otherwise existing
`services.env` values are retained. If installing without `--start` over running
components, use `restart client` to apply changes. For automatic startup, the
Jetson desktop must be available for Face.

Speaker waits up to 20 seconds for its named playback endpoint/mixer before
reporting readiness; systemd allows 45 seconds for startup. It performs the
mute-to-unmute transition and applies the configured volume once at startup.
Normal utterances only check endpoint presence, without mixer changes or the
150 ms initialization wait. Restarting Speaker repeats initialization. A
missing endpoint fails Speaker for independent systemd recovery.
See [speaker reliability, tests, and rollback](operations.md#speaker-output). Existing
numeric `NAVI_SPEAKER_DEVICE` overrides are preserved; migrate them explicitly
to the intended named card to avoid reboot-dependent routing.

## If setup fails

The script prints the failed check. Fix it and rerun; it never automatically
retries a lifecycle request. Installation errors restore previous files. A
startup/health failure leaves the new units installed and the legacy service
disabled so you can inspect the failure without running competing clients.

From NaviRemoteControl:

```text
diagnose client
```

Or on Jetson:

```bash
journalctl --user -u 'navi-*.service' -n 40 --no-pager
```

For protocol details, manual installation, recovery and rollback, see the
[architecture reference](architecture.md) and
[migration and recovery notes](history.md). Updating the checkout alone does
not replace running processes: rerun `python3 setup_services.py --start` after
updating the source.
