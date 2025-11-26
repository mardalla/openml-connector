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
from requests.exceptions import JSONDecodeError


class Modes(StrEnum):
    ALL = auto()
    SINCE = auto()
    ID = auto()


class AssetType(StrEnum):
    DATASET = "dataset"
    MODEL = "model"  

logger = logging.getLogger(__name__)
REQUEST_TIMEOUT = 10
MAX_TEXT = 65535
MAX_DESCRIPTION_LENGTH = 1800
PLATFORM_NAME = "openml"
STOP_ON_UNEXPECTED_ERROR: bool = False
PER_DATASET_DELAY: float | None = None 

class ParsingError(Exception):
    pass


class ServerError(Exception):
    pass



def list_datasets(from_: int | None = None):
    """Yield full OpenML dataset dicts starting from a given OpenML id."""

    def paginate_all_datasets(items_per_page: int = 50):
        url_data = (
            "https://www.openml.org/api/v1/json/data/list/"
            "limit/{items_per_page}/offset/{offset}"
        )

        for offset in range(0, 1_000_000, items_per_page):
            response = requests.get(
                url_data.format(items_per_page=items_per_page, offset=offset),
                timeout=REQUEST_TIMEOUT,
            )
            if not response.ok:
                status_code = response.status_code
                try:
                    msg = response.json()["error"]["message"]
                except Exception as e:
                    logger.error("Error while paginating datasets, cannot continue.")
                    logger.exception(e)
                    msg = response.content
                err_msg = (
                    f"Error while fetching {url_data} from OpenML: "
                    f"({status_code}) {msg}"
                )
                raise ServerError(err_msg)

            logger.debug(f"Paging through datasets (offset {offset})")

            try:
                data = response.json()
                dataset_summaries = data["data"]["dataset"]
                if dataset_summaries:
                    yield from dataset_summaries
                    logger.debug(
                        "Paged through datasets (batch size %d)",
                        len(dataset_summaries),
                    )
                else:
                    break
            except Exception:
                raise ParsingError(
                    f"Could not parse dataset list response "
                    f"({response.status_code}): {response.content}"
                )

    from_ = from_ or 0
    for dataset in paginate_all_datasets():
        try:
            identifier = int(dataset["did"])
            if identifier < from_:
                continue
        except KeyError:
            logger.error("Received invalid dataset summary: %s", dataset, exc_info=True)
            continue

        try:
            yield fetch_openml_dataset(identifier, dataset.get("quality"))
        except Exception as e:
            logger.error("Exception when processing dataset %s", identifier)
            logger.exception(e)


def fetch_openml_dataset(identifier_: int, qualities: dict | None = None) -> dict:
    """Fetch a *single* dataset (metadata + qualities) from OpenML."""

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
        try:
            qualities = qualities_response.json()["data_qualities"]["quality"]
        except Exception:
            raise ParsingError(
                f"Error parsing JSON of dataset qualities: "
                f"{qualities_response.content}"
            )

    url_data = f"https://www.openml.org/api/v1/json/data/{identifier_}"
    response = requests.get(url_data, timeout=REQUEST_TIMEOUT)

    if not response.ok:
        status_code = response.status_code
        try:
            msg = response.json()["error"]["message"]
        except Exception:
            msg = response.content
        err_msg = (
            f"Error while fetching {url_data} from OpenML: "
            f"({status_code}) {msg}"
        )
        raise ServerError(err_msg)

    try:
        dataset_json = response.json()["data_set_description"]
        qualities_json = {quality["name"]: quality["value"] for quality in qualities}
        return dataset_json | {"qualities": qualities_json}
    except Exception:
        raise ParsingError(
            f"Error parsing JSON of dataset {identifier_}: {response.content}"
        )


