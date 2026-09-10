# FALCO Orchestrator V1.4

## Goal

V1.4 extends the persisted production pipeline upstream so a production can advance from a persisted concept/master prompt to usable video assets without requiring the GPT to manually coordinate image creation first.

The orchestrator remains conservative: it never invents missing creative instructions. It only acts on prompts and continuity state already persisted in the production.

## New locks

V1.4 formalizes four continuity locks:

1. **Identity Lock** — same person, face, hair, morphology, clothing and shoes.
2. **Environment Lock** — same physical world, weather, light, palette and recurring set elements.
3. **Spatial Continuity Lock** — each plan has an explicit spatial state and may only progress through the persisted sequence.
4. **Narrative State Lock** — each plan has an explicit action/state describing what has already happened and what is allowed to happen next.

These locks are persisted in production metadata and/or per-plan state. They are instructions for image generation and later QA; they are not inferred by the backend.

## Automatic master generation

When `master_image` is missing, V1.4 may generate it automatically if `metadata.master_prompt` is persisted.

The resulting image identifiers are persisted immediately:

```json
"master_image": {
  "image_id": "...",
  "object_name": "falco/images/...jpg",
  "generated_at": "..."
}
```

If `metadata.master_prompt` is absent, the orchestrator stops with `awaiting_master_prompt`.

## Automatic keyframe generation

For each plan without a keyframe, V1.4 may generate one automatically only when:

- a persisted master image exists;
- the plan has a persisted `keyframe_prompt`.

Each keyframe is generated directly from the master image. Keyframes are never chained from another keyframe.

The generated identifiers are persisted immediately on the plan:

```json
{
  "keyframe_image_id": "...",
  "keyframe_object_name": "falco/images/...jpg",
  "keyframe_generated_at": "..."
}
```

If a plan is missing `keyframe_prompt`, the orchestrator stops with `awaiting_keyframe_prompts` and reports the exact plans concerned.

## Spatial and narrative continuity

Recommended per-plan persisted structure:

```json
{
  "spatial_state": {
    "location": "inside",
    "transition_to_next": "threshold"
  },
  "narrative_state": {
    "character": "preparing",
    "allowed_action": "reach_for_door"
  }
}
```

Example progression:

```text
plan_01: INSIDE / PREPARING
plan_02: THRESHOLD / EXITING
plan_03: OUTSIDE / RUNNING
```

The backend preserves these fields and passes the persisted keyframe prompt to image generation. It does not invent or rewrite the intended progression.

## Pipeline

V1.4 target flow:

```text
production state
→ master image (automatic)
→ keyframes from master (automatic)
→ Veo submissions
→ retry once for recoverable Veo failure
→ FFmpeg image fallback when needed
→ montage
→ completed
```

## Safety and idempotency

- Existing master images are reused.
- Existing keyframes are reused.
- Existing Veo operation names are never resubmitted.
- Published clips are never regenerated.
- Existing completed montage is reused.
- No creative prompt is invented by the backend.
- Missing required prompts produce an explicit waiting state instead of guessing.
