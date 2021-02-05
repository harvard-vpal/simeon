"""
simeon is a command line tool that helps with processing edx data
"""
import os
import sys
import traceback
from argparse import (
    ArgumentParser, FileType
)

from simeon.download import (
    aws, emails, logs, sqls, utilities as downutils
)
from simeon.exceptions import AWSException
from simeon.report import (
    batch_user_info_combos, make_user_info_combo
)
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
    info = aws.BUCKETS.get(parsed_args.file_type)
    info['Prefix'] = info['Prefix'].format(
        site=parsed_args.site, year=parsed_args.year,
        date=parsed_args.begin_date, org=parsed_args.org,
        request=parsed_args.request_id or '',
    )
    bucket = aws.make_s3_bucket(info['Bucket'])
    try:
        blobs = aws.S3Blob.from_prefix(
            bucket=bucket, prefix=info['Prefix']
        )
    except AWSException as excp:
        errmsg = 'Failed to list files: {e}'.format(e=excp)
        parsed_args.logger.error(errmsg)
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
    msg = '{w} file name {f}'
    for fname in parsed_args.downloaded_files:
        parsed_args.logger.info(
            msg.format(f=fname, w='Splitting')
        )
        try:
            logs.split_tracking_log(
                filename=fname, ddir=parsed_args.destination,
                dynamic_date=parsed_args.dynamic_date,
                courses=parsed_args.courses,
            )
            parsed_args.logger.info(
                msg.format(f=fname, w='Done splitting')
            )
        except Exception as excp:
            # _, _, tb = sys.exc_info()
            # traces = '\n'.join(map(str.strip, traceback.format_tb(tb)))
            failed = True
            msg = 'Failed to split {f}: {e}'.format(f=fname, e=excp)
            parsed_args.logger.error(msg)
    sys.exit(0 if not failed else 1)


def split_sql_files(parsed_args):
    """
    Split the SQL data archive into separate folders.
    """
    failed = False
    msg = '{w} file name {f}'
    for fname in parsed_args.downloaded_files:
        parsed_args.logger.info(
            msg.format(f=fname, w='Splitting')
        )
        try:
            to_decrypt = sqls.process_sql_archive(
                archive=fname, ddir=parsed_args.destination,
                include_edge=parsed_args.include_edge,
                courses=parsed_args.courses,
            )
            parsed_args.logger.info(
                msg.format(f=fname, w='Done splitting')
            )
            if parsed_args.no_decryption:
                continue
            parsed_args.logger.info(
                msg.format(f=fname, w='Decrypting the contents in')
            )
            sqls.batch_decrypt_files(
                all_files=to_decrypt, size=100,
                verbose=parsed_args.verbose, logger=parsed_args.logger,
                timeout=parsed_args.decryption_timeout,
                keepfiles=parsed_args.keep_encrypted
            )
            parsed_args.logger.info(
                msg.format(f=fname, w='Done decrypting the contents in')
            )
            parsed_args.logger.info('Making user info combo reports')
            dirnames = set(
                os.path.dirname(f) for f in to_decrypt if 'ora/' not in f
            )
            if len(dirnames) >= 10:
                batch_user_info_combos(
                    dirnames=dirnames, verbose=parsed_args.verbose,
                    logger=parsed_args.logger
                )
            else:
                for folder in dirnames:
                    msg = 'Making a user info combo report with files in {d}'
                    parsed_args.logger.info(msg.format(d=folder))
                    make_user_info_combo(folder)
                    parsed_args.logger.info(
                        'Report generated for files in {d}'.format(d=folder)
                    )
        except Exception as excp:
            # _, _, tb = sys.exc_info()
            # traces = '\n'.join(map(str.strip, traceback.format_tb(tb)))
            failed = True
            msg = 'Failed to split and decrypt {f}: {e}'.format(
                f=fname, e=excp
            )
            parsed_args.logger.error(msg)
    sys.exit(0 if not failed else 1)


def split_files(parsed_args):
    """
    Split log or SQL files
    """
    if parsed_args.file_type == 'log':
        split_log_files(parsed_args)
    elif parsed_args.file_type == 'sql':
        split_sql_files(parsed_args)
    else:
        parsed_args.logger.error(
            'The split command does not support file type {ft}'.format(
                ft=parsed_args.file_type
            )
        )
        sys.exit(1)


