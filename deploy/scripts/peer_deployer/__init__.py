"""Peer-supervised deployment toolkit for the Control Room.

Hard safety contract:

  * Target instance and supervisor instance must be derived from the
    authenticated caller's cgroup, never from caller-supplied claims.
  * Target == supervisor is REFUSED at every layer.
  * Every mutable phase is preceded by a strict preflight.
  * Promotion plans are loaded from root-owned files, not constructed
    from request data.
  * The accepted artifact identity is loaded from a root-owned trusted
    registry, not hardcoded in application code.
  * Every mutated resource is recorded under a transaction identity.
  * Rollback is restricted to resources owned by a transaction.
  * The peer-deployer never overwrites a supervisor it did not
    authenticate as the supervisor.

The bidirectional design is real: whichever (caller, target) pair
the operator bootstrapped a plan for, the daemon executes.  If no
plan exists, the daemon refuses.  The legacy hardcoded O2 -> O1
state machine in :mod:`host_promotion` is preserved for compatibility
with the existing O2 -> O1 v3 promotion CLI but is NOT used by the
permanent service.
"""
