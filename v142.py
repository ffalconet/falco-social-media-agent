import json
import threading
import time

from flask import jsonify

import app as base
import v14
import v141
from v141 import app
from app import load_production_state, require_falco_auth

# FALCO Orchestrator V1.4.2
# Visual Continuity Gate + production preflight + Veo quota guard.
#
# This layer deliberately does NOT invent creative decisions. It verifies that
# the agent persisted a complete visual contract before paid generation starts,
# strengthens every keyframe/video prompt with that contract, and prevents
# rapid Veo resubmission when quota is exhausted.

_v141_orchestrator = app.view_functions["orchestrate_falco_production"]
_original_lock_block = v14._lock_block
_original_submit_veo = base.submit_veo_for_persisted_plan

_submit_lock = threading.Lock()
_last_submit_at = 0.0
MIN_VEO_SUBMISSION_INTERVAL_SECONDS = 31.0


class FalcoQuotaWait(RuntimeError):
    pass


def _as_text(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).strip()


def _enhanced_lock_block(state, plan):
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    visual = metadata.get("visual_contract") if isinstance(metadata.get("visual_contract"), dict) else {}

    parts = [
        _original_lock_block(state, plan),
        "",
        "[FALCO_VISUAL_CONTINUITY_V1]",
        "ABSOLUTE RULE: visual continuity is more important than visual novelty.",
        "Do not redesign, reinterpret, restyle or improve the person, wardrobe, shoes, accessories or environment.",
        "Preserve all visible object states exactly unless this plan explicitly changes one of them.",
        "ONE SHOT = ONE PRIMARY PHYSICAL ACTION. Do not add secondary actions.",
    ]

    fields = [
        ("Character reference", visual.get("character")),
        ("Wardrobe lock", visual.get("wardrobe")),
        ("Footwear lock", visual.get("footwear")),
        ("Accessory lock", visual.get("accessories")),
        ("Environment lock", visual.get("environment")),
        ("Object-state lock", visual.get("object_state")),
        ("Forbidden changes", visual.get("forbidden_changes")),
        ("Primary action", plan.get("primary_action")),
        ("Action lock", plan.get("action_lock")),
        ("Start state", plan.get("start_state")),
        ("End state", plan.get("end_state")),
        ("Spatial state", plan.get("spatial_state")),
        ("Narrative state", plan.get("narrative_state")),
    ]

    for label, value in fields:
        rendered = _as_text(value)
        if rendered:
            parts.append(f"{label}: {rendered}")

    parts.extend([
        "No clothing may appear, disappear, change color, change material or change length.",
        "Shoes and laces must remain physically coherent and keep their persisted state.",
        "No new object, room, door, accessory or person may appear unless explicitly persisted.",
        "Do not cross a doorway or change location unless the primary action explicitly requires it.",
        "Never reverse an irreversible spatial state (for example outside -> inside).",
        "Human anatomy, gait, hands, feet and object interactions must remain realistic.",
        "If the requested action would require inventing an unpersisted transition, keep the subject in the persisted location and perform only the primary action.",
    ])

    return "\n".join(parts)


# Patch both modules because v141 imported _lock_block by value.
v14._lock_block = _enhanced_lock_block
v141._lock_block = _enhanced_lock_block


def _is_quota_error(exc):
    text = str(exc).lower()
    return (
        "429" in text
        or "resource_exhausted" in text
        or "resource exhausted" in text
        or "rate limit" in text
        or "quota" in text
    )


def _quota_guarded_submit(prompt, image_object_name):
    global _last_submit_at

    with _submit_lock:
        now = time.monotonic()
        elapsed = now - _last_submit_at

        if _last_submit_at and elapsed < MIN_VEO_SUBMISSION_INTERVAL_SECONDS:
            wait = int(MIN_VEO_SUBMISSION_INTERVAL_SECONDS - elapsed) + 1
            raise FalcoQuotaWait(
                f"FALCO_QUOTA_WAIT retry_after_seconds={wait}; do not resubmit immediately"
            )

        try:
            operation_name = _original_submit_veo(prompt, image_object_name)
        except Exception as exc:
            if _is_quota_error(exc):
                raise FalcoQuotaWait(
                    "FALCO_QUOTA_WAIT Veo quota/rate limit reached; preserve state and wait"
                ) from exc
            raise

        _last_submit_at = time.monotonic()
        return operation_name


