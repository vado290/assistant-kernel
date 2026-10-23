---
name: assistant-volcengine-models
description: Use the Volcengine Ark text generation, image OCR, embedding and video capabilities Kernel provides centrally; the model credentials are held by the shared worker configuration and never enter prompts.
metadata:
  version: "1.2.0"
  load: {roles: [pm], stages: [requirements, design, build, review, operate], on_demand: true}
  changelog:
    - "1.2.0 2026-10-23 Written in English, the skills' source language; what the user reads follows the user's language (ROUND6, 0.46.0)"
    - "2026-10-16 Read on demand (only name and purpose listed in the prompt); content not yet rewritten for core v6 — any old process terms and output fields in it give way to assistant-project-agent and the output requirements"
---

# Shared Volcengine model capabilities

Kernel provides shared Volcengine model capabilities to every project and agent. The credential file belongs to Kernel's
control plane and never enters a project scope, context view, prompt or an agent's shell environment. Model calls go
through a Kernel tool or a deterministic worker, which reads the credentials and records the actual model and result.

## Current fixed models

- General generation and structured text: `doubao-seed-2-0-mini-260428`
- OCR of image pages: `doubao-seed-2-0-mini-260428`
- Text embeddings: `doubao-embedding-vision-251215`, fixed at 1024 dimensions
- Reserved for video generation: `doubao-seedance-2-0-fast-260128`
- Reserved for ASR: `Seed-ASR 2.0`, LAS resource `seedasr`
- Reserved for rerank: `doubao-seed-rerank`

## Tools agents can use

1. First call `volcengine_model_status` to check that a capability is enabled; it returns only booleans and model names,
   never credentials.
2. Use `volcengine_generate` for ordinary low-cost generation, extraction, classification or format conversion.
3. With an HTTPS image URL or `data:image/...` content at hand, use `volcengine_ocr` to extract the text; never ask a
   model to guess content that is not in the image.
4. Embeddings, batch document parsing, ASR, rerank and video generation belong to Kernel's deterministic workers and APIs;
   agents never assemble keys in a shell or call the provider's API directly.

## Limits

- The Volcengine models are an aid; they do not replace the current run's harness, session, approvals, scope or business
  state.
- When a tool fails, report the actual error; on an authentication failure never look for, read or ask to reveal the
  control-plane credential file — report only that the capability is not configured or was refused, and leave it to
  Kernel operations.
- Never output a key, AK, SK or token in replies, artifacts, logs, command lines, environment diagnostics or error
  messages.
- Never blindly retry the same external call; for billed side effects such as video, check the job id and receipt first.
