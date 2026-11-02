"""Allow `python -m prebidding.seed` via package helpers."""

from prebidding.seed import seed

if __name__ == "__main__":
    print(seed(force=True))