def download_files(parsed_args):
    """
    Using the Namespace object generated by argparse, download the files
    that match the given criteria
    """
    parsed_args.year = parsed_args.begin_date[:4]
    info = aws.BUCKETS.get(parsed_args.file_type)
    info['Prefix'] = info['Prefix'].format(
        site=parsed_args.site, year=parsed_args.year,
        date=parsed_args.begin_date, org=parsed_args.org,
        request=parsed_args.request_id or '',
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
            parsed_args.logger.info(
                'Downloading {n} into {f}'.format(n=blob.name, f=fullname)
            )
            blob.download_file(fullname)
            downloads[fullname] += 1
            parsed_args.logger.info(
                'Done downloading {n}'.format(n=blob.name)
            )
            try:
                parsed_args.logger.info('Decrypting {f}'.format(f=fullname))
                if parsed_args.file_type == 'email':
                    emails.process_email_file(
                        fname=fullname, verbose=parsed_args.verbose,
                        logger=parsed_args.logger,
                        timeout=parsed_args.decryption_timeout,
                    )
                    if parsed_args.verbose:
                        parsed_args.logger.info(
                            'Downloaded and decrypted {f}'.format(f=fullname)
                        )
                elif parsed_args.file_type == 'log':
                    downutils.decrypt_files(
                        fnames=fullname, verbose=parsed_args.verbose,
                        logger=parsed_args.logger,
                        timeout=parsed_args.decryption_timeout,
                    )
                    if parsed_args.verbose:
                        parsed_args.logger.info(
                            'Downloaded and decrypted {f}'.format(f=fullname)
                        )
                downloads[fullname] += 1
            except Exception as excp:
                parsed_args.logger.error(excp)
    if not downloads:
        parsed_args.logger.info('No files found matching the given criteria')
    if parsed_args.file_type == 'log' and parsed_args.split:
        parsed_args.downloaded_files = []
        for k, v in downloads.items():
            if v == 2:
                k, _ = os.path.splitext(k)
                parsed_args.downloaded_files.append(k)
        split_log_files(parsed_args)
    elif parsed_args.file_type == 'sql' and parsed_args.split:
        parsed_args.downloaded_files = list(downloads)
        split_sql_files(parsed_args)
    rc = 0 if all(v == 2 for v in downloads.values()) else 1
    sys.exit(rc)


def push_to_bq(parsed_args):
    """
    Push to BigQuery
    """
    if not parsed_args.items:
        parsed_args.logger.info('No items to process')
        sys.exit(0)
    parsed_args.logger.info('Connecting to BigQuery')
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
        parsed_args.logger.error(errmsg.format(e=excp))
        sys.exit(1)
    all_jobs = []
    for item in parsed_args.items:
        if not os.path.exists(item):
            errmsg = 'Skipping {f!r}. It does not exist.'
            parsed_args.logger.error(errmsg.format(f=item))
            if parsed_args.fail_fast:
                parsed_args.logger.error('Exiting...')
                sys.exit(1)
            continue
        if os.path.isdir(item):
            loader = client.load_tables_from_dir
            appender = all_jobs.extend
        else:
            loader = client.load_one_file_to_table
            appender = all_jobs.append
        parsed_args.logger.info(
            'Loading item(s) in {f!r} to BigQuery'.format(f=item)
        )
        appender(
            loader(
                item, parsed_args.file_type, parsed_args.project,
                parsed_args.create, parsed_args.append,
                parsed_args.use_storage, parsed_args.bucket
            )
        )
        parsed_args.logger.info(
            'Created BigQuery load job(s) for item(s) in {f!r}'.format(f=item)
        )
    if not all_jobs:
        errmsg = (
            'No items processed. Perhaps, the given directory is empty?'
        )
        parsed_args.logger.error(errmsg)
        sys.exit(1)
    if parsed_args.wait_for_loads:
        wait_for_bq_jobs(all_jobs)
    errors = []
    for job in all_jobs:
        if job.errors:
            parsed_args.logger.error(
                'Error encountered: {e}'.format(e=job.errors)
            )
            errors.extend(job.errors)
    if errors:
        sys.exit(1)
    if parsed_args.wait_for_loads:
        parsed_args.logger.info(
            '{c} item(s) loaded to BigQuery'.format(c=len(all_jobs))
        )
        sys.exit(0)
    msg = (
        '{c} BigQuery data load jobs started. Please consult your '
        'BigQuery console for more details about the status of said jobs.'
    )
    parsed_args.logger.info(msg.format(c=len(all_jobs)))


def push_to_gcs(parsed_args):
    """
    Push to Storage
    """
    if not parsed_args.items:
        parsed_args.logger.info('No items to process')
        sys.exit(0)
    parsed_args.logger.info(
        'Connecting to Google Cloud Storage'
    )
    try:
        if parsed_args.service_account_file is not None:
            client = gcp.GCSClient.from_service_account_json(
                parsed_args.service_account_file,
                project=parsed_args.project
            )
        else:
            client = client = gcp.GCSClient(
                project=parsed_args.project
            )
    except Exception as excp:
        errmsg = 'Failed to connect to Google Cloud Storage: {e}'
        parsed_args.logger.error(errmsg.format(e=excp))
        sys.exit(1)
    failed = False
    for item in parsed_args.items:
        if not os.path.exists(item):
            errmsg = 'Skipping {f!r}. It does not exist.'
            parsed_args.logger.error(errmsg.format(f=item))
            if parsed_args.fail_fast:
                parsed_args.logger.error('Exiting...')
                sys.exit(1)
            continue
        if os.path.isdir(item):
            loader = client.load_dir
        else:
            loader = client.load_on_file_to_gcs
        try:
            parsed_args.logger.info(
                'Loading {f} to GCS'.format(f=item)
            )
            loader(
                item, parsed_args.file_type,
                parsed_args.bucket, parsed_args.overwrite
            )
            parsed_args.logger.info(
                'Done loading {f} to GCS'.format(f=item)
            )
        except Exception as excp:
            errmsg = 'Failed to load {f} to GCS: {e}'
            parsed_args.logger.error(errmsg.format(f=item, e=excp))
            if parsed_args.fail_fast:
                parsed_args.logger.error('Exiting...')
                sys.exit(1)
            failed = True
    sys.exit(1 if failed else 0)


def push_generated_files(parsed_args):
    """
    Using the Namespace object generated by argparse, push data files
    to a target destination
    """
    if parsed_args.destination == 'bq':
        push_to_bq(parsed_args)
    else:
        push_to_gcs(parsed_args)


def main():
    """
    Entry point
    """
    COMMANDS = {
        'list': list_files,
        'download': download_files,
        'split': split_files,
        'push': push_generated_files,
    }
    parser = ArgumentParser(description=__doc__)
    parser.add_argument(
        '--quiet', '-Q',
        help='Only print error messages to standard streams.',
        action='store_true',
    )
    parser.add_argument(
        '--debug', '-B',
        help='Show some stacktrace if simeon stops because of a fatal error',
        action='store_true',
    )
    parser.add_argument(
        '--config-file', '-C',
        help=(
            'The INI configuration file to use for default arguments.'
        ),
    )
    parser.add_argument(
        '--log-file', '-L',
        help='Log file to use when simeon prints messages. Default: stdout',
        type=FileType('w'),
        default=sys.stdout,
    )
    subparsers = parser.add_subparsers(
        description='Choose a subcommand to carry out a task with simeon',
        dest='command'
    )
    subparsers.required = True
    downloader = subparsers.add_parser(
        'download',
        help='Download edX research data with the given criteria',
        description=(
            'Download edX research data with the given criteria below'
        )
    )
    downloader.set_defaults(command='download')
    downloader.add_argument(
        '--file-type', '-f',
        help='The type of files to get. Default: %(default)s',
        choices=['email', 'sql', 'log', 'rdx'],
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
    downloader.add_argument(
        '--dynamic-date', '-m',
        help=(
            'If splitting the downloaded files, use the '
            'dates from the records to make tracking log file names. '
            'Otherwise, the dates in the GZIP file names are used.'
        ),
        action='store_true',
    )
    downloader.add_argument(
        '--request-id', '-r',
        help='Request ID when listing RDX files',
    )
    downloader.add_argument(
        '--decryption-timeout', '-t',
        help='Number of seconds to wait for the decryption of files.',
        type=int,
    )
    downloader.add_argument(
        '--courses', '-c',
        help=(
            'A list of white space separated course IDs whose data files '
            'are unpacked and decrypted.'
        ),
        type=cli_utils.course_listings,
    )
    downloader.add_argument(
        '--no-decryption', '-D',
        help='Don\'t decrypt the unpacked SQL files.',
        action='store_true',
    )
    downloader.add_argument(
        '--include-edge', '-E',
        help='Include the edge site files when splitting SQL data packages.',
        action='store_true',
    )
    downloader.add_argument(
        '--keep-encrypted', '-k',
        help='Keep the encrypted files after decrypting them',
        action='store_true',
    )
    lister = subparsers.add_parser(
        'list',
        help='List edX research data with the given criteria',
        description=(
            'List edX research data with the given criteria below'
        )
    )
    lister.set_defaults(command='list')
    lister.add_argument(
        '--file-type', '-f',
        help='The type of files to list. Default: %(default)s',
        choices=['email', 'sql', 'log', 'rdx'],
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
        '--request-id', '-r',
        help='Request ID when listing RDX files',
    )
    lister.add_argument(
        '--json', '-j',
        help='Format the file listing in JSON',
        action='store_true',
    )
    splitter = subparsers.add_parser(
        'split',
        help='Split downloaded tracking log or SQL files',
        description='Split downloaded tracking log or SQL files'
    )
    splitter.set_defaults(command='split')
    splitter.add_argument(
        'downloaded_files',
        help='List of tracking log or SQL archives to split',
        nargs='+'
    )
    splitter.add_argument(
        '--file-type', '-f',
        help='The file type of the items provided. Default: %(default)s',
        default='log',
        choices=['log', 'sql'],
    )
    splitter.add_argument(
        '--no-decryption', '-D',
        help='Don\'t decrypt the unpacked SQL files.',
        action='store_true',
    )
    splitter.add_argument(
        '--include-edge', '-E',
        help='Include the edge site files when splitting SQL data packages.',
        action='store_true',
    )
    splitter.add_argument(
        '--keep-encrypted', '-k',
        help='Keep the encrypted files after decrypting them',
        action='store_true',
    )
    splitter.add_argument(
        '--decryption-timeout', '-t',
        help='Number of seconds to wait for the decryption of files.',
        type=int,
    )
    splitter.add_argument(
        '--destination', '-d',
        help=(
            'Directory where to place the files from splitting the item(s).'
            ' Default: %(default)s'
        ),
        default=os.getcwd(),
    )
    splitter.add_argument(
        '--courses', '-c',
        help=(
            'A list of white space separated course IDs whose data files '
            'are unpacked and decrypted.'
        ),
        type=cli_utils.course_listings,
    )
    splitter.add_argument(
        '--dynamic-date', '-m',
        help=(
            'Use the dates from the records to make tracking log file names. '
            'Otherwise, the dates in the GZIP file names are used.'
        ),
        action='store_true',
    )
    pusher = subparsers.add_parser(
        'push',
        help='Push the generated data files to some target destination',
        description=(
            'Push to the given items to Google Cloud Storage or BigQuery'
        ),
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
        choices=['email', 'sql', 'log', 'rdx'],
        default='sql',
    )
    pusher.add_argument(
        '--create', '-c',
        help=(
            'Whether to create destination BigQuery tables and '
            'datasets if they don\'t exist'
        ),
        action='store_true',
    )
    pusher.add_argument(
        '--append', '-a',
        help=(
            'Whether to append to destination tables if they exist'
            ' when pushing data to BigQuery'
        ),
        action='store_true',
    )
    pusher.add_argument(
        '--overwrite', '-o',
        help=(
            'Overwrite the destination when loading '
            'items to Google Cloud Storage'
        ),
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
    args.verbose = not args.quiet
    args.logger = cli_utils.make_logger(args.verbose, args.log_file)
    try:
        COMMANDS.get(args.command)(args)
    except:
        _, excp, tb = sys.exc_info()
        if isinstance(excp, SystemExit):
            raise excp
        msg = 'The command {c} failed: {e}'
        if args.debug:
            traces = ['{e}'.format(e=excp)]
            traces += map(str.strip, traceback.format_tb(tb))
            msg = msg.format(c=args.command, e='\n'.join(traces))
        else:
            msg = msg.format(c=args.command, e=excp)
        args.logger.error(msg)
        sys.exit(1)


if __name__ == '__main__':
    main()
