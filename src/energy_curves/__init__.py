"""Energy price ingestion, forward-curve generation, and alerting."""


def main() -> None:
    from energy_curves.cli import main as cli_main

    raise SystemExit(cli_main())
