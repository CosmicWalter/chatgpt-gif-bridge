"""OpenAI Images response envelope for the existing native GIF job transport."""

import base64
import re
import time

MODEL = "chatgpt-native-gif"
WAIT_SECONDS = 360


class APIError(Exception):
    def __init__(self, status, message, code, param=None, job_id=None):
        self.status = status
        self.error = {"message": message, "type": "invalid_request_error" if status < 500 else "server_error", "code": code, "param": param}
        if job_id:
            self.error["job_id"] = job_id


def validate_request(data):
    if not isinstance(data, dict):
        raise APIError(400, "Request must be a JSON object.", "invalid_request")
    allowed = {"model", "prompt", "n", "response_format", "output_format", "size", "quality"}
    for key in data:
        if key not in allowed:
            raise APIError(400, f"Unsupported parameter: {key}.", "unsupported_parameter", key)
    if data.get("model", MODEL) != MODEL:
        raise APIError(400, f"Use model {MODEL}. This bridge currently generates native GIF only.", "model_not_found", "model")
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 8000:
        raise APIError(400, "Provide a prompt of 1 to 8000 characters.", "invalid_prompt", "prompt")
    if type(data.get("n", 1)) is not int or data.get("n", 1) != 1:
        raise APIError(400, "Only n=1 is supported.", "unsupported_parameter", "n")
    if data.get("response_format", "b64_json") not in ("b64_json", "url"):
        raise APIError(400, "Use response_format=b64_json or url.", "unsupported_parameter", "response_format")
    if data.get("output_format", "gif") != "gif":
        raise APIError(400, "Only native GIF output is supported.", "unsupported_parameter", "output_format")
    hints = []
    size = data.get("size", "auto")
    if not isinstance(size, str) or not (size == "auto" or re.fullmatch(r"[1-9][0-9]{0,4}x[1-9][0-9]{0,4}", size)):
        raise APIError(400, "Use size=auto or WIDTHxHEIGHT.", "invalid_parameter", "size")
    quality = data.get("quality", "auto")
    if quality not in ("auto", "low", "medium", "high", "standard", "hd"):
        raise APIError(400, "Unsupported quality value.", "invalid_parameter", "quality")
    if size != "auto":
        hints.append("Requested dimensions: " + size)
    if quality != "auto":
        hints.append("Requested quality: " + quality)
    return prompt.strip() + ("\n" + "; ".join(hints) + ". Use native GIF output; do not resize or re-encode." if hints else "")


def generate(jobs, browser, prompt, timeout=WAIT_SECONDS, response_format="b64_json", results=None, base_url=None):
    # Submission happens once. A timeout leaves the original job available for inspection.
    job_id = browser.submit(prompt)
    deadline = time.monotonic() + timeout
    while True:
        job = jobs.get(job_id)
        if job is None:
            raise APIError(502, "The job record is no longer available.", "job_unavailable", job_id=job_id)
        if job.status == "completed":
            if response_format == "url":
                if results is None or base_url is None:
                    raise APIError(503, "Result storage is not configured.", "result_store_unavailable", job_id=job_id)
                item = {"url": results.save(job_id, job.result, base_url)}
            else:
                item = {"b64_json": base64.b64encode(job.result).decode("ascii")}
            return {"created": int(time.time()), "data": [item]}
        if job.status == "waiting_verification":
            state = browser.snapshot()
            raise APIError(503, state.get("error") or "Complete browser verification in the management page, then query the original job.", state.get("status", "verification_required"), job_id=job_id)
        if job.status == "failed":
            raise APIError(502, job.error or "Browser generation failed.", "generation_failed", job_id=job_id)
        if time.monotonic() >= deadline:
            raise APIError(504, "Generation is still pending. Query /v1/gif/jobs/" + job_id + " before retrying.", "generation_timeout", job_id=job_id)
        time.sleep(min(.2, max(0, deadline - time.monotonic())))
