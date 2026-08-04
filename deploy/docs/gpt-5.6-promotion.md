# GPT-5.6 canary and future Omnigent 1 promotion

Omnigent 2 canary exposes GPT-5.6 through the existing Pi -> OmniRoute gateway provider. The default remains `custom/best-coding`.

Tracked model keys in the Omnigent config template:

- `default: custom/best-coding`
- `gpt56_sol: codex/gpt-5.6-sol`
- `gpt56_terra: codex/gpt-5.6-terra`
- `gpt56_luna: codex/gpt-5.6-luna`

Pi version proven for the canary: `0.83.0`. Pi already contains GPT-5.6 model metadata. Supported reasoning selector levels for these models are `off` (`none` on wire), `low`, `medium`, `high`, `xhigh`, and `max`; `minimal` is unsupported.

OmniRoute must include the paired `omniroute-customizations` GPT-5.6 Codex client requirement (`CODEX_CLIENT_VERSION=0.146.0`).

Future Omnigent 1 promotion: deploy the same committed Omnigent application/configuration version and the same Pi/OmniRoute versions to the Omnigent 1 instance in place. Preserve Omnigent 1 instance state and identity: `/opt/omnigent`, `/etc/omnigent`, `/var/lib/omnigent`, database, HOME, host identity, local port `4097`, and public port `1111`. Do not copy Omnigent 2 sessions, database, HOME, runtime paths, or host identity into Omnigent 1.
