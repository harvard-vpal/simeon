"""
Utility functions for the CLI tool
"""
from argparse import ArgumentTypeError

from dateutil.parser import parse as dateparse


def parsed_date(datestr: str) -> str:
    """
    Function to parse the --start-date and --end-date
    options of simeon
    """
    try:
        return dateparse(datestr).strftime('%Y-%m-%d')
    except Exception:
        msg = '{d!r} could not be parsed into a proper date'
        raise ArgumentTypeError(msg.format(d=datestr))


def gcs_bucket(bucket: str) -> str:
    """
    Clean up a GCS bucket name if it does not start with the gs:// protocol
    """
    if not bucket.startswith('gs://'):
        return 'gs://{b}'.format(b=bucket)
    return bucket