# app.py V1.3 resolves this global at runtime; v141 captured it by import.
base.submit_veo_for_persisted_plan = _quota_guarded_submit
v141.submit_veo_for_persisted_plan = _quota_guarded_submit


def _generation_started(state):
    master = state.get("master_image")
    if isinstance(master, dict) and master.get("object_name"):
        return True

    plans = state.get("plans")
    if isinstance(plans, dict):
        for plan in plans.values():
            if not isinstance(plan, dict):
                continue
            if plan.get("keyframe_object_name"):
                return True
            video = plan.get("video")
            if isinstance(video, dict) and (video.get("operation_name") or video.get("filename")):
                return True
    return False


def _preflight_missing(state):
    missing = []
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}

    if not _as_text(metadata.get("master_prompt")):
        missing.append("metadata.master_prompt")
    if not (metadata.get("identity_lock") or metadata.get("continuity_lock")):
        missing.append("metadata.identity_lock_or_continuity_lock")
    if not metadata.get("environment_lock"):
        missing.append("metadata.environment_lock")

    visual = metadata.get("visual_contract")
    if not isinstance(visual, dict):
        missing.append("metadata.visual_contract")
    else:
        for field in (
            "character",
            "wardrobe",
            "footwear",
            "environment",
            "object_state",
            "forbidden_changes",
        ):
            if not _as_text(visual.get(field)):
                missing.append(f"metadata.visual_contract.{field}")

    plans = state.get("plans")
    if not isinstance(plans, dict) or not plans:
        missing.append("plans")
    else:
        for key in sorted(plans):
            plan = plans.get(key)
            if not isinstance(plan, dict):
                missing.append(f"plans.{key}")
                continue
            for field in (
                "keyframe_prompt",
                "video_prompt",
                "spatial_state",
                "narrative_state",
                "primary_action",
                "action_lock",
                "start_state",
                "end_state",
            ):
                if not _as_text(plan.get(field)):
                    missing.append(f"plans.{key}.{field}")

    montage = state.get("montage")
    if not isinstance(montage, dict):
        missing.append("montage")
    else:
        for field in ("plan_durations", "texts", "transition", "end_card", "logo"):
            if field not in montage:
                missing.append(f"montage.{field}")

        durations = montage.get("plan_durations")
        if isinstance(plans, dict) and isinstance(durations, dict):
            for key in sorted(plans):
                if key not in durations:
                    missing.append(f"montage.plan_durations.{key}")

    return missing


@require_falco_auth
def orchestrate_falco_production_v142(production_id):
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

    # Strict gate only for new productions. Existing productions and targeted
    # revisions remain resumable for backwards compatibility.
    if state.get("status") != "completed" and not _generation_started(state):
        missing = _preflight_missing(state)
        if missing:
            return jsonify({
                "production_id": production_id,
                "status": "awaiting_preflight",
                "revision": state.get("revision"),
                "advanced": False,
                "next_action": "persist_preflight_requirements",
                "summary": {
                    "visual_continuity_gate": "failed",
                    "missing": missing,
                    "message": "No paid generation started. Persist the complete visual contract, one-action plan states and montage config first.",
                },
            })

    try:
        return _v141_orchestrator(production_id)
    except FalcoQuotaWait as exc:
        return jsonify({
            "production_id": production_id,
            "status": "quota_wait",
            "revision": state.get("revision"),
            "advanced": False,
            "next_action": "retry_after_quota_reset",
            "summary": {
                "quota": "wait",
                "submitted": False,
                "details": str(exc),
                "instruction": "Preserve this production. Do not create a new production and do not retry immediately.",
            },
        }), 429


app.view_functions["orchestrate_falco_production"] = orchestrate_falco_production_v142
