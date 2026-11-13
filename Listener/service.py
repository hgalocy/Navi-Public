"""Listener owns the microphone and returns private recordings through IPC."""
from Services.ipc import IPCError
from Services import runtime
from .audio import ContinuousAudio


def create_service(audio=None):
    if audio is None:
        from .wakeword import Detector, settings
        enabled, threshold = settings()
        audio = ContinuousAudio(Detector() if enabled else None, threshold)
    from Logging import interaction_context, interaction_details

    def handler(op, args, context):
        if op != "record" or set(args) - {"activation_id"}:
            raise IPCError("Listener supports record with optional activation_id")
        activation_id = args.get("activation_id")
        if "activation_id" in args and (not isinstance(activation_id, str) or len(activation_id) != 32):
            raise IPCError("Invalid activation_id")
        activation_id = audio.reserve(activation_id)

        def work():
            with interaction_context(interaction_details(context)):
                try:
                    return audio.recording(activation_id)
                except Exception:
                    audio.release(activation_id)
                    raise
        return work, True

    def control(op, args, context):
        if op == "playback_started" and not args:
            audio.playback_started()
            return {"suppressed": True}
        if op == "release" and set(args) == {"activation_id"} and isinstance(args["activation_id"], str):
            audio.release(args["activation_id"])
            return {"released": True}
        return None

    service = runtime.Service("listener", handler, deadline=12, control=control,
                              start=lambda: audio.start(service.track_process),
                              tick=audio.check, close=audio.close, diagnostics=audio.status)
    return service


def run():
    with runtime.Lease("microphone"):
        runtime.run(create_service())
