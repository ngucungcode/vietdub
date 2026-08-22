"""Minimal TIGER layer package surface required by TIGER-DnR inference.

The pinned upstream initializer eagerly imports training-only STFT helpers and
their legacy dependency graph.  TIGER-DnR itself imports only ``activations``
and ``normalizations``; Python resolves those two local submodules lazily.
Keeping this initializer intentionally empty avoids installing unused training
packages in the offline production worker without changing model code.
"""

__all__: tuple[str, ...] = ()