def _convert_dataset_to_aiod(dataset: dict) -> dict:
    """Map OpenML dataset JSON to AIoD dataset metadata."""
    identifier = dataset["id"]

    description = dataset.get("description", "")
    if isinstance(description, list) and len(description) == 0:
        description = ""
    if not isinstance(description, str):
        logger.warning("Ignoring non-string description for dataset %s: %r", identifier, description)
        description = ""
    if len(description) > MAX_DESCRIPTION_LENGTH:
        text_break = " [...]"
        description = description[: MAX_DESCRIPTION_LENGTH - len(text_break)] + text_break

    size = None
    if (n_rows := dataset.get("qualities", {}).get("NumberOfInstances")):
        try:
            size = {
                "unit": "instances",
                "value": int(float(n_rows)),  # OpenML stores e.g. "123.0"
            }
        except Exception:
            logger.warning(
                "Could not parse NumberOfInstances %r for dataset %s", n_rows, identifier
            )

    keyword = dataset.get("tag", [])
    if isinstance(keyword, str):
        keyword = [keyword]

    return dict(
        platform=PLATFORM_NAME,
        platform_resource_identifier=str(identifier),
        name=dataset["name"],
        version=dataset.get("version"),
        same_as=f"https://www.openml.org/d/{identifier}",
        description=dict(plain=description),
        date_published=dateutil.parser.parse(dataset["upload_date"]).isoformat()
        if dataset.get("upload_date")
        else None,
        license=dataset.get("licence") or dataset.get("license"),
        distribution=[
            dict(
                content_url=dataset.get("url") or f"https://www.openml.org/d/{identifier}",
                encoding_format=dataset.get("format") or "text/html",
            )
        ],
        is_accessible_for_free=True,
        keyword=keyword,
        size=size,
    )


def upsert_dataset(dataset: dict) -> int:
    """Create / update dataset asset in AIoD."""

    identifier = str(dataset["id"])
    try:
        local_dataset = _convert_dataset_to_aiod(dataset)

        try:
            aiod_dataset = aiod.datasets.get_asset_from_platform(
                platform=PLATFORM_NAME,
                platform_identifier=identifier,
                data_format="json",
            )
        except (KeyError, JSONDecodeError):
            aiod_dataset = None

        if not aiod_dataset:
            response = aiod.datasets.register(metadata=local_dataset)
            if isinstance(response, str):
                logger.debug("Indexed dataset %s: %s", identifier, response)
                return HTTPStatus.OK
            elif isinstance(response, requests.Response):
                logger.warning(
                    "Error uploading dataset %s (%s): %s",
                    identifier,
                    response.status_code,
                    response.content,
                )
                return response.status_code
            raise RuntimeError(
                f"Unexpected response when registering dataset {identifier}: {response!r}"
            )

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
                "Updated dataset %s: %s", identifier, aiod_dataset["identifier"]
            )
        else:
            logger.warning(
                "Could not update %s for openml dataset %s (%s): %s",
                aiod_dataset["identifier"],
                identifier,
                response.status_code,
                response.content,
            )
    except Exception as e:
        logger.exception(
            "Exception encountered when upserting dataset %s.", identifier, exc_info=e
        )
        if STOP_ON_UNEXPECTED_ERROR:
            raise
        return -1
    return response.status_code



