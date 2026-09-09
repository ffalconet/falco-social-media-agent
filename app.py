import base64
import json
import mimetypes
from flask import Response

import shlex

import tempfile
from pathlib import Path

import subprocess

import google.auth
from google.auth.transport.requests import Request as GoogleAuthRequest

import os
from functools import wraps
from urllib.parse import urlparse

import requests
from flask import Flask, request, jsonify

from flask import send_file

import uuid
from datetime import datetime, timedelta, timezone
from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage

app = Flask(__name__)

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
FALCO_API_KEY = os.environ.get("FALCO_API_KEY", "").strip()

MODEL = "veo-3.1-lite-generate-preview"
BUCKET_NAME = "falco-video-output-549259282828"

def get_gcs_image_as_base64(object_name):
    storage_client = storage.Client()
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(object_name)

    if not blob.exists():
        raise FileNotFoundError(f"Image not found: {object_name}")

    image_bytes = blob.download_as_bytes()

    mime_type = blob.content_type
    if not mime_type:
        mime_type, _ = mimetypes.guess_type(object_name)

    if not mime_type:
        mime_type = "image/png"

    return base64.b64encode(image_bytes).decode("utf-8"), mime_type


def require_falco_auth(func):
    @wraps(func)
    def wrapper(*args, **kwargs):

        if not FALCO_API_KEY:
            return jsonify({
                "error": "FALCO_API_KEY is not configured"
            }), 500

        authorization = request.headers.get("Authorization", "")

        expected = f"Bearer {FALCO_API_KEY}"

        if authorization != expected:
            return jsonify({
                "error": "Unauthorized"
            }), 401

        return func(*args, **kwargs)

    return wrapper



# ============================================================
# PRODUCTION STATE V1
# ============================================================

PRODUCTION_PREFIX = "falco/productions"


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def validate_production_id(production_id):
    try:
        uuid.UUID(production_id)
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def production_object_name(production_id):
    return (
        f"{PRODUCTION_PREFIX}/"
        f"{production_id}/state.json"
    )


def merge_state_dict(target, patch):
    """Recursively merge patch into target without replacing whole subtrees."""
    for key, value in patch.items():
        if (
            isinstance(value, dict)
            and isinstance(target.get(key), dict)
        ):
            merge_state_dict(target[key], value)
        else:
            target[key] = value

    return target


def load_production_state(production_id):
    if not validate_production_id(production_id):
        return None, None, "invalid"

    storage_client = storage.Client()
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(
        production_object_name(production_id)
    )

    if not blob.exists():
        return None, None, "missing"

    blob.reload()

    try:
        state = json.loads(
            blob.download_as_text(
                encoding="utf-8"
            )
        )
    except (ValueError, TypeError):
        return None, None, "corrupt"

    return state, blob.generation, None


def save_production_state(
    production_id,
    state,
    expected_generation=None
):
    storage_client = storage.Client()
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(
        production_object_name(production_id)
    )

    payload = json.dumps(
        state,
        ensure_ascii=False,
        indent=2,
        sort_keys=True
    )

    if expected_generation is None:
        blob.upload_from_string(
            payload,
            content_type="application/json",
            if_generation_match=0
        )
    else:
        blob.upload_from_string(
            payload,
            content_type="application/json",
            if_generation_match=expected_generation
        )


@app.post("/productions")
@require_falco_auth
def create_falco_production():

    data = request.get_json(silent=True) or {}

    if not isinstance(data, dict):
        return jsonify({
            "error": "Request body must be an object"
        }), 400

    label = data.get("label")

    if label is not None:
        if (
            not isinstance(label, str)
            or not label.strip()
            or len(label.strip()) > 200
        ):
            return jsonify({
                "error": (
                    "label must be a non-empty string "
                    "of at most 200 characters"
                )
            }), 400

        label = label.strip()

    metadata = data.get("metadata", {})

    if not isinstance(metadata, dict):
        return jsonify({
            "error": "metadata must be an object"
        }), 400

    production_id = str(uuid.uuid4())
    now = utc_now_iso()

    state = {
        "schema_version": 1,
        "production_id": production_id,
        "label": label,
        "status": "created",
        "created_at": now,
        "updated_at": now,
        "revision": 1,
        "master_image": None,
        "plans": {},
        "montage": {},
        "metadata": metadata
    }

    try:
        save_production_state(
            production_id,
            state
        )
    except PreconditionFailed:
        return jsonify({
            "error": "Production ID conflict"
        }), 409
    except Exception as e:
        return jsonify({
            "error": "Failed to create production state",
            "details": str(e)
        }), 500

    return jsonify(state), 201


@app.get("/productions/<production_id>")
@require_falco_auth
def get_falco_production(production_id):

    try:
        state, generation, error = (
            load_production_state(production_id)
        )
    except Exception as e:
        return jsonify({
            "error": "Failed to load production state",
            "details": str(e)
        }), 500

    if error == "invalid":
        return jsonify({
            "error": "Invalid production_id"
        }), 400

    if error == "missing":
        return jsonify({
            "error": "Production not found"
        }), 404

    if error == "corrupt":
        return jsonify({
            "error": "Production state is corrupt"
        }), 500

    return jsonify(state)


@app.patch("/productions/<production_id>")
@require_falco_auth
def update_falco_production(production_id):

    data = request.get_json(silent=True) or {}

    if not isinstance(data, dict) or not data:
        return jsonify({
            "error": "Patch body must be a non-empty object"
        }), 400

    allowed_fields = {
        "label",
        "status",
        "master_image",
        "plans",
        "montage",
        "metadata"
    }

    unknown_fields = (
        set(data.keys()) - allowed_fields
    )

    if unknown_fields:
        return jsonify({
            "error": "Unsupported production fields",
            "fields": sorted(unknown_fields)
        }), 400

    if "label" in data:
        label = data["label"]

        if label is not None and (
            not isinstance(label, str)
            or not label.strip()
            or len(label.strip()) > 200
        ):
            return jsonify({
                "error": (
                    "label must be null or a non-empty "
                    "string of at most 200 characters"
                )
            }), 400

        if isinstance(label, str):
            data["label"] = label.strip()

    if "status" in data:
        status_value = data["status"]

        if (
            not isinstance(status_value, str)
            or not status_value.strip()
            or len(status_value.strip()) > 80
        ):
            return jsonify({
                "error": (
                    "status must be a non-empty string "
                    "of at most 80 characters"
                )
            }), 400

        data["status"] = status_value.strip()

    for field in [
        "master_image",
        "plans",
        "montage",
        "metadata"
    ]:
        if (
            field in data
            and data[field] is not None
            and not isinstance(data[field], dict)
        ):
            return jsonify({
                "error": f"{field} must be an object or null"
            }), 400

    try:
        state, generation, error = (
            load_production_state(production_id)
        )
    except Exception as e:
        return jsonify({
            "error": "Failed to load production state",
            "details": str(e)
        }), 500

    if error == "invalid":
        return jsonify({
            "error": "Invalid production_id"
        }), 400

    if error == "missing":
        return jsonify({
            "error": "Production not found"
        }), 404

    if error == "corrupt":
        return jsonify({
            "error": "Production state is corrupt"
        }), 500

    merge_state_dict(
        state,
        data
    )

    state["updated_at"] = utc_now_iso()
    state["revision"] = int(
        state.get("revision", 0)
    ) + 1

    try:
        save_production_state(
            production_id,
            state,
            expected_generation=generation
        )
    except PreconditionFailed:
        return jsonify({
            "error": (
                "Production state changed concurrently. "
                "Reload and retry."
            )
        }), 409
    except Exception as e:
        return jsonify({
            "error": "Failed to update production state",
            "details": str(e)
        }), 500

    return jsonify(state)


