"""
simeon is a command line tool that helps with processing edx data
"""
import os
import sys
import traceback
from argparse import ArgumentParser

from simeon.download import aws, logs
from simeon.exceptions import AWSException
from simeon.scripts import utilities as cli_utils
from simeon.upload import gcp


def wait_for_bq_jobs(job_list):
    """
    Given a list of BigQuery data load jobs,
    wait for them all to finish.

    :type job_list: Iterable[LoadJob]
    :param job_list: An Iterable of LoadJob objects from the bigquery package
    :rtype: None
    :return: Nothing
    :TODO: Improve this function to behave a little less like a tight loop
    """
    done = 0
    while done < len(job_list):
        for job in job_list:
            state = job.done()
            if not state:
                job.reload()
            done += state


def list_files(parsed_args):
    """
    Using the Namespace object generated by argparse, list the files
    that match the given criteria
    """
    parsed_args.year = parsed_args.begin_date[:4]
    parsed_args.verbose = not parsed_args.quiet
    info = aws.BUCKETS.get(parsed_args.file_type)
    info['Prefix'] = info['Prefix'].format(
        site=parsed_args.site, year=parsed_args.year,
        date=parsed_args.begin_date, org=parsed_args.org
    )
    bucket = aws.make_s3_bucket(info['Bucket'])
    try:
        blobs = aws.S3Blob.from_prefix(
            bucket=bucket, prefix=info['Prefix']
        )
    except AWSException as excp:
        errmsg = 'Failed to list files: {e}'.format(e=excp)
        print(errmsg, file=sys.stderr)
        sys.exit(1)
    for blob in blobs:
        fdate = aws.get_file_date(blob.name)
        if parsed_args.begin_date <= fdate <= parsed_args.end_date:
            if parsed_args.json:
                print(blob.to_json())
            else:
                print(blob)


def split_log_files(parsed_args):
    """
    Using the Namespace object generated by argparse, parse the given
    tracking log files and put them in the provider destination directory
    """
    failed = False
    for fname in parsed_args.tracking_logs:
        try:
            logs.split_tracking_log(fname, parsed_args.destination)
        except Exception as excp:
            _, _, tb = sys.exc_info()
            traces = '\n'.join(map(str.strip, traceback.format_tb(tb)))
            failed = True
            msg = 'Failed to split {f}: {e}'.format(f=fname, e=traces)
            print(msg, file=sys.stderr)
    sys.exit(0 if not failed else 1)


def download_files(parsed_args):
    """
    Using the Namespace object generated by argparse, download the files
    that match the given criteria
    """
    parsed_args.year = parsed_args.begin_date[:4]
    parsed_args.verbose = not parsed_args.quiet
    info = aws.BUCKETS.get(parsed_args.file_type)
    info['Prefix'] = info['Prefix'].format(
        site=parsed_args.site, year=parsed_args.year,
        date=parsed_args.begin_date, org=parsed_args.org
    )
    bucket = aws.make_s3_bucket(info['Bucket'])
    blobs = aws.S3Blob.from_prefix(bucket=bucket, prefix=info['Prefix'])
    downloads = dict()
    for blob in blobs:
        fdate = aws.get_file_date(blob.name)
        if parsed_args.begin_date <= fdate <= parsed_args.end_date:
            fullname = os.path.join(
                parsed_args.destination,
                os.path.basename(os.path.join(*blob.name.split('/')))
            )
            downloads.setdefault(fullname, 0)
            blob.download_file(fullname)
            downloads[fullname] += 1
            try:
                if parsed_args.file_type == 'email':
                    aws.process_email_file(fullname, parsed_args.verbose)
                else:
                    aws.decrypt_file(fullname, parsed_args.verbose)
                    downloads[fullname] += 1
                if parsed_args.verbose:
                    print('Downloaded and decrypted {f}'.format(f=fullname))
                if parsed_args.file_type == 'log' and parsed_args.split:
                    decrypted_fname, _ = os.path.splitext(fullname)
                    logs.split_tracking_log(decrypted_fname, parsed_args.destination)
            except Exception as excp:
                print(excp, file=sys.stderr)
    if not downloads:
        print('No files found matching the given criteria')
    if parsed_args.file_type == 'log' and parsed_args.split:
        parsed_args.tracking_logs = []
        for k, v in downloads.items():
            if v == 2:
                parsed_args.tracking_logs.append(k)
        split_log_files(parsed_args)
    rc = 0 if all(v == 2 for v in downloads.values()) else 1
    sys.exit(rc)


