# FALCO Spatial Continuity Lock V1

## Purpose

Prevent physically incoherent edits between consecutive plans, such as a character appearing outside and then back inside without a scripted return.

## Principle

A FALCO production must describe not only visual identity but also the physical progression of the scene.

Each plan should persist:

- `spatial_state.location`
- `spatial_state.orientation` when relevant
- `spatial_state.entry_exit_state` when relevant
- `spatial_state.transition_to_next`
- `narrative_state.character`
- `narrative_state.allowed_action`
- `narrative_state.forbidden_actions` when needed

## Example

```json
"plan_01": {
  "spatial_state": {
    "location": "inside",
    "entry_exit_state": "door_closed_or_opening",
    "transition_to_next": "threshold"
  },
  "narrative_state": {
    "character": "preparing",
    "allowed_action": "prepare_and_reach_for_exit"
  }
}
```

```json
"plan_02": {
  "spatial_state": {
    "location": "threshold",
    "entry_exit_state": "door_open",
    "transition_to_next": "outside"
  },
  "narrative_state": {
    "character": "exiting",
    "allowed_action": "cross_threshold_and_begin_run"
  }
}
```

```json
"plan_03": {
  "spatial_state": {
    "location": "outside",
    "entry_exit_state": "door_behind_character",
    "transition_to_next": null
  },
  "narrative_state": {
    "character": "running",
    "allowed_action": "steady_run"
  }
}
```

## Rule

The storyboard is authoritative. The generator must not reverse the persisted spatial progression unless such a reversal is explicitly part of the storyboard.
