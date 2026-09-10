import copy
import json

from flask import jsonify, request
from google.api_core.exceptions import PreconditionFailed

from v14 import app, _lock_block
from app import (
    load_production_state,
    require_falco_auth,
    save_production_state,
    submit_veo_for_persisted_plan,
    utc_now_iso,
)


def _persist_state(production_id, state, generation):
    state["updated_at"] = utc_now_iso()
    state["revision"] = int(state.get("revision", 0)) + 1
    save_production_state(
        production_id,
        state,
        expected_generation=generation,
    )


def _clean_montage_for_rebuild(montage):
    """Keep creative montage config but invalidate generated output fields."""
    if not isinstance(montage, dict):
        return {}

    cleaned = copy.deepcopy(montage)

    for key in (
        "filename",
        "video_url",
        "video_id",
        "content_duration",
        "final_duration",
        "output",
        "completed_at",
        "rendered_at",
        "error",
    ):
        cleaned.pop(key, None)

    cleaned["status"] = "invalidated_for_plan_revision"
    cleaned["invalidated_at"] = utc_now_iso()
    return cleaned


def _revision_history_entry(plan_key, old_plan, old_montage, reason):
    return {
        "revised_at": utc_now_iso(),
        "plan_key": plan_key,
        "reason": reason,
        "previous_video": copy.deepcopy(old_plan.get("video")),
        "previous_plan_status": old_plan.get("status"),
        "previous_mode": old_plan.get("mode"),
        "previous_source_image": old_plan.get("source_image"),
        "previous_montage": {
            "filename": old_montage.get("filename") if isinstance(old_montage, dict) else None,
            "video_url": old_montage.get("video_url") if isinstance(old_montage, dict) else None,
            "status": old_montage.get("status") if isinstance(old_montage, dict) else None,
        },
    }


@app.post("/productions/<production_id>/plans/<plan_key>/revise-video")
@require_falco_auth
def revise_falco_plan_video(production_id, plan_key):
    """
    V1.4.1 targeted plan revision.

    Reuses the persisted keyframe, submits exactly one replacement Veo job,
    preserves all other plans and invalidates only the final montage output.
    The regular advanceFalcoProduction orchestrator then owns status checks,
    retry/fallback, publication and montage rebuild.
    """
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be an object"}), 400

    new_prompt = str(data.get("video_prompt", "")).strip()
    reason = str(data.get("reason", "targeted_plan_revision")).strip()

    if not new_prompt:
        return jsonify({"error": "video_prompt is required"}), 400
    if len(new_prompt) > 5000:
        return jsonify({"error": "video_prompt exceeds 5000 characters"}), 400
    if len(reason) > 500:
        return jsonify({"error": "reason exceeds 500 characters"}), 400

    try:
        state, generation, error = load_production_state(production_id)
    except Exception as exc:
        return jsonify({"error": "Failed to load production state", "details": str(exc)}), 500

    if error == "invalid":
        return jsonify({"error": "Invalid production_id"}), 400
    if error == "missing":
        return jsonify({"error": "Production not found"}), 404
    if error == "corrupt":
        return jsonify({"error": "Production state is corrupt"}), 500

    if state.get("status") != "completed":
        return jsonify({
            "error": "Targeted revision requires a completed production",
            "current_status": state.get("status"),
        }), 409

    plans = state.get("plans")
    if not isinstance(plans, dict) or plan_key not in plans or not isinstance(plans[plan_key], dict):
        return jsonify({"error": "Plan not found", "plan_key": plan_key}), 404

    plan = plans[plan_key]
    keyframe_object_name = plan.get("keyframe_object_name")
    if not keyframe_object_name:
        return jsonify({
            "error": "Targeted revision requires the existing persisted keyframe",
            "plan_key": plan_key,
        }), 409

    current_video = plan.get("video")
    if not isinstance(current_video, dict) or current_video.get("status") != "published":
        return jsonify({
            "error": "Targeted revision requires a published plan video",
            "plan_key": plan_key,
            "video_status": current_video.get("status") if isinstance(current_video, dict) else None,
        }), 409

    existing_revision = plan.get("targeted_revision")
    if isinstance(existing_revision, dict) and existing_revision.get("status") in {"submitted", "processing"}:
        return jsonify({
            "error": "A targeted revision is already active for this plan",
            "plan_key": plan_key,
            "operation_name": existing_revision.get("operation_name"),
        }), 409

    # Persist a full trace before mutating the selected plan.
    history = plan.setdefault("video_revision_history", [])
    if not isinstance(history, list):
        history = []
        plan["video_revision_history"] = history

    old_plan = copy.deepcopy(plan)
    old_montage = copy.deepcopy(state.get("montage", {}))
    history.append(_revision_history_entry(plan_key, old_plan, old_montage, reason))

    # Apply V1.4 continuity locks to the replacement prompt, once.
    if "[FALCO_V14_LOCKS]" not in new_prompt:
        new_prompt = new_prompt + "\n\n" + _lock_block(state, plan)

    try:
        operation_name = submit_veo_for_persisted_plan(
            new_prompt,
            keyframe_object_name,
        )
    except FileNotFoundError:
        return jsonify({
            "error": "Persisted keyframe not found in storage",
            "keyframe_object_name": keyframe_object_name,
        }), 404
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:
        return jsonify({
            "error": "Failed to submit targeted Veo revision",
            "details": str(exc),
        }), 502

    # Only the selected video is reopened. Master, keyframe and other plans stay untouched.
    plan["video_prompt"] = new_prompt
    plan["status"] = "video_processing"
    plan.pop("mode", None)
    plan.pop("source_image", None)

    for key in ("filename", "video_uri", "video_url", "published_filename"):
        plan.pop(key, None)

    plan["video"] = {
        "status": "processing",
        "operation_name": operation_name,
        "submitted_at": utc_now_iso(),
        "retry_count": 0,
        "revision_reason": reason,
        "revision_of": history[-1].get("previous_video"),
    }
    plan["targeted_revision"] = {
        "status": "processing",
        "operation_name": operation_name,
        "reason": reason,
        "started_at": utc_now_iso(),
        "keyframe_object_name": keyframe_object_name,
    }

    state["montage"] = _clean_montage_for_rebuild(state.get("montage", {}))
    state["status"] = "videos_processing"

    metadata = state.setdefault("metadata", {})
    if isinstance(metadata, dict):
        metadata["orchestrator_v1_4_1"] = {
            "last_targeted_revision_at": utc_now_iso(),
            "plan_key": plan_key,
            "reason": reason,
            "next_action": "check_existing_operations",
        }

    try:
        _persist_state(production_id, state, generation)
    except PreconditionFailed:
        return jsonify({
            "error": "Production state changed concurrently. Reload and retry."
        }), 409
    except Exception as exc:
        return jsonify({
            "error": "Failed to persist targeted revision state",
            "details": str(exc),
        }), 500

    return jsonify({
        "production_id": production_id,
        "plan_key": plan_key,
        "status": state.get("status"),
        "revision": state.get("revision"),
        "revision_status": "processing",
        "operation_name": operation_name,
        "next_action": "check_existing_operations",
        "preserved": {
            "master_image": True,
            "keyframe": True,
            "other_plans": True,
            "montage_config": True,
        },
    })
