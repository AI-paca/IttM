# Local LLM OCR runners

This directory is an experimental staging area for local OCR/VLM engines. It is intentionally outside the main application Docker/compose flow so model downloads do not inflate normal development images.

Tracked files here are only scripts, manifests, and examples. Real tokens and model caches stay outside git:

- `LLM-OCR/.env` and `LLM-OCR/.evn` are ignored local token files.
- `HF_HOME` defaults to `${HOME}/.cache/huggingface`, so Hugging Face weights are reused from the user cache instead of copied into the repository.
- `NEMOTRON_OCR_REPO_DIR` defaults to `${HOME}/.cache/huggingface/nemotron-ocr-v2-src`, because Nemotron needs its source package installed as well as the weights.
- Ollama keeps its own model cache.

Quick start:

```bash
scripts/llm-ocr/create-workdir.sh
scripts/llm-ocr/run-local-api.sh --list
scripts/llm-ocr/run-local-api.sh
```

Non-interactive examples:

```bash
scripts/llm-ocr/run-local-api.sh --model glm-ocr --backend ollama
scripts/llm-ocr/run-local-api.sh --model qianfan-ocr --backend vllm --docker --port 18081
scripts/llm-ocr/run-local-api.sh --model paddle-ocrv6-medium --backend paddleocr
scripts/llm-ocr/run-local-api.sh --model nemotron-ocr-v2 --backend nemotron --docker
```

Use `--dry-run` first if you only want to inspect the download and server commands.

The manifest separates flags by responsibility:

- `alignment`: rotation, perspective, language/weight selection, unwarping.
- `positioning`: detection of where text or layout regions are.
- `composition`: Markdown blocks, tables, lists, reading order, merge level.
- `recognition`: text decoding and generation length.
- `postprocess`: schema or Markdown cleanup after recognition.

Each flag is marked as `required` or `quality`. The goal is to keep model-specific requirements explicit while preventing quality hints from becoming hidden pipeline behavior.

Model notes:

- `zai-org/GLM-OCR`: supports Ollama and OpenAI-compatible serving through vLLM/SGLang.
- `PaddlePaddle/PP-OCRv6_medium_det_safetensors`: detector model used through PaddleOCR; it needs a recognition model for text output.
- `baidu/Qianfan-OCR`: vision-language OCR model with a vLLM deployment path and a Markdown parsing prompt.
- `nvidia/nemotron-ocr-v2`: local Python/Docker-oriented OCR pipeline with detector, recognizer, relational grouping, and merge-level controls.
