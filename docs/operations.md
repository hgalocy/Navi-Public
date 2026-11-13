# Operation and troubleshooting

Use [setup](setup.md) for installation and [architecture](architecture.md) for
process/IPC details. See [migration and recovery](history.md) for older layouts
and local backup handling.

## Normal operation

NaviRemoteControl keeps the existing commands: `start client`, `stop client`,
`status client`, `restart listener`, `restart speaker`, and `diagnose client`.
The group is `navi.target`; individual components restart independently.
Start NaviServer separately before requesting a conversation.

On Jetson:

```bash
python3 -m Services.control status
python3 -m Services.control command --text listen
systemctl --user restart navi-listener.service
systemctl --user restart navi-speaker.service
pgrep -a arecord
```

Only Listener owns the microphone. Do not run standalone research recording
tools alongside it. Both wake detection and typed `listen` reuse the same
continuous capture, including during synthesis/playback suppression.

## Wake-word listening

Production assets live in `Listener/models/`. Listener uses openWakeWord 0.6.0
and 16 kHz mono PCM in 80 ms frames. A rising score of 0.95 reserves up to
1.5 seconds of preceding audio and exactly four seconds after detection.
Coordinator fetches the single-use activation through IPC and sends its WAV
to NaviServer. The maximum request WAV is 176044 bytes; audio stays in memory.
Typed `listen` records four seconds without wake pre-roll.

The activation remains reserved through the response. Duplicate IDs and
continuing high scores cannot generate another request. Speaker notifies
Listener before playback; Listener also checks Speaker health. Detection
resumes after release, model reset and a two-second cooldown. Face is outside
this control path. A restarted Listener discards its old ring/reservations;
interrupted conversations are not replayed.

Settings in `~/.config/navi/services.env`:

| Setting | Default |
| --- | --- |
| `NAVI_WAKE_ENABLED` | `1` (use `0` for continuous capture with typed `listen` only) |
| `NAVI_WAKE_THRESHOLD` | `0.95`, finite and in `(0, 1]` |
| `NAVI_MIC_DEVICE` | `plughw:1,0` |

Restart Listener after changing these values. Threshold scores are not
calibrated probabilities; buffering preserves the request when detection
occurs, but does not improve the classifier's recall. Evaluation results and
limitations are recorded in the [model manifest](../Listener/models/hey_navi_v3.manifest.json).
Training and evaluation datasets are not required at runtime.

Listener health includes `audio.monitoring`, `model_ready`, `speaker_safe`,
`threshold`, `activation_id`, `last_score`, `peak_score` and `inference_frames`.
Wait for `monitoring=true` before speaking. It is false during a request,
playback, cooldown or feature reset. During reset, capture continues while
inference frames are withheld to avoid an artificial queue overflow.

## Speaker output

The default output configuration targets a C-Media `0d8c:0014` USB-to-AUX
adapter with ALSA ID `Device`. Select the device and mixer for your hardware.
Use the card name instead of its boot-dependent index:

```ini
NAVI_SPEAKER_DEVICE="plughw:CARD=Device,DEV=0"
NAVI_SPEAKER_MIXER="Speaker"
NAVI_SPEAKER_VOLUME="46%"
```

Speaker waits for the playback endpoint, forces a mute-to-unmute transition
with a 150 ms settling interval, applies volume and reads back the channels
before reporting readiness. This happens once at startup, including each
independent Speaker restart. Normal utterances only check endpoint presence;
they do not toggle mute, rewrite volume or repeat the settling wait.

A mute-to-unmute transition can recover silent output even when ALSA reports
an unmuted mixer. The initialization policy is described in the
[reliability notes](history.md#reliability-behavior). An enabled mixer and a
successful aplay exit alone do not prove that sound is audible.

`audio.mute_cycle_on="startup"` identifies the policy. If silence returns,
save diagnostics before restarting:

```bash
python3 -m Services.control status speaker
aplay -l
amixer -D hw:CARD=Device sget Speaker
journalctl --user -u navi-speaker.service -n 60 --no-pager
```

Check speaker power and the AUX output connection. Restart only Speaker to
rerun initialization. Its heartbeat detects a lost hardware endpoint and lets
systemd recover it independently. Do not reset the whole USB bus: the microphone
is a separate USB device. A custom output route needs its actual mixer name;
set `NAVI_SPEAKER_MIXER=""` to leave an unmanaged mixer alone.

## Health and logs

`ready` means responsive and idle. Audio diagnostics cover software readiness,
not physical audibility, image rendering or AI answer quality. Health retains
the latest job/error until a new job or process restart.

Each process writes `Logging/logs/<component>/navi.log` and
`navi_conversation.log`, plus journal output. `NAVI_LOG_DIR` overrides the base.
Session IDs distinguish process launches; interaction IDs correlate requests
across services and the server. Logs rotate at 5 MiB with three backups.

```bash
journalctl --user -u navi-listener.service -f
systemctl --user show navi-listener.service -p NRestarts -p Result -p ExecMainStatus
systemctl --user reset-failed navi-listener.service
```

## Verify a deployment

1. Confirm four responsive services and one Listener-owned `arecord`.
2. Say "Hey Navi, what's your name?" continuously; confirm one upload, full
   request transcription, audible answer and mouth animation.
3. Repeat after playback/cooldown; verify wake monitoring resumes.
4. Check typed `listen`, playback self-activation suppression, and independent
   Listener/Face/Speaker restart behavior. Peer PIDs and the recorder should
   remain unchanged when another component restarts.

Automated tests simulate devices/model/server; they do not establish live wake
accuracy or physical audibility. Repeat these live checks for each installation
and keep deployment-specific diagnostics outside the source repository.

## Rollback

For typed-only debugging, append `NAVI_WAKE_ENABLED=0` to services.env and
restart only Listener. The last environment assignment wins. Restore `1` to
enable detection again.

Before changing source revisions, stop `navi.target`, preserve local changes,
and use a separate checkout of the intended version. Use its installer and
paths together. Keep the inference environment and feature assets for that version.
Never run two checkouts against the same microphone.

Installer backups live under `~/.config/navi/backups/`. Inspect the backups
created by your installation and follow the [recovery notes](history.md#recovery).
Updating source files does not rewrite or remove those backups.
