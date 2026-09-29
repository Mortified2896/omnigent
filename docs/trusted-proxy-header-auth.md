# Trusted proxy identity with accounts authentication

Accounts authentication can optionally accept one identity header from a
trusted reverse proxy while keeping the normal accounts cookie and Bearer
token paths enabled. The feature is disabled unless both environment variables
below are set:

```text
OMNIGENT_AUTH_PROVIDER=accounts
OMNIGENT_AUTH_TRUSTED_HEADER=Tailscale-User-Login
OMNIGENT_AUTH_TRUSTED_HEADER_MAP={"Mortified2896@github":"admin"}
```

`OMNIGENT_AUTH_TRUSTED_HEADER_MAP` is a JSON object. Each key is an exact
header value asserted by the proxy; each value is an existing Omnigent user
ID. The target must exist in the accounts store when the app is constructed.
Reserved identities such as `local` and `__public__`, empty identities,
duplicate JSON keys, malformed JSON, and non-string keys or values are
rejected at startup. The mapping never creates accounts or rewrites ownership.

An exact match authenticates as its mapped Omnigent user. An absent or
unmapped value falls through to the unchanged accounts cookie/Bearer check.
The trusted identity therefore sees the mapped user's existing sessions and
projects. Keep the mapped user's admin status in the existing admin list or
permission store; the header mapping itself does not grant administrator
permissions.

## Required ingress boundary

Only enable this behind a reverse proxy that authenticates the caller and
strips client-supplied copies of the configured identity header before
injecting its own value. Do not expose the Omnigent backend directly to
untrusted clients. For Tailscale Serve, use the tailnet-only Serve listener,
keep the Omnigent backend bound to loopback (or an equivalently restricted
private path), and leave Funnel off. Preserve existing Tailscale ACLs.

The application cannot distinguish a request forwarded by Serve from a local
process connecting directly to the loopback backend. A process running on the
Omnigent host can therefore forge the header; this feature does not protect
against a compromised host or malicious local process. It must not be used
with an ingress that passes through caller-supplied identity headers.

Accounts password login remains available for recovery, and headerless
accounts Bearer tokens continue to authenticate internal CLI/host traffic.
