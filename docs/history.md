# Migration and recovery notes

## Component layout

Navi uses four independent systemd user services: Listener, Speaker, Coordinator,
and Face. Shared IPC and lifecycle code lives in `Services/`; Speaker is
implemented in `Audio/`. `navi.target` groups the services.

Older installations may use a single `navi-client.service` or load wake-word
assets from `wakeword_poc/`. The current installer disables the legacy service
and uses `Listener/models/` with a separate `Listener/.venv` environment.
See [setup and upgrade](setup.md#upgrade-from-the-previous-layout) for the
migration procedure.

Do not rename an existing virtual environment: its scripts contain absolute
paths. Prepare the new environment and retain the old environment and model
assets until the live checks in [operations](operations.md#verify-a-deployment)
pass. Never run competing clients against the same microphone.

## Reliability behavior

Listener withholds inference frames while the wake model resets. Microphone
capture and request buffering continue during that reset. Monitoring resumes
only after model readiness, playback suppression, and cooldown checks pass.
This avoids filling the inference queue during feature reconstruction.

Speaker initializes its output endpoint once per process startup. It waits for
the device, performs a mute-to-unmute transition, applies the configured volume,
and verifies the mixer state. Normal utterances check endpoint presence without
repeating initialization. Restart Speaker to repeat initialization when
troubleshooting a silent output. Mixer state and a successful playback command
still require a physical audibility check.

## Recovery

The installer saves changed user units and settings under
`~/.config/navi/backups/`. Backup names and contents belong to the installation
that created them. Inspect those local files before choosing a recovery action;
there is no universal backup identifier or rollback script for every version.

Before restoring source or service configuration:

1. Stop the services with `systemctl --user stop navi.target`.
2. Preserve current local settings and source changes outside the checkout.
3. Restore a matching source version, its inference environment, and its model
   assets. Use the setup instructions shipped with that version.
4. Restore any required unit/settings backup, then run
   `systemctl --user daemon-reload`.
5. Start the services and perform the live deployment checks.

For typed-only diagnosis, set `NAVI_WAKE_ENABLED=0` in
`~/.config/navi/services.env` and restart Listener. Set it to `1` to restore
wake detection.
