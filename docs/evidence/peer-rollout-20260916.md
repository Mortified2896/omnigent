# Initial peer rollout — 2026-09-16

Historical observation extracted from `deploy/docs/rtx-peer-v2.md`; not current
environment selection or a fresh live verification.

RTX O1 and O2 both run accepted artifact
`1496332611ffb301e7217c7cc66674f29e23e8db` after deep task/UI/auth/state acceptance.
O2's task supervised O1 adoption; O1's task supervised a one-shot candidate-start
failure on O2, which restored the exact prior release and DB/state while keeping
O1 PIDs unchanged. The fault was removed and O2 resumed a saved session afterward.
HomeLab records full evidence, endpoints, screenshots and service persistence.
This proves the tested same-schema startup-failure path, not arbitrary migrations.
