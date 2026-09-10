import json

from flask import jsonify
from google.api_core.exceptions import PreconditionFailed

from app import (
    app,
    FALCO_API_KEY,
    generate_falco_image,
    load_production_state,
    require_falco_auth,
    save_production_state,
    utc_now_iso,
)


# Preserve the validated V1.3 downstream orchestrator. The Flask route
# already points to the endpoint name "orchestrate_falco_production";
# replacing the view function below upgrades the existing Action without
# changing its URL or operationId.
_v13_orchestrator = app.view_functions["orchestrate_falco_production"]


def _response_json(response):
    status_code = 200
    response_object = response

    if isinstance(response, tuple):
        response_object = response[0]
        status_code = response[1]

    try:
        body = response_object.get_json()
    except Exception:
        body = None

    return status_code, body


def _generate_image(prompt, reference_image=None):
    payload = {"prompt": str(prompt).strip()}

    if reference_image:
        payload["reference_image"] = reference_image

    with app.test_request_context(
        "/generate-image",
        method="POST",
        json=payload,
        headers={"Authorization": f"Bearer {FALCO_API_KEY}"},
    ):
        response = generate_falco_image()

    status_code, body = _response_json(response)

    if status_code >= 400 or not isinstance(body, dict):
        raise RuntimeError(
            "Image generation failed: "
            + json.dumps(body, ensure_ascii=False)[:1800]
        )

    image_id = body.get("image_id")
    object_name = body.get("object_name")

    if not image_id or not object_name:
        raise RuntimeError("Image generation returned no usable image identifiers")

    return {
        "image_id": image_id,
        "object_name": object_name,
        "image_url": body.get("image_url"),
        "mime_type": body.get("mime_type"),
    }


def _persist_and_reload(production_id, state, generation):
    state["updated_at"] = utc_now_iso()
    state["revision"] = int(state.get("revision", 0)) + 1

    save_production_state(
        production_id,
        state,
        expected_generation=generation,
    )

    new_state, new_generation, error = load_production_state(production_id)

    if error:
        raise RuntimeError(f"Failed to reload production state after persistence: {error}")

    return new_state, new_generation


def _lock_block(state, plan):
    metadata = state.get("metadata", {})
    if not isinstance(metadata, dict):
        metadata = {}

    parts = [
        "[FALCO_V14_LOCKS]",
        "Use the persisted master image as the direct visual continuity reference.",
        "Preserve identity, clothing, environment and physical scene logic exactly.",
        "Do not reverse the persisted spatial progression.",
    ]

    for label, value in [
        ("Identity lock", metadata.get("identity_lock") or metadata.get("continuity_lock")),
        ("Environment lock", metadata.get("environment_lock")),
        ("Spatial state", plan.get("spatial_state")),
        ("Narrative state", plan.get("narrative_state")),
    ]:
        if value:
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            parts.append(f"{label}: {value}")

    return "\n".join(parts)


def _effective_keyframe_prompt(state, plan):
    prompt = str(plan.get("keyframe_prompt", "")).strip()
    if not prompt:
        return ""

    if "[FALCO_V14_LOCKS]" in prompt:
        return prompt

    return prompt + "\n\n" + _lock_block(state, plan)


def _ensure_video_prompt_locks(state, plan):
    prompt = str(plan.get("video_prompt", "")).strip()

    if not prompt or "[FALCO_V14_LOCKS]" in prompt:
        return False

    plan["video_prompt"] = prompt + "\n\n" + _lock_block(state, plan)
    return True


def _v14_reply(state, next_action, summary):
    return jsonify(
        {
            "production_id": state.get("production_id"),
            "status": state.get("status"),
            "revision": state.get("revision"),
            "advanced": True,
            "next_action": next_action,
            "summary": summary,
        }
    )


