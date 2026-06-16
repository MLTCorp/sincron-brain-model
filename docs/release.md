# Release Process

This project publishes Python distributions from signed GitHub Actions release
workflows, not from a developer laptop.

## One-Time Setup

1. Create or claim the `sincron-brain-model` project on PyPI.
2. Configure PyPI Trusted Publishing for this repository:
   - repository owner: `MLTCorp`
   - repository name: `sincron-brain-model`
   - workflow name: `release.yml`
   - environment name: `pypi`
3. In GitHub, create an environment named `pypi`.
4. Require manual approval on the `pypi` environment if a human approval gate is
   desired before publishing.
5. Protect release tags matching `v*.*.*` so only maintainers can create them.

No `PYPI_TOKEN` or password secret should be configured for the release workflow.

## Release Checklist

1. Update `version` in `pyproject.toml`.
2. Run:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -e ".[dev]"
   .\.venv\Scripts\python.exe -m ruff check .
   .\.venv\Scripts\python.exe -m pyright
   .\.venv\Scripts\python.exe -m pytest -q
   .\.venv\Scripts\python.exe -m pip_audit
   Remove-Item -Recurse -Force dist -ErrorAction SilentlyContinue
   .\.venv\Scripts\python.exe -m build
   .\.venv\Scripts\python.exe -m twine check dist/*.whl dist/*.tar.gz
   ```

3. Commit the version bump and release notes.
4. Create and push a signed tag that matches the package version:

   ```powershell
   git tag -s v0.1.0 -m "sincron-brain-model v0.1.0"
   git push origin v0.1.0
   ```

   If GPG signing is not available yet, use an annotated tag temporarily:

   ```powershell
   git tag -a v0.1.0 -m "sincron-brain-model v0.1.0"
   git push origin v0.1.0
   ```

The release workflow refuses to publish if the tag does not match
`pyproject.toml` exactly.

## Release Artifacts

The workflow produces:

- source distribution (`.tar.gz`);
- wheel (`.whl`);
- `SHA256SUMS.txt`;
- GitHub artifact attestation for the `dist/` contents;
- draft GitHub Release with the artifacts attached;
- PyPI publication via Trusted Publishing.

## Verifying Artifacts

Download the release assets and compare SHA256 values:

```powershell
Get-FileHash .\sincron_brain_model-*.whl -Algorithm SHA256
Get-Content .\SHA256SUMS.txt
```

For GitHub provenance, use GitHub's artifact attestation tooling for the release
asset from the published repository.
