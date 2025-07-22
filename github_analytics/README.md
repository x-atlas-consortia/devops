# GitHub Analytics

This directory contains scripts used to index GitHub analytics to Elastic Search. The analytics are stored in AWS S3 before being uploaded to Elastic Search since GitHub only provides two weeks of analytics.

## Upload Analytics to AWS S3

The `upload_to_s3.py` script uploads GitHub clone and view analytics to an AWS S3 bucket given a list of GitHub repositories. The repository analytics are retrieved using the [GitHub REST API](https://docs.github.com/en/rest/metrics/traffic?apiVersion=2022-11-28) and uploaded to the S3 bucket using AWS's [boto3](https://boto3.amazonaws.com/v1/documentation/api/latest/index.html).

- [Clones](https://docs.github.com/en/rest/metrics/traffic?apiVersion=2022-11-28#get-repository-clones)
- [Referrers](https://docs.github.com/en/rest/metrics/traffic?apiVersion=2022-11-28#get-top-referral-sources)
- [Views](https://docs.github.com/en/rest/metrics/traffic?apiVersion=2022-11-28#get-page-views)

## Index Analytics to Elastic Search

The `index_to_es.py` script reads JSON files from an AWS S3 bucket and indexes the contents in Elasticsearch/OpenSearch.

## Setup

Both scripts require a `config.ini` file to run. An example configuration can be found in `config.ini.example`.

The `upload_to_s3.py` script requires a [GitHub <u>Classic</u> Personal Access Token](https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api?apiVersion=2022-11-28#authenticating-with-a-personal-access-token) with `repo:public_repo` scope access in order to authenticate. Note that the GitHub recommended fine-grained tokens might cause permission issues. The script also requires an AWS Access Key ID and Secret Access Key. See the [boto3 documentation](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/quickstart.html#configuration) for more details.

To setup the script, create and activate a Python 3 virtual environment. Install the dependencies using the `requirements.txt` file.

```bash
pip install -r requirements.txt
```
