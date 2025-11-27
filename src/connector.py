import datetime as dt
import time
from datetime import datetime
import os

import dateutil.parser
from argparse import ArgumentParser
from http import HTTPStatus
from pathlib import Path
import logging
from enum import StrEnum, auto

import aiod
from aiod.authentication import set_token, Token
from dotenv import load_dotenv
import requests
from requests.exceptions import JSONDecodeError as RequestsJSONDecodeError


class Modes(StrEnum):
    ALL = auto()
    SINCE = auto()
    ID = auto()


logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = 10
MAX_TEXT = 65535
MAX_DESCRIPTION_LENGTH = 1800
PLATFORM_NAME = "openml"
STOP_ON_UNEXPECTED_ERROR = False
PER_DATASET_DELAY = None


class ParsingError(Exception):
    pass


class ServerError(Exception):
    pass


def list_datasets(from_: int | None = None):

    def paginate_all_datasets(items_per_page: int = 50):
        url_template = (
            "https://www.openml.org/api/v1/json/data/list/limit/"
            f"{items_per_page}/offset/{{offset}}"
        )
        for offset in range(0, 1_000_000, items_per_page):
            url = url_template.format(offset=offset)
            response = requests.get(url, timeout=REQUEST_TIMEOUT)

            if not response.ok:
                status_code = response.status_code
                # OpenML “database connection error, please wait N seconds” → 412
                # Don’t kill the whole run; skip this page and continue.
                if status_code == 412:
                    logger.warning(
                        "OpenML returned 412 (database connection error) for %s; "
                        "skipping this page and continuing with the next offset.",
                        url,
                    )
                    continue

                try:
                    msg = response.json()["error"]["message"]
                except Exception as e:
                    logger.error("Error while paginating datasets, cannot continue.")
                    logger.exception(e)
                    msg = response.content
                err_msg = (
                    f"Error while fetching {url} from OpenML: "
                    f"({status_code}) {msg}"
                )
                raise ServerError(err_msg)

            logger.debug(f"Paging through datasets (offset {offset})")

            try:
                dataset_summaries = response.json()["data"]["dataset"]
                if dataset_summaries:
                    yield from dataset_summaries
                    logger.debug(
                        f"Paged through datasets (total {len(dataset_summaries)})"
                    )
                else:
                    break
            except Exception as e:
                # If OpenML returns non-JSON or a weird structure for some page,
                # treat it as fatal for this run.
                raise ParsingError(
                    f"Could not parse response ({response.status_code}): "
                    f"{response.content}"
                ) from e

    from_ = from_ or 0
    for dataset in paginate_all_datasets():
        try:
            identifier = dataset["did"]
            if identifier < from_:
                continue
        except KeyError:
            logger.error(f"Received invalid summary: {dataset}", exc_info=True)
            continue

        try:
            # dataset["quality"] is the pre-fetched qualities summary, if present.
            yield fetch_openml_dataset(identifier, dataset.get("quality"))
        except Exception as e:
            logger.error(f"Exception when processing dataset {identifier}")
            logger.exception(e)


def fetch_openml_dataset(identifier_: int, qualities: dict | None = None):
    # Only hit /data/qualities if we don’t already have them from the list call.
    if not qualities:
        qualities_url = (
            f"https://www.openml.org/api/v1/json/data/qualities/{identifier_}"
        )
        qualities_response = requests.get(
            qualities_url,
            timeout=REQUEST_TIMEOUT,
        )

        if not qualities_response.ok:
            status_code = qualities_response.status_code
            # “No qualities found” / DB error → 412. We just continue without qualities.
            if status_code == 412:
                logger.warning(
                    "OpenML returned 412 for qualities of dataset %s; "
                    "continuing without qualities.",
                    identifier_,
                )
                qualities = []
            else:
                try:
                    msg = qualities_response.json()["error"]["message"]
                except Exception as e:
                    logger.exception(e)
                    msg = qualities_response.content
                err_msg = (
                    f"Error while fetching {qualities_url} from OpenML: "
                    f"({status_code}) {msg}"
                )
                raise ServerError(err_msg)
        else:
            try:
                qualities = qualities_response.json()["data_qualities"]["quality"]
            except Exception as e:
                raise ParsingError(
                    f"Error parsing JSON of qualities of dataset "
                    f"{qualities_response.content}"
                ) from e

    url_data = f"https://www.openml.org/api/v1/json/data/{identifier_}"
    response = requests.get(url_data, timeout=REQUEST_TIMEOUT)

    if not response.ok:
        status_code = response.status_code
        try:
            msg = response.json()["error"]["message"]
        except Exception:
            msg = response.content
        err_msg = f"Error while fetching {url_data} from OpenML: ({status_code}) {msg}"
        raise ServerError(err_msg)

    try:
        dataset_json = response.json()["data_set_description"]
        qualities_json = {
            quality["name"]: quality["value"] for quality in (qualities or [])
        }
        return dataset_json | {"qualities": qualities_json}
    except Exception as e:
        raise ParsingError(
            f"Error parsing JSON of dataset {response.content}"
        ) from e


