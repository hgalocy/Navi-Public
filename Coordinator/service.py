"""Conversation routing only; hardware ownership belongs to Listener/Speaker."""
import base64
from collections import deque
import io
import re

from Services.ipc import IPCError, call
from Services.validation import require_text
from Services import runtime


def create_service(network=None):
    if network is None:
        from Network.Client import ServerClient
        network = ServerClient()
    from Logging import interaction_context, interaction_details, log_conversation, logger
    seen_activations = deque(maxlen=128)

    def handler(op, args, context):
        activation_id = None
        if op == "wake":
            activation_id = args.get("activation_id")
            if (set(args) != {"activation_id"} or not isinstance(activation_id, str)
                    or not re.fullmatch("[a-f0-9]{32}", activation_id)):
                raise IPCError("Invalid wake activation_id")
            if activation_id in seen_activations:
                raise IPCError("Duplicate wake activation")
            seen_activations.append(activation_id)
            command = "listen"
        elif op == "command":
            command = require_text(args)
        else:
            raise IPCError("Coordinator supports command or wake")
        details = interaction_details(context)

        def work():
            capture_id = activation_id
            with interaction_context(details):
                try:
                    source, trigger = "keyboard", "typed_repeat"
                    text = command
                    if command.lower() == "listen":
                        source, trigger = "microphone", "wake_word" if activation_id else "typed_listen"
                        if activation_id:
                            audio = call("listener", "record", {"activation_id": activation_id}, context=details, timeout=14)
                        else:
                            audio = call("listener", "record", context=details, timeout=14)
                        capture_id = audio.get("activation_id", capture_id)
                        wav = base64.b64decode(audio["wav_base64"], validate=True)
                        with io.BytesIO(wav) as stream:
                            response = network.send_audio({"file": stream}, source=source, trigger=trigger)
                        response.raise_for_status()
                        payload = response.json()
                        user_text = payload.get("user_text", "").strip()
                        text = payload.get("text", "").strip()
                        if user_text:
                            log_conversation("User", user_text, source, trigger)
                        else:
                            logger.warning("Server returned no user transcription; conversation log will contain only the reply")
                    else:
                        log_conversation("User", command, source, trigger)
                    if text:
                        call("speaker", "speak", {"text": text, "source": source, "trigger": trigger},
                             context=details, timeout=185)
                finally:
                    if capture_id:
                        try:
                            call("listener", "release", {"activation_id": capture_id}, context=details, timeout=1)
                        except IPCError as exc:
                            logger.warning("listener_release_failed activation_id=%s error=%s", capture_id, exc)
            return {"completed": True}
        return work, False

    return runtime.Service("coordinator", handler, deadline=300)


def run():
    runtime.run(create_service())