# ============================================================
# PRODUCTION ORCHESTRATOR V1
# ============================================================

def extract_veo_video_uri(result):
    try:
        return (
            result["response"]
            ["generateVideoResponse"]
            ["generatedSamples"][0]
            ["video"]["uri"]
        )
    except (KeyError, IndexError, TypeError):
        return None


def extract_veo_failure_reason(result):
    try:
        reasons = (
            result["response"]
            ["generateVideoResponse"]
            .get("raiMediaFilteredReasons")
        )

        if reasons:
            return {
                "type": "rai_media_filtered",
                "reasons": reasons
            }
    except (KeyError, TypeError):
        pass

    if isinstance(result.get("error"), dict):
        return {
            "type": "veo_error",
            "error": result["error"]
        }

    return {
        "type": "no_video_uri",
        "message": (
            "Veo operation completed without a usable video_uri"
        )
    }


def safe_plan_key(plan_key):
    cleaned = "".join(
        char
        for char in str(plan_key)
        if char.isalnum() or char in ("_", "-")
    )

    if not cleaned:
        raise ValueError("Invalid plan key")

    return cleaned[:80]


def submit_veo_for_persisted_plan(
    prompt,
    image_object_name
):
    if not prompt:
        raise ValueError("Missing persisted video_prompt")

    if len(prompt) > 5000:
        raise ValueError("Persisted video_prompt is too long")

    image_b64, mime_type = get_gcs_image_as_base64(
        image_object_name
    )

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{MODEL}:predictLongRunning"
    )

    payload = {
        "instances": [
            {
                "prompt": prompt,
                "image": {
                    "bytesBase64Encoded": image_b64,
                    "mimeType": mime_type
                }
            }
        ],
        "parameters": {
            "aspectRatio": "9:16"
        }
    }

    response = requests.post(
        url,
        headers={
            "x-goog-api-key": GEMINI_API_KEY,
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=60
    )

    if not response.ok:
        raise RuntimeError(
            "Veo submission failed: "
            f"{response.status_code} "
            f"{response.text[:1500]}"
        )

    result = response.json()
    operation_name = result.get("name")

    if not operation_name:
        raise RuntimeError(
            "Veo submission returned no operation_name"
        )

    return operation_name


def publish_veo_uri_for_production(
    production_id,
    plan_key,
    video_uri
):
    parsed_url = urlparse(video_uri)

    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname
        != "generativelanguage.googleapis.com"
    ):
        raise ValueError("Invalid Veo video URI")

    response = requests.get(
        video_uri,
        headers={
            "x-goog-api-key": GEMINI_API_KEY
        },
        timeout=180
    )

    if not response.ok:
        raise RuntimeError(
            "Veo video download failed: "
            f"{response.status_code} "
            f"{response.text[:1000]}"
        )

    plan_slug = safe_plan_key(plan_key)

    filename = (
        f"{PRODUCTION_PREFIX}/"
        f"{production_id}/clips/"
        f"{plan_slug}.mp4"
    )

    storage_client = storage.Client()
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(filename)

    # Deterministic filename makes retries idempotent.
    blob.upload_from_string(
        response.content,
        content_type="video/mp4"
    )

    return filename



def build_persisted_montage_payload(state):
    montage = state.get("montage")

    if not isinstance(montage, dict):
        return None, ["montage"]

    required_fields = [
        "plan_durations",
        "texts",
        "transition",
        "end_card"
    ]

    missing = [
        field
        for field in required_fields
        if field not in montage
    ]

    if missing:
        return None, missing

    plan_durations = montage.get("plan_durations")

    if not isinstance(plan_durations, dict):
        return None, ["plan_durations"]

    texts = montage.get("texts")

    if not isinstance(texts, list):
        return None, ["texts"]

    transition = montage.get("transition")

    if not isinstance(transition, dict):
        return None, ["transition"]

    end_card = montage.get("end_card")

    if not isinstance(end_card, dict):
        return None, ["end_card"]

    logo = montage.get("logo", {
        "enabled": False
    })

    if not isinstance(logo, dict):
        return None, ["logo"]

    plan_starts = montage.get("plan_starts", {})

    if not isinstance(plan_starts, dict):
        return None, ["plan_starts"]

    plans = state.get("plans", {})

    if not isinstance(plans, dict) or not plans:
        return None, ["plans"]

    clips = []

    for plan_key in sorted(plans.keys()):
        plan = plans.get(plan_key)

        if not isinstance(plan, dict):
            return None, [
                f"plans.{plan_key}"
            ]

        duration = plan_durations.get(plan_key)

        try:
            duration = float(duration)

            if duration <= 0 or duration > 30:
                raise ValueError()

        except (TypeError, ValueError):
            return None, [
                f"plan_durations.{plan_key}"
            ]

        start = plan_starts.get(plan_key, 0)

        try:
            start = float(start)

            if start < 0:
                raise ValueError()

        except (TypeError, ValueError):
            return None, [
                f"plan_starts.{plan_key}"
            ]

        video = plan.get("video")

        if (
            isinstance(video, dict)
            and video.get("filename")
        ):
            clips.append({
                "type": "video",
                "filename": video["filename"],
                "start": start,
                "duration": duration
            })
            continue

        if (
            plan.get("mode")
            == "ffmpeg_image_fallback"
        ):
            source_image = (
                plan.get("source_image")
                or plan.get("keyframe_object_name")
            )

            if not source_image:
                return None, [
                    f"plans.{plan_key}.source_image"
                ]

            clips.append({
                "type": "image",
                "filename": source_image,
                "start": 0,
                "duration": duration
            })
            continue

        return None, [
            f"plans.{plan_key}.usable_asset"
        ]

    return {
        "clips": clips,
        "texts": texts,
        "transition": transition,
        "end_card": end_card,
        "logo": logo
    }, []


def run_montage_from_persisted_state(state):
    payload, missing = (
        build_persisted_montage_payload(state)
    )

    if missing:
        return {
            "status": "missing_config",
            "missing": missing
        }

    # Reuse the validated /montage-simple implementation directly.
    # A nested Flask request context lets the existing endpoint keep
    # its validation and rendering behavior without duplicating it.
    with app.test_request_context(
        "/montage-simple",
        method="POST",
        json=payload,
        headers={
            "Authorization": (
                f"Bearer {FALCO_API_KEY}"
            )
        }
    ):
        response = montage_simple()

    status_code = 200
    response_object = response

    if isinstance(response, tuple):
        response_object = response[0]
        status_code = response[1]

    try:
        body = response_object.get_json()
    except Exception:
        body = None

    if status_code >= 400:
        return {
            "status": "error",
            "http_status": status_code,
            "details": body
        }

    if not isinstance(body, dict):
        return {
            "status": "error",
            "http_status": 500,
            "details": {
                "error": (
                    "montage-simple returned "
                    "an invalid response"
                )
            }
        }

    return {
        "status": "done",
        "result": body
    }


