# Dependency and supply-chain policy

## Locked profiles

- `requirements/docker-gpu.lock`: hashed transitive application/ML runtime, without PyTorch.
- `requirements/torch-cu128.lock`: Docker and CUDA 12.8 native PyTorch 2.10 profile.
- `requirements/torch-cu126.lock`: CUDA 12.6/Volta PyTorch 2.10 profile. V100 must use this profile.
- `requirements/torch-cpu.lock`: Windows CPU compatibility profile.
- `requirements/native.lock`: hashed transitive native application runtime, without provider-managed PyTorch.
- `requirements/release-python.lock`: cross-platform transitive release inventory used by SBOM and audit.

Regenerate with `uv pip compile --python-version 3.12 --universal --no-header --generate-hashes` from matching `.in` file. Torch profile locks add matching `--torch-backend {cu126,cu128,cpu}`. Review lock diffs and run both audits before merge.

PyTorch 2.10 is deliberate compatibility ceiling: official 2.10 CUDA matrix supports CUDA 12.6 and 12.8, including Volta `sm_70`; later CUDA 12.8 wheels remove Volta. GPU acceptance still requires real target hardware and is not claimed by CPU CI.

## Audit policy and VEX

CI runs `npm audit` with no ignores. CI runs `pip-audit` against full transitive release lock with two explicit ignores:

| Advisory | Status | Justification | Removal condition |
|---|---|---|---|
| `PYSEC-2026-139` / `CVE-2026-4538` | `not_affected` | Finding requires loading an attacker-controlled PT2 artifact. Project has no PT2, `torch.export`, `torch.jit`, or `torch.load` path; models use locked allowlists and verified hashes. No patched 2.10-compatible version exists. | Remove ignore when upstream publishes compatible patch, or before adding PT2 artifact loading. |
| `PYSEC-2025-194` / `CVE-2025-3000` | `not_affected` | Finding targets local untrusted `torch.jit.script` input. Project does not invoke TorchScript/JIT APIs and never accepts user-supplied model code. Fixed 2.13 breaks release's V100/CUDA 12.8 compatibility contract. | Remove ignore when compatible fixed PyTorch keeps `sm_70`, or before adding JIT/script loading. |

No other audit suppressions are allowed without same reachability evidence, owner review, and removal condition.