@require_falco_auth
def orchestrate_falco_production_v14(production_id):
    """
    V1.4 prepares visual assets before delegating to the validated V1.3
    Veo/retry/fallback/montage pipeline.

    It never invents creative prompts. A persisted master_prompt is required
    for the master image and each plan requires a persisted keyframe_prompt.
    One image-generation step is performed per orchestrator call so each
    expensive result is persisted immediately and requests remain bounded.
    """

    try:
        state, generation, error = load_production_state(production_id)
    except Exception as exc:
        return jsonify({
            "error": "Failed to load production state",
            "details": str(exc),
        }), 500

    if error == "invalid":
        return jsonify({"error": "Invalid production_id"}), 400
    if error == "missing":
        return jsonify({"error": "Production not found"}), 404
    if error == "corrupt":
        return jsonify({"error": "Production state is corrupt"}), 500

    if state.get("status") == "completed":
        return _v13_orchestrator(production_id)

    plans = state.get("plans", {})
    if not isinstance(plans, dict) or not plans:
        return _v13_orchestrator(production_id)

    metadata = state.setdefault("metadata", {})
    if not isinstance(metadata, dict):
        return jsonify({"error": "Production metadata is invalid"}), 500

    summary = {
        "master_generated": False,
        "keyframe_generated": None,
        "missing_keyframe_prompts": [],
        "visual_preparation": None,
    }

    master = state.get("master_image")
    master_object_name = None

    if isinstance(master, dict):
        master_object_name = master.get("object_name")

    if not master_object_name:
        master_prompt = str(metadata.get("master_prompt", "")).strip()

        if not master_prompt:
            state["status"] = "awaiting_master_prompt"
            next_action = "persist_master_prompt"
            summary["visual_preparation"] = "awaiting_master_prompt"
            metadata["orchestrator_v1_4"] = {
                "last_run_at": utc_now_iso(),
                "next_action": next_action,
            }

            try:
                state, generation = _persist_and_reload(
                    production_id, state, generation
                )
            except PreconditionFailed:
                return jsonify({
                    "error": "Production state changed concurrently. Reload and retry."
                }), 409
            except Exception as exc:
                return jsonify({
                    "error": "Failed to persist V1.4 state",
                    "details": str(exc),
                }), 500

            return _v14_reply(state, next_action, summary)

        try:
            generated = _generate_image(master_prompt)
        except Exception as exc:
            state["status"] = "blocked"
            next_action = "retry_master_image_generation"
            summary["visual_preparation"] = "master_generation_failed"
            summary["error"] = str(exc)
            metadata["orchestrator_v1_4"] = {
                "last_run_at": utc_now_iso(),
                "next_action": next_action,
            }

            try:
                state, generation = _persist_and_reload(
                    production_id, state, generation
                )
            except Exception:
                pass

            return _v14_reply(state, next_action, summary)

        state["master_image"] = {
            **generated,
            "generated_at": utc_now_iso(),
            "source": "orchestrator_v1_4",
        }
        state["status"] = "master_ready"
        next_action = "generate_missing_keyframes"
        summary["master_generated"] = True
        summary["visual_preparation"] = "master_ready"
        metadata["orchestrator_v1_4"] = {
            "last_run_at": utc_now_iso(),
            "next_action": next_action,
        }

        try:
            state, generation = _persist_and_reload(
                production_id, state, generation
            )
        except PreconditionFailed:
            return jsonify({
                "error": "Production state changed concurrently. Reload and retry."
            }), 409
        except Exception as exc:
            return jsonify({
                "error": "Failed to persist generated master image",
                "details": str(exc),
            }), 500

        return _v14_reply(state, next_action, summary)

    missing_keyframes = []
    missing_prompts = []

    for plan_key in sorted(plans.keys()):
        plan = plans.get(plan_key)
        if not isinstance(plan, dict):
            continue

        if not plan.get("keyframe_object_name"):
            missing_keyframes.append(plan_key)
            if not str(plan.get("keyframe_prompt", "")).strip():
                missing_prompts.append(plan_key)

    if missing_prompts:
        state["status"] = "awaiting_keyframe_prompts"
        next_action = "persist_missing_keyframe_prompts"
        summary["missing_keyframe_prompts"] = missing_prompts
        summary["visual_preparation"] = "awaiting_keyframe_prompts"
        metadata["orchestrator_v1_4"] = {
            "last_run_at": utc_now_iso(),
            "next_action": next_action,
        }

        try:
            state, generation = _persist_and_reload(
                production_id, state, generation
            )
        except PreconditionFailed:
            return jsonify({
                "error": "Production state changed concurrently. Reload and retry."
            }), 409
        except Exception as exc:
            return jsonify({
                "error": "Failed to persist V1.4 waiting state",
                "details": str(exc),
            }), 500

        return _v14_reply(state, next_action, summary)

    if missing_keyframes:
        plan_key = missing_keyframes[0]
        plan = plans[plan_key]
        keyframe_prompt = _effective_keyframe_prompt(state, plan)

        try:
            generated = _generate_image(
                keyframe_prompt,
                reference_image=master_object_name,
            )
        except Exception as exc:
            plan["status"] = "keyframe_generation_failed"
            state["status"] = "blocked"
            next_action = "retry_keyframe_generation"
            summary["keyframe_generated"] = plan_key
            summary["visual_preparation"] = "keyframe_generation_failed"
            summary["error"] = str(exc)
            metadata["orchestrator_v1_4"] = {
                "last_run_at": utc_now_iso(),
                "next_action": next_action,
            }

            try:
                state, generation = _persist_and_reload(
                    production_id, state, generation
                )
            except Exception:
                pass

            return _v14_reply(state, next_action, summary)

        plan["keyframe_image_id"] = generated["image_id"]
        plan["keyframe_object_name"] = generated["object_name"]
        plan["keyframe_image_url"] = generated.get("image_url")
        plan["keyframe_generated_at"] = utc_now_iso()
        plan["keyframe_source"] = "master_image"
        plan["status"] = "keyframe_ready"

        _ensure_video_prompt_locks(state, plan)

        remaining = [
            key
            for key in missing_keyframes
            if key != plan_key
        ]

        next_action = (
            "generate_missing_keyframes"
            if remaining
            else "submit_missing_video_operations"
        )
        state["status"] = (
            "keyframes_preparing"
            if remaining
            else "keyframes_ready"
        )
        summary["keyframe_generated"] = plan_key
        summary["visual_preparation"] = state["status"]
        metadata["orchestrator_v1_4"] = {
            "last_run_at": utc_now_iso(),
            "next_action": next_action,
        }

        try:
            state, generation = _persist_and_reload(
                production_id, state, generation
            )
        except PreconditionFailed:
            return jsonify({
                "error": "Production state changed concurrently. Reload and retry."
            }), 409
        except Exception as exc:
            return jsonify({
                "error": "Failed to persist generated keyframe",
                "details": str(exc),
            }), 500

        return _v14_reply(state, next_action, summary)

    # Visual assets already exist. Apply persisted continuity locks to any
    # still-unsubmitted video prompts once, persist that state, then hand off
    # to the validated V1.3 video/recovery/montage engine.
    prompts_changed = False
    for plan in plans.values():
        if isinstance(plan, dict) and not (plan.get("video") or {}).get("operation_name"):
            prompts_changed = _ensure_video_prompt_locks(state, plan) or prompts_changed

    metadata["orchestrator_v1_4"] = {
        "last_run_at": utc_now_iso(),
        "next_action": "delegate_v1_3",
        "visual_state": "ready",
    }

    if prompts_changed or "orchestrator_v1_4" not in metadata:
        try:
            state, generation = _persist_and_reload(
                production_id, state, generation
            )
        except PreconditionFailed:
            return jsonify({
                "error": "Production state changed concurrently. Reload and retry."
            }), 409
        except Exception as exc:
            return jsonify({
                "error": "Failed to persist V1.4 continuity locks",
                "details": str(exc),
            }), 500
    else:
        # Persist metadata even when prompts were already locked, so the
        # production records that V1.4 owns the visual stage.
        try:
            state, generation = _persist_and_reload(
                production_id, state, generation
            )
        except Exception as exc:
            return jsonify({
                "error": "Failed to persist V1.4 metadata",
                "details": str(exc),
            }), 500

    return _v13_orchestrator(production_id)


# Upgrade the existing Flask route in place.
app.view_functions["orchestrate_falco_production"] = orchestrate_falco_production_v14
