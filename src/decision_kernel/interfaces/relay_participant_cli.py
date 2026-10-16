"""Reviewed participant sources through the existing batch CLI engine."""
from decision_kernel.interfaces.relay_financial_cli import main as batch_main


def main(argv=None):
    return batch_main(argv, family="participants")


if __name__ == "__main__":
    raise SystemExit(main())
