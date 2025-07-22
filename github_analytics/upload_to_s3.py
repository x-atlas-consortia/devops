import json
import logging
import sys
import time
from configparser import ConfigParser
from datetime import datetime, timedelta, timezone
from io import BytesIO
from urllib.request import HTTPError, Request, urlopen

import boto3

# configure logging
current_time = time.strftime("%Y%m%d-%H%M%S")
file_handler = logging.FileHandler(filename=f"upload_to_s3_{current_time}.log")
stdout_handler = logging.StreamHandler(sys.stdout)
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] {%(filename)s:%(lineno)d} %(levelname)s - %(message)s",
    handlers=[file_handler, stdout_handler],
)


logger = logging.getLogger()

try:
    config = ConfigParser()
    config.read("./config.ini")

    GITHUB_REPOS = [r.strip() for r in config.get("GitHub", "GITHUB_REPOS").split(",")]
    GITHUB_TOKEN = config.get("GitHub", "GITHUB_TOKEN")

    AWS_S3_BUCKET = config.get("AWS", "AWS_S3_BUCKET")
    AWS_ACCESS_KEY = config.get("AWS", "AWS_ACCESS_KEY")
    AWS_SECRET_KEY = config.get("AWS", "AWS_SECRET_KEY")
except Exception:
    logger.exception("Error reading configuration from 'config.ini'")
    sys.exit(3)


s3 = boto3.resource("s3", aws_access_key_id=AWS_ACCESS_KEY, aws_secret_access_key=AWS_SECRET_KEY)
s3_bucket = s3.Bucket(AWS_S3_BUCKET)

headers = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
end_date = today - timedelta(days=1)
start_date = end_date - timedelta(days=13)
end_date_str = end_date.strftime("%Y-%m-%d")
start_date_str = start_date.strftime("%Y-%m-%d")

for repo in GITHUB_REPOS:
    try:
        # get clone data from GitHub API
        url = f"https://api.github.com/repos/{repo}/traffic/clones?per=day"
        req = Request(url, method="GET", headers=headers)
        with urlopen(req) as res:
            b_data = res.read()
        clone_data = json.loads(b_data)

        # don't include today's date since it may be incomplete
        clone_data["clones"] = [
            d
            for d in clone_data["clones"]
            if datetime.strptime(d["timestamp"], "%Y-%m-%dT%H:%M:%SZ") < today
        ]

        # get views data from GitHub API
        url = f"https://api.github.com/repos/{repo}/traffic/views?per=day"
        req = Request(url, method="GET", headers=headers)
        with urlopen(req) as res:
            b_data = res.read()
        view_data = json.loads(b_data)

        # don't include today's date since it may be incomplete
        view_data["views"] = [
            d
            for d in view_data["views"]
            if datetime.strptime(d["timestamp"], "%Y-%m-%dT%H:%M:%SZ") < today
        ]

        # get referrer data from GitHub API
        url = f"https://api.github.com/repos/{repo}/traffic/popular/referrers?per=day"
        req = Request(url, method="GET", headers=headers)
        with urlopen(req) as res:
            b_data = res.read()
        referrer_data = json.loads(b_data)

        # combine clone and view data
        data = {"clones": clone_data, "views": view_data, "referrers": referrer_data}
        filename = f"{repo.replace('/', '-')}_{start_date_str}_{end_date_str}.json"
        b_data = json.dumps(data, separators=(",", ":")).encode("utf-8")

        # upload data to S3
        byteio = BytesIO(b_data)
        s3_bucket.upload_fileobj(byteio, filename)
        logger.info(f"Uploaded analytics for {repo} to {filename}")
    except HTTPError as e:
        logger.error(f"Failed to get analytics for {repo}: {e.code} {e.reason}")
    except Exception as e:
        logger.error(f"Failed to upload analytics for {repo}: {e}")
