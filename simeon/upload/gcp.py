"""
Utilities functions and classes to help with loading data to Google Cloud
"""
import glob
import gzip
import os
from datetime import datetime
from typing import List

from google.cloud import bigquery

from simeon.upload import utilities as uputils


class BigqueryClient(bigquery.Client):
    """
    Subclass bigquery.Client and add convenience methods
    """
    def load_tables_from_dir(
        self, dirname: str, file_type: str, project: str,
        create: bool, append: bool, use_storage: bool=False,
        bucket: str=None
    ) -> List[bigquery.LoadJob]:
        """
        Load all the files in the given directory.

        :type dirname: str
        :param dirname: Grandparent or parent directory of split up files
        :type file_type: str
        :param file_type: One of sql, email, log
        :type project: str
        :param project: Target GCP project
        :type create: bool
        :param create: Whether or not to create the destination table
        :type append: bool
        :param append: Whether or not to append the records to the table
        :type use_storage: bool
        :param use_storage: Whether or not to load the data from GCS
        :rtype: List[bigquery.LoadJob]
        :return: List of load jobs
        :raises: Propagates exceptions from self.load_table_from_file and
        self.load_table_from_uri
        """
        format_ = 'json' if file_type == 'log' else 'csv'
        patts = (
            os.path.join(dirname, '*.{f}.gz'.format(f=format_)),
            os.path.join(dirname, '*', '*.{f}.gz'.format(f=format_))
        )
        files = []
        for patt in patts:
            files.extend(glob.glob(patt))
        jobs = []
        for file_ in files:
            jobs.append(
                self.load_one_file_to_table(
                    file_, file_type, project,
                    create, append, use_storage, bucket,
                )
            )
        return jobs

    def load_one_file_to_table(
        self, fname: str, file_type: str, project: str,
        create: bool, append: bool, use_storage: bool=False,
        bucket: str=None,
    ):
        """
        :type fname: str
        :param fname: The specific file to load
        :type file_type: str
        :param file_type: One of sql, email, log
        :type project: str
        :param project: Target GCP project
        :type create: bool
        :param create: Whether or not to create the destination table
        :type append: bool
        :param append: Whether or not to append the records to the table
        :type use_storage: bool
        :param use_storage: Whether or not to load the data from GCS
        :rtype: bigquery.LoadJob
        :return: The LoadJob object associated with the work being done
        :raises: Propagates exceptions from self.load_table_from_file and
        self.load_table_from_uri
        """
        if use_storage:
            if bucket is None:
                raise ValueError('use_storage=True requires a bucket name')
            loader = self.load_table_from_uri
        else:
            loader = self.load_table_from_file
        format_ = 'json' if file_type == 'log' else 'csv'
        job_prefix = '{t}_data_load_{dt}-'.format(
            t=file_type, dt=datetime.now().strftime('%Y%m%d%H%M%S%f')
        )
        dest = uputils.local_to_bq_table(fname, file_type, project)
        dataset, _ = dest.rsplit('.', 1)
        self.create_dataset(dataset, exists_ok=True)
        if use_storage:
            fname = uputils.local_to_gcs_path(fname, file_type, bucket)
        else:
            fname = gzip.open(fname, 'rb')
        config = uputils.make_bq_config(dest, append, create, format_)
        return loader(
            fname, dest, job_config=config, job_id_prefix=job_prefix
        )