@app.post("/productions/<production_id>/orchestrate")
@require_falco_auth
def orchestrate_falco_production(production_id):
    """
    Advance already-submitted Veo operations and publish completed clips.

    V1.2 does not create images. It may submit a missing Veo job only
    when the persisted plan contains keyframe_object_name and video_prompt.
    When all plans are usable, it may render the final montage only from
    explicitly persisted montage configuration.
    """

    if not GEMINI_API_KEY:
        return jsonify({
            "error": "GEMINI_API_KEY is not configured"
        }), 500

    try:
        state, generation, error = (
            load_production_state(production_id)
        )
    except Exception as e:
        return jsonify({
            "error": "Failed to load production state",
            "details": str(e)
        }), 500

    if error == "invalid":
        return jsonify({
            "error": "Invalid production_id"
        }), 400

    if error == "missing":
        return jsonify({
            "error": "Production not found"
        }), 404

    if error == "corrupt":
        return jsonify({
            "error": "Production state is corrupt"
        }), 500

    plans = state.get("plans", {})

    if not isinstance(plans, dict) or not plans:
        return jsonify({
            "production_id": production_id,
            "status": state.get("status"),
            "revision": state.get("revision"),
            "advanced": False,
            "next_action": "awaiting_plans"
        })

    summary = {
        "published": [],
        "processing": [],
        "failed": [],
        "waiting_for_submission": [],
        "submitted": [],
        "missing_video_prompt": [],
        "fallback_ready": [],
        "montage": None
    }

    for plan_key in sorted(plans.keys()):
        plan = plans.get(plan_key)

        if not isinstance(plan, dict):
            summary["failed"].append({
                "plan": plan_key,
                "reason": "invalid_plan_state"
            })
            continue

        if (
            plan.get("mode") == "ffmpeg_image_fallback"
            and plan.get("status") == "ready"
        ):
            summary["fallback_ready"].append(plan_key)
            continue

        video = plan.get("video")

        if not isinstance(video, dict):
            video = {}
            plan["video"] = video

        if video.get("filename"):
            video["status"] = "published"
            plan["status"] = "published"

            summary["published"].append(plan_key)
            continue

        operation_name = video.get("operation_name")

        if not operation_name:
            keyframe_object_name = (
                plan.get("keyframe_object_name")
            )
            video_prompt = plan.get("video_prompt")

            if not keyframe_object_name:
                summary["waiting_for_submission"].append(
                    plan_key
                )
                continue

            if not video_prompt:
                plan["status"] = "awaiting_video_prompt"
                video["status"] = "not_submitted"

                summary["missing_video_prompt"].append(
                    plan_key
                )
                continue

            try:
                operation_name = (
                    submit_veo_for_persisted_plan(
                        str(video_prompt).strip(),
                        keyframe_object_name
                    )
                )
            except FileNotFoundError:
                plan["status"] = "blocked"
                video["status"] = "submission_failed"
                video["last_error"] = (
                    "Persisted keyframe image not found"
                )

                summary["failed"].append({
                    "plan": plan_key,
                    "reason": "keyframe_not_found"
                })
                continue
            except Exception as e:
                plan["status"] = "blocked"
                video["status"] = "submission_failed"
                video["last_error"] = str(e)

                summary["failed"].append({
                    "plan": plan_key,
                    "reason": "veo_submission_failed"
                })
                continue

            video["operation_name"] = operation_name
            video["status"] = "processing"
            video["submitted_at"] = utc_now_iso()
            plan["status"] = "video_processing"

            summary["submitted"].append(plan_key)
            summary["processing"].append(plan_key)

            # Do not immediately poll a freshly submitted operation.
            continue

        if not str(operation_name).startswith(
            "models/veo-3.1-lite-generate-preview/operations/"
        ):
            video["status"] = "failed"
            video["failure"] = {
                "type": "invalid_operation_name"
            }

            summary["failed"].append({
                "plan": plan_key,
                "reason": "invalid_operation_name"
            })
            continue

        status_url = (
            "https://generativelanguage.googleapis.com/"
            "v1beta/"
            + str(operation_name)
        )

        try:
            response = requests.get(
                status_url,
                headers={
                    "x-goog-api-key": GEMINI_API_KEY
                },
                timeout=60
            )
        except requests.RequestException as e:
            video["status"] = "status_check_failed"
            video["last_error"] = str(e)

            summary["failed"].append({
                "plan": plan_key,
                "reason": "status_request_failed"
            })
            continue

        if not response.ok:
            video["status"] = "status_check_failed"
            video["last_error"] = (
                f"{response.status_code}: "
                f"{response.text[:1000]}"
            )

            summary["failed"].append({
                "plan": plan_key,
                "reason": "status_request_failed"
            })
            continue

        try:
            result = response.json()
        except ValueError:
            video["status"] = "status_check_failed"
            video["last_error"] = (
                "Veo returned invalid JSON"
            )

            summary["failed"].append({
                "plan": plan_key,
                "reason": "invalid_status_json"
            })
            continue

        if not result.get("done"):
            video["status"] = "processing"
            video["last_checked_at"] = utc_now_iso()
            plan["status"] = "video_processing"

            summary["processing"].append(plan_key)
            continue

        video_uri = extract_veo_video_uri(result)

        if not video_uri:
            video["status"] = "failed"
            plan["status"] = "video_failed"
            video["last_checked_at"] = utc_now_iso()
            video["failure"] = (
                extract_veo_failure_reason(result)
            )

            summary["failed"].append({
                "plan": plan_key,
                "reason": video["failure"]
            })
            continue

        video["status"] = "done"
        plan["status"] = "video_done"
        video["video_uri"] = video_uri
        video["last_checked_at"] = utc_now_iso()

        try:
            filename = publish_veo_uri_for_production(
                production_id,
                plan_key,
                video_uri
            )
        except Exception as e:
            video["status"] = "publish_failed"
            plan["status"] = "publish_failed"
            video["last_error"] = str(e)

            summary["failed"].append({
                "plan": plan_key,
                "reason": "publish_failed"
            })
            continue

        video["filename"] = filename
        video["status"] = "published"
        video["published_at"] = utc_now_iso()
        plan["status"] = "published"

        summary["published"].append(plan_key)

    usable_plan_count = (
        len(summary["published"])
        + len(summary["fallback_ready"])
    )

    total_plan_count = len(plans)

    if usable_plan_count == total_plan_count:
        montage_state = state.setdefault(
            "montage",
            {}
        )

        if (
            isinstance(montage_state, dict)
            and montage_state.get("filename")
            and montage_state.get("video_url")
        ):
            montage_state["status"] = "completed"
            state["status"] = "completed"
            next_action = "none"

            summary["montage"] = {
                "status": "completed",
                "filename": montage_state.get(
                    "filename"
                ),
                "video_url": montage_state.get(
                    "video_url"
                )
            }
        else:
            montage_run = (
                run_montage_from_persisted_state(
                    state
                )
            )

            if (
                montage_run.get("status")
                == "missing_config"
            ):
                missing_config = montage_run.get(
                    "missing",
                    []
                )

                if not isinstance(
                    montage_state,
                    dict
                ):
                    montage_state = {}
                    state["montage"] = (
                        montage_state
                    )

                montage_state["status"] = (
                    "awaiting_config"
                )
                montage_state["missing"] = (
                    missing_config
                )

                state["status"] = (
                    "awaiting_montage_config"
                )
                next_action = (
                    "persist_montage_config"
                )

                summary["montage"] = {
                    "status": "awaiting_config",
                    "missing": missing_config
                }

            elif (
                montage_run.get("status")
                == "error"
            ):
                if not isinstance(
                    montage_state,
                    dict
                ):
                    montage_state = {}
                    state["montage"] = (
                        montage_state
                    )

                montage_state["status"] = (
                    "failed"
                )
                montage_state["last_error"] = (
                    montage_run.get("details")
                )

                state["status"] = "blocked"
                next_action = "retry_montage"

                summary["montage"] = {
                    "status": "failed",
                    "details": montage_run.get(
                        "details"
                    )
                }

            else:
                result = montage_run["result"]

                if not isinstance(
                    montage_state,
                    dict
                ):
                    montage_state = {}
                    state["montage"] = (
                        montage_state
                    )

                montage_state.update({
                    "status": "completed",
                    "filename": result.get(
                        "filename"
                    ),
                    "video_id": result.get(
                        "video_id"
                    ),
                    "video_url": result.get(
                        "video_url"
                    ),
                    "content_duration": (
                        result.get(
                            "content_duration"
                        )
                    ),
                    "final_duration": (
                        result.get(
                            "final_duration"
                        )
                    ),
                    "transition_used": (
                        result.get("transition")
                    ),
                    "end_card_used": (
                        result.get("end_card")
                    ),
                    "completed_at": utc_now_iso()
                })

                state["status"] = "completed"
                next_action = "none"

                summary["montage"] = {
                    "status": "completed",
                    "filename": result.get(
                        "filename"
                    ),
                    "video_url": result.get(
                        "video_url"
                    ),
                    "content_duration": (
                        result.get(
                            "content_duration"
                        )
                    ),
                    "final_duration": (
                        result.get(
                            "final_duration"
                        )
                    )
                }

    elif summary["processing"]:
        state["status"] = "videos_processing"
        next_action = "check_existing_operations"
    elif summary["failed"]:
        state["status"] = "blocked"
        next_action = "retry_or_fallback_failed_plans"
    elif summary["missing_video_prompt"]:
        state["status"] = "awaiting_video_prompts"
        next_action = "persist_missing_video_prompts"
    else:
        state["status"] = "videos_submitted"
        next_action = "submit_missing_video_operations"

    metadata = state.setdefault("metadata", {})

    if isinstance(metadata, dict):
        metadata["orchestrator_v1_2"] = {
            "last_run_at": utc_now_iso(),
            "next_action": next_action
        }

    state["updated_at"] = utc_now_iso()
    state["revision"] = int(
        state.get("revision", 0)
    ) + 1

    try:
        save_production_state(
            production_id,
            state,
            expected_generation=generation
        )
    except PreconditionFailed:
        return jsonify({
            "error": (
                "Production state changed concurrently. "
                "Reload and retry."
            )
        }), 409
    except Exception as e:
        return jsonify({
            "error": "Failed to persist orchestrator state",
            "details": str(e)
        }), 500

    return jsonify({
        "production_id": production_id,
        "status": state["status"],
        "revision": state["revision"],
        "advanced": True,
        "next_action": next_action,
        "summary": summary
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "falco-video-api"
    })

