"""Policy-controlled, provider-neutral embedding generation."""

from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from math import isfinite, sqrt
from typing import Protocol

from memory_ops.user_memory import Sensitivity


EMBEDDING_DIMENSIONS = 64


@dataclass(frozen=True)
class EmbeddingModel:
    provider: str
    name: str
    version: str
    dimensions: int = EMBEDDING_DIMENSIONS


class EmbeddingProvider(Protocol):
    metadata: EmbeddingModel

    def embed(self, text: str) -> tuple[float, ...]: ...


@dataclass(frozen=True)
class EmbeddingPolicy:
    allowed_sensitivities: Mapping[str, frozenset[Sensitivity]]

    def allows(self, model: EmbeddingModel, sensitivity: Sensitivity) -> bool:
        return sensitivity in self.allowed_sensitivities.get(model.provider, frozenset())


class HashEmbeddingProvider:
    """Small deterministic local baseline; replace only after measured need."""

    metadata = EmbeddingModel("local", "hash-ngrams", "1.0.0")

    def embed(self, text: str) -> tuple[float, ...]:
        normalized = " ".join(text.casefold().split())
        features = normalized.split() + [
            normalized[index : index + 3]
            for index in range(max(0, len(normalized) - 2))
        ]
        values = [0.0] * self.metadata.dimensions
        for feature in features:
            digest = sha256(feature.encode()).digest()
            index = int.from_bytes(digest[:2]) % self.metadata.dimensions
            values[index] += 1.0 if digest[2] & 1 else -1.0
        magnitude = sqrt(sum(value * value for value in values))
        return tuple(value / magnitude for value in values) if magnitude else tuple(values)


def vector_literal(values: tuple[float, ...], dimensions: int) -> str:
    if len(values) != dimensions or dimensions != EMBEDDING_DIMENSIONS:
        raise ValueError(f"embedding must contain {EMBEDDING_DIMENSIONS} dimensions")
    if not all(isfinite(value) for value in values):
        raise ValueError("embedding values must be finite")
    return "[" + ",".join(format(value, ".17g") for value in values) + "]"
