# FALCO Orchestrator V1.3

## Goal

V1.3 makes recurring Veo media/audio failures a normal recoverable state.

## Recovery policy

For a recoverable completed Veo operation without a usable video_uri:

1. persist the failure;
2. if the plan has never been retried, submit exactly one retry using the same persisted keyframe and video_prompt;
3. persist the new operation_name, retry_count, retry_of and retry_reason;
4. never retry more than once automatically;
5. if the retry also ends with a recoverable no-video result, switch the plan to FFmpeg image fallback.

Recoverable failure types:

- rai_media_filtered
- no_video_uri

Other failures remain blocked for manual diagnosis.

## Fallback

After one unsuccessful automatic retry:

```json
{
  "mode": "ffmpeg_image_fallback",
  "source_image": "falco/images/keyframe.jpg",
  "status": "ready"
}
```

The fallback uses the persisted keyframe and the validated montage engine.

## Idempotency

- A published plan is never retried.
- A plan with an active operation_name is checked, not resubmitted.
- retry_count is persisted.
- Maximum automatic retry count is 1.
- A fallback-ready plan is treated as usable for montage.

## Expected production path

```text
Veo failed
→ automatic retry
→ success → publish
OR
→ second recoverable failure
→ FFmpeg image fallback
→ montage
→ completed
```
