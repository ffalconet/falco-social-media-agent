# FALCO Visual Continuity & QA Protocol V1 — Orchestrator V1.4.2

## Objective
Make AI generation visually invisible by preventing continuity drift before expensive generation starts.

## Core rule
One shot = one primary physical action.

Do not ask Veo to combine preparation, standing, opening a door, crossing a threshold and running in one shot. Split narrative changes across cuts. Prefer a simple credible shot over a complex generative transition.

## Mandatory visual contract
Every new production must persist metadata.visual_contract before the first generated asset:

- character: immutable physical identity
- wardrobe: exact garments, colors, materials and lengths
- footwear: exact shoes and lace state
- accessories: optional but immutable when present
- environment: authorized locations and stable scene geometry
- object_state: state of relevant objects before generation
- forbidden_changes: explicit visual changes that must never happen

Existing identity_lock/continuity_lock and environment_lock remain required.

## Mandatory per-plan state
Every plan must persist:
- keyframe_prompt
- video_prompt
- spatial_state
- narrative_state
- primary_action
- action_lock
- start_state
- end_state

The primary_action must describe one physical action only. action_lock must explicitly forbid secondary actions.

## Irreversible state
A plan may advance the story but must not reverse a persisted state unless explicitly requested.
Examples:
- once outside, never return inside;
- once shoes are tied, laces remain tied;
- clothing cannot appear/disappear/change;
- a door/location transition occurs only when explicitly assigned to that plan.

## Prompt enforcement
V1.4.2 appends FALCO_VISUAL_CONTINUITY_V1 to keyframe and video prompts. It repeats the visual contract, start/end state, action lock and physical realism constraints.

## Preflight gate
Before any new production generates its MASTER, advanceFalcoProduction checks that the complete creative state and montage config are persisted.

If incomplete:
- status: awaiting_preflight
- next_action: persist_preflight_requirements
- no paid generation is started.

The gate also requires complete montage configuration including plan_durations, texts, transition, end_card and logo.

## Veo quota guard
V1.4.2 spaces new Veo submissions and converts quota/rate-limit conditions into:
- status: quota_wait
- next_action: retry_after_quota_reset

A quota event must never trigger an immediate resubmission loop.

## Visual QA roadmap
V1.4.2 establishes deterministic continuity constraints and prevents structurally unsafe generations. Automated frame-level visual QA is the next isolated layer: compare generated keyframes/video frames against the visual contract and reject a clip before montage when identity, wardrobe, footwear, spatial state, anatomy or object state drift.

Until that automated visual classifier is added, production review should reject any clip showing:
- wardrobe or footwear drift;
- lace/object-state inconsistency;
- wrong room/location;
- reversed doorway/spatial movement;
- extra actions;
- anatomical or locomotion artifacts;
- newly invented objects/persons.

## 6H42 example
Prefer:
1. wake up — one action;
2. final preparation, shoes already tied — one action;
3. already outside, begin running — one action.

Avoid a single Veo shot that ties shoes, stands, opens a door, crosses the threshold and starts running.