def list_models(from_: int | None = None):
    """Yield full OpenML flow dicts (models) starting from a given OpenML id."""

    def paginate_all_flows(items_per_page: int = 50):
        url_flows = (
            "https://www.openml.org/api/v1/json/flow/list/"
            "limit/{items_per_page}/offset/{offset}"
        )

        for offset in range(0, 1_000_000, items_per_page):
            response = requests.get(
                url_flows.format(items_per_page=items_per_page, offset=offset),
                timeout=REQUEST_TIMEOUT,
            )
            if not response.ok:
                status_code = response.status_code
                try:
                    msg = response.json()["error"]["message"]
                except Exception as e:
                    logger.error("Error while paginating flows, cannot continue.")
                    logger.exception(e)
                    msg = response.content
                err_msg = (
                    f"Error while fetching {url_flows} from OpenML: "
                    f"({status_code}) {msg}"
                )
                raise ServerError(err_msg)

            logger.debug(f"Paging through flows (offset {offset})")

            try:
                data = response.json()
                flows = None
                if "flows" in data and isinstance(data["flows"], dict):
                    flows = data["flows"].get("flow")
                elif "flow" in data:
                    flows = data["flow"]

                if not flows:
                    break

                if isinstance(flows, dict):
                    flows = [flows]

                yield from flows
            except Exception:
                raise ParsingError(
                    f"Could not parse flow list response "
                    f"({response.status_code}): {response.content}"
                )

    from_ = from_ or 0
    for flow in paginate_all_flows():
        try:
            raw_id = flow.get("flow_id") or flow.get("id")
            identifier = int(raw_id)
            if identifier < from_:
                continue
        except Exception:
            logger.error("Received invalid flow summary: %s", flow, exc_info=True)
            continue

        try:
            yield fetch_openml_model(identifier)
        except Exception as e:
            logger.error("Exception when processing flow %s", identifier)
            logger.exception(e)


def fetch_openml_model(identifier_: int) -> dict:
    """Fetch a *single* flow (model) from OpenML."""
    url_flow = f"https://www.openml.org/api/v1/json/flow/{identifier_}"
    response = requests.get(url_flow, timeout=REQUEST_TIMEOUT)

    if not response.ok:
        status_code = response.status_code
        try:
            msg = response.json()["error"]["message"]
        except Exception:
            msg = response.content
        err_msg = (
            f"Error while fetching {url_flow} from OpenML: "
            f"({status_code}) {msg}"
        )
        raise ServerError(err_msg)

    try:
        data = response.json()
        flow = data.get("flow")
        if isinstance(flow, list):
            if not flow:
                raise ParsingError(
                    f"Empty flow list for id {identifier_}: {response.content}"
                )
            flow = flow[0]
        if not isinstance(flow, dict):
            raise ParsingError(
                f"Unexpected flow structure for id {identifier_}: {data}"
            )
        return flow
    except Exception:
        raise ParsingError(
            f"Error parsing JSON of flow {identifier_}: {response.content}"
        )


def _convert_model_to_aiod(flow: dict) -> dict:
    """Map OpenML flow JSON to AIoD ML Model metadata."""

    raw_id = flow.get("flow_id") or flow.get("id")
    identifier = str(raw_id)

    name = (
        flow.get("name")
        or flow.get("full_name")
        or f"OpenML flow {identifier}"
    )

    description = flow.get("description") or ""
    if not isinstance(description, str):
        logger.warning(
            "Ignoring non-string description for flow %s: %r", identifier, description
        )
        description = ""
    if len(description) > MAX_DESCRIPTION_LENGTH:
        text_break = " [...]"
        description = description[: MAX_DESCRIPTION_LENGTH - len(text_break)] + text_break

    version = flow.get("external_version") or flow.get("version")

    keyword = flow.get("tag", [])
    if isinstance(keyword, str):
        keyword = [keyword]

    upload_date_raw = flow.get("upload_date")
    if upload_date_raw:
        try:
            date_published = dateutil.parser.parse(upload_date_raw).isoformat()
        except Exception:
            logger.warning(
                "Could not parse upload_date %r for flow %s",
                upload_date_raw,
                identifier,
            )
            date_published = None
    else:
        date_published = None

    openml_url = f"https://www.openml.org/f/{identifier}"

    return dict(
        platform=PLATFORM_NAME,
        platform_resource_identifier=identifier,
        name=name,
        version=version,
        same_as=openml_url,
        description=dict(plain=description),
        date_published=date_published,
        license=flow.get("licence") or flow.get("license"),
        is_accessible_for_free=True,
        distribution=[
            dict(
                content_url=openml_url,
                encoding_format="text/html",
            )
        ],
        keyword=keyword,
    )


