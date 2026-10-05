from agents.base import AgentResult, BaseReviewAgent, FileDiff, Finding

__all__ = ["AgentResult", "BaseReviewAgent", "FileDiff", "Finding", "StyleAgent"]


def __getattr__(name: str):
    if name == "StyleAgent":
        from agents.style_agent import StyleAgent

        return StyleAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
