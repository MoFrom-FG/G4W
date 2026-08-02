from G4W.features.native_sop import execute, print_result


def run(action: str, arguments: dict | None = None, context_path="G4W-context.json"):
    return execute("timeline", action, arguments, context_path)
