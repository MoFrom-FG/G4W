from G4W.features.native_sop import execute, print_result


def run(action: str, arguments: dict | None = None):
    return execute("dida", action, arguments)
