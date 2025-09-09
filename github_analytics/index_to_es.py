import json
import logging
import os
import sys
import time
from argparse import ArgumentParser
from configparser import ConfigParser
from datetime import UTC, datetime, timedelta
from io import BytesIO
from typing import Optional

import boto3
import botocore
from requests import Session
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# configure logging
if not os.path.exists("logs"):
    os.makedirs("logs")
current_time = time.strftime("%Y%m%d-%H%M%S")
log_file = os.path.join("logs", f"index-to-es-{current_time}.log")
file_handler = logging.FileHandler(filename=log_file)
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

    AWS_BUCKET = config.get("AWS", "AWS_BUCKET")
    AWS_ACCESS_KEY_ID = config.get("AWS", "AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = config.get("AWS", "AWS_SECRET_ACCESS_KEY")

    ELASTIC_SEARCH_URL = config.get("ElasticSearch", "ELASTIC_SEARCH_URL")
    ELASTIC_SEARCH_INDEX = config.get("ElasticSearch", "ELASTIC_SEARCH_INDEX")

    SLACK_WEBHOOK_URL = config.get("Slack", "SLACK_WEBHOOK_URL", fallback=None)
except Exception:
    logger.exception("Error reading configuration from 'config.ini'")
    sys.exit(3)


class AWSS3Manager:
    def __init__(self, bucket_name: str, access_key: str, secret_key: str):
        s3 = boto3.resource(
            "s3",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
        )
        self._bucket = s3.Bucket(bucket_name)

    def get_last_index_time(self) -> datetime:
        filename = "last_index_time.json"
        try:
            obj = self._bucket.Object(filename)
            res = obj.get()
            data = json.loads(res["Body"].read())
            timestamp = data.get("timestamp")
            if not timestamp:
                return datetime.fromtimestamp(0, UTC)

            return datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ")
        except botocore.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "NoSuchKey":
                return datetime.fromtimestamp(0, UTC)
            else:
                raise e
        except Exception as e:
            logger.error(f"Could not retrieve {filename} from S3: {e}")
            raise e

    def save_last_index_time(self, timestamp: datetime):
        filename = "last_index_time.json"
        data = {"timestamp": timestamp.strftime("%Y-%m-%dT%H:%M:%SZ")}
        json_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
        byteio = BytesIO(json_bytes)
        self._bucket.upload_fileobj(byteio, filename)

    def get_logs(self, month: str) -> list[dict]:
        filename = f"github_analytics_{month}.json"
        try:
            obj = self._bucket.Object(filename)
            response = obj.get()
            data = json.loads(response["Body"].read())
            return data
        except botocore.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "NoSuchKey":
                return []
            else:
                raise e
        except Exception as e:
            logger.error(f"Could not retrieve {filename} from S3: {e}")
            raise e


def bulk_update_logs(logs: list[dict], session: Session) -> Optional[list[str]]:
    upserts = [
        f'{{"update":{{"_id":"{doc["dataset_uuid"]}/{doc["rel_path"]}"}}}}\n{{"doc":{json.dumps(doc, separators=(",", ":"))},"doc_as_upsert":true}}'
        for doc in logs
    ]

    # split upserts into chunks to avoid exceeding the request size limit. 100 is arbitrary
    error_msgs = []
    chunk_size = 100
    chunks = [upserts[i : i + chunk_size] for i in range(0, len(upserts), chunk_size)]
    for chunk in chunks:
        body = "\n".join(chunk) + "\n"
        url = f"{ELASTIC_SEARCH_URL}/{ELASTIC_SEARCH_INDEX}/_bulk"
        res = session.post(
            url,
            headers={"Content-Type": "application/x-ndjson"},
            data=body,
            timeout=60,
        )
        if res.status_code != 200:
            raise Exception(
                f"Error indexing logs in {ELASTIC_SEARCH_INDEX}: {res.status_code}, {res.text}"
            )

        res_body = res.json().get("items", [])
        result_values = [item.get("update") for item in res_body if "update" in item]
        msgs = [
            f"{item['_id']}: Update - {item.get('error', {}).get('reason')}"
            for item in result_values
            if item["status"] not in [200, 201]
        ]
        if msgs:
            error_msgs.extend(msgs)

    return error_msgs if error_msgs else None


def convert_time_to_month(timestamp: datetime) -> str:
    return timestamp.strftime("%Y-%m")


def months_between_datetimes(start: datetime, end: datetime) -> set[str]:
    months = set()
    current = start
    while current <= end:
        months.add(current.strftime("%Y-%m"))
        current = current + timedelta(days=1)
    return months


def main():
    session = Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[408, 429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)

    s3_manager = AWSS3Manager(
        bucket_name=AWS_BUCKET,
        access_key=AWS_ACCESS_KEY_ID,
        secret_key=AWS_SECRET_ACCESS_KEY,
    )

    current_time = datetime.now(UTC)
    has_error = False

    try:
        last_index_time = s3_manager.get_last_index_time()
    except Exception as e:
        logger.error(f"Failed to get last index time: {e}")
        sys.exit(3)

    months_to_index = months_between_datetimes(last_index_time, current_time)
    for month in months_to_index:
        try:
            logs = s3_manager.get_logs(month)
            bulk_errors = bulk_update_logs(logs=logs, session=session)
            if bulk_errors:
                has_error = True
                for err in bulk_errors:
                    logger.error(err)
            else:
                logger.info(f"Successfully indexed logs for month {month}")
        except Exception as e:
            logger.error(f"Failed to index logs for month {month}: {e}")

    if has_error is False:
        try:
            s3_manager.save_last_index_time(current_time)
        except Exception as e:
            logger.error(f"Failed to save last index time: {e}")
            has_error = True

    if has_error and SLACK_WEBHOOK_URL:
        # send Slack notification
        try:
            message = {
                "text": (
                    "GitHub Analytics: Index to Elastic Search process completed with errors. "
                    "Check the logs for details."
                )
            }
            res = session.post(SLACK_WEBHOOK_URL, json=message, timeout=30)
            if res.status_code != 200:
                logger.error(f"Failed to send Slack notification: {res.status_code} {res.text}")
        except Exception as e:
            logger.error(f"Failed to send Slack notification: {e}")


if __name__ == "__main__":
    main()
