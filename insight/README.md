# tuvl Insight

**Intelligent workflows. Deterministic execution.**

**tuvl Insight** is the development UI that ships with [tuvl](https://tuvl.io). It takes you from a spec to a running workflow in one place:

- a spec and task board for what the feature must do
- a workflow canvas over the same YAML
- runs with breakpoints, step mode and approvals
- checks, judge calibration, and data, model and IAM settings

Insight is served by `tuvl dev` only; it never runs in production.

```bash
uv tool install "tuvl[standard]"
tuvl dev --auto-login     # Insight at http://localhost:8885/insight
```

[Documentation](https://tuvl.dev) · [Source](https://github.com/tuvl-io/tuvl) · Apache-2.0

## Verifying a release

From 2.0.0, `tuvl` and `tuvl-insight` are published to PyPI by a GitHub Actions workflow in the public repository [tuvl-io/tuvl](https://github.com/tuvl-io/tuvl), using PyPI Trusted Publishing, and each file carries a signed [provenance attestation](https://docs.pypi.org/attestations/) naming that repository and `publish.yml`.

PyPI may show **"The repository or configuration that created this release differs from a previous release"**. It compares each release's publisher with the previous one, and earlier releases came from elsewhere:

| Release | Published from |
|---|---|
| Releases before 2.0 | A build server, without provenance |
| 2.0.0b1 to 2.0.0b10 | A private repository (`tuvl-io/tuvl-private`), with provenance for some of them |
| 2.0.0 and later | The public repository `tuvl-io/tuvl` |

The publisher changed once, when the source moved to the public repository. The notice does not mean a release was altered. To check one yourself, run this in the folder you downloaded to:

```bash
pip download tuvl-insight==2.0.0 --no-deps
pypi-attestations verify pypi --repository https://github.com/tuvl-io/tuvl pypi:tuvl_insight-2.0.0-py3-none-any.whl
```

More in the [installation guide](https://tuvl.dev/getting-started/installation/#verifying-a-release-and-the-pypi-provenance-notice).
