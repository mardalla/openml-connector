import argparse
import logging
import sys

from dotenv import load_dotenv

from common import AssetType, SynchronizationMode
from datasets import synchronize_datasets

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
    load_dotenv()

    logging.basicConfig(level=logging.INFO)
    logging.info(f"Started {' '.join(sys.argv)}")


    synchronize_datasets(SynchronizationMode.ADD)


if __name__ == '__main__':
    main()
