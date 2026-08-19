# Deployment troubleshooting

## Manifest validation fails

Run the contract checks without starting a service:

```bash
python scripts/check_module_layers.py
python scripts/validate_release_variant.py release/variants/standard.json --repo-root .
```

Remove secrets from the manifest and put them in the node-local environment.
High-risk modules require explicit acknowledgement; an absent or false
acknowledgement is intentional failure.

## Hosted embedding is unhealthy

An HTTP 500 from a hosted embedding endpoint is a provider failure, not proof
that SQLite or MCP is corrupt. SQLite remains writable and derived work must
wait in durable outbox state.

## LanceDB generation is stale

Do not weaken manifest or project-scope validation. Create a new shadow
generation and promote it only after identity, quality, and project-isolation
evidence passes in a later controlled operation.

## Dashboard automated browser regression

Run this only on a development workstation after confirming ports `19020` and
`19040` are free:

```bash
PP_PYTHON=.venv/bin/python node scripts/dashboard_browser_regression.mjs
```

The runner starts an isolated loopback-only static Dashboard fixture, strict
mock control API, and disposable headless Chromium-family profile. It verifies
the node CPU title icon, control-session flow, node-page refresh/scroll
preservation, explicit diagnostic-bundle POST/download, and absence of browser
errors. It refuses to start if either loopback fixture port is already in use;
it does not open SQLite, start MCP or Maintenance, connect to a provider, or
operate an installed service. Set `PP_BROWSER_BIN` only when the automatic
Chrome/Edge/Chromium discovery does not find a local browser.

For manual visual inspection only, run `python scripts/dashboard_browser_smoke.py`
and stop the fixture with `Ctrl-C` when finished.

## What this PR does not fix

PR 1 does not start MCP, start Maintenance, create SSH accounts/tunnels,
migrate databases, promote LanceDB, install models, or publish artifacts.
