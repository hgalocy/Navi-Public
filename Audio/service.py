"""Speaker owns Piper/aplay; speech envelopes leave this process only via IPC."""
from Services.ipc import IPCError, call
from Services.validation import require_text
from Services import runtime


def create_service(sender, playback=None, output=None):
    if playback is None:
        from Audio.Playback import play as playback
        from Audio.Output import Output
        output = output if output is not None else Output()
    from Logging import interaction_context, interaction_details, log_conversation, logger

    def handler(op, args, context):
        if op != "speak":
            raise IPCError("Speaker supports speak")
        text = require_text(args)
        source, trigger = args.get("source", "system"), args.get("trigger", "direct")

        def work():
            with interaction_context(interaction_details(context)):
                log_conversation("Navi", text, source, trigger)
                logger.info("speech_submitted source=%s trigger=%s", source, trigger)
                try:
                    # Acknowledge suppression before Piper can emit sound. Listener also
                    # polls our busy state, including after either process restarts.
                    call("listener", "playback_started", context=interaction_details(context), timeout=1)
                except IPCError as exc:
                    logger.warning("listener_playback_barrier_unavailable error=%s", exc)
                options = {"on_envelope": sender.send, "process_callback": service.track_process}
                if output is not None:
                    options["output"] = output
                if not playback(text, **options):
                    raise RuntimeError("Speech generation/playback failed; see runtime log")
            return {"played": True}
        return work, True

    service = runtime.Service("speaker", handler, deadline=180,
                              start=(lambda: output.prepare(timeout=20)) if output is not None else None,
                              tick=output.check if output is not None else None,
                              diagnostics=output.status if output is not None else None)
    return service


def run():
    from Services.animation import EnvelopeSender
    with runtime.Lease("playback"):
        sender = EnvelopeSender()
        try:
            runtime.run(create_service(sender))
        finally:
            sender.close()
