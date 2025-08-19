import os

from common import SynchronizationMode, find_most_recent_resource, AssetType


def synchronize_datasets(
        mode: SynchronizationMode,
        openml_server: str | None = None,
        aiod_server: str | None = None,
):
    openml_server = openml_server or os.getenv("OPENML_SERVER")
    if not openml_server:
        raise ValueError(f"No valid OpenML server configured: {openml_server!r}.")
    aiod_server = aiod_server or os.getenv("AIOD_SERVER")
    if not aiod_server:
        raise ValueError(f"No valid AIoD server configured: {openml_server!r}.")
    # if mode in [SynchronizationMode.ADD, SynchronizationMode.UPDATE, SynchronizationMode.REMOVE]:
    #     raise ValueError(f"Synchronization mode {mode} for datasets is not supported.")


    resource = find_most_recent_resource(aiod_server, AssetType.DATASET, platform="openml")