def _convert_dataset_to_aiod(dataset: dict) -> dict:
    identifier = dataset["id"]

    description = dataset["description"]
    if isinstance(description, list) and len(description) == 0:
        description = ""
    if not isinstance(description, str):
        logger.warning(
            f"Ignoring description {description} of dataset {identifier}."
        )
        description = ""
    if len(description) > MAX_DESCRIPTION_LENGTH:
        text_break = " [...]"
        description = (
            description[: MAX_DESCRIPTION_LENGTH - len(text_break)] + text_break
        )

    size = None
    if n_rows := dataset["qualities"].get("NumberOfInstances"):
        size = {
            "unit": "instances",
            "value": int(float(n_rows)),  # OpenML adds the decimal: xxx.0
        }
    keyword = dataset.get("tag", [])
    if isinstance(keyword, str):
        keyword = [keyword]

    return dict(
        platform=PLATFORM_NAME,
        platform_resource_identifier=identifier,
        name=dataset["name"],
        version=dataset["version"],
        same_as=f"https://www.openml.org/api/v1/json/data/{identifier}",
        description=dict(plain=description),
        date_published=dateutil.parser.parse(
            dataset["upload_date"]
        ).isoformat(),
        license=dataset.get("licence"),
        distribution=[
            dict(content_url=dataset["url"], encoding_format=dataset["format"])
        ],
        is_accessible_for_free=True,
        keyword=keyword,
        size=size,
    )


def upsert_dataset(dataset: dict) -> int:
    identifier = dataset["id"]
    try:
        local_dataset = _convert_dataset_to_aiod(dataset)

        try:
            # Try to find an existing AIOD dataset for this OpenML ID.
            aiod_dataset = aiod.datasets.get_asset_from_platform(
                platform=PLATFORM_NAME,
                platform_identifier=identifier,
                data_format="json",
            )
        except (KeyError, RequestsJSONDecodeError) as e:
            # Treat “not found” or “non-JSON” as “no existing dataset yet”.
            logger.debug(
                "No existing AI-on-Demand dataset for OpenML id %s "
                "or response was non-JSON (%s). Registering new asset.",
                identifier,
                e,
            )
            response = aiod.datasets.register(metadata=local_dataset)
            if isinstance(response, str):
                logger.debug(f"Indexed dataset {identifier}: {response}")
                return HTTPStatus.OK
            elif isinstance(response, requests.Response):
                logger.warning(
                    "Error uploading dataset %s (%s): %s",
                    identifier,
                    response.status_code,
                    response.content,
                )
                return response.status_code
            # Anything else is unexpected – surface it.
            raise

        # We got an existing dataset back, ensure it has an identifier.
        if "identifier" not in aiod_dataset:
            raise RuntimeError(
                "Unexpected server response retrieving OpenML dataset "
                f"{identifier} from AI-on-Demand: {aiod_dataset}"
            )

        response = aiod.datasets.replace(
            identifier=aiod_dataset["identifier"],
            metadata=local_dataset,
        )
        if response.status_code == HTTPStatus.OK:
            logger.debug(
                "Updated dataset %s: %s",
                identifier,
                aiod_dataset["identifier"],
            )
        else:
            logger.warning(
                "Could not update %s for OpenML dataset %s (%s): %s",
                aiod_dataset["identifier"],
                identifier,
                response.status_code,
                response.content,
            )
    except Exception as e:
        logger.exception(
            msg=f"Exception encountered when upserting dataset {identifier}.",
            exc_info=e,
        )
        if STOP_ON_UNEXPECTED_ERROR:
            raise
        return -1
    return response.status_code


def parse_args():
    parser = ArgumentParser()

    parser.add_argument(
        "--mode",
        choices=list(Modes),
    )
    parser.add_argument(
        "--value",
        default=None,
        required=False,
        type=str,
        help=(
            "For mode 'ID' this must be an openml identifier. "
            "For mode 'SINCE' this must be an openml identifier, this dataset and "
            "all datasets with higher identifier will be indexed. It can also be "
            "set to 'auto', in which case the last inserted dataset on AI-on-Demand "
            "will be determined and only datasets uploaded after that one are indexed. "
            "Cannot be set with mode 'ALL'."
        ),
    )
    log_levels = [level.lower() for level in logging.getLevelNamesMapping()]
    parser.add_argument(
        "--app-log-level",
        choices=log_levels,
        default="info",
        help="Emit all log messages generated of at least this level by the app.",
    )
    parser.add_argument(
        "--root-log-level",
        choices=log_levels,
        default="error",
        help=(
            "Emit all log messages generated of at least this level "
            "by the app's dependencies."
        ),
    )
    args = parser.parse_args()
    if args.mode == Modes.ALL and args.value:
        logger.error("Cannot run mode 'all' when a value is supplied.")
        quit(code=1)

    return args


