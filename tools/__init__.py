__all__ = [
    "GitHubClient", "PRDiff", "FileDiff",
    "ASTParser", "CodeStructure", "FunctionInfo",
    "SemgrepRunner", "SemgrepScanError", "SecurityIssue",
]


_EXPORTS = {
    "GitHubClient": "tools.github_client",
    "PRDiff": "tools.github_client",
    "FileDiff": "tools.github_client",
    "ASTParser": "tools.ast_parser",
    "CodeStructure": "tools.ast_parser",
    "FunctionInfo": "tools.ast_parser",
    "SemgrepRunner": "tools.semgrep_runner",
    "SemgrepScanError": "tools.semgrep_runner",
    "SecurityIssue": "tools.semgrep_runner",
}


def __getattr__(name: str):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value