@app.get("/ffmpeg-test")
@require_falco_auth
def ffmpeg_test():

    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=10
        )

        first_line = result.stdout.splitlines()[0]

        return jsonify({
            "status": "ok",
            "ffmpeg": first_line
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e)
        }), 500

@app.post("/generate")
@require_falco_auth
def generate():

    if not GEMINI_API_KEY:
        return jsonify({
            "error": "GEMINI_API_KEY is not configured"
        }), 500

    data = request.get_json(silent=True) or {}

    prompt = str(data.get("prompt", "")).strip()
    image_object_name = data.get("image_object_name")

    if not prompt:
        return jsonify({
            "error": "Missing prompt"
        }), 400

    if len(prompt) > 5000:
        return jsonify({
            "error": "Prompt too long"
        }), 400

    aspect_ratio = data.get("aspect_ratio", "9:16")

    if aspect_ratio != "9:16":
        return jsonify({
            "error": "Only 9:16 is currently allowed"
        }), 400

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        f"models/{MODEL}:predictLongRunning"
    )

    instance = {
        "prompt": prompt
    }

    # Optional image-to-video mode
    if image_object_name:
        try:
            image_b64, mime_type = get_gcs_image_as_base64(
                image_object_name
            )
        except FileNotFoundError:
            return jsonify({
                "error": "Input image not found",
                "image_object_name": image_object_name
            }), 404
        except Exception as e:
            return jsonify({
                "error": "Failed to load input image",
                "details": str(e)
            }), 500

        instance["image"] = {
            "bytesBase64Encoded": image_b64,
            "mimeType": mime_type
        }

    payload = {
        "instances": [
            instance
        ],
        "parameters": {
            "aspectRatio": aspect_ratio
        }
    }

    try:
        response = requests.post(
            url,
            headers={
                "x-goog-api-key": GEMINI_API_KEY,
                "Content-Type": "application/json"
            },
            json=payload,
            timeout=60
        )
    except requests.RequestException as e:
        return jsonify({
            "error": "Veo request failed",
            "details": str(e)
        }), 502

    if not response.ok:
        return jsonify({
            "error": "Veo request failed",
            "status_code": response.status_code,
            "details": response.text
        }), response.status_code

    try:
        result = response.json()
    except ValueError:
        return jsonify({
            "error": "Invalid JSON returned by Veo",
            "details": response.text
        }), 502

    operation_name = result.get("name")

    if not operation_name:
        return jsonify({
            "error": "Veo did not return an operation name"
        }), 502

    return jsonify({
        "status": "submitted",
        "operation_name": operation_name,
        "mode": (
            "image-to-video"
            if image_object_name
            else "text-to-video"
        ),
        "image_object_name": image_object_name
    })


@app.get("/status")
@require_falco_auth
def status():

    if not GEMINI_API_KEY:
        return jsonify({
            "error": "GEMINI_API_KEY is not configured"
        }), 500

    operation_name = request.args.get("operation_name")

    if not operation_name:
        return jsonify({
            "error": "Missing operation_name"
        }), 400

    if not operation_name.startswith(
        "models/veo-3.1-lite-generate-preview/operations/"
    ):
        return jsonify({
            "error": "Invalid operation_name"
        }), 400

    url = (
        "https://generativelanguage.googleapis.com/v1beta/"
        + operation_name
    )

    response = requests.get(
        url,
        headers={
            "x-goog-api-key": GEMINI_API_KEY
        },
        timeout=60
    )

    if not response.ok:
        return jsonify({
            "error": "Status request failed",
            "status_code": response.status_code,
            "details": response.text
        }), response.status_code

    result = response.json()

    if not result.get("done"):
        return jsonify({
            "status": "processing",
            "done": False
        })

    try:
        video_uri = (
            result["response"]
            ["generateVideoResponse"]
            ["generatedSamples"][0]
            ["video"]["uri"]
        )

    except (KeyError, IndexError):
        return jsonify({
            "status": "done",
            "done": True,
            "raw_response": result
        })

    return jsonify({
        "status": "done",
        "done": True,
        "video_uri": video_uri
    })

