"""HF-015 runnable pass/fail/skip example used by test manifests."""


def main(outcome: str = "pass") -> dict:
    if outcome not in {"pass", "fail", "skip"}:
        raise ValueError("outcome must be pass, fail, or skip")
    return {"status": outcome, "details": f"deliberate {outcome} example"}
