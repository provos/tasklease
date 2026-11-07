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
  JSON Schema contract, the Python package and `tl` CLI (published on PyPI),
  the `tl monitor` dashboard, testing, deployment, milestones.

The first user is FluxShot Editor. Its conventions (verify, build and advise
topics, their request and reply schemas, the worker loops) and its workers
live in the FluxShot repository:
[tasklease-profiles.md](https://github.com/provos/fluxshoteditor/blob/master/docs/point-designs/tasklease-profiles.md).

Status: draft specification and implementation design, no code yet.
