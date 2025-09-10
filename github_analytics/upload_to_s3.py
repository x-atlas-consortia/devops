import json
import logging
import os
import sys
import time
from argparse import ArgumentParser
from configparser import ConfigParser
from dataclasses import asdict, dataclass
from io import BytesIO
from typing import Literal

import boto3
import botocore
from requests import Session
from requests.adapters import HTTPAdapter
from requests.exceptions import HTTPError
from urllib3.util.retry import Retry


@dataclass
class LogItem:
    owner: str
    repository: str
    type: Literal["clone", "view"]
    timestamp: str  # ISO 8601 format
    count: int
    uniques: int

    def __eq__(self, other):
        return (
            self.owner == other.owner
            and self.repository == other.repository
            and self.type == other.type
            and self.timestamp == other.timestamp
        )

    def __hash__(self):
        return hash((self.owner, self.repository, self.type, self.timestamp))


# configure logging
if not os.path.exists("logs"):
    os.makedirs("logs")
current_time = time.strftime("%Y%m%d-%H%M%S")
log_file = os.path.join("logs", f"upload-to-s3-{current_time}.log")
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

    GITHUB_REPOS = [r.strip() for r in config.get("GitHub", "GITHUB_REPOS").split(",")]
    GITHUB_TOKEN = config.get("GitHub", "GITHUB_TOKEN")

    AWS_BUCKET = config.get("AWS", "AWS_BUCKET")
    AWS_ACCESS_KEY_ID = config.get("AWS", "AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = config.get("AWS", "AWS_SECRET_ACCESS_KEY")

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

    def get_logs(self, month: str) -> set[LogItem]:
        filename = f"github_analytics_{month}.json"
        try:
            obj = self._bucket.Object(filename)
            response = obj.get()
            data = json.loads(response["Body"].read())
            return set(LogItem(**item) for item in data)
        except botocore.exceptions.ClientError as e:
            error_code = e.response.get("Error", {}).get("Code")
            if error_code == "NoSuchKey":
                return set()
            else:
                raise e
        except Exception as e:
            logger.error(f"Could not retrieve {filename} from S3: {e}")
            raise e

    def upload_logs(self, month: str, log_set: set[LogItem]):
        filename = f"github_analytics_{month}.json"
        data = sorted(
            [asdict(log) for log in log_set],
            key=lambda x: (x["timestamp"], x["repository"], x["type"]),
        )
        json_bytes = json.dumps(data, separators=(",", ":")).encode("utf-8")
        byteio = BytesIO(json_bytes)
        self._bucket.upload_fileobj(byteio, filename)


headers = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def split_repo_name(repo: str) -> tuple[str, str]:
    return tuple(repo.split("/"))


def get_github_data(
    repo: str,
    data_type: Literal["clone", "view"],
    session: Session,
) -> list[LogItem]:
    name = f"{data_type}s"
    url = f"https://api.github.com/repos/{repo}/traffic/{name}?per=day"
    res = session.get(url, headers=headers, timeout=30)
    if res.status_code != 200:
        raise Exception(f"GitHub API request failed with status {res.status_code}: {res.text}")
    data = res.json()
    owner, repo_name = split_repo_name(repo)
    return [
        LogItem(owner=owner, repository=repo_name, type=data_type, **item)
        for item in data.get(name, [])
    ]


def convert_time_to_month(timestamp: str) -> str:
    # "2024-01-17T00:00:00Z" -> "2024-01"
    parts = timestamp.split("-")
    return "-".join(parts[:2])


def main():
    start_time = time.time()

    session = Session()
    retries = Retry(total=3, backoff_factor=1, status_forcelist=[408, 429, 500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)

    s3_manager = AWSS3Manager(
        bucket_name=AWS_BUCKET,
        access_key=AWS_ACCESS_KEY_ID,
        secret_key=AWS_SECRET_ACCESS_KEY,
    )

    has_error = False
    current_log_map = dict()

    for repo in GITHUB_REPOS:
        try:
            # get clone data from GitHub API
            clone_logs = get_github_data(repo, "clone", session)

            # get views data from GitHub API
            view_logs = get_github_data(repo, "view", session)

            # get the unique months and make sure the current s3 logs are loaded for them
            months = {convert_time_to_month(log.timestamp) for log in clone_logs + view_logs}
            for month in months:
                if month not in current_log_map:
                    current_log_map[month] = s3_manager.get_logs(month)

            # merge the logs
            for log in clone_logs + view_logs:
                month = convert_time_to_month(log.timestamp)
                if log in current_log_map[month]:
                    # replace existing log
                    current_log_map[month].remove(log)
                    current_log_map[month].add(log)
                    logger.info(
                        f"Updated log for {log.owner}/{log.repository} {log.type} {log.timestamp}"
                    )
                else:
                    current_log_map[month].add(log)
                    logger.info(
                        f"Added log for {log.owner}/{log.repository} {log.type} {log.timestamp}"
                    )

        except HTTPError as e:
            logger.error(f"Failed to get analytics for {repo}: {e.code} {e.reason}")
            has_error = True
        except Exception as e:
            logger.error(f"Failed to upload analytics for {repo}: {e}")
            has_error = True

    if has_error is False:
        # upload data to S3
        for month, log_set in current_log_map.items():
            try:
                s3_manager.upload_logs(month, log_set)
            except Exception as e:
                logger.error(f"Failed to upload logs for {month}: {e}")

    if has_error and SLACK_WEBHOOK_URL:
        # send Slack notification
        try:
            message = {
                "text": (
                    "GitHub Analytics: Upload to S3 process completed with errors. "
                    "Check the logs for details."
                )
            }
            res = session.post(SLACK_WEBHOOK_URL, json=message, timeout=30)
            if res.status_code != 200:
                logger.error(f"Failed to send Slack notification: {res.status_code} {res.text}")
        except Exception as e:
            logger.error(f"Failed to send Slack notification: {e}")

    logger.info(f"Process completed in {time.time() - start_time:.2f} seconds")


if __name__ == "__main__":
    main()
