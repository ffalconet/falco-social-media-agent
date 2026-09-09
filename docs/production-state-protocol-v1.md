# FALCO Production State Protocol V1

## Purpose

This document defines the mandatory persistence rules for every FALCO social-media production.

The goal is simple:

**The agent must never depend on conversation memory to recover a production.**

Every production must have a persistent `production_id`, and every significant asset or state change must be saved immediately through the FALCO Production State API.

---

## Mandatory rule

Before any image generation, video generation or montage:

1. create a production with `createFalcoProduction`;
2. keep the returned `production_id`;
3. use this same `production_id` for the entire production lifecycle.

If a production already exists, do **not** create a new one. Use `getFalcoProduction` and resume from its saved state.

---

## Source of truth

The persistent production state is the source of truth.

Conversation history is secondary.

If conversation memory and persisted production state disagree:

**trust persisted production state.**

Never invent an `object_name`, `operation_name`, `filename`, status or duration that is absent from the saved state.

---

## Persistent lifecycle

A normal production follows this sequence:

```text
create production
→ save master image
→ save each keyframe
→ save each Veo operation_name
→ save each completed/published clip filename
→ save fallback decision if used
→ save montage result
→ mark production completed
```

Every successful step must be persisted before moving to the next dependent step.

---

## Recommended statuses

Use clear production-level statuses.

```text
created
master_ready
keyframes_ready
videos_submitted
videos_processing
clips_ready
montage_ready
completed
blocked
failed
```

The agent may use a more precise status when useful, but must remain concise and deterministic.

---

## State structure

The state should use this structure:

```json
{
  "production_id": "...",
  "label": "POST 04",
  "status": "videos_processing",
  "master_image": {
    "object_name": "falco/images/..."
  },
  "plans": {
    "plan_01": {
      "keyframe_object_name": "falco/images/...",
      "video": {
        "operation_name": "models/veo-...",
        "status": "processing",
        "video_uri": null,
        "filename": null
      }
    }
  },
  "montage": {},
  "metadata": {}
}
```

Only save confirmed values.

---

## Master image

Immediately after `generateFalcoImage` successfully creates the master image, call `updateFalcoProduction`.

Example patch:

```json
{
  "status": "master_ready",
  "master_image": {
    "object_name": "falco/images/master.jpg"
  }
}
```

Do not generate keyframes before the master image state has been persisted.

---

## Keyframes

Every keyframe must be saved immediately after successful generation.

Example:

```json
{
  "plans": {
    "plan_01": {
      "keyframe_object_name": "falco/images/keyframe-01.jpg"
    }
  }
}
```

Keyframes must continue to follow the FALCO continuity rule:

**all keyframes derive directly from the same master image.**

---

## Veo generation

Immediately after `generateFalcoVideo` returns an `operation_name`, save it before submitting another dependent action.

Example:

```json
{
  "status": "videos_submitted",
  "plans": {
    "plan_01": {
      "video": {
        "operation_name": "models/veo-3.1-lite-generate-preview/operations/...",
        "status": "processing"
      }
    }
  }
}
```

Never lose an `operation_name` by relying only on conversation history.

---

## Video status

After `getFalcoVideoStatus`:

### If processing

Persist:

```json
{
  "plans": {
    "plan_01": {
      "video": {
        "status": "processing"
      }
    }
  }
}
```

Then stop cleanly if the current execution cannot continue.

Do not resubmit the same generation.

### If done with video_uri

Persist the result, then publish.

### If done without video_uri

Persist the failure state and reason if available.

Do not invent a result.

---

## Publish

Immediately after `publishFalcoVideo` succeeds, persist the GCS filename.

Example:

```json
{
  "plans": {
    "plan_01": {
      "video": {
        "status": "published",
        "filename": "falco/abc.mp4"
      }
    }
  }
}
```

The GCS filename is the canonical published clip reference.

---

## Retry policy

Do not restart an entire production because one plan fails.

Default behavior:

1. keep the master and all successful keyframes;
2. retry only the failed plan when appropriate;
3. reuse all successful clips;
4. never regenerate successful plans without an explicit reason.

Every retry must update only the affected plan state.

---

## Fallback policy

If Veo repeatedly fails or remains unusable for one plan, the agent may use the validated FFmpeg image fallback when appropriate.

Persist the fallback explicitly.

Example:

```json
{
  "plans": {
    "plan_03": {
      "mode": "ffmpeg_image_fallback",
      "source_image": "falco/images/keyframe-03.jpg",
      "status": "ready"
    }
  }
}
```

Do not describe a fallback plan as a Veo-generated clip.

---

## Montage

Before montage, read the production state and verify that every required plan has a usable asset.

The montage request must be built from persisted filenames / fallback image assets, not from conversation memory.

After `montageFalcoVideo` succeeds, persist:

```json
{
  "status": "completed",
  "montage": {
    "filename": "falco/montages/final.mp4",
    "video_url": "https://...",
    "content_duration": 9.0,
    "final_duration": 11.0,
    "transition": {
      "type": "crossfade",
      "duration": 0.2
    },
    "end_card": "falco_premium_v1"
  }
}
```

Only mark the production `completed` after the final montage exists.

---

## Resume command

When the founder says:

```text
Reprends la production <production_id>
```

the agent must:

1. call `getFalcoProduction`;
2. inspect the persisted state;
3. identify the first incomplete required step;
4. resume from that step only;
5. reuse all persisted assets;
6. never regenerate completed steps unless explicitly requested.

No new production may be created for a resume request.

---

## PRODUIRE POST X integration

For every `PRODUIRE POST X` command:

### Before generation

Create a production if none exists.

Save:
- label;
- relevant metadata;
- production_id.

### During production

Persist after every successful state-changing step.

### If execution stops

Return:
- production_id;
- current status;
- next incomplete step.

### On resume

Start with `getFalcoProduction`.

---

## Failure rule

If a technical error occurs:

- save the last confirmed state when possible;
- do not overwrite confirmed assets;
- do not invent missing values;
- do not silently start a replacement production.

If continuation would risk corrupting continuity or duplicating expensive generation, stop and return the `production_id`.

---

## Final principle

A FALCO production is not a chat session.

It is a persistent workflow identified by a `production_id`.

The agent may lose conversational context.

The production state must not.
