"""Validate the Azure OpenAI vision deployment before Docling picture enrichment.

This developer tool sends one image to the configured Azure OpenAI chat
completions endpoint and prints the returned caption. It is intentionally
separate from ingestion so credentials and remote calls are tested explicitly.
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
from pathlib import Path
import sys

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def parse_arguments() -> argparse.Namespace:
    """Reads the image and prompt settings for one standalone vision request."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image_path", type=Path, help="A local PNG/JPG image to send to the vision deployment.")
    parser.add_argument("--prompt", default="Describe this engineering diagram. Extract visible labels, numbers, dimensions, and callouts. Do not invent values.")
    return parser.parse_args()


def required_environment_value(name: str) -> str:
    """Reads one required environment variable without printing its value."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} must be set.")
    return value


def image_data_url(path: Path) -> str:
    """Encodes a local image as a data URL accepted by OpenAI-compatible vision APIs."""
    media_type = mimetypes.guess_type(path.name)[0] or "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{media_type};base64,{encoded}"


def main() -> int:
    """Sends one test image to Azure OpenAI and prints the first response text."""
    arguments = parse_arguments()
    if not arguments.image_path.is_file():
        raise FileNotFoundError(f"Image file was not found: {arguments.image_path}")
    endpoint = required_environment_value("AZURE_OPENAI_ENDPOINT").rstrip("/")
    deployment = required_environment_value("AZURE_OPENAI_VISION_DEPLOYMENT")
    api_key = required_environment_value("AZURE_OPENAI_API_KEY")
    api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
    url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": arguments.prompt},
                    {"type": "image_url", "image_url": {"url": image_data_url(arguments.image_path)}},
                ],
            }
        ],
        "max_tokens": 500,
    }
    response = requests.post(url, headers={"api-key": api_key, "Content-Type": "application/json"},
                             data=json.dumps(payload), timeout=60)
    response.raise_for_status()
    body = response.json()
    print(body["choices"][0]["message"]["content"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