def configure_connector():
    global BATCH_SIZE, PLATFORM_NAME, STOP_ON_UNEXPECTED_ERROR, PER_DATASET_DELAY

    dot_file = Path("~/.aiod/openml/.env").expanduser()
    if dot_file.exists() and load_dotenv(dot_file):
        logger.info(f"Loaded variables from {dot_file}")
    else:
        reason = "file does not exist" if not dot_file else "unknown reason"
        logger.info(f"No environment variables loaded from {dot_file}: {reason}.")

    BATCH_SIZE = os.getenv("AIOD_BATCH_SIZE", 25)
    PLATFORM_NAME = os.getenv("PLATFORM_NAME", PLATFORM_NAME)
    PER_DATASET_DELAY = float(delay) if (delay := os.getenv("PER_DATASET_DELAY")) else None
    STOP_ON_UNEXPECTED_ERROR = os.getenv(
        "STOP_ON_UNEXPECTED_ERROR", STOP_ON_UNEXPECTED_ERROR
    )

    token = os.getenv("CLIENT_SECRET")
    assert token, "CLIENT_SECRET environment variable not set"

    masked_token = "*" * (len(token) + 4) + token[-4:]
    logger.info(f"{'aiondemand version:':25} {aiod.version}")
    logger.info(f"{'STOP_ON_UNEXPECTED_ERROR:':25} {STOP_ON_UNEXPECTED_ERROR}")
    logger.info(f"{'PER_DATASET_DELAY:':25} {PER_DATASET_DELAY}")
    logger.info(f"{'AI-on-Demand API server:':25} {aiod.config.api_server}")
    logger.info(f"{'Platform Name:':25} {PLATFORM_NAME}")
    logger.info(f"{'Authentication server:':25} {aiod.config.auth_server}")
    logger.info(f"{'Client ID:':25} {aiod.config.client_id}")
    logger.info(f"{'Using secret:':25} {masked_token}")

    # Configure AIoD token using client credentials
    set_token(Token(client_secret=token))

    # Try to call authorization_test, but don't die if it misbehaves.
    try:
        user = aiod.get_current_user()
    except Exception as e:
        logger.warning(
            "Could not verify AI-on-Demand connectivity via authorization_test (%s). "
            "Continuing with configured client credentials; if subsequent calls fail "
            "with 401/403, please check AIoD/Keycloak configuration.",
            e,
        )
    else:
        roles = getattr(user, "roles", []) or []
        required_role = f"platform_{PLATFORM_NAME}"
        if required_role not in roles:
            logger.warning(
                "Client roles %s do not include required %r role. "
                "Please make sure the `PLATFORM_NAME` environment variable is "
                "configured correctly, or contact your Keycloak administrator.",
                roles,
                required_role,
            )
        else:
            logger.info("Successfully authenticated and connected to AI-on-Demand.")


def get_newest_indexed_dataset() -> str:
    logger.info("Finding last uploaded OpenML dataset on AI-on-Demand")
    last_dataset = 0
    batch_size = 100
    for offset in range(0, 1_000_000, batch_size):
        openml_datasets = aiod.datasets.get_list(
            platform=PLATFORM_NAME,
            data_format="json",
            offset=offset,
            limit=batch_size,
        )
        if not openml_datasets:
            break
        last_dataset = max(
            int(d["platform_resource_identifier"]) for d in openml_datasets
        )
        logger.info(
            "Found dataset %s was already indexed on AI-on-Demand.",
            last_dataset,
        )
    logger.info(
        "Dataset %s is the last dataset indexed on AI-on-Demand.",
        last_dataset,
    )
    return str(last_dataset)


def main():
    args = parse_args()
    logging.basicConfig(level=args.root_log_level.upper())
    logger.setLevel(args.app_log_level.upper())
    configure_connector()

    errors = []

    match (args.mode, args.value):
        case Modes.ID, id_:
            if not id_.isdigit():
                logger.error(
                    f"Identifier specified should be an integer, is {id_!r}"
                )
                quit(1)
            dataset = fetch_openml_dataset(int(id_))
            upsert_dataset(dataset)
        case Modes.SINCE, id_:
            if id_ == "auto":
                id_ = get_newest_indexed_dataset()
            if not id_.isdigit():
                logger.error(
                    f"Identifier specified should be an integer, is {id_!r}"
                )
                quit(1)
            for dataset in list_datasets(from_=int(id_)):
                try:
                    upsert_dataset(dataset)
                    errors.append(None)
                except Exception as e:
                    logger.error(
                        f"Unrecoverable error upserting dataset {dataset}"
                    )
                    logger.exception(e)
                    errors.append(e)
                if len(errors) > 10:
                    errors.pop(0)
                    if sum(e is not None for e in errors) > 5:
                        logger.error(
                            "Quitting because we are encountering too many errors"
                        )
                        quit(1)
                if PER_DATASET_DELAY:
                    time.sleep(PER_DATASET_DELAY)
        case Modes.ALL, None:
            for dataset in list_datasets():
                upsert_dataset(dataset)
                if PER_DATASET_DELAY:
                    time.sleep(PER_DATASET_DELAY)
        case _:
            raise NotImplemented(
                f"Unexpected arguments: {args}"
            )


if __name__ == "__main__":
    main()