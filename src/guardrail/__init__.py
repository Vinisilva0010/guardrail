from guardrail.logging_setup import configure_logging

# Every command and the long-running processes import this package, so secrets are
# masked in logs wherever the work starts.
configure_logging()


def main() -> None:
    print("Hello from guardrail!")