def push_to_bq(parsed_args):
    """
    Push to BigQuery
    """
    if not parsed_args.items:
        print('No items to process')
        sys.exit(0)
    try:
        if parsed_args.service_account_file is not None:
            client = gcp.BigqueryClient.from_service_account_json(
                parsed_args.service_account_file,
                project=parsed_args.project
            )
        else:
            client = client = gcp.BigqueryClient(
                project=parsed_args.project
            )
    except Exception as excp:
        errmsg = 'Failed to connect to BigQuery: {e}'
        print(errmsg.format(e=excp), file=sys.stderr)
        sys.exit(1)
    all_jobs = []
    for item in parsed_args.items:
        if not os.path.exists(item):
            errmsg = 'Skipping {f!r}. It does not exist.'
            print(errmsg.format(f=item), file=sys.stderr)
            if parsed_args.fail_fast:
                print('Exiting...', file=sys.stderr)
                sys.exit(1)
            continue
        if os.path.isdir(item):
            loader = client.load_tables_from_dir
            appender = all_jobs.extend
        else:
            loader = client.load_one_file_to_table
            appender = all_jobs.append
        appender(
            loader(
                item, parsed_args.file_type, parsed_args.project,
                parsed_args.create, parsed_args.append,
                parsed_args.use_storage, parsed_args.bucket
            )
        )
    if not all_jobs:
        errmsg = (
            'No items processed. Perhaps, the given directory is empty?'
        )
        print(errmsg, file=sys.stderr)
        sys.exit(1)
    if parsed_args.wait_for_loads:
        wait_for_bq_jobs(all_jobs)
    errors = []
    for job in all_jobs:
        if job.errors:
            print(
                'Error encountered: {e}'.format(e=job.errors), file=sys.stderr
            )
            errors.extend(job.errors)
    if errors:
        sys.exit(1)
    if parsed_args.wait_for_loads:
        print(
            '{c} item(s) loaded to BigQuery'.format(c=len(all_jobs))
        )
        sys.exit(0)
    msg = (
        '{c} BigQuery data load jobs started. Please consult your '
        'BigQuery console for more details about the status of said jobs.'
    )
    print(msg.format(c=len(all_jobs)))


def push_generated_files(parsed_args):
    """
    Using the Namespace object generated by argparse, push data files
    to a target destination
    """
    if parsed_args.destination == 'bq':
        push_to_bq(parsed_args)
    msg = (
        'Push has not yet been implemented for gcs.\n'
        'When ready, your data will be pushed with info:\n'
        'Project: {p} - Bucket: {b}'
    )
    print(
        msg.format(
            p=parsed_args.project, b=parsed_args.bucket,
            d=parsed_args.destination.upper()
        )
    )
    sys.exit(0)


