def evidence_gate(state):
    return "stop" if state["status"] != "RUNNING" else "continue"


def after_classify(state):
    if state["status"] != "RUNNING":
        return "stop"
    return "retry" if state.get("reason_code") == "CLASSIFICATION_FAILED" else "continue"


def after_validate(state):
    if state["status"] != "RUNNING":
        return "stop"
    if not state.get("validation_errors"):
        return "valid"
    return "repair" if state["compose_attempts"] <= state["repair_cap"] else "stop"


def mode(state):
    return state["mode"]