@app.post("/montage-simple")
@require_falco_auth
def montage_simple():

    data = request.get_json(silent=True) or {}

    clips = data.get("clips")
    texts = data.get("texts", [])
    logo = data.get("logo", {})
    end_card = data.get("end_card", {})
    transition = data.get("transition", {})

    # =========================================================
    # CONFIGURATION FALCO
    # =========================================================

    font_regular = "/app/fonts/Luciole-Regular.ttf"
    font_bold = "/app/fonts/Luciole-Bold.ttf"

    official_logo_object = "falco/assets/falco-logo.png"

    color_map = {
        "blue": "0x4173ff",
        "cream": "0xf7efe4",
        "black": "0x363636",
        "light_grey": "0xf9fafb",
        "white": "0xffffff"
    }

    text_position_map = {
        "top": "h*0.18",
        "center": "(h-text_h)/2",
        "bottom": "h*0.78"
    }

    # =========================================================
    # VALIDATION CLIPS
    # =========================================================

    if not isinstance(clips, list) or len(clips) < 2:
        return jsonify({
            "error": "clips must contain at least 2 items"
        }), 400

    if len(clips) > 5:
        return jsonify({
            "error": "Maximum 5 clips allowed"
        }), 400

    for index, clip in enumerate(clips):

        if not isinstance(clip, dict):
            return jsonify({
                "error": f"Clip {index} must be an object"
            }), 400

        filename = clip.get("filename")
        start = clip.get("start", 0)
        duration = clip.get("duration")

        if (
            not isinstance(filename, str)
            or not filename.startswith("falco/")
        ):
            return jsonify({
                "error": f"Invalid filename for clip {index}"
            }), 400

        try:
            start = float(start)

            if start < 0:
                raise ValueError()

        except (TypeError, ValueError):
            return jsonify({
                "error": f"Invalid start for clip {index}"
            }), 400

        try:
            duration = float(duration)

            if duration <= 0 or duration > 30:
                raise ValueError()

        except (TypeError, ValueError):
            return jsonify({
                "error": f"Invalid duration for clip {index}"
            }), 400

    # =========================================================
    # VALIDATION TRANSITION
    # =========================================================

    if not isinstance(transition, dict):
        return jsonify({
            "error": "transition must be an object"
        }), 400

    transition_type = transition.get("type", "cut")

    if transition_type not in [
        "cut",
        "crossfade",
        "fade"
    ]:
        return jsonify({
            "error": (
                "Invalid transition type. "
                "Allowed: cut, crossfade, fade."
            )
        }), 400

    transition_duration = transition.get(
        "duration",
        0.20
    )

    try:
        transition_duration = float(
            transition_duration
        )

        if (
            transition_duration < 0.10
            or transition_duration > 0.50
        ):
            raise ValueError()

    except (TypeError, ValueError):
        return jsonify({
            "error": (
                "Invalid transition duration. "
                "Allowed: 0.10 to 0.50 seconds."
            )
        }), 400


    raw_content_duration = sum(
        float(clip["duration"])
        for clip in clips
    )

    content_duration = raw_content_duration

    # =========================================================
    # VALIDATION TEXTS
    # =========================================================

    if not isinstance(texts, list):
        return jsonify({
            "error": "texts must be a list"
        }), 400

    text_specs = []

    for index, text_item in enumerate(texts):

        if not isinstance(text_item, dict):
            return jsonify({
                "error": f"Text {index} must be an object"
            }), 400

        text_value = text_item.get("text")
        start_time = text_item.get("start")
        end_time = text_item.get("end")

        weight = text_item.get("weight", "bold")
        size = text_item.get("size", 58)
        position = text_item.get("position", "bottom")
        color = text_item.get("color", "cream")

        if not text_value:
            return jsonify({
                "error": f"Missing text for item {index}"
            }), 400

        try:
            start_time = float(start_time)
            end_time = float(end_time)

            if (
                start_time < 0
                or end_time <= start_time
                or end_time > content_duration
            ):
                raise ValueError()

        except (TypeError, ValueError):
            return jsonify({
                "error": (
                    f"Invalid timing for text {index}. "
                    f"Content duration: {content_duration}s"
                )
            }), 400

        if weight == "regular":
            font_path = font_regular

        elif weight == "bold":
            font_path = font_bold

        else:
            return jsonify({
                "error": f"Invalid weight for text {index}"
            }), 400

        try:
            size = int(size)

            if size < 24 or size > 120:
                raise ValueError()

        except (TypeError, ValueError):
            return jsonify({
                "error": f"Invalid size for text {index}"
            }), 400

        if color not in color_map:
            return jsonify({
                "error": f"Invalid color for text {index}"
            }), 400

        if position not in text_position_map:
            return jsonify({
                "error": f"Invalid position for text {index}"
            }), 400

        font_color = color_map[color]
        y_position = text_position_map[position]

        text_specs.append({
            "text": str(text_value).replace("\n", " "),
            "font_path": font_path,
            "font_color": font_color,
            "size": size,
            "y_position": y_position,
            "start_time": start_time,
            "end_time": end_time
        })

    # =========================================================
    # VALIDATION OPTIONAL FREE LOGO
    # =========================================================

    if not isinstance(logo, dict):
        return jsonify({
            "error": "logo must be an object"
        }), 400

    logo_enabled = logo.get("enabled", False)

    logo_start = 0
    logo_end = content_duration
    logo_width = 220
    logo_position = "center"

    if logo_enabled:

        logo_start = logo.get("start", 0)
        logo_end = logo.get("end", content_duration)
        logo_width = logo.get("width", 220)
        logo_position = logo.get("position", "center")

        try:
            logo_start = float(logo_start)
            logo_end = float(logo_end)

            if (
                logo_start < 0
                or logo_end <= logo_start
                or logo_end > content_duration
            ):
                raise ValueError()

        except (TypeError, ValueError):
            return jsonify({
                "error": "Invalid logo timing"
            }), 400

        try:
            logo_width = int(logo_width)

            if logo_width < 80 or logo_width > 600:
                raise ValueError()

        except (TypeError, ValueError):
            return jsonify({
                "error": "Invalid logo width"
            }), 400

        if logo_position not in [
            "top",
            "center",
            "bottom"
        ]:
            return jsonify({
                "error": "Invalid logo position"
            }), 400

    # =========================================================
    # VALIDATION PREMIUM END CARD
    # =========================================================

    if not isinstance(end_card, dict):
        return jsonify({
            "error": "end_card must be an object"
        }), 400

    end_card_enabled = end_card.get("enabled", False)
    end_card_duration = 2.0

    if end_card_enabled:

        try:
            end_card_duration = float(
                end_card.get("duration", 2)
            )

            if (
                end_card_duration < 1
                or end_card_duration > 4
            ):
                raise ValueError()

        except (TypeError, ValueError):
            return jsonify({
                "error": (
                    "Invalid end_card duration. "
                    "Allowed: 1 to 4 seconds."
                )
            }), 400

    final_duration = (
        content_duration + end_card_duration
        if end_card_enabled
        else content_duration
    )

    # =========================================================
    # STORAGE
    # =========================================================

    storage_client = storage.Client()
    bucket = storage_client.bucket(BUCKET_NAME)

    with tempfile.TemporaryDirectory() as tmpdir:

        tmpdir_path = Path(tmpdir)
        normalized_files = []

        # =====================================================
        # DOWNLOAD + CUT + NORMALIZE CLIPS
        # =====================================================

        for index, clip in enumerate(clips):

            filename = clip["filename"]
            clip_type = clip.get("type", "video")

            start = float(clip.get("start", 0))
            duration = float(clip["duration"])

            blob = bucket.blob(filename)

            if not blob.exists():
                return jsonify({
                    "error": f"Clip not found: {filename}"
                }), 404

            normalized_path = (
                tmpdir_path / f"normalized_{index}.mp4"
            )

            # =====================================================
            # IMAGE FALLBACK
            # =====================================================

            if clip_type == "image":

                source_path = (
                    tmpdir_path / f"source_image_{index}.jpg"
                )

                blob.download_to_filename(
                    str(source_path)
                )

                # Very subtle slow push-in.
                # FALCO: movement must remain almost imperceptible.
                image_command = [
                    "ffmpeg",
                    "-y",

                    "-loop", "1",
                    "-framerate", "30",
                    "-i", str(source_path),

                    "-f", "lavfi",
                    "-i",
                    "anullsrc=channel_layout=stereo:sample_rate=48000",

                    "-t", str(duration),

                    "-vf",
                    (
                        "scale=1080:1920:"
                        "force_original_aspect_ratio=increase,"
                        "crop=1080:1920,"
                        "zoompan="
                        "z='min(zoom+0.00025,1.025)':"
                        "x='iw/2-(iw/zoom/2)':"
                        "y='ih/2-(ih/zoom/2)':"
                        "d=1:"
                        "s=1080x1920:"
                        "fps=30"
                    ),

                    "-map", "0:v:0",
                    "-map", "1:a:0",

                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "21",
                    "-pix_fmt", "yuv420p",

                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-ar", "48000",
                    "-ac", "2",

                    "-shortest",

                    "-movflags", "+faststart",

                    str(normalized_path)
                ]

                result = subprocess.run(
                    image_command,
                    capture_output=True,
                    text=True,
                    timeout=300
                )

                if result.returncode != 0:
                    return jsonify({
                        "error": "FFmpeg image fallback failed",
                        "clip": filename,
                        "details": result.stderr[-6000:]
                    }), 500

                normalized_files.append(
                    normalized_path
                )

                continue

            # =====================================================
            # STANDARD VIDEO
            # =====================================================

            source_path = (
                tmpdir_path / f"source_{index}.mp4"
            )

            blob.download_to_filename(
                str(source_path)
            )

            probe_command = [
                "ffprobe",
                "-v", "error",
                "-select_streams", "a:0",
                "-show_entries", "stream=codec_type",
                "-of", "csv=p=0",
                str(source_path)
            ]

            probe_result = subprocess.run(
                probe_command,
                capture_output=True,
                text=True,
                timeout=30
            )

            has_audio = (
                probe_result.returncode == 0
                and "audio" in probe_result.stdout
            )

            if has_audio:

                normalize_command = [
                    "ffmpeg",
                    "-y",

                    "-ss", str(start),
                    "-i", str(source_path),

                    "-t", str(duration),

                    "-map", "0:v:0",
                    "-map", "0:a:0",

                    "-vf",
                    (
                        "scale=1080:1920:"
                        "force_original_aspect_ratio=decrease,"
                        "pad=1080:1920:"
                        "(ow-iw)/2:(oh-ih)/2,"
                        "fps=30"
                    ),

                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "21",
                    "-pix_fmt", "yuv420p",

                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-ar", "48000",
                    "-ac", "2",

                    "-movflags", "+faststart",

                    str(normalized_path)
                ]

            else:

                normalize_command = [
                    "ffmpeg",
                    "-y",

                    "-ss", str(start),
                    "-i", str(source_path),

                    "-f", "lavfi",
                    "-i",
                    "anullsrc=channel_layout=stereo:sample_rate=48000",

                    "-t", str(duration),

                    "-map", "0:v:0",
                    "-map", "1:a:0",

                    "-vf",
                    (
                        "scale=1080:1920:"
                        "force_original_aspect_ratio=decrease,"
                        "pad=1080:1920:"
                        "(ow-iw)/2:(oh-ih)/2,"
                        "fps=30"
                    ),

                    "-c:v", "libx264",
                    "-preset", "veryfast",
                    "-crf", "21",
                    "-pix_fmt", "yuv420p",

                    "-c:a", "aac",
                    "-b:a", "192k",
                    "-ar", "48000",
                    "-ac", "2",

                    "-shortest",

                    "-movflags", "+faststart",

                    str(normalized_path)
                ]

            result = subprocess.run(
                normalize_command,
                capture_output=True,
                text=True,
                timeout=300
            )

            if result.returncode != 0:
                return jsonify({
                    "error": "FFmpeg normalization failed",
                    "clip": filename,
                    "details": result.stderr[-6000:]
                }), 500

            normalized_files.append(
                normalized_path
            )
        
        # =====================================================
        # ASSEMBLE NORMALIZED CLIPS
        # =====================================================

        base_video_path = (
            tmpdir_path / "base.mp4"
        )

        if not normalized_files:
            return jsonify({
                "error": "No normalized files available for montage"
            }), 500

        concat_file = (
            tmpdir_path / "concat.txt"
        )

        with open(
            concat_file,
            "w",
            encoding="utf-8"
        ) as f:

            for normalized_file in normalized_files:
                f.write(
                    f"file '{normalized_file.as_posix()}'\n"
                )

        concat_command = [
            "ffmpeg",
            "-y",

            "-f", "concat",
            "-safe", "0",

            "-i", str(concat_file),

            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "21",
            "-pix_fmt", "yuv420p",

            "-c:a", "aac",
            "-b:a", "192k",
            "-ar", "48000",
            "-ac", "2",

            "-movflags", "+faststart",

            str(base_video_path)
        ]

        result = subprocess.run(
            concat_command,
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode != 0:
            return jsonify({
                "error": "FFmpeg base concat failed",
                "details": result.stderr[-6000:]
            }), 500

        # =====================================================
        # APPLY CONTENT TEXTS + OPTIONAL FREE LOGO
        # =====================================================

        content_video_path = (
            tmpdir_path / "content.mp4"
        )

        need_logo_asset = (
            logo_enabled or end_card_enabled
        )

        logo_path = None

        if need_logo_asset:

            logo_blob = bucket.blob(
                official_logo_object
            )

            if not logo_blob.exists():
                return jsonify({
                    "error": (
                        "Official Falco logo not found"
                    )
                }), 404

            logo_path = (
                tmpdir_path / "falco-logo.png"
            )

            logo_blob.download_to_filename(
                str(logo_path)
            )

        content_command = [
            "ffmpeg",
            "-y",
            "-i", str(base_video_path)
        ]

        if logo_enabled:

            content_command += [
                "-loop", "1",
                "-framerate", "30",
                "-i", str(logo_path)
            ]

        content_filter_parts = []
        drawtext_filters = []

        # Write user-facing text to UTF-8 files and let FFmpeg read it
        # with textfile=. This avoids fragile escaping of apostrophes,
        # accents, colons, commas and percent signs in filter_complex.
        for index, spec in enumerate(text_specs):

            text_path = (
                tmpdir_path / f"drawtext_{index}.txt"
            )

            text_path.write_text(
                spec["text"],
                encoding="utf-8"
            )

            drawtext_filters.append(
                "drawtext="
                f"fontfile='{spec['font_path']}':"
                f"textfile='{text_path.as_posix()}':"
                "reload=0:"
                f"fontcolor={spec['font_color']}:"
                f"fontsize={spec['size']}:"
                "x=(w-text_w)/2:"
                f"y={spec['y_position']}:"
                f"enable='between(t,{spec['start_time']},{spec['end_time']})'"
            )

        if drawtext_filters:

            content_filter_parts.append(
                "[0:v]"
                + ",".join(drawtext_filters)
                + "[content_text]"
            )

            current_content_label = (
                "[content_text]"
            )

        else:

            content_filter_parts.append(
                "[0:v]null[content_text]"
            )

            current_content_label = (
                "[content_text]"
            )

        if logo_enabled:

            content_filter_parts.append(
                f"[1:v]scale={logo_width}:-1"
                "[free_logo]"
            )

            if logo_position == "top":
                free_logo_y = "160"

            elif logo_position == "center":
                free_logo_y = (
                    "(main_h-overlay_h)/2"
                )

            else:
                free_logo_y = (
                    "main_h-overlay_h-180"
                )

            content_filter_parts.append(
                f"{current_content_label}"
                "[free_logo]"
                "overlay="
                "x=(main_w-overlay_w)/2:"
                f"y={free_logo_y}:"
                f"enable='between(t,"
                f"{logo_start},{logo_end})':"
                "eof_action=pass"
                "[content_out]"
            )

            current_content_label = (
                "[content_out]"
            )

        content_command += [
            "-filter_complex",
            ";".join(content_filter_parts),

            "-map",
            current_content_label,

            "-map",
            "0:a?",

            "-c:v",
            "libx264",

            "-preset",
            "veryfast",

            "-crf",
            "21",

            "-pix_fmt",
            "yuv420p",

            "-c:a",
            "aac",

            "-b:a",
            "192k",

            "-ar",
            "48000",

            "-movflags",
            "+faststart",

            str(content_video_path)
        ]

        result = subprocess.run(
            content_command,
            capture_output=True,
            text=True,
            timeout=300
        )

        if result.returncode != 0:
            return jsonify({
                "error": "FFmpeg content render failed",
                "details": result.stderr[-4000:]
            }), 500

        # =====================================================
        # PREMIUM FALCO END CARD
        # =====================================================

        final_local_path = content_video_path

        if end_card_enabled:

            end_card_path = (
                tmpdir_path / "end_card.mp4"
            )

            # -------------------------------------------------
            # PREMIUM END CARD V1
            #
            # Background : FALCO Black #363636
            # Logo       : official asset, 220 px wide
            # FALCO      : Luciole Bold / Cream
            # Tagline    : Luciole Regular / Cream
            #
            # IMPORTANT:
            # Extra vertical spacing prevents overlap between
            # the bottom of the symbol and the FALCO wordmark.
            # -------------------------------------------------

            end_card_filter = (
                "[1:v]"
                "scale=220:-1"
                "[end_logo];"

                "[0:v]"
                "[end_logo]"
                "overlay="
                "x=(main_w-overlay_w)/2:"
                "y=560:"
                "format=auto"
                "[card_logo];"

                "[card_logo]"
                "drawtext="
                f"fontfile='{font_bold}':"
                "text='FALCO':"
                "fontcolor=0xf7efe4:"
                "fontsize=64:"
                "x=(w-text_w)/2:"
                "y=1030"
                "[card_brand];"

                "[card_brand]"
                "drawtext="
                f"fontfile='{font_regular}':"
                "text='Fly on your own.':"
                "fontcolor=0xf7efe4:"
                "fontsize=38:"
                "x=(w-text_w)/2:"
                "y=1125,"
                "fade="
                "t=in:"
                "st=0:"
                "d=0.25"
                "[card_out]"
            )

            end_card_command = [
                "ffmpeg",
                "-y",

                # FALCO black background
                "-f", "lavfi",

                "-i",
                (
                    "color="
                    "c=0x363636:"
                    "s=1080x1920:"
                    "r=30:"
                    f"d={end_card_duration}"
                ),

                # Official logo
                "-loop", "1",
                "-framerate", "30",
                "-i", str(logo_path),

                "-filter_complex",
                end_card_filter,

                "-map",
                "[card_out]",

                "-t",
                str(end_card_duration),

                "-c:v",
                "libx264",

                "-preset",
                "veryfast",

                "-crf",
                "21",

                "-pix_fmt",
                "yuv420p",

                "-an",

                "-movflags",
                "+faststart",

                str(end_card_path)
            ]

            result = subprocess.run(
                end_card_command,
                capture_output=True,
                text=True,
                timeout=300
            )

            if result.returncode != 0:
                return jsonify({
                    "error": "FFmpeg end card failed",
                    "details": result.stderr[-4000:]
                }), 500

            # =================================================
            # CONCAT CONTENT + END CARD
            # =================================================

            final_local_path = (
                tmpdir_path / "output.mp4"
            )

            final_concat_file = (
                tmpdir_path / "final_concat.txt"
            )

            with open(
                final_concat_file,
                "w",
                encoding="utf-8"
            ) as f:

                f.write(
                    f"file '{content_video_path.as_posix()}'\n"
                )

                f.write(
                    f"file '{end_card_path.as_posix()}'\n"
                )

            final_concat_command = [
                "ffmpeg",
                "-y",

                "-f", "concat",
                "-safe", "0",

                "-i", str(final_concat_file),

                "-c:v",
                "libx264",

                "-preset",
                "veryfast",

                "-crf",
                "21",

                "-pix_fmt",
                "yuv420p",

                "-c:a",
                "aac",

                "-b:a",
                "192k",

                "-ar",
                "48000",

                "-movflags",
                "+faststart",

                str(final_local_path)
            ]

            result = subprocess.run(
                final_concat_command,
                capture_output=True,
                text=True,
                timeout=300
            )

            if result.returncode != 0:
                return jsonify({
                    "error": (
                        "FFmpeg final concat failed"
                    ),
                    "details": result.stderr[-4000:]
                }), 500

        # =====================================================
        # UPLOAD FINAL VIDEO
        # =====================================================

        video_id = str(uuid.uuid4())

        output_filename = (
            "falco/montages/"
            f"{video_id}.mp4"
        )

        output_blob = bucket.blob(
            output_filename
        )

        output_blob.upload_from_filename(
            str(final_local_path),
            content_type="video/mp4"
        )

        # =====================================================
        # SIGNED URL
        # =====================================================

        credentials, project_id = (
            google.auth.default()
        )

        auth_request = (
            GoogleAuthRequest()
        )

        credentials.refresh(
            auth_request
        )

        service_account_email = getattr(
            credentials,
            "service_account_email",
            None
        )

        if not service_account_email:

            service_account_email = (
                "549259282828-compute@"
                "developer.gserviceaccount.com"
            )

        signed_url = (
            output_blob.generate_signed_url(
                version="v4",
                expiration=timedelta(hours=1),
                method="GET",
                service_account_email=(
                    service_account_email
                ),
                access_token=credentials.token
            )
        )

        public_video_url = (
            "https://falco-video-api-549259282828."
            "europe-west9.run.app/"
            f"video/{video_id}"
        )

        # =====================================================
        # RESPONSE
        # =====================================================

        return jsonify({

            "status": "ready",

            "filename": output_filename,

            "video_id": video_id,

            "video_url": public_video_url,

            "content_duration": content_duration,

            "final_duration": final_duration,

            "clips": clips,

            "texts": texts,

            "logo": logo,

            "transition": {
                "type": transition_type,
                "duration": (
                    transition_duration
                    if transition_type != "cut"
                    else 0
                )
            },

            "end_card": {
                "enabled": end_card_enabled,
                "duration": (
                    end_card_duration
                    if end_card_enabled
                    else 0
                ),
                "style": "falco_premium_v1"
            },

            "output": {
                "width": 1080,
                "height": 1920,
                "fps": 30,
                "video_codec": "h264",
                "font": "Luciole"
            }
        })



@app.post("/publish")
@require_falco_auth
def publish():

    if not GEMINI_API_KEY:
        return jsonify({
            "error": "GEMINI_API_KEY is not configured"
        }), 500

    data = request.get_json(silent=True) or {}
    video_uri = data.get("video_uri")

    if not video_uri:
        return jsonify({
            "error": "Missing video_uri"
        }), 400

    parsed_url = urlparse(video_uri)

    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "generativelanguage.googleapis.com"
    ):
        return jsonify({
            "error": "Invalid video URI"
        }), 400

    try:
        response = requests.get(
            video_uri,
            headers={
                "x-goog-api-key": GEMINI_API_KEY
            },
            timeout=180
        )
    except requests.RequestException as e:
        return jsonify({
            "error": "Veo video download failed",
            "details": str(e)
        }), 502

    if not response.ok:
        return jsonify({
            "error": "Veo video download failed",
            "status_code": response.status_code,
            "details": response.text
        }), response.status_code

    # Generate stable video ID
    video_id = str(uuid.uuid4())

    # Object stored in private GCS bucket
    filename = f"falco/{video_id}.mp4"

    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(filename)

        blob.upload_from_string(
            response.content,
            content_type="video/mp4"
        )

    except Exception as e:
        return jsonify({
            "error": "Failed to upload video to GCS",
            "details": str(e)
        }), 500

    # Stable Cloud Run proxy URL
    video_url = (
        "https://falco-video-api-549259282828."
        "europe-west9.run.app/"
        f"video/{video_id}"
    )

    return jsonify({
        "status": "published",
        "filename": filename,
        "video_id": video_id,
        "video_url": video_url
    })

