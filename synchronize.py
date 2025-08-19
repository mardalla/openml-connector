import argparse
import logging
from enum import StrEnum, auto


class AssetType(StrEnum):
    DATASET = auto()
    MODEL = auto()
    EXPERIMENT = auto()


class SynchronizationMode(StrEnum):
    ADD = auto()
    UPDATE = auto()
    REMOVE = auto()
    ALL = auto()


SUPPORTED_OPERATIONS: list[tuple[AssetType, SynchronizationMode]] = []


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--type",
        default=[],
        nargs='+',
        help=f"Asset type(s) which to synchronize.",
        dest="types",
        required=True,
        choices=list(map(str, AssetType)),
    )

    parser.add_argument(
        "--mode",
        default=[],
        nargs='+',
        help=f"How to synchronize.",
        dest="modes",
        choices=list(map(str, SynchronizationMode)),
        required=True,
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()


if __name__ == '__main__':
    main()
