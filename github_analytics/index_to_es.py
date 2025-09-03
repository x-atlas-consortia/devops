import json
import logging
import sys
import time
from argparse import ArgumentParser
from configparser import ConfigParser
from tempfile import NamedTemporaryFile
from urllib.request import HTTPError, Request, urlopen

import boto3

# configure logging
current_time = time.strftime("%Y%m%d-%H%M%S")
file_handler = logging.FileHandler(filename=f"index_to_es_{current_time}.log")
stdout_handler = logging.StreamHandler(sys.stdout)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] {%(filename)s:%(lineno)d} %(levelname)s - %(message)s",
    handlers=[file_handler, stdout_handler],
)


logger = logging.getLogger()

try:
    parser = ArgumentParser(description="Upload GitHub repository analytics to AWS S3")
    parser.add_argument(
        "--config",
        default="config.ini",
        help="Path to the configuration file (ini format)",
    )
    args = parser.parse_args()

    config = ConfigParser()
    config.read(args.config)

    ES_BASE_URL = config.get("ElasticSearch", "ES_BASE_URL")

    AWS_S3_BUCKET = config.get("AWS", "AWS_S3_BUCKET")
    AWS_ACCESS_KEY = config.get("AWS", "AWS_ACCESS_KEY")
    AWS_SECRET_KEY = config.get("AWS", "AWS_SECRET_KEY")
except Exception:
    logger.exception("Error reading configuration from 'config.ini'")
    sys.exit(3)

s3 = boto3.resource("s3", aws_access_key_id=AWS_ACCESS_KEY, aws_secret_access_key=AWS_SECRET_KEY)
s3_bucket = s3.Bucket(AWS_S3_BUCKET)


def does_index_exist(idx_name: str) -> bool:
    """Check if an index exists in Elasticsearch/OpenSearch.

    Parameters
    ----------
    idx_name : str
        Name of the index.

    Returns
    -------
    bool
        True if index exists, False otherwise.
    """
    url = f"{ES_BASE_URL}/{idx_name}"
    req = Request(url, method="GET")
    try:
        with urlopen(req) as res:
            return res.status == 200
    except HTTPError as e:
        logger.error(f"Error checking index {idx_name}: {e}")
        return False


def create_index(idx_name: str) -> bool:
    """Create an index in Elasticsearch/OpenSearch.

    Parameters
    ----------
    idx_name : str
        Name of the index.

    Returns
    -------
    bool
        True if index is created, False otherwise.
    """
    url = f"{ES_BASE_URL}/{idx_name}"
    req = Request(url, method="PUT")
    try:
        with urlopen(req) as res:
            logger.info(f"{res.status}: {res.read()}")
            return res.status == 200
    except HTTPError as e:
        logger.error(f"Error creating index {idx_name}: {e}")
        return False


def index_data(idx_name: str, doc_id: str, data: dict) -> bool:
    """Create or update document in Elasticsearch/OpenSearch.

    Parameters
    ----------
    idx_name : str
        Name of the index.
    doc_id : str
        ID of the document.
    data : dict
        Data to be indexed.

    Returns
    -------
    bool
        True if document is indexed, False otherwise.
    """
    url = f"{ES_BASE_URL}/{idx_name}/_doc/{doc_id}"
    req = Request(url, method="PUT", data=json.dumps(data).encode("utf-8"))
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req) as res:
            logger.info(f"{res.status}: {res.read()}")
            return res.status == 200 or res.status == 201
    except HTTPError as e:
        logger.error(f"Error indexing data for {idx_name} with ID {doc_id}: {e}")
        return False


for file in s3_bucket.objects.all():
    filename = file.key
    repo_name, start_date, end_date = filename.removesuffix(".json").split("_")

    idx_name = f"analytics_{repo_name.replace('-', '_')}"
    doc_id = f"{start_date}_{end_date}"
    if not does_index_exist(idx_name):
        res = create_index(idx_name)
        if not res:
            logger.error(f"Failed to create index {idx_name} for {repo_name}")
            continue

    with NamedTemporaryFile(mode="w+b") as tmp:
        s3_bucket.download_fileobj(filename, tmp)
        tmp.seek(0)

        json_data = json.load(tmp)
        res = index_data(idx_name, doc_id, json_data)
        if not res:
            logger.error(f"Failed to index {repo_name} for {doc_id}")
