## Summary


## Validation


## Framework Layer

Framework-Layer-Spec:

- [ ] This PR does not change the agent-service framework layer.
- [ ] This PR changes the framework layer and cites the spec above.

Framework layer means the agent-service plugin host (`app/host/**`), the round's
contract and fixed loop (`app/agent/contracts.py`, `app/agent/round.py`), the app
manifests (`app/manifests/**`, `app/deployment.py`), the operational plugins
(`app/plugins/**`), the storage core, outbound queues, the entrypoint, the CI checks
(`apps/agent-service/scripts/**`, `.github/workflows/**`) and the framework docs; see
`docs/governance/framework-layer.md`.