@app.get("/video/<video_id>")
def serve_falco_video(video_id):

    # UUID validation
    try:
        uuid.UUID(video_id)
    except ValueError:
        return jsonify({
            "error": "Invalid video ID"
        }), 400

    filename = (
        f"falco/montages/{video_id}.mp4"
    )

    storage_client = storage.Client()
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(filename)

    if not blob.exists():
        return jsonify({
            "error": "Video not found"
        }), 404

    with tempfile.NamedTemporaryFile(
        suffix=".mp4",
        delete=False
    ) as tmp_file:

        tmp_path = tmp_file.name

    try:

        blob.download_to_filename(tmp_path)

        return send_file(
            tmp_path,
            mimetype="video/mp4",
            as_attachment=False,
            download_name=f"falco-{video_id}.mp4"
        )

    finally:
        pass

@app.post("/generate-image")
@require_falco_auth
def generate_falco_image():

    data = request.get_json(force=True)

    prompt = str(data.get("prompt", "")).strip()
    reference_image = data.get("reference_image")

    if not prompt:
        return jsonify({"error": "prompt is required"}), 400

    if len(prompt) > 5000:
        return jsonify({"error": "prompt too long"}), 400

    model = "gemini-3.1-flash-image"

    input_items = []

    # Optional reference image for continuity
    if reference_image:
        try:
            image_b64, reference_mime_type = get_gcs_image_as_base64(reference_image)
        except FileNotFoundError:
            return jsonify({"error": "reference image not found"}), 404
        except Exception as e:
            return jsonify({
                "error": "failed to load reference image",
                "details": str(e)
            }), 500

        input_items.append({
            "type": "image",
            "mime_type": reference_mime_type,
            "data": image_b64
        })

    input_items.append({
        "type": "text",
        "text": prompt
    })

    url = "https://generativelanguage.googleapis.com/v1beta/interactions"

    try:
        response = requests.post(
            url,
            headers={
                "x-goog-api-key": GEMINI_API_KEY,
                "Content-Type": "application/json"
            },
            json={
                "model": model,
                "input": input_items
            },
            timeout=180
        )
    except requests.RequestException as e:
        return jsonify({
            "error": "Gemini request failed",
            "details": str(e)
        }), 502

    if response.status_code >= 400:
        return jsonify({
            "error": "Gemini image generation failed",
            "status_code": response.status_code,
            "details": response.text
        }), 502

    try:
        result = response.json()
    except ValueError:
        return jsonify({
            "error": "Gemini returned invalid JSON",
            "details": response.text
        }), 502

    image_data = None
    mime_type = "image/png"

    # Gemini Interactions API:
    # steps -> model_output -> content -> image
    for step in result.get("steps", []):

        if step.get("type") != "model_output":
            continue

        for content in step.get("content", []):

            if (
                content.get("type") == "image"
                and content.get("data")
            ):
                image_data = content.get("data")
                mime_type = content.get(
                    "mime_type",
                    "image/png"
                )
                break

        if image_data:
            break

    if not image_data:
        return jsonify({
            "error": "No image returned",
            "raw": result
        }), 502

    try:
        image_bytes = base64.b64decode(image_data)
    except Exception as e:
        return jsonify({
            "error": "Failed to decode generated image",
            "details": str(e)
        }), 500

    image_id = str(uuid.uuid4())

    extension = ".png"

    if mime_type == "image/jpeg":
        extension = ".jpg"

    elif mime_type == "image/webp":
        extension = ".webp"

    object_name = f"falco/images/{image_id}{extension}"

    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(object_name)

        blob.upload_from_string(
            image_bytes,
            content_type=mime_type
        )

    except Exception as e:
        return jsonify({
            "error": "Failed to upload image to GCS",
            "details": str(e)
        }), 500

    image_url = (
        "https://falco-video-api-549259282828."
        "europe-west9.run.app/"
        f"image/{image_id}"
    )

    return jsonify({
        "status": "done",
        "image_id": image_id,
        "object_name": object_name,
        "image_url": image_url,
        "mime_type": mime_type
    })

@app.get("/image/<image_id>")
def serve_falco_image(image_id):

    try:
        uuid.UUID(image_id)
    except ValueError:
        return jsonify({"error": "Invalid image ID"}), 400

    storage_client = storage.Client()
    bucket = storage_client.bucket(BUCKET_NAME)

    possible_files = [
        f"falco/images/{image_id}.png",
        f"falco/images/{image_id}.jpg"
    ]

    blob = None

    for filename in possible_files:
        candidate = bucket.blob(filename)

        if candidate.exists():
            blob = candidate
            break

    if blob is None:
        return jsonify({"error": "Image not found"}), 404

    image_bytes = blob.download_as_bytes()

    return Response(
        image_bytes,
        mimetype=blob.content_type or "image/png"
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))

    app.run(
        host="0.0.0.0",
        port=port
    )