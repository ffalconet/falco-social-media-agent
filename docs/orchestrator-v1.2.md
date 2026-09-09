# FALCO Orchestrator V1.2

## Goal

V1.2 completes the production pipeline from persisted clips to the final montage.

It does not invent montage choices.

## Required montage state

Before automatic montage, persist:

```json
{
  "montage": {
    "plan_durations": {
      "plan_01": 3,
      "plan_02": 3,
      "plan_03": 3
    },
    "texts": [
      {
        "text": "Example",
        "start": 0.5,
        "end": 4.0,
        "weight": "bold",
        "size": 58,
        "position": "bottom",
        "color": "cream"
      }
    ],
    "transition": {
      "type": "crossfade",
      "duration": 0.2
    },
    "end_card": {
      "enabled": true,
      "duration": 2
    },
    "logo": {
      "enabled": false
    }
  }
}
```

`plan_starts` is optional and defaults to 0 for each video plan.

## Safety rule

If any required montage field is missing, the orchestrator must:

- not render;
- set production status to `awaiting_montage_config`;
- return `persist_montage_config`;
- list the missing fields.

## Automatic montage

When all plans have usable assets and montage config is complete:

1. build clips in plan order;
2. use published GCS filenames or explicit FFmpeg image fallback assets;
3. call the validated montage engine;
4. persist final filename, video_url and durations;
5. set production status to `completed`.

## Completion

A completed production contains:

- final montage filename;
- final video_url;
- content_duration;
- final_duration;
- transition actually used;
- end-card metadata.

The orchestrator must not re-render a montage that already has both filename and video_url persisted.