def upsert_model(flow: dict) -> int:
    """Create / update ML Model asset in AIoD based on an OpenML flow."""

    raw_id = flow.get("flow_id") or flow.get("id")
    identifier = str(raw_id)

    try:
        local_model = _convert_model_to_aiod(flow)

        try:
            aiod_model = aiod.ml_models.get_asset_from_platform(
                platform=PLATFORM_NAME,
                platform_identifier=identifier,
                data_format="json",
            )
        except (KeyError, JSONDecodeError):
            aiod_model = None

        if not aiod_model:
            response = aiod.ml_models.register(metadata=local_model)
            if isinstance(response, str):
                logger.debug("Indexed model %s: %s", identifier, response)
                return HTTPStatus.OK
            elif isinstance(response, requests.Response):
                logger.warning(
                    "Error uploading model %s (%s): %s",
                    identifier,
                    response.status_code,
                    response.content,
                )
                return response.status_code
            raise RuntimeError(
                f"Unexpected response when registering model {identifier}: {response!r}"
            )

        if "identifier" not in aiod_model:
            raise RuntimeError(
                "Unexpected server response retrieving OpenML model "
                f"{identifier} from AI-on-Demand: {aiod_model}"
            )

        response = aiod.ml_models.replace(
            identifier=aiod_model["identifier"],
            metadata=local_model,
        )
        if response.status_code == HTTPStatus.OK:
            logger.debug(
                "Updated model %s: %s", identifier, aiod_model["identifier"]
            )
        else:
            logger.warning(
                "Could not update %s for openml model %s (%s): %s",
                aiod_model["identifier"],
                identifier,
                response.status_code,
                response.content,
            )
    except Exception as e:
        logger.exception(
            "Exception encountered when upserting model %s.", identifier, exc_info=e
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
            "For mode 'ID' this must be an OpenML identifier. "
            "For mode 'SINCE' this must be an OpenML identifier, "
            "this resource and all resources with higher identifier "
            "will be indexed. It can also be set to 'auto', in which "
            "case the last inserted asset on AI-on-Demand will be "
            "determined and only newer assets are indexed. "
            "Cannot be set with mode 'ALL'."
        ),
    )
    parser.add_argument(
        "--asset-type",
        choices=[t.value for t in AssetType],
        default=AssetType.DATASET.value,
        help="What to index: 'dataset' (OpenML data) or 'model' (OpenML flows).",
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
        help="Emit all log messages generated of at least this level by dependencies.",
    )
    args = parser.parse_args()
    if args.mode == Modes.ALL and args.value:
        logger.error("Cannot run mode 'ALL' when a value is supplied.")
        quit(code=1)

    return args


def configure_connector():
    global BATCH_SIZE, PLATFORM_NAME, STOP_ON_UNEXPECTED_ERROR, PER_DATASET_DELAY

    dot_file = Path("~/.aiod/openml/.env").expanduser()
    if dot_file.exists() and load_dotenv(dot_file):
        logger.info("Loaded variables from %s", dot_file)
    else:
        reason = "file does not exist" if not dot_file.exists() else "unknown reason"
        logger.info("No environment variables loaded from %s: %s.", dot_file, reason)

    try:
        BATCH_SIZE = int(os.getenv("AIOD_BATCH_SIZE", "25"))
    except ValueError:
        BATCH_SIZE = 25

    PLATFORM_NAME = os.getenv("PLATFORM_NAME", PLATFORM_NAME)

    delay = os.getenv("PER_DATASET_DELAY")
    PER_DATASET_DELAY = float(delay) if delay else None

    soe_raw = os.getenv("STOP_ON_UNEXPECTED_ERROR", str(STOP_ON_UNEXPECTED_ERROR))
    STOP_ON_UNEXPECTED_ERROR = str(soe_raw).lower() in {"1", "true", "yes", "y"}

    token = os.getenv("CLIENT_SECRET")
    assert token, "CLIENT_SECRET environment variable not set"

    masked_token = "*" * max(4, len(token) - 4) + token[-4:]
    logger.info("%-25s %s", "aiondemand version:", aiod.version)
    logger.info("%-25s %s", "STOP_ON_UNEXPECTED_ERROR:", STOP_ON_UNEXPECTED_ERROR)
    logger.info("%-25s %s", "PER_DATASET_DELAY:", PER_DATASET_DELAY)
    logger.info("%-25s %s", "AI-on-Demand API server:", aiod.config.api_server)
    logger.info("%-25s %s", "Platform Name:", PLATFORM_NAME)
    logger.info("%-25s %s", "Authentication server:", aiod.config.auth_server)
    logger.info("%-25s %s", "Client ID:", aiod.config.client_id)
    logger.info("%-25s %s", "Using secret:", masked_token)

    set_token(Token(client_secret=token))
    
    logger.info(
        "Configured AI-on-Demand client token (skipping authorization_test for OpenML connector)."
    )


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
            "Found dataset %s already indexed on AI-on-Demand.", last_dataset
        )
    logger.info(
        "Dataset %s is the last dataset indexed on AI-on-Demand.", last_dataset
    )
    return str(last_dataset)


