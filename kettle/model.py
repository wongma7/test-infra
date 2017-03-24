# Copyright 2017 The Kubernetes Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.



import json
import zlib
import sqlite3
import time


class Database(object):
    """
    Store build and test result information, and support incremental updates to results.
    """

    DEFAULT_INCREMENTAL_TABLE = 'build_emitted'

    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.executescript('''
            create table if not exists build(gcs_path primary key, started_json, finished_json, finished_time);
            create table if not exists file(path string primary key, data);
            create table if not exists build_junit_grabbed(build_id integer primary key);
            ''')

    def commit(self):
        self.db.commit()

    def get_existing_builds(self, jobs_dir):
        """
        Return a set of (job, number) tuples indicating already present builds.

        A build is already present if it has a finished.json, or if it's older than
        five days with no finished.json.
        """
        jobs_like = jobs_dir + '%'
        builds_have_paths = self.db.execute(
            'select gcs_path from build'
            ' where gcs_path LIKE ?'
            ' and finished_json IS NOT NULL'
            ,
            (jobs_dir + '%',)).fetchall()
        path_tuple = lambda path: tuple(path[len(jobs_dir):].split('/')[-2:])
        builds_have = {path_tuple(path) for (path,) in builds_have_paths}
        for path, started_json in self.db.execute(
                'select gcs_path, started_json from build'
                ' where gcs_path LIKE ?'
                ' and started_json IS NOT NULL and finished_json IS NULL',
                (jobs_dir + '%',)):
            started = json.loads(started_json)
            if int(started['timestamp']) < time.time() - 60*60*24*5:
                # over 5 days old, no need to try looking for finished any more.
                builds_have.add(path_tuple(path))
        return builds_have

    ### make_db

    def insert_build(self, build_dir, started, finished):
        """
        Add a build with optional started and finished dictionaries to the database.
        """
        started_json = started and json.dumps(started, sort_keys=True)
        finished_json = finished and json.dumps(finished, sort_keys=True)
        if not self.db.execute('select 1 from build where gcs_path=? '
                'and started_json=? and finished_json=?',
                (build_dir, started_json, finished_json)).fetchone():
            self.db.execute('replace into build values(?,?,?,?)',
                 (build_dir, started_json, finished_json,
                  finished and finished.get('timestamp', None)))

    def get_builds_missing_junit(self):
        """
        Return (rowid, path) for each build that hasn't enumerated junit files.
        """
        return self.db.execute(
            'select rowid, gcs_path from build'
            ' where rowid not in (select build_id from build_junit_grabbed)'
        ).fetchall()

    def insert_build_junits(self, build_id, junits):
        """
        Insert a junit dictionary {gcs_path: contents} for a given build's rowid.
        """
        for path, data in junits.iteritems():
            self.db.execute('replace into file values(?,?)',
                            (path, buffer(zlib.compress(data, 9))))
        self.db.execute('insert into build_junit_grabbed values(?)', (build_id,))

    ### make_json

    def _init_incremental(self, table):
        """
        Create tables necessary for storing incremental emission state.
        """
        self.db.execute('create table if not exists %s(build_id integer primary key, gen)' % table)

    def get_builds(self, path='', min_started=None, incremental_table=DEFAULT_INCREMENTAL_TABLE):
        """
        Iterate through (buildid, gcs_path, started, finished) for each build under
        the given path that has not already been emitted.
        """
        self._init_incremental(incremental_table)
        results = self.db.execute(
            'select rowid, gcs_path, started_json, finished_json from build '
            'where gcs_path like ?'
            ' and finished_time >= ?' +
            ' and rowid not in (select build_id from %s)'
            ' order by finished_time' % incremental_table
        #   ' limit 10000'
            , (path + '%', min_started or 0)).fetchall()
        for rowid, path, started, finished in results:
            started = started and json.loads(started)
            finished = finished and json.loads(finished)
            yield rowid, path, started, finished

    def test_results_for_build(self, path):
        """
        Return a list of file data under the given path. Intended for JUnit artifacts.
        """
        results = []
        for dataz, in self.db.execute(
                'select data from file where path between ? and ?',
                (path, path + '\x7F')):
            data = zlib.decompress(dataz)
            if data:
                results.append(data)
        return results

    def reset_emitted(self, incremental_table=DEFAULT_INCREMENTAL_TABLE):
        self.db.execute('drop table if exists %s' % incremental_table)

    def insert_emitted(self, rows_emitted, incremental_table=DEFAULT_INCREMENTAL_TABLE):
        self._init_incremental(incremental_table)
        gen, = self.db.execute('select max(gen)+1 from %s' % incremental_table).fetchone()
        if not gen:
            gen = 0
        self.db.executemany('insert into %s values(?,?)' % incremental_table, ((row, gen) for row in rows_emitted))
        self.db.commit()
        return gen

