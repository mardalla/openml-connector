# openml-connector
Synchronizes metadata of OpenML assets with AI-on-Demand.
This project is work in progress.


## Usage
Simply invoke the script and specify which asset types to synchronize (default: all supported types) and whether you want to only register new assets or update modified existing assets (default: only add new assets). For example:

`python sync.py --type dataset --type model --mode add`

Note that updating existing assets, e.g., with `--mode all`, requires significantly more network requests and so will take much longer and put additional burden on the OpenML server.

It is going to support the following synchronization modes:

 - add: register only new assets, do not modify existing ones.
 - update: update existing assets, e.g., if their description changed.
 - remove: remove assets which no longer exist on the platform.
 - all: perform all currently supported synchronization modes for each asset type.

And it will be able to synchronize the following asset types:

 - dataset: from the OpenML dataset
 - model: from the OpenML flow
 - experiment: from the OpenML run

The package is work in progress, reference the table below (x is done, - is to do):

| Sync Mode | Dataset | Model | Experiment |
|-----------|---------|-------|------------|
| Add       |    -    |   -   |     -      |
| Update    |    -    |   -   |     -      |
| Remove    |    -    |   -   |     -      |


note: Always refer to `python main.py --help` for the most recent instructions.

## Installation
Invoke the following command in a dedicated virtual environment: `python -m pip install -r requirements.txt`

## Configuration
Configuration happens through environment variables, which may be defined in environment files.
We recommend using a file named `.env.override` for your non-default configurations, and use `.env` only as a reference to see which variables should be set.

