"""Peer-supervised deployment toolkit for the Control Room.

Hard safety contract:

  * Target instance and supervisor instance must be explicit.
  * Target == supervisor is refused.
  * Every mutable phase is preceded by a strict preflight.
  * Every mutated resource is recorded under a transaction identity.
  * Rollback is restricted to resources owned by a transaction.
  * The peer-deployer never overwrites the supervisor's runtime.
  * Stale transactions are reconciled via a first-class
    quarantine operation that preserves the historical evidence.
  * The path-safety allowlist is the single source of truth for
    which paths the deployer is allowed to mutate.

See ``deploy/docs/control-room-dual-instance-upgrade-safety.md`` for the
full design.
"""
