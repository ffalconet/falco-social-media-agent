# FALCO Orchestrator V1.4.1 — Targeted Plan Video Revision

## Goal
Allow a completed FALCO production to reopen exactly one already-published plan video, regenerate only that video from the persisted keyframe, preserve all other production assets, then let the normal orchestrator rebuild the final montage.

## Endpoint
`POST /productions/{production_id}/plans/{plan_key}/revise-video`

Operation intent: `retryFalcoPlanVideo`.

Request body:

```json
{
  "video_prompt": "Replacement Veo prompt",
  "reason": "Optional human-readable revision reason"
}
```

## Preconditions
- Production status must be `completed`.
- Target plan must exist.
- Target plan must have a persisted `keyframe_object_name`.
- Target plan video must currently be `published`.
- No targeted revision may already be active for that plan.

## Guarantees
The endpoint preserves:
- the production_id;
- master image;
- target keyframe;
- every other plan and published video;
- montage creative configuration;
- official FALCO end-card settings.

It changes only the selected plan video and invalidates the rendered montage output.

## History
Before reopening the selected plan, the previous video state and previous montage reference are appended to `plans.<plan>.video_revision_history`.

The old asset is not silently overwritten.

## Continuity
The replacement `video_prompt` is supplemented with the V1.4 continuity lock block when it does not already contain `[FALCO_V14_LOCKS]`.

The existing keyframe is reused. No master or keyframe is regenerated.

## Submission
The endpoint submits one replacement Veo operation immediately and persists:
- new `operation_name`;
- `video.status = processing`;
- `retry_count = 0`;
- revision reason;
- targeted revision metadata.

Production status becomes `videos_processing` and `next_action` is conceptually `check_existing_operations`.

## Downstream behavior
After the targeted revision endpoint succeeds, the agent must use the existing `advanceFalcoProduction` action on the same production_id.

The existing V1.3/V1.4 downstream engine remains responsible for:
- checking the new operation;
- publishing the new plan clip;
- one retry on recoverable Veo failures;
- FFmpeg fallback when required;
- rebuilding the final montage from persisted configuration;
- returning the production to `completed`.

## Agent rule
For a completed production where only one published video plan needs correction:
1. Call `retryFalcoPlanVideo` once with the corrected video prompt.
2. Never regenerate the master or keyframe.
3. Never regenerate unaffected plans.
4. Continue with `advanceFalcoProduction` until `completed` or a genuine human-blocking state.

## Non-goals
V1.4.1 does not revise:
- master images;
- keyframes;
- text overlays;
- montage creative parameters;
- end-card design.

Those remain separate explicit workflows.
