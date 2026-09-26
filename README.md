# Agent Memory Layer

An incremental experiment in giving AI agents memory. The project deliberately starts with the smallest useful question:

> Can an agent store a piece of information and retrieve it later?

No version is implemented yet. The repository is in Genesis discovery so V1 can be specified and approved before code is written. JEV, semantic search, persistence, memory types, and lifecycle policies remain deferred.

## Proposed V1

V1 is expected to provide two operations:

```python
remember(content, source)
recall(query)
```

The current proposal uses an in-process store and case-insensitive substring matching. These choices must pass the Genesis specification and planning gates before implementation.

## Project structure

```text
.
├── .genesis/                  # Repository-native project state and evidence
├── ARCHITECTURE.md            # Components, flow, and design decisions
└── ROADMAP.md                 # Evidence-driven evolution plan
```

See [ARCHITECTURE.md](ARCHITECTURE.md) for the proposed V1 design and [ROADMAP.md](ROADMAP.md) for what is deliberately postponed.
