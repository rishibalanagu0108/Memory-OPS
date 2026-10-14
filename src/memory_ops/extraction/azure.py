"""Azure OpenAI structured-output adapter for shadow memory extraction."""

import json
import os
from urllib.request import Request, urlopen

from memory_ops.extraction import (
    ExtractionDraft,
    ExtractionPolicy,
    ModelIdentity,
    ShadowExtractionPipeline,
)


_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "statement": {"type": "string", "minLength": 1},
                    "semantic_type": {
                        "type": "string",
                        "enum": ["fact", "preference", "goal", "constraint", "episode"],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "explanation": {"type": "string", "minLength": 1},
                },
                "required": [
                    "statement",
                    "semantic_type",
                    "confidence",
                    "explanation",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}


def _response_text(payload: dict) -> str:
    for output in payload.get("output", []):
        for content in output.get("content", []):
            if content.get("type") == "output_text":
                return content["text"]
    raise ValueError("model response contained no output text")


class AzureOpenAIExtractor:
    """Call one configured Azure Responses deployment without an SDK dependency."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        deployment: str,
        *,
        timeout_seconds: int = 60,
    ) -> None:
        if not all(value.strip() for value in (base_url, api_key, deployment)):
            raise ValueError("Azure extraction configuration must not be empty")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.deployment = deployment
        self.timeout_seconds = timeout_seconds

    def __call__(self, content: str) -> tuple[ExtractionDraft, ...]:
        body = {
            "model": self.deployment,
            "instructions": (
                "Extract durable, user-specific memories from the utterance. Extract only "
                "explicit facts, preferences, current goals, hard constraints, or completed "
                "autobiographical episodes. Abstain from questions, hypotheticals, third-party "
                "claims, completed goals, secrets, and requests not to remember. Make each "
                "statement standalone and concise. Confidence is the probability the extraction "
                "is correct. Return an empty candidates array when nothing qualifies."
            ),
            "input": content,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "memory_extraction",
                    "strict": True,
                    "schema": _SCHEMA,
                }
            },
        }
        request = Request(
            self.base_url + "/responses",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "api-key": self.api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            payload = json.load(response)
        parsed = json.loads(_response_text(payload))
        return tuple(
            ExtractionDraft.model_validate(candidate)
            for candidate in parsed["candidates"]
        )


def configured_azure_pipeline(policy_version: str) -> ShadowExtractionPipeline | None:
    """Build the optional extractor only when every required value is present."""

    base_url = os.getenv("AZURE_OPENAI_BASE_URL")
    api_key = os.getenv("AZURE_OPENAI_API_KEY")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT")
    if not all((base_url, api_key, deployment)):
        return None
    try:
        timeout = int(os.getenv("MEMORY_OPS_EXTRACTION_TIMEOUT_SECONDS", "60"))
    except ValueError:
        return None
    if not 1 <= timeout <= 180:
        return None

    assert base_url is not None and api_key is not None and deployment is not None
    model = ModelIdentity(
        provider="azure-openai",
        model_name=deployment,
        version=deployment,
    )
    return ShadowExtractionPipeline(
        ExtractionPolicy(
            version=policy_version,
            allowed_categories=frozenset(
                {"fact", "preference", "goal", "constraint", "episode"}
            ),
            allowed_external_models=(model,),
            allowed_external_sensitivities=frozenset({"normal"}),
        ),
        model,
        AzureOpenAIExtractor(
            base_url,
            api_key,
            deployment,
            timeout_seconds=timeout,
        ),
    )


__all__ = ["AzureOpenAIExtractor", "configured_azure_pipeline"]
