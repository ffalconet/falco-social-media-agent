# FALCO Orchestrator V1.1

## Mandatory plan contract

Before a plan can be automatically submitted to Veo, persist both:

- `keyframe_object_name`
- `video_prompt`

Example:

```json
{
  "plans": {
    "plan_02": {
      "keyframe_object_name": "falco/images/...",
      "video_prompt": "Validated motion prompt...",
      "status": "keyframe_ready"
    }
  }
}
```

The orchestrator must never invent a missing video prompt.

## Automatic behavior

`advanceFalcoProduction` may:

1. detect a plan with keyframe + persisted video_prompt but no operation;
2. submit that exact prompt/image pair to Veo;
3. persist the returned operation_name;
4. later check existing operations;
5. publish completed videos;
6. persist canonical GCS filenames;
7. synchronize plan.status with video.status.

It must not generate images or final montage.

## Missing prompt

If a keyframe exists but video_prompt is absent:

- set plan.status to `awaiting_video_prompt`;
- do not submit Veo;
- return `persist_missing_video_prompts`.

## Idempotency

If an operation_name exists, never submit another operation automatically.
If a filename exists, treat the plan as published.
