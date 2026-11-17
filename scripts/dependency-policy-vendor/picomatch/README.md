# Picomatch security backport

Tasklease’s build tools use `@tasklease/picomatch@4.0.7-tasklease.1` through the
`picomatch` override. Its base is the MIT-licensed npm release `picomatch@4.0.7`,
published August 24, 2026. The tarball URL and verified SHA-512 are in
[package.json](package.json). The original runtime files and license are included.
[security.patch](security.patch) records the complete parser change.

A repeated glob branch containing a segment wildcard can produce nested
unbounded repetition. When the branch consists of one star surrounded by
literal filename characters, the star can absorb the intervening copies.
The parser therefore emits one copy for one-or-more, and an optional copy for
zero-or-more. Captures reflect that single copy. For more complex single
branches containing a wildcard token, the parser uses its existing literal
fallback. This guard also applies when callers disable the upstream recursion
check with `maxExtglobRecursion: false`.

The override covers the build dependency named in the advisory. Repeated
alternatives and nested negation can still produce costly matches. Keep build
patterns under repository control. Broader hardening should cover these forms
when evaluating an upstream replacement. [Tracking issue](https://github.com/provos/tasklease/issues/24)

The dependency policy pins each runtime file, the manifest and the license.
Regression tests inspect the compiled repetition and exercise filename, path
separator and literal matching through both package entry points. Replace
the local override when a compatible upstream fix clears the seven-day age
check. [GitHub advisory](https://github.com/advisories/GHSA-fc5q-39xc-vjpf)
