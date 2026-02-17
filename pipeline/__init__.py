"""Pipeline — Generalized product research pipeline."""


def __getattr__(name):
    if name == "PipelineSession":
        from .session import PipelineSession
        return PipelineSession
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["PipelineSession"]
