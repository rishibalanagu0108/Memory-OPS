# Recovery incident procedure

1. **Detect and classify:** open an incident, record the affected branch and time window, and page the
   incident commander, database operator, security owner, and application owner. Never put memory or
   document content in the incident record.
2. **Contain:** stop API traffic and workers for the target environment, preserve logs and the current
   branch, revoke exposed credentials if compromise is suspected, and keep policy enforcement closed.
3. **Recover offline:** select the restore point with Time Travel Assist, create the isolated recovery
   branch, replay the external deletion ledger, migrate, and run recovery, isolation, deletion, and
   compatibility gates.
4. **Authorize cutover:** require evidence for RPO, RTO, deletion completeness, zero resurrection,
   encryption controls, and rollback. The incident commander and database operator approve cutover;
   a security owner also approves security incidents.
5. **Restore service:** enable the worker first, verify durable backlog recovery, then enable the API
   canary and monitor errors, index lag, authorization denials, and resurrection probes before full
   traffic.
6. **Close and learn:** preserve content-free evidence, rotate temporary credentials, remove expiring
   drill branches, document actual RPO/RTO, and create a bounded corrective task for every failed
   objective. Never weaken a deletion or policy gate to shorten recovery.
