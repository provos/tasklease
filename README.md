# tasklease

A small task board for coordinating agents and workers across machines. Every
task is held under a lease, and its ID changes each time it is owned. Topics
live in domains: separate namespaces with any name, for example a GitHub
repository (`github.com/provos/fluxshoteditor`) or `render-farm.example`. It
runs on Cloudflare Workers and Durable Objects, with large files in R2.

An agent on one machine posts a task ("verify this PR on Linux"). A worker on
another machine owns it, does the work and consumes it. Consuming deletes the
request and posts the reply to a topic the requesting agent watches, in one
atomic step.

- [docs/spec.md](docs/spec.md): the generic service: task model, operations,
  delivery semantics, authorization, Cloudflare implementation, CLI.
- [docs/design.md](docs/design.md): how to build it: TypeScript service,
  JSON Schema contract, Python package and `tl` CLI, testing, deployment,
  milestones.
- [docs/fluxshot-profiles.md](docs/fluxshot-profiles.md): the first user,
  FluxShot Editor: verify, build and advise topics, their request and reply
  schemas, and the worker loops.

Status: draft specification and implementation design, no code yet.
