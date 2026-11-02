# Releasing `arango-sparql-py` to PyPI

**Published on PyPI**: 0.2.0 (2026-09-21, a `workflow_dispatch` run with
`target: pypi`), 0.3.0 (2026-10-16, tag `v0.3.0`), 0.3.1 (2026-10-20, tag
`v0.3.1`), 0.4.0 (2026-10-21, tag `v0.4.0`), 0.4.1 (2026-10-23, tag
`v0.4.1`), 0.5.0 (2026-10-27, tag `v0.5.0`), 0.6.0 (2026-11-02, tag
`v0.6.0`) and 0.6.1 (2026-11-02, tag `v0.6.1`). Later releases follow
"Cut a release" below, minus the one-time setup. The workflow that does the upload —
[`.github/workflows/publish.yml`](.github/workflows/publish.yml) — is **inert
until a `v*` tag is pushed**, and mirrors `arango-query-core`'s proven setup
(Trusted Publishing / OIDC, no API tokens).

## Prerequisite (blocker): repoint the `arango-query-core` dependency — ✅ DONE (#9)

**PyPI rejects any package whose `Requires-Dist` contains a direct URL** (a
`… @ git+https://…` reference). The `[nl]`/`[dense]` extras used to git-pin the
shared engine, which would have made this package unpublishable.

This is now resolved — PR #9 repointed both extras from `@ git+…@f2f3061` to the
published range (matching this repo's convention for its other published
dependency, `arangodb-schema-analyzer[...]` — whose band is declared only in
`pyproject.toml` and governed by the alignment invariant in
`docs/architecture/PRD.md` §12.1):

```toml
# [nl] extra
"arango-query-core>=0.2.3,<0.3.0"
# [dense] extra
"arango-query-core[dense]>=0.2.3,<0.3.0"
```

The floor is 0.2.3, the first release with `PostconditionViolation.soft`
(PRD §7.3.1): on 0.2.0–0.2.2 a soft postcondition silently fails closed.
`tests/nl2sparql/test_postconditions.py::test_the_declared_query_core_floor_ships_soft_postconditions`
fails if a declared floor drops below it.

`arango-query-core 0.2.0` is on PyPI and verified to contain
`nl/postconditions.py` (the merged `f2f3061` work) plus the `dense`/`nl` extras.
The repoint also required implementing seam 8 (`path_index`/`path_prompt_section`)
on both adapters as an opt-out, since `0.2.0`'s engine calls `path_index()`
unconditionally — see #9. `uv.lock` was regenerated and the goldens re-run green
(CC-9). **No remaining direct-URL dependencies**, so `twine`/upload will not be
rejected.

> If a git pin ever creeps back in, `publish.yml`'s `twine check` plus the
> upload step will fail fast — repoint before tagging.

## We publish from ArthurKeen — the Trusted-Publisher binding is repo-EXACT

We publish from **`ArthurKeen/arango-sparql-py`** (Arthur has no admin on the
arango-solutions org). `publish.yml`'s guard already matches, and the dual-push
mirror to arango-solutions is a clean no-op there.

> ⚠️ **A tag you cannot publish is a version number you have burned.** The
> Trusted-Publisher binding is `owner/repo/workflow/environment`-exact. The
> sister project `arango-schema-analyzer` has been unable to publish since its
> repo was *renamed*: PyPI's OIDC entry no longer matched the token's
> `repository`, and every tagged release fails with
> `invalid-publisher: valid token, but no corresponding publisher`
> (see that repo's `RELEASING.md`). So **register the publisher for the exact
> values below, and prove it on TestPyPI before tagging.**

### One-time setup

1. **PyPI** → *Your projects* → *Publishing* → **Add a pending publisher (GitHub)**:

   | Field | Value |
   |---|---|
   | PyPI Project Name | `arango-sparql-py` |
   | Owner | `ArthurKeen` |
   | Repository name | `arango-sparql-py` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

2. **TestPyPI** (<https://test.pypi.org>) → add the same pending publisher, but
   Environment `testpypi`. This is what the dry run uploads to.
3. **GitHub** (ArthurKeen repo) → *Settings* → *Environments* → create `pypi`
   **and** `testpypi`. No secrets — OIDC only.

## Cut a release

1. Dependency repoint — ✅ already landed (#9); no direct-URL deps remain.
2. Build it locally to sanity-check: `python -m build && twine check --strict
   dist/*` — both must PASS.
3. Register the publishers + create the environments (above).
4. **Prove OIDC on TestPyPI FIRST — do not skip.** ArthurKeen repo → *Actions* →
   *Publish to PyPI* → *Run workflow* → `target: testpypi`. A green run that
   uploads to <https://test.pypi.org/project/arango-sparql-py/> confirms the
   publisher is registered and working. A red `invalid-publisher` here is the
   analyzer's trap — fix the PyPI entry and re-run; **no tag has been burned.**
5. Bump `[project].version` in `pyproject.toml` **and** `arango_sparql.__version__`
   (`tests/test_version_parity.py` keeps them equal), run `uv lock` so the
   package's own entry in `uv.lock` follows (0.3.0 missed it, fixed after in
   `fe7e9ae`), merge, then tag the merge commit and push the tag (dual-push sends
   it to both remotes; the workflow fires only on ArthurKeen, is skipped on the
   mirror):
   ```bash
   git tag -a v<version> -m "Release <version>" && git push origin v<version>
   ```
6. Verify: `pip install arango-sparql-py==<version>` in a clean venv.
7. CDF pins this package **exactly** (`deploy/pins/arango-sparql-py.txt`, its
   CC-9): move that pin by a CDF PR and run CDF's engine-free suite and live
   golden gate on it.

## Notes

- `arango-sparql-py` now depends on the **published** `arango-query-core`
  (`>=0.2.0,<0.3.0`), so a future engine release within that band is picked up by
  a normal `uv lock` / reinstall — no more git-pin bumps for minor engine work.
- `arango-cypher-py` consumes the engine editable (`>=0.1.0,<0.2.0`); a published
  `0.2.0` is outside that range, so if it ever moves off the editable sibling
  install it needs its own range widened (tracked in that repo).
