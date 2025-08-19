from datetime import date, timedelta
from enum import StrEnum, auto
from http import HTTPStatus
import logging

import requests

logger = logging.getLogger(__file__)


class AssetType(StrEnum):
    DATASET = auto()
    MODEL = auto()
    EXPERIMENT = auto()


class SynchronizationMode(StrEnum):
    ADD = auto()
    UPDATE = auto()
    REMOVE = auto()
    ALL = auto()


class FailedRequest(Exception):

    def __init__(self, response: requests.Response, message: str = ""):
        super().__init__(message)
        self.response = response
        self.request = response.request


def abort_if_error(response: requests.Response) -> None:
    if response.status_code == HTTPStatus.OK:
        return
    logger.error(f"Request {vars(response.request)} failed.")
    logger.error(f"Status code: {response.status_code}")
    logger.error(f"Response Body: {response.content}")
    raise FailedRequest(response=response)


def find_most_recent_resource(server: str, asset_type: AssetType, platform: str = "openml") -> dict:
    """Return JSON of the most recent asset of the type on AIoD from the platform."""
    # There is no direct way to request this information, in the future this should probably
    # just send an elastic search query instead, when that endpoint is available.
    # For now we use a work-around and find the most recently modified example, which has a
    # matching creation date (this means it is also the most recent registered asset)
    url = f"{server}/platforms/{platform}/{asset_type}s?schema=aiod&offset=0&limit=10"

