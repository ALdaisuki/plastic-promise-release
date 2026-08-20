# Windows Release Builder installation

Run this only from an interactive Windows desktop session after the Plastic
Promise package is installed. The installer writes no credentials and creates
no task, service, polling loop, tunnel, or server connection.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
  D:\PlasticPromise\remote-builds\<40-char-source-sha>\source\deploy\release-builder\windows-install.ps1
```

The default mode is `desktop-interactive`, which leaves Docker Desktop's normal
interactive credential helper available to a later explicitly confirmed release
request. It does not initiate a build or publication. The optional
`headless-builder` mode keeps local build/smoke capability but rejects stable
publication until separate credentials are provisioned.

The installer keeps all Builder files under `D:\PlasticPromise\release-builder`
and rejects `C:`. It is request-triggered rather than a daemon: use the created
`run-release-builder.ps1` wrapper to submit or confirm a request. Never put a
token, private key, Docker config, or server endpoint in `builder-config.json`.
