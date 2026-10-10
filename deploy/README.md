# Single-region deployment

Build one immutable image from `deploy/Dockerfile`. Run it as three process types in one approved
region:

- release: `memory-ops-migrate`, once per deployment, using `DATABASE_URL_UNPOOLED`;
- API: the image default `memory-ops-api`, using the pooled `DATABASE_URL`;
- worker: override the command with `memory-ops-worker`, using the pooled `DATABASE_URL`.

The API and worker may scale as separate processes but remain one modular service. PostgreSQL is the
durable queue and canonical store; Redis, Kafka, Kubernetes, a graph store, and a dedicated vector
store are not required.

Route traffic only after `/health/ready` returns 200. `/health/live` reports process liveness and
must not be used as readiness. On `SIGTERM`, the API rejects new protected work and receives the
configured Uvicorn drain window; the worker finishes its claimed batch before exiting. Configure
`MEMORY_OPS_API_MAX_IN_FLIGHT` and `MEMORY_OPS_WORKER_BATCH_SIZE` as the local process quotas.

Required secrets and identity values come from the deployment platform; never bake them into the
image. Use TLS for PostgreSQL and object storage, retain the direct migration URL only in the release
process, and keep API and worker egress in the approved region.