def main():
    """
    Entry point
    """
    COMMANDS = {
        'list': list_files,
        'download': download_files,
        'split': split_log_files,
        'push': push_generated_files,
    }
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        '--quiet', '-q',
        help='Only print error messages to standard streams.',
        action='store_true',
    )
    subparsers = parser.add_subparsers(
        description='Choose a subcommand to carry out a task with simeon',
        dest='command'
    )
    subparsers.required = True
    downloader = subparsers.add_parser(
        'download',
        help='Download edX research data with the given criteria'
    )
    downloader.set_defaults(command='download')
    downloader.add_argument(
        '--file-type', '-f',
        help='The type of files to get. Default: %(default)s',
        choices=['email', 'sql', 'log'],
        default='sql',
    )
    downloader.add_argument(
        '--destination', '-d',
        help='Directory where to download the file(s). Default: %(default)s',
        default=os.getcwd(),
    )
    downloader.add_argument(
        '--begin-date', '-b',
        help=(
            'Start date of the download timeframe. '
            'Default: %(default)s'
        ),
        default=aws.BEGIN_DATE,
        type=cli_utils.parsed_date
    )
    downloader.add_argument(
        '--end-date', '-e',
        help=(
            'End date of the download timeframe. '
            'Default: %(default)s'
        ),
        default=aws.END_DATE,
        type=cli_utils.parsed_date
    )
    downloader.add_argument(
        '--org', '-o',
        help='The organization whose data is fetched. Default: %(default)s',
        default='mitx',
    )
    downloader.add_argument(
        '--site', '-s',
        help='The edX site from which to pull data. Default: %(default)s',
        choices=['edge', 'edx', 'patches'],
        default='edx',
    )
    downloader.add_argument(
        '--split', '-S',
        help='Split downloaded log files',
        action='store_true',
    )
    lister = subparsers.add_parser(
        'list',
        help='List edX research data with the given criteria'
    )
    lister.set_defaults(command='list')
    lister.add_argument(
        '--file-type', '-f',
        help='The type of files to list. Default: %(default)s',
        choices=['email', 'sql', 'log'],
        default='sql',
    )
    lister.add_argument(
        '--begin-date', '-b',
        help=(
            'Start date of the listing timeframe. '
            'Default: %(default)s'
        ),
        default=aws.BEGIN_DATE,
        type=cli_utils.parsed_date
    )
    lister.add_argument(
        '--end-date', '-e',
        help=(
            'End date of the listing timeframe. '
            'Default: %(default)s'
        ),
        default=aws.END_DATE,
        type=cli_utils.parsed_date
    )
    lister.add_argument(
        '--org', '-o',
        help='The organization whose data is listed. Default: %(default)s',
        default='mitx',
    )
    lister.add_argument(
        '--site', '-s',
        help='The edX site from which to list data. Default: %(default)s',
        choices=['edge', 'edx', 'patches'],
        default='edx',
    )
    lister.add_argument(
        '--json', '-j',
        help='Format the file listing in JSON',
        action='store_true',
    )
    splitter = subparsers.add_parser(
        'split',
        help='Split downloaded tracking log files'
    )
    splitter.set_defaults(command='split')
    splitter.add_argument(
        'tracking_logs',
        help='List of tracking log files to split',
        nargs='+'
    )
    splitter.add_argument(
        '--destination', '-d',
        help='Directory where to download the file(s). Default: %(default)s',
        default=os.getcwd(),
    )
    pusher = subparsers.add_parser(
        'push',
        help='Push the generated data files to some target destination'
    )
    pusher.set_defaults(command='push')
    pusher.add_argument(
        'destination',
        help='Sink for the generated data files',
        choices=['gcs', 'bq']
    )
    pusher.add_argument(
        'items',
        help='The items (file or folder) to push to GCS or BigQuery',
        nargs='+',
    )
    pusher.add_argument(
        '--project', '-p',
        help='GCP project associated with the target sink',
        required=True
    )
    pusher.add_argument(
        '--bucket', '-b',
        help='GCS bucket name associated with the target sink',
        required=True,
        type=cli_utils.gcs_bucket,
    )
    pusher.add_argument(
        '--service-account-file', '-S',
        help='The service account to carry out the data load',
        type=cli_utils.optional_file
    )
    pusher.add_argument(
        '--file-type', '-f',
        help='The type of files to push. Default: %(default)s',
        choices=['email', 'sql', 'log'],
        default='sql',
    )
    pusher.add_argument(
        '--create', '-c',
        help='Whether to create destination tables when pushing to bq',
        action='store_true',
    )
    pusher.add_argument(
        '--append', '-a',
        help='Whether to append to destination tables when pushing to bq',
        action='store_true',
    )
    pusher.add_argument(
        '--use-storage', '-s',
        help='Whether to use GCS for actual files when loading to bq',
        action='store_true',
    )
    pusher.add_argument(
        '--fail-fast', '-F',
        help=(
            'Force simeon to quit as soon as an error is encountered'
            ' with any of the given items.'
        ),
        action='store_true',
    )
    pusher.add_argument(
        '--wait-for-loads', '-w',
        help=(
            'Wait for asynchronous BigQuery load jobs to finish. '
            'Otherwise, simeon creates load jobs and exits.'
        ),
        action='store_true',
    )
    args = parser.parse_args()
    COMMANDS.get(args.command)(args)


if __name__ == '__main__':
    main()