def get_newest_indexed_model() -> str:
    logger.info("Finding last uploaded OpenML model (flow) on AI-on-Demand")
    last_model = 0
    batch_size = 100
    for offset in range(0, 1_000_000, batch_size):
        openml_models = aiod.ml_models.get_list(
            platform=PLATFORM_NAME,
            data_format="json",
            offset=offset,
            limit=batch_size,
        )
        if not openml_models:
            break
        last_model = max(
            int(m["platform_resource_identifier"]) for m in openml_models
        )
        logger.info("Found model %s already indexed on AI-on-Demand.", last_model)
    logger.info(
        "Model %s is the last model indexed on AI-on-Demand.", last_model
    )
    return str(last_model)



def main():
    args = parse_args()
    logging.basicConfig(level=args.root_log_level.upper())
    logger.setLevel(args.app_log_level.upper())
    configure_connector()

    errors: list[Exception | None] = []
    asset_type = AssetType(args.asset_type)

    def handle_iterable(iterable, upsert_func):
        nonlocal errors
        for obj in iterable:
            try:
                upsert_func(obj)
                errors.append(None)
            except Exception as e:
                logger.error("Unrecoverable error upserting object: %r", obj)
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

    match (asset_type, args.mode, args.value):
        
        case AssetType.DATASET, Modes.ID, id_:
            if not id_.isdigit():
                logger.error("Identifier specified should be an integer, is %r", id_)
                quit(1)
            dataset = fetch_openml_dataset(int(id_))
            upsert_dataset(dataset)

        case AssetType.DATASET, Modes.SINCE, id_:
            if id_ == "auto":
                id_ = get_newest_indexed_dataset()
            if not id_.isdigit():
                logger.error("Identifier specified should be an integer, is %r", id_)
                quit(1)
            handle_iterable(
                list_datasets(from_=int(id_)),
                upsert_dataset,
            )

        case AssetType.DATASET, Modes.ALL, None:
            handle_iterable(
                list_datasets(),
                upsert_dataset,
            )
        
        case AssetType.MODEL, Modes.ID, id_:
            if not id_.isdigit():
                logger.error("Identifier specified should be an integer, is %r", id_)
                quit(1)
            flow = fetch_openml_model(int(id_))
            upsert_model(flow)

        case AssetType.MODEL, Modes.SINCE, id_:
            if id_ == "auto":
                id_ = get_newest_indexed_model()
            if not id_.isdigit():
                logger.error("Identifier specified should be an integer, is %r", id_)
                quit(1)
            handle_iterable(
                list_models(from_=int(id_)),
                upsert_model,
            )

        case AssetType.MODEL, Modes.ALL, None:
            handle_iterable(
                list_models(),
                upsert_model,
            )

        case _:
            raise NotImplementedError(f"Unexpected arguments: {args}")


if __name__ == "__main__":
    main()