# FALCO Agent Production Persistence Rules

Use this document as mandatory operational behavior for every production.

## Core rule

Every production must have a persistent `production_id`.

Never rely on chat history as the only place where these values exist:

- master image object_name;
- keyframe object_name;
- Veo operation_name;
- video_uri;
- published GCS filename;
- fallback asset;
- montage filename;
- final video_url.

Persist them immediately with `updateFalcoProduction`.

## Starting a new production

Before any generation:

1. call `createFalcoProduction`;
2. save the returned `production_id`;
3. continue only with this production_id.

Do not create a second production for the same workflow unless explicitly requested.

## Resuming

If given an existing production_id:

1. call `getFalcoProduction`;
2. trust persisted state over conversation memory;
3. find the first incomplete step;
4. continue from there;
5. reuse every completed asset.

Never regenerate a completed step only because its value is missing from chat context.

## Mandatory checkpoints

Call `updateFalcoProduction` immediately after:

1. master image generation;
2. each keyframe generation;
3. each Veo submission;
4. each Veo completion or failure;
5. each publish action;
6. each fallback decision;
7. final montage completion.

## Retry rule

Retry only the failed plan.

Never restart the whole production because one plan failed.

## Fallback rule

If a plan uses FFmpeg image fallback, record it explicitly in the production state.

## Stop behavior

If the run cannot continue because Veo is still processing or a technical action fails:

return the production_id and current persisted status.

Do not create replacement assets unless the protocol explicitly requires a retry.

## Completion

A production is completed only when the final montage is saved and its result is persisted.

The final response should include at least:

- production_id;
- final status;
- final video_url;
- final montage filename.
