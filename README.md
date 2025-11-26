# AI-on-Demand OpenML connector

Collects dataset metadata from [OpenML](https://www.openml.org) and uploads it to AI-on-Demand.

This package is not intended to be used directly by others, but may serve as an example of how to build a connector for the AI-on-Demand platform. For more information on how to test this connector locally as a springboard for developing your own connector, reference the **Development** section below.

### TODO

This package is work in progress.

- [] Automatically publish to Docker Hub on release  
- [] Add tests  

## Installation

You can use the image directly from Docker Hub (TODO) or build it locally.

From Docker Hub:

```bash
docker pull aiondemand/openml-connector
````

To build a local image:

* Clone the repository:

  ```bash
  git clone https://github.com/aiondemand/openml-connector
  cd openml-connector
  ```

* Build the image:

  ```bash
  docker build -t aiondemand/openml-connector -f Dockerfile .
  ```

### Configuring Client Credentials

You will need to configure what server the connector should connect to, as well as the credentials for the client that allow you to upload data.

The connector requires a `config.toml` file with a valid[aiondemand configuration](https://aiondemand.github.io/), the default configuration can be found in the [`script/config.prod.toml`](script/config.prod.toml) file.
You will also need to have the *Client Secret* for the client, which can be obtained from the Keycloak administrator.
The client secret must be provided to the Docker container as an environment variable or in a dotenv file similar to
[`script/.local.env`](script/.local.env) but named e.g. `script/.test.env` or `script/.prod.env`, depending on the
environment you are targeting. The OpenML connector expects, at minimum:

* `CLIENT_SECRET` – the Keycloak client secret
* `PLATFORM_NAME=openml` – the platform name for assets registered by this connector
* Optional: `STOP_ON_UNEXPECTED_ERROR`, `PER_DATASET_DELAY`, etc.

Please contact the Keycloak service maintainer to obtain the credentials you need if you are in charge of deploying this OpenML connector.

## Running the Connector

You will need to mount the aiondemand configuration to `/home/appuser/.aiod/config.toml` and provide environment variables directly with `-e` or through mounting the dotfile in `/home/appuser/.aiod/openml/.env`.

The [`script/run.sh`](script/run.sh) script provides a convenience that automatically does this. It takes one positional
argument that has to be `local`, `test`, or `prod` to use the respective files in the `script` folder for configuration.

Any following arguments are interpreted as arguments to the main script.
For the latest command line arguments, use:

```bash
docker run aiondemand/openml-connector --help
```

Some example invocations that use the `script/run.sh` script:

* Sync one specific OpenML dataset and produce debug logs for the connector only:

  ```bash
  script/run.sh local --mode id --value 61 --app-log-level debug
  ```

* Sync all datasets with identifier `>= 100` (in ascending order), with debug logs for dependencies:

  ```bash
  script/run.sh test --mode since --value 100 --root-log-level debug
  ```

* Index **all** datasets on OpenML, producing info logs for the connector and all its dependencies (this is the default):

  ```bash
  script/run.sh prod --mode all --root-log-level info
  ```

## Development

You can test the connector when running the [metadata catalogue](https://github.com/aiondemand/AIOD-rest-api) locally.
The default configurations for this setup can be found in the [`script/.local.env`](script/.local.env) and
[`script/config.local.toml`](script/config.local.toml) files.

When connecting to the AI-on-Demand test or production server, you will need a dedicated client registered in the
Keycloak instance which is connected to the REST API you want to upload data to. See the AI-on-Demand documentation / form
to apply for a client. The client will need to have a `platform_X` role attached, where `X` is the name of the platform
from which you register assets (for this connector: `platform_openml`).

When a client is created, you will need its *Client ID* and *Client Secret* and update the relevant configuration and
environment files accordingly.

## Disclaimer

This project is not affiliated with OpenML in any way.