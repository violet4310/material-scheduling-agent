"""Run the CLI acceptance suite offline by default."""

from run_minimax_regression import main


if __name__ == "__main__":
    raise SystemExit(main(default_mode="mock"))
