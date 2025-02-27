"""."""

import stat
import os
import sys
import datetime
import time
import json
import logging
import tempfile
import fnmatch
import shutil
from typing import List, Union
import warnings
import numpy as np
from git import Repo, GitCommandError
from gitpandas.cache import multicache  # , EphemeralCache, RedisDFCache
from pandas import DataFrame, to_datetime

# try:
#     from joblib import delayed, Parallel        # type: ignore

#     _has_joblib = True
# except ImportError:
#     _has_joblib = False

_has_joblib = False


def _parallel_cumulative_blame_func(self_, x, committer, ignore_globs, include_globs):
    blm = self_.blame(
        rev=x['rev'],
        committer=committer,
        ignore_globs=ignore_globs,
        include_globs=include_globs
    )
    x.update(json.loads(blm.to_json())['loc'])

    return x


class Repository():
    """The base class for a generic git repository, from which to gather statistics.
    The object encapulates a single
    gitpython Repo instance.

    :param working_dir: the directory of the git repository, meaning a .git directory is in it (default None=cwd)
    :param verbose: optional, verbosity level of output, bool
    :param tmp_dir: optional, a path to clone the repo into if necessary. Will create one if none passed.
    :param cache_backend: optional, an instantiated cache backend from gitpandas.cache
    :return:
    """

    def __init__(self, working_dir: str = '', verbose=False, tmp_dir: str = '', cache_backend=None):
        self.verbose = verbose
        self.log = logging.getLogger('gitpandas')
        self.__delete_hook = False
        self._git_repo_name = ''
        self.cache_backend = cache_backend
        if working_dir:
            if working_dir[:3] == 'git':
                # if a tmp dir is passed, clone into that, otherwise make a temp directory.
                if tmp_dir is None:
                    if self.verbose:
                        print(f'cloning repository: {working_dir} into a temporary location')
                    dir_path = tempfile.mkdtemp()
                else:
                    dir_path = tmp_dir

                self.repo = Repo.clone_from(working_dir, dir_path)
                self._git_repo_name = working_dir.split(
                    os.sep)[-1].split('.')[0]
                self.git_dir = dir_path
                self.__delete_hook = True
            else:
                self.git_dir = working_dir
                self.repo = Repo(self.git_dir)
        else:
            self.git_dir = os.getcwd()
            self.repo = Repo(self.git_dir)

        if self.verbose:
            print('Repository [%s] instantiated at directory: %s' % (
                self._repo_name(), self.git_dir))

        self.coverage_file = self.git_dir + os.sep + '.coverage'

    def __del__(self):
        """
        On delete, clean up any temporary repositories still hanging around

        :return:
        """
        if self.__delete_hook and os.path.exists(self.git_dir):
            print("Deleting ###########################################################")

            for root, dirs, files in os.walk(self.git_dir, topdown=False):
                for name in files:
                    filename = os.path.join(root, name)
                    os.chmod(filename, stat.S_IWRITE)
                    os.remove(filename)
                    print("Deleting Files ###########################################################")
                for name in dirs:
                    os.rmdir(os.path.join(root, name))
            os.rmdir(self.git_dir)
            # shutil.rmtree(self.git_dir)

    def is_bare(self):
        """
        Returns a boolean for if the repo is bare or not

        :return: bool
        """

        return self.repo.bare

    def has_coverage(self):
        """
        Returns a boolean for is a parseable .coverage file can be found in the repository

        :return: bool

        """

        if self.coverage_file:
            try:
                with open(self.coverage_file, 'r') as f:
                    blob = f.read()
                    blob = blob.split('!')[2]
                    json.loads(blob)
                return True
            except Exception:
                return False
        else:
            return False

    def coverage(self):
        """
        If there is a .coverage file available, this will attempt to form a DataFrame with that information in it, which
        will contain the columns:

         * filename
         * lines_covered
         * total_lines
         * coverage

        If it can't be found or parsed, an empty DataFrame of that form will be returned.

        :return: DataFrame
        """

        if not self.has_coverage():
            return DataFrame(columns=['filename', 'lines_covered', 'total_lines', 'coverage'])

        with open(self.coverage_file, 'r') as f:
            blob = f.read()
            blob = blob.split('!')[2]
            cov = json.loads(blob)

        ds = []
        for filename in cov['lines'].keys():
            _i = 0
            try:
                with open(filename, 'r') as f:
                    for _i, _x in enumerate(f):
                        pass
            except FileNotFoundError:
                if self.verbose:
                    warnings.warn(
                        'Could not find file %s for coverage' % (filename, ))

            num_lines = _i + 1

            try:
                short_filename = filename.split(self.git_dir + os.sep)[1]
                ds.append([short_filename, len(
                    cov['lines'][filename]), num_lines])
            except IndexError:
                if self.verbose:
                    warnings.warn(
                        'Could not find file %s for coverage' % (filename, ))

        df = DataFrame(
            ds, columns=['filename', 'lines_covered', 'total_lines'])
        df['coverage'] = df['lines_covered'] / df['total_lines']

        return df

    def hours_estimate(self, branch: str = 'master', grouping_window: float = 0.5, single_commit_hours: float = 0.5,
                       limit: int = 0, days: int = 0,
                       committer: float = True, ignore_globs=None, include_globs=None):
        """
        https://github.com/kimmobrunfeldt/git-hours/blob/8aaeee237cb9d9028e7a2592a25ad8468b1f45e4/index.js#L114-L143

        Iterates through the commit history of repo to estimate the time commitement of each author or committer over
        the course of time indicated by limit/extensions/days/etc.

        :param branch: the branch to return commits for
        :param limit: (optional, default=None) a maximum number of commits to return, None for no limit
        :param grouping_window: (optional, default=0.5 hours) the threhold for how close two commits need to be to
            consider them part of one coding session
        :param single_commit_hours: (optional, default 0.5 hours) the time range to associate with one single commit
        :param days: (optional, default=None) number of days to return, if limit is None
        :param committer: (optional, default=True) whether to use committer vs. author
        :param ignore_globs: (optional, default=None) a list of globs to ignore, default none excludes nothing
        :param include_globs: (optinal, default=None) a list of globs to include, default of None includes everything.
        :return: DataFrame
        """

        max_diff_in_minutes = grouping_window * 60.0
        first_commit_addition_in_minutes = single_commit_hours * 60.0

        # First get the commit history
        ch = self.commit_history(branch=branch, limit=limit, days=days, ignore_globs=ignore_globs,
                                 include_globs=include_globs)

        # split by committer|author
        if committer:
            by = 'committer'
        else:
            by = 'author'
        people = set(ch[by].values)

        ds = []
        for person in people:
            commits = ch[ch[by] == person]
            commits_ts: List[float] = [
                x * 10e-10 for x in sorted(commits.index.values.tolist())]

            if len(commits_ts) < 2:
                ds.append([person, 0])
                continue

            def estimate(index: int, date: float) -> float:
                next_ts = commits_ts[index + 1]
                diff_in_minutes = next_ts - date
                diff_in_minutes /= 60.0
                if diff_in_minutes < max_diff_in_minutes:
                    return diff_in_minutes / 60.0
                return first_commit_addition_in_minutes / 60.0

            hours_list = [estimate(a, b) for a, b in enumerate(commits_ts[:-1])]
            hours = round(sum(hours_list), 2)
            ds.append([person, hours])

        df = DataFrame(ds, columns=[by, 'hours'])

        return df

    def commit_history(self, branch: str = 'master', limit: int = 0, days: int = 0, ignore_globs=None, include_globs=None):
        """
        Returns a pandas DataFrame containing all of the commits for a given branch. Included in that DataFrame will be
        the columns:

         * date (index)
         * author
         * committer
         * message
         * lines
         * insertions
         * deletions
         * net

        :param branch: the branch to return commits for
        :param limit: (optional, default=None) a maximum number of commits to return, None for no limit
        :param days: (optional, default=None) number of days to return, if limit is None
        :param ignore_globs: (optional, default=None) a list of globs to ignore, default none excludes nothing
        :param include_globs: (optinal, default=None) a list of globs to include, default of None includes everything.
        :return: DataFrame
        """

        # setup the data-set of commits
        if not limit:
            if not days:
                ds = [[
                    x.author.name,
                    x.committer.name,
                    x.committed_date,
                    x.message,
                    self.__check_extension(
                        x.stats.files, ignore_globs=ignore_globs, include_globs=include_globs)
                ] for x in self.repo.iter_commits(branch, max_count=sys.maxsize)]
            else:
                ds = []
                c_date = time.time()
                commits = self.repo.iter_commits(branch, max_count=sys.maxsize)
                dlim = time.time() - days * 24 * 3600
                while c_date > dlim:
                    try:
                        x = next(commits)
                    except StopIteration:
                        break
                    c_date = x.committed_date
                    if c_date > dlim:
                        ds.append([
                            x.author.name,
                            x.committer.name,
                            x.committed_date,
                            x.message,
                            self.__check_extension(x.stats.files, ignore_globs=ignore_globs,
                                                   include_globs=include_globs)
                        ])

        else:
            ds = [[
                x.author.name,
                x.committer.name,
                x.committed_date,
                x.message,
                self.__check_extension(
                    x.stats.files, ignore_globs=ignore_globs, include_globs=include_globs)
            ] for x in self.repo.iter_commits(branch, max_count=limit)]

        # aggregate stats
        ds = [x[:-1] + [sum([x[-1][key]['lines'] for key in x[-1].keys()]),
                        sum([x[-1][key]['insertions']
                            for key in x[-1].keys()]),
                        sum([x[-1][key]['deletions'] for key in x[-1].keys()]),
                        sum([x[-1][key]['insertions'] for key in x[-1].keys()]) - sum(
                            [x[-1][key]['deletions'] for key in x[-1].keys()])
                        ] for x in ds if len(x[-1].keys()) > 0]

        # make it a pandas dataframe
        df = DataFrame(ds,
                       columns=['author', 'committer', 'date', 'message', 'lines', 'insertions', 'deletions', 'net'])

        # format the date col and make it the index
        df['date'] = to_datetime(df['date'].map(
            datetime.datetime.fromtimestamp))
        df.set_index(keys=['date'], drop=True, inplace=True)

        return df

    def file_change_history(self, branch='master', limit: int = 0, days: int = 0, ignore_globs=None, include_globs=None):
        """
        Returns a DataFrame of all file changes (via the commit history) for the specified branch.  This is similar to
        the commit history DataFrame, but is one row per file edit rather than one row per commit (which may encapsulate
        many file changes). Included in the DataFrame will be the columns:

         * date (index)
         * author
         * committer
         * message
         * filename
         * insertions
         * deletions

        :param branch: the branch to return commits for
        :param limit: (optional, default=0) a maximum number of commits to return, 0 for no limit
        :param days: (optional, default=None) number of days to return if limit is None
        :param ignore_globs: (optional, default=None) a list of globs to ignore, default none excludes nothing
        :param include_globs: (optinal, default=None) a list of globs to include, default of None includes everything.
        :return: DataFrame
        """
        assert isinstance(limit, int) and limit >= 0, f"limit must be a positive integer, got {limit}"
        # setup the dataset of commits
        if limit == 0 or limit is None:
            if days is None:
                ds = [[
                    x.author.name,
                    x.committer.name,
                    x.committed_date,
                    x.message,
                    x.name_rev.split()[0],
                    self.__check_extension(
                        x.stats.files, ignore_globs=ignore_globs, include_globs=include_globs)
                ] for x in self.repo.iter_commits(branch, max_count=sys.maxsize)]
            else:
                ds = []
                c_date = time.time()
                commits = self.repo.iter_commits(branch, max_count=sys.maxsize)
                dlim = time.time() - days * 24 * 3600
                while c_date > dlim:
                    try:
                        x = next(commits)
                    except StopIteration:
                        break

                    c_date = x.committed_date
                    if c_date > dlim:
                        ds.append([
                            x.author.name,
                            x.committer.name,
                            x.committed_date,
                            x.message,
                            x.name_rev.split()[0],
                            self.__check_extension(x.stats.files, ignore_globs=ignore_globs,
                                                   include_globs=include_globs)
                        ])

        else:
            ds = [[
                x.author.name,
                x.committer.name,
                x.committed_date,
                x.message,
                x.name_rev.split()[0],
                self.__check_extension(
                    x.stats.files, ignore_globs=ignore_globs, include_globs=include_globs)
            ] for x in self.repo.iter_commits(branch, max_count=limit)]

        ds = [x[:-1] + [fn, x[-1][fn]['insertions'], x[-1][fn]['deletions']] for x in ds for fn in x[-1].keys() if
              len(x[-1].keys()) > 0]

        # make it a pandas dataframe
        df = DataFrame(ds,
                       columns=['author', 'committer', 'date', 'message', 'rev', 'filename', 'insertions', 'deletions'])

        # format the date col and make it the index
        df['date'] = to_datetime(df['date'].map(
            datetime.datetime.fromtimestamp))
        df.set_index(keys=['date'], drop=True, inplace=True)

        return df

    def file_change_rates(self, branch: str = 'master', limit: int = 0, coverage: bool = False, days: int = 0,
                          ignore_globs=None, include_globs=None):
        """
        This function will return a DataFrame containing some basic aggregations of the file change history data, and
        optionally test coverage data from a coverage_data.py .coverage file.  The aim here is to identify files in the
        project which have abnormal edit rates, or the rate of changes without growing the files size.  If a file has
        a high change rate and poor test coverage, then it is a great candidate for writing more tests.

        :param branch: (optional, default=master) the branch to return commits for
        :param limit: (optional, default=0) a maximum number of commits to return, 0 for no limit
        :param coverage: (optional, default=False) a bool for whether or not to attempt to join in coverage data.
        :param days: (optional, default=None) number of days to return if limit is None
        :param ignore_globs: (optional, default=None) a list of globs to ignore, default none excludes nothing
        :param include_globs: (optinal, default=None) a list of globs to include, default of None includes everything.
        :return: DataFrame
        """

        fch = self.file_change_history(
            branch=branch,
            limit=limit,
            days=days,
            ignore_globs=ignore_globs,
            include_globs=include_globs
        )
        fch.reset_index(level=0, inplace=True)

        if fch.shape[0] > 0:
            file_history = fch.groupby('filename').agg(
                {
                    'insertions': [np.sum, np.max, np.mean],
                    'deletions': [np.sum, np.max, np.mean],
                    'message': lambda x: ','.join(['"' + str(y) + '"' for y in x]),
                    'committer': lambda x: ','.join(['"' + str(y) + '"' for y in x]),
                    'author': lambda x: ','.join(['"' + str(y) + '"' for y in x]),
                    'date': [np.max, np.min]
                }
            )

            file_history.columns = [
                ' '.join(col).strip() for col in file_history.columns.values]

            file_history = file_history.rename(columns={
                'message <lambda>': 'messages',
                'committer <lambda>': 'committers',
                'insertions sum': 'total_insertions',
                'insertions amax': 'max_insertions',
                'insertions mean': 'mean_insertions',
                'author <lambda>': 'authors',
                'date amax': 'max_date',
                'date amin': 'min_date',
                'deletions sum': 'total_deletions',
                'deletions amax': 'max_deletions',
                'deletions mean': 'mean_deletions'
            })

            # get some building block values for later use
            file_history['net_change'] = file_history['total_insertions'] - \
                file_history['total_deletions']
            file_history['abs_change'] = file_history['total_insertions'] + \
                file_history['total_deletions']
            file_history['delta_time'] = file_history['max_date'] - \
                file_history['min_date']

            try:
                file_history['delta_days'] = file_history['delta_time'].map(
                    lambda x: np.ceil(x.seconds / (24 * 3600) + 0.01))
            except AttributeError:
                file_history['delta_days'] = file_history['delta_time'].map(
                    lambda x: np.ceil((float(x.total_seconds()) * 10e-6) / (24 * 3600) + 0.01))

            # calculate metrics
            file_history['net_rate_of_change'] = file_history['net_change'] / \
                file_history['delta_days']
            file_history['abs_rate_of_change'] = file_history['abs_change'] / \
                file_history['delta_days']
            file_history['edit_rate'] = file_history['abs_rate_of_change'] - \
                file_history['net_rate_of_change']
            file_history['unique_committers'] = file_history['committers'].map(
                lambda x: len(set(x.split(','))))

            # reindex
            file_history = file_history.reindex(
                columns=['unique_committers', 'abs_rate_of_change', 'net_rate_of_change', 'net_change', 'abs_change',
                         'edit_rate'])
            file_history.sort_values(by=['edit_rate'], inplace=True)

            if coverage and self.has_coverage():
                file_history = file_history.merge(
                    self.coverage(), left_index=True, right_on='filename', how='outer')
                file_history.set_index(
                    keys=['filename'], drop=True, inplace=True)
        else:
            file_history = DataFrame(
                columns=['unique_committers', 'abs_rate_of_change', 'net_rate_of_change', 'net_change', 'abs_change',
                         'edit_rate'])

        return file_history

    @staticmethod
    def __check_extension(files, ignore_globs=None, include_globs=None):
        """
        Internal method to filter a list of file changes by extension and ignore_dirs.

        :param files:
        :param ignore_globs: a list of globs to ignore (if none falls back to extensions and ignore_dir)
        :param include_globs: a list of globs to include (if none, includes all).
        :return: dict
        """

        if include_globs is None or include_globs == []:
            include_globs = ['*']

        out = {}
        for key in files.keys():
            # count up the number of patterns in the ignore globs list that match
            if ignore_globs is not None:
                count_exclude = sum([1 if fnmatch.fnmatch(
                    key, g) else 0 for g in ignore_globs])
            else:
                count_exclude = 0

            # count up the number of patterns in the include globs list that match
            count_include = sum([1 if fnmatch.fnmatch(
                key, g) else 0 for g in include_globs])

            # if we have one vote or more to include and none to exclude, then we use the file.
            if count_include > 0 and count_exclude == 0:
                out[key] = files[key]

        return out

    @multicache(
        key_prefix='blame',
        key_list=['rev', 'committer', 'by', 'ignore_blobs', 'include_globs'],
        skip_if=lambda x: True if x.get(
            'rev') is None or x.get('rev') == 'HEAD' else False
    )
    def blame(self, rev='HEAD', committer=True, by='repository', ignore_globs=None, include_globs=None):
        """
        Returns the blame from the current HEAD of the repository as a DataFrame.  The DataFrame is grouped by committer
        name, so it will be the sum of all contributions to the repository by each committer. As with the commit history
        method, extensions and ignore_dirs parameters can be passed to exclude certain directories, or focus on certain
        file extensions. The DataFrame will have the columns:

         * committer
         * loc

        :param rev: (optional, default=HEAD) the specific revision to blame
        :param committer: (optional, default=True) true if committer should be reported, false if author
        :param by: (optional, default=repository) whether to group by repository or by file
        :param ignore_globs: (optional, default=None) a list of globs to ignore, default none excludes nothing
        :param include_globs: (optinal, default=None) a list of globs to include, default of None includes everything.
        :return: DataFrame
        """

        blames = []
        file_names = [x for x in self.repo.git.log(pretty='format:', name_only=True, diff_filter='A').split('\n') if
                      x.strip() != '']
        for file in self.__check_extension({x: x for x in file_names}, ignore_globs=ignore_globs,
                                           include_globs=include_globs).keys():
            try:
                blames.append(
                    [x + [str(file).replace(self.git_dir + '/', '')] for x in
                     self.repo.blame(rev, str(file).replace(self.git_dir + '/', ''))]
                )
            except GitCommandError:
                pass

        blames = [item for sublist in blames for item in sublist]
        if committer:
            if by == 'repository':
                blames = DataFrame(
                    [[x[0].committer.name, len(x[1])] for x in blames],
                    columns=['committer', 'loc']
                ).groupby('committer').agg({'loc': np.sum})
            elif by == 'file':
                blames = DataFrame(
                    [[x[0].committer.name, len(x[1]), x[2]] for x in blames],
                    columns=['committer', 'loc', 'file']
                ).groupby(['committer', 'file']).agg({'loc': np.sum})
        else:
            if by == 'repository':
                blames = DataFrame(
                    [[x[0].author.name, len(x[1])] for x in blames],
                    columns=['author', 'loc']
                ).groupby('author').agg({'loc': np.sum})
            elif by == 'file':
                blames = DataFrame(
                    [[x[0].author.name, len(x[1]), x[2]] for x in blames],
                    columns=['author', 'loc', 'file']
                ).groupby(['author', 'file']).agg({'loc': np.sum})

        return blames

    def revs(self, branch: str = 'master', limit: int = 0, skip: int = 0, num_datapoints: int = 0):
        """
        Returns a dataframe of all revision tags and their timestamps. It will have the columns:

         * date
         * rev

        :param branch: (optional, default 'master') the branch to work in
        :param limit: (optional, default None), the maximum number of revisions to return, None for no limit
        :param skip: (optional, default None), the number of revisions to skip. Ex: skip=2 returns every other revision, None for no skipping.
        :param num_datapoints: (optional, default=None) if limit and skip are none, and this isn't, then num_datapoints evenly spaced revs will be used
        :return: DataFrame

        """

        if not limit and not skip and num_datapoints:
            limit = sum(1 for _ in self.repo.iter_commits())
            skip = int(float(limit) / num_datapoints)
        else:
            if not limit:
                limit = sys.maxsize
            elif not skip:
                limit = limit * skip

        ds = [[x.committed_date, x.name_rev.split(
            ' ')[0]] for x in self.repo.iter_commits(branch, max_count=limit)]
        df = DataFrame(ds, columns=['date', 'rev'])

        if not skip:
            if skip == 0:
                skip = 1

            if df.shape[0] >= skip:
                df = df.ix[range(0, df.shape[0], skip)]
                df.reset_index()
            else:
                df = df.ix[[0]]
                df.reset_index()

        return df

    def cumulative_blame(self, branch: str = 'master', limit: int = 0, skip: int = 0, num_datapoints: int = 0, committer: bool = True,
                         ignore_globs=None, include_globs=None):
        """
        Returns the blame at every revision of interest. Index is a datetime, column per committer, with number of lines
        blamed to each committer at each timestamp as data.

        :param branch: (optional, default 'master') the branch to work in
        :param limit: (optional, default None), the maximum number of revisions to return, None for no limit
        :param skip: (optional, default None), the number of revisions to skip. Ex: skip=2 returns every other revision, None for no skipping.
        :param num_datapoints: (optional, default=None) if limit and skip are none, and this isn't, then num_datapoints evenly spaced revs will be used
        :param committer: (optional, defualt=True) true if committer should be reported, false if author
        :param ignore_globs: (optional, default=None) a list of globs to ignore, default none excludes nothing
        :param include_globs: (optinal, default=None) a list of globs to include, default of None includes everything.
        :return: DataFrame

        """

        revs = self.revs(branch=branch, limit=limit, skip=skip,
                         num_datapoints=num_datapoints)

        # get the commit history to stub out committers (hacky and slow)
        committers = {x.committer.name for x in self.repo.iter_commits(
            branch, max_count=sys.maxsize)}

        for y in committers:
            revs[y] = 0

        if self.verbose:
            print('Beginning processing for cumulative blame:')

        # now populate that table with some actual values
        for idx, row in revs.iterrows():
            if self.verbose:
                print('%s. [%s] getting blame for rev: %s' % (
                    str(idx), datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'), row.rev,))

            blame = self.blame(rev=row.rev, committer=committer,
                               ignore_globs=ignore_globs, include_globs=include_globs)
            for y in committers:
                try:
                    loc = blame.loc[y, 'loc']
                    revs.set_value(idx, y, loc)
                except KeyError:
                    pass

        del revs['rev']

        revs['date'] = to_datetime(
            revs['date'].map(datetime.datetime.fromtimestamp))
        revs.set_index(keys=['date'], drop=True, inplace=True)
        revs = revs.fillna(0.0)

        # drop 0 cols
        for col in revs.columns.values:
            if col != 'col':
                if revs[col].sum() == 0:
                    del revs[col]

        # drop 0 rows
        keep_idx = []
        committers = {x for x in revs.columns.values if x != 'date'}
        for idx, row in revs.iterrows():
            if sum([row[x] for x in committers]) > 0:
                keep_idx.append(idx)

        revs = revs.ix[keep_idx]

        return revs

    # def parallel_cumulative_blame(self, branch: str = 'master', limit: int = 0, skip: int = 0, num_datapoints: int = 0,
    #                               committer: bool = True,
    #                               workers: int = 1, ignore_globs=None, include_globs=None):
    #     """
    #     Returns the blame at every revision of interest. Index is a datetime, column per committer, with number of lines
    #     blamed to each committer at each timestamp as data.

    #     :param branch: (optional, default 'master') the branch to work in
    #     :param limit: (optional, default None), the maximum number of revisions to return, None for no limit
    #     :param skip: (optional, default None), the number of revisions to skip. Ex: skip=2 returns every other revision, None for no skipping.
    #     :param num_datapoints: (optional, default=None) if limit and skip are none, and this isn't, then num_datapoints evenly spaced revs will be used
    #     :param committer: (optional, defualt=True) true if committer should be reported, false if author
    #     :param ignore_globs: (optional, default=None) a list of globs to ignore, default none excludes nothing
    #     :param include_globs: (optinal, default=None) a list of globs to include, default of None includes everything.
    #     :param workers: (optional, default=1) integer, the number of workers to use in the threadpool, -1 for one per core.
    #     :return: DataFrame

    #     """

    #     if not _has_joblib:
    #         raise ImportError('''Must have joblib installed to use parallel_cumulative_blame(), please use
    #         cumulative_blame() instead.''')

    #     revs = self.revs(branch=branch, limit=limit, skip=skip,
    #                      num_datapoints=num_datapoints)

    #     if self.verbose:
    #         print('Beginning processing for cumulative blame:')

    #     revisions = json.loads(revs.to_json(orient='index'))
    #     revisions = [revisions[key] for key in revisions]

    #     ds = Parallel(n_jobs=workers, backend='threading', verbose=5)(
    #         delayed(_parallel_cumulative_blame_func)
    #         (self, x, committer, ignore_globs, include_globs) for x in revisions
    #     )

    #     revs = DataFrame(ds)
    #     del revs['rev']

    #     revs['date'] = to_datetime(
    #         revs['date'].map(datetime.datetime.fromtimestamp))
    #     revs.set_index(keys=['date'], drop=True, inplace=True)
    #     revs = revs.fillna(0.0)

    #     # drop 0 cols
    #     for col in revs.columns.values:
    #         if col != 'col':
    #             if revs[col].sum() == 0:
    #                 del revs[col]

    #     # drop 0 rows
    #     keep_idx = []
    #     committers = [x for x in revs.columns.values if x != 'date']
    #     for idx, row in revs.iterrows():
    #         if sum([row[x] for x in committers]) > 0:
    #             keep_idx.append(idx)

    #     revs = revs.ix[keep_idx]
    #     revs.sort_index(ascending=False, inplace=True)

    #     return revs

    def branches(self):
        """
        Returns a data frame of all branches in origin.  The DataFrame will have the columns:

         * repository
         * branch
         * local

        :returns: DataFrame
        """

        # first pull the local branches
        local_branches = self.repo.branches
        data = [[x.name, True] for x in list(local_branches)]

        # then the remotes
        remote_branches_list: List[str] = self.repo.git.branch(all=True).split('\n')
        remote_branches = {x.split('/')[-1]
                           for x in remote_branches_list if 'remotes' in x}

        data += [[x, False] for x in remote_branches]

        df = DataFrame(data, columns=['branch', 'local'])
        df['repository'] = self._repo_name()

        return df

    def tags(self):
        """
        Returns a data frame of all tags in origin.  The DataFrame will have the columns:

         * repository
         * tag

        :returns: DataFrame
        """

        tags = self.repo.tags
        df = DataFrame([x.name for x in list(tags)], columns=['tag'])
        df['repository'] = self._repo_name()

        return df

    @property
    def repo_name(self):
        return self._repo_name()

    def _repo_name(self) -> str:
        """
        Returns the name of the repository, using the local directory name.
        :returns: str
        """
        if self._git_repo_name:
            return self._git_repo_name
        else:
            reponame = str(self.repo.git_dir).split(os.sep)[-2]
            if reponame.strip() == '':
                return 'unknown_repo'
            return reponame

    def __str__(self):
        """
        A pretty name for the repository object.

        :returns: str
        """
        return 'git repository: %s at: %s' % (self._repo_name(), self.git_dir,)

    def __repr__(self):
        """
        A unique name for the repository object.

        :returns: str
        """
        return str(self.git_dir)

    def bus_factor(self, by='repository', ignore_globs=None, include_globs=None):
        """
        An experimental heuristic for truck factor of a repository calculated by the current distribution of blame in
        the repository's primary branch.  The factor is the fewest number of contributors whose contributions make up at
        least 50% of the codebase's LOC

        :param ignore_globs: (optional, default=None) a list of globs to ignore, default none excludes nothing
        :param include_globs: (optinal, default=None) a list of globs to include, default of None includes everything.
        :param by: (optional, default=repository) whether to group by repository or by file
        :return:
        """

        if by == 'file':
            raise NotImplementedError('File-wise bus factor')

        blame = self.blame(include_globs=include_globs,
                           ignore_globs=ignore_globs, by=by)
        blame = blame.sort_values(by=['loc'], ascending=False)

        total = blame['loc'].sum()
        cumulative = 0
        tc = 0
        for idx in range(blame.shape[0]):
            cumulative += blame.ix[idx, 'loc']
            tc += 1
            if cumulative >= total / 2:
                break

        return DataFrame([[self._repo_name(), tc]], columns=['repository', 'bus factor'])

    def file_owner(self, rev, filename, committer=True):
        """
        Returns the owner (by majority blame) of a given file in a given rev. Returns the committers' name.

        :param rev:
        :param filename:
        :param committer:
        """
        try:
            if committer:
                cm = 'committer'
            else:
                cm = 'author'

            blame = self.repo.blame(rev, os.path.join(self.git_dir, filename))
            blame = DataFrame([[x[0].committer.name, len(x[1])] for x in blame], columns=[cm, 'loc']).groupby(cm).agg(
                {'loc': np.sum})
            if blame.shape[0] > 0:
                return blame['loc'].idxmax()
            else:
                return None
        except (GitCommandError, KeyError):
            if self.verbose:
                print('Couldn\'t Calcualte File Owner for %s' % (rev,))
            return None

    def _file_last_edit(self, filename):
        """

        :param filename:
        :return:
        """

        tmp = self.repo.git.log('-n 1 -- %s' % (filename,)).split('\n')
        date_string = [x for x in tmp if x.startswith('Date:')]

        if len(date_string) > 0:
            return date_string[0].replace('Date:', '').strip()
        else:
            return None

    @multicache(
        key_prefix='file_detail',
        key_list=['include_globs', 'ignore_globs', 'rev', 'committer'],
        skip_if=lambda x: True if x.get(
            'rev') is None or x.get('rev') == 'HEAD' else False
    )
    def file_detail(self, include_globs=None, ignore_globs=None, rev='HEAD', committer=True):
        """
        Returns a table of all current files in the repos, with some high level information about each file (total LOC,
        file owner, extension, most recent edit date, etc.).

        :param ignore_globs: (optional, default=None) a list of globs to ignore, default none excludes nothing
        :param include_globs: (optinal, default=None) a list of globs to include, default of None includes everything.
        :param committer: (optional, default=True) true if committer should be reported, false if author
        :return:
        """

        # first get the blame
        blame = self.blame(
            include_globs=include_globs,
            ignore_globs=ignore_globs,
            rev=rev,
            committer=committer,
            by='file'
        )
        blame = blame.reset_index(level=1)
        blame = blame.reset_index(level=1)

        # reduce it to files and total LOC
        df = blame.reindex(columns=['file', 'loc'])
        df = df.groupby('file').agg({'loc': np.sum})
        df = df.reset_index(level=1)

        # map in file owners
        df['file_owner'] = df['file'].map(
            lambda x: self.file_owner(rev, x, committer=committer))

        # add extension (something like the language)
        df['ext'] = df['file'].map(lambda x: x.split('.')[-1])

        # add in last edit date for the file
        df['last_edit_date'] = df['file'].map(self._file_last_edit)
        df['last_edit_date'] = to_datetime(df['last_edit_date'])

        df = df.set_index('file')

        return df

    def punchcard(self, branch: str = 'master', limit: int = 0, days: int = 0, by: str = '', normalize=None, ignore_globs=None,
                  include_globs=None):
        """
        Returns a pandas DataFrame containing all of the data for a punchcard.

         * day_of_week
         * hour_of_day
         * author / committer
         * lines
         * insertions
         * deletions
         * net

        :param branch: the branch to return commits for
        :param limit: (optional, default=None) a maximum number of commits to return, None for no limit
        :param days: (optional, default=None) number of days to return, if limit is None
        :param by: (optional, default=None) agg by options, None for no aggregation (just a high level punchcard), or 'committer', 'author'
        :param normalize: (optional, default=None) if an integer, returns the data normalized to max value of that (for plotting)
        :param ignore_globs: (optional, default=None) a list of globs to ignore, default none excludes nothing
        :param include_globs: (optinal, default=None) a list of globs to include, default of None includes everything.
        :return: DataFrame
        """

        ch = self.commit_history(
            branch=branch,
            limit=limit,
            days=days,
            ignore_globs=ignore_globs,
            include_globs=include_globs
        )

        # add in the date fields
        ch['day_of_week'] = ch.index.map(lambda x: x.weekday())
        ch['hour_of_day'] = ch.index.map(lambda x: x.hour)

        aggs = ['hour_of_day', 'day_of_week']
        if by:
            aggs.append(by)

        punch_card = ch.groupby(aggs).agg({
            'lines': np.sum,
            'insertions': np.sum,
            'deletions': np.sum,
            'net': np.sum
        })
        punch_card.reset_index(inplace=True)

        # normalize all cols
        if normalize is not None:
            for col in ['lines', 'insertions', 'deletions', 'net']:
                punch_card[col] = (punch_card[col] /
                                   punch_card[col].sum()) * normalize

        return punch_card


class GitFlowRepository(Repository):
    """
    A special case where git flow is followed, so we know something about the branching scheme
    """

    def __init__(self):
        super(GitFlowRepository, self).__init__()
