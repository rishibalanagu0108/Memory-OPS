import io
import json

from memory_ops.extraction.azure import AzureOpenAIExtractor


def test_azure_extractor_requests_strict_structured_output(monkeypatch) -> None:
    observed = {}
    payload = {
        "output": [
            {
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            {
                                "candidates": [
                                    {
                                        "statement": "The user prefers aisle seats.",
                                        "semantic_type": "preference",
                                        "confidence": 0.98,
                                        "explanation": "An explicit recurring preference.",
                                    }
                                ]
                            }
                        ),
                    }
                ]
            }
        ]
    }

    class Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    def open_request(request, timeout):
        observed["url"] = request.full_url
        observed["body"] = json.loads(request.data)
        observed["timeout"] = timeout
        return Response(json.dumps(payload).encode())

    monkeypatch.setattr("memory_ops.extraction.azure.urlopen", open_request)
    extractor = AzureOpenAIExtractor(
        "https://azure.example/openai/v1", "secret", "extractor-model", timeout_seconds=9
    )

    candidates = extractor("I prefer aisle seats.")

    assert candidates[0].semantic_type == "preference"
    assert observed["url"] == "https://azure.example/openai/v1/responses"
    assert observed["timeout"] == 9
    assert observed["body"]["input"] == "I prefer aisle seats."
    assert observed["body"]["text"]["format"]["strict"] is True
    assert observed["body"]["text"]["format"]["schema"]["additionalProperties"] is False
