# Reviewed deepmerge security backport

This visibly named fork, `@tasklease/deepmerge@4.3.1-tasklease.1`, replaces the
transitive `deepmerge` used by the Svelte Vite plugin. It is reviewed repository
source, not a newly published registry dependency. Remove it when an aged,
compatible upstream release fixes
[GHSA-gcqx-w48m-w2h4 / CVE-2026-93753](https://github.com/advisories/GHSA-gcqx-w48m-w2h4).
The advisory lists deepmerge 4.2.0 through 4.3.1 as affected with no patched
version available. Downgrading to 4.1 would discard later protections.

The base is the official `deepmerge@4.3.1` tarball published
2023-03-16T22:58:56.511Z:

```text
https://registry.npmjs.org/deepmerge/-/deepmerge-4.3.1.tgz
sha512-3sUqbMEc77XqpdNO7FRyRog+eW3ph+GYCbj+rK+uYyRMuwsVy0rMiVtPn+QJlKFvWP/1PYpapqYn0Me2knFn+A==
```

The tarball SHA-512 was verified before extracting `dist/cjs.js` as `index.cjs`,
`index.d.ts`, and `license.txt` as `LICENSE`. The original `dist/cjs.js` SHA-256 is
`2e5160f84a1141bd927264afe4d15766f1e6cf07daef182b37ff53fea121de7d`.
The types and MIT license are unchanged. `security.patch` records the complete
code change against that original file.

The patch defines output keys as own data properties rather than assigning them.
In particular, a target's own enumerable `__proto__` key cannot invoke the
inherited setter and change the merged result's prototype. Existing source-key
guards remain. Ordinary results retain `Object.prototype`; symbols, arrays,
cloning and custom merge behavior remain covered by regression tests.

`scripts/dependency-policy.test.mjs` exercises the advisory case and compatibility.
The policy script pins the fork's runtime, types, license and manifest hashes;
changing the vendored source requires reviewing those hashes and tests together.
The root direct file dependency and `$deepmerge` override ensure npm uses this
source for the plugin, and the policy rejects a registry deepmerge or a different
local replacement.
