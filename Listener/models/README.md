# Production wake-word assets

Listener loads `hey_navi_v3.onnx`, `melspectrogram.onnx` and `embedding_model.onnx`
from this directory. The v3 model is unchanged; its checksum and training
manifest are versioned beside it. The two feature models are downloaded or
copied from verified existing assets by `python3 Listener/setup_env.py`, with
pinned checksums. Inference never downloads files.

Use [setup](../../docs/setup.md) for the runtime environment. Training tools,
recordings and datasets are not included or required at runtime.

The [manifest](hey_navi_v3.manifest.json) records upstream sources, checksums,
evaluation results and limitations. Its license notice identifies this as a
research/noncommercial experiment with noncommercial training inputs; do not
present the model as having a permissive commercial license.
