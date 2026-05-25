# Skill Assets CI Gate Maintenance

## Goal

Keep this PR focused on project-local AI skill contracts while allowing the existing framework grep gate to run correctly.

## Changes

- Treat runtime outbox propagation files as framework-owned allowlist entries for Gap 11.
- Make closed-gap grep counters tolerate zero matches under GitHub Actions `pipefail`; zero matches should produce `count=0`, not abort the job before the explicit assertion message.

## Validation

- Run the Gap 11 contextvar and raw-header checks locally.
- Run the full closed-gap grep gate locally with `bash --noprofile --norc -e -o pipefail`.
