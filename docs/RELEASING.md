# Releasing

The version lives in one place: `version` in `pyproject.toml`. The app reads it
at runtime, so `/openapi.json` and `app_assignment_1.__version__` always match.
Each release is a Git tag `vX.Y.Z` on `main`. Pre-1.0: a breaking change bumps
the minor number, anything else the patch number.

1. Move the `[Unreleased]` entries in `CHANGELOG.md` under a new `## [X.Y.Z]` heading and fix the compare links at the bottom.
2. Change `version` in `pyproject.toml`, then run `uv lock` (the lock records the project version).
3. Open a pull request with both changes; merge it once CI is green and a teammate approved it.
4. Tag the merge commit and push the tag:
   ```bash
   git checkout main && git pull
   git tag -a vX.Y.Z -m "vX.Y.Z" && git push origin vX.Y.Z
   ```
   You should see the tag under the repository's Tags. CI publishes `ghcr.io/sv642-hub/app-assignment-1/gateway` and `.../proxy` for every push, tagged with the commit sha. On the tag push it also adds the version tag (`X.Y.Z`, no `v`); deploy that tag.
