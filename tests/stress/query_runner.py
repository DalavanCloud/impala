#!/usr/bin/env impala-python
#
# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

import logging
from multiprocessing import Value
import os
import re
from textwrap import dedent
from threading import current_thread
from time import sleep, time
from sys import maxint

from tests.stress.queries import QueryType
from tests.stress.util import create_and_start_daemon_thread, increment
from tests.util.thrift_util import op_handle_to_query_id

LOG = logging.getLogger(os.path.splitext(os.path.basename(__file__))[0])

# Metrics collected during the stress running process.
NUM_QUERIES_DEQUEUED = "num_queries_dequeued"
# The number of queries that were submitted to a query runner.
NUM_QUERIES_SUBMITTED = "num_queries_submitted"
# The number of queries that have entered the RUNNING state (i.e. got through Impala's
# admission control and started executing) or were cancelled or hit an error.
NUM_QUERIES_STARTED_RUNNING_OR_CANCELLED = "num_queries_started_running_or_cancelled"
NUM_QUERIES_FINISHED = "num_queries_finished"
NUM_QUERIES_EXCEEDED_MEM_LIMIT = "num_queries_exceeded_mem_limit"
NUM_QUERIES_AC_REJECTED = "num_queries_ac_rejected"
NUM_QUERIES_AC_TIMEDOUT = "num_queries_ac_timedout"
NUM_QUERIES_CANCELLED = "num_queries_cancelled"
NUM_RESULT_MISMATCHES = "num_result_mismatches"
NUM_OTHER_ERRORS = "num_other_errors"

RESULT_HASHES_DIR = "result_hashes"


class QueryTimeout(Exception):
  pass


class QueryRunner(object):
  """Encapsulates functionality to run a query and provide a runtime report."""

  SPILLED_PATTERNS = [re.compile("ExecOption:.*Spilled"), re.compile("SpilledRuns: [^0]")]
  BATCH_SIZE = 1024

  def __init__(self, impalad, results_dir, use_kerberos, common_query_options,
               test_admission_control, check_if_mem_was_spilled=False):
    """Creates a new instance, but does not start the process. """
    self.impalad = impalad
    self.use_kerberos = use_kerberos
    self.results_dir = results_dir
    self.check_if_mem_was_spilled = check_if_mem_was_spilled
    self.common_query_options = common_query_options
    self.test_admission_control = test_admission_control
    # proc is filled out by caller
    self.proc = None
    # impalad_conn is initialised in connect()
    self.impalad_conn = None

    # All these values are shared values between processes. We want these to be accessible
    # by the parent process that started this QueryRunner, for operational purposes.
    self._metrics = {
        NUM_QUERIES_DEQUEUED: Value("i", 0),
        NUM_QUERIES_SUBMITTED: Value("i", 0),
        NUM_QUERIES_STARTED_RUNNING_OR_CANCELLED: Value("i", 0),
        NUM_QUERIES_FINISHED: Value("i", 0),
        NUM_QUERIES_EXCEEDED_MEM_LIMIT: Value("i", 0),
        NUM_QUERIES_AC_REJECTED: Value("i", 0),
        NUM_QUERIES_AC_TIMEDOUT: Value("i", 0),
        NUM_QUERIES_CANCELLED: Value("i", 0),
        NUM_RESULT_MISMATCHES: Value("i", 0),
        NUM_OTHER_ERRORS: Value("i", 0)}

  def connect(self):
    """Connect to the server and start the query runner thread."""
    self.impalad_conn = self.impalad.impala.connect(impalad=self.impalad)

  def run_query(self, query, mem_limit_mb, run_set_up=False,
                timeout_secs=maxint, should_cancel=False, retain_profile=False):
    """Run a query and return an execution report. If 'run_set_up' is True, set up sql
    will be executed before the main query. This should be the case during the binary
    search phase of the stress test.
    If 'should_cancel' is True, don't get the query profile for timed out queries because
    the query was purposely cancelled by setting the query timeout too short to complete,
    rather than having some problem that needs to be investigated.
    """
    if not self.impalad_conn:
      raise Exception("connect() must first be called")

    timeout_unix_time = time() + timeout_secs
    report = QueryReport(query)
    try:
      with self.impalad_conn.cursor() as cursor:
        start_time = time()
        self._set_db_and_options(cursor, query, run_set_up, mem_limit_mb, timeout_secs)
        error = None
        try:
          cursor.execute_async(
              "/* Mem: %s MB. Coordinator: %s. */\n"
              % (mem_limit_mb, self.impalad.host_name) + query.sql)
          report.query_id = op_handle_to_query_id(cursor._last_operation.handle if
                                                  cursor._last_operation else None)
          LOG.debug("Query id is %s", report.query_id)
          if not self._wait_until_fetchable(cursor, report, timeout_unix_time,
                                            should_cancel):
            return report

          if query.query_type == QueryType.SELECT:
            try:
              report.result_hash = self._hash_result(cursor, timeout_unix_time, query)
              if retain_profile or \
                 query.result_hash and report.result_hash != query.result_hash:
                fetch_and_set_profile(cursor, report)
            except QueryTimeout:
              self._cancel(cursor, report)
              return report
          else:
            # If query is in error state, this will raise an exception
            cursor._wait_to_finish()
        except Exception as error:
          report.query_id = op_handle_to_query_id(cursor._last_operation.handle if
                                                  cursor._last_operation else None)
          LOG.debug("Error running query with id %s: %s", report.query_id, error)
          self._check_for_memory_errors(report, cursor, error)
        if report.has_query_error():
          return report
        report.runtime_secs = time() - start_time
        if cursor.execution_failed() or self.check_if_mem_was_spilled:
          fetch_and_set_profile(cursor, report)
          report.mem_was_spilled = any([
              pattern.search(report.profile) is not None
              for pattern in QueryRunner.SPILLED_PATTERNS])
          report.not_enough_memory = "Memory limit exceeded" in report.profile
    except Exception as error:
      # A mem limit error would have been caught above, no need to check for that here.
      report.other_error = error
    return report

  def _set_db_and_options(self, cursor, query, run_set_up, mem_limit_mb, timeout_secs):
    """Set up a new cursor for running a query by switching to the correct database and
    setting query options."""
    if query.db_name:
      LOG.debug("Using %s database", query.db_name)
      cursor.execute("USE %s" % query.db_name)
    if run_set_up and query.set_up_sql:
      LOG.debug("Running set up query:\n%s", query.set_up_sql)
      cursor.execute(query.set_up_sql)
    for query_option, value in self.common_query_options.iteritems():
      cursor.execute(
          "SET {query_option}={value}".format(query_option=query_option, value=value))
    for query_option, value in query.options.iteritems():
      cursor.execute(
          "SET {query_option}={value}".format(query_option=query_option, value=value))
    cursor.execute("SET ABORT_ON_ERROR=1")
    if self.test_admission_control:
      LOG.debug(
          "Running query without mem limit at %s with timeout secs %s:\n%s",
          self.impalad.host_name, timeout_secs, query.sql)
    else:
      LOG.debug("Setting mem limit to %s MB", mem_limit_mb)
      cursor.execute("SET MEM_LIMIT=%sM" % mem_limit_mb)
      LOG.debug(
          "Running query with %s MB mem limit at %s with timeout secs %s:\n%s",
          mem_limit_mb, self.impalad.host_name, timeout_secs, query.sql)

  def _wait_until_fetchable(self, cursor, report, timeout_unix_time, should_cancel):
    """Wait up until timeout_unix_time until the query results can be fetched (if it's
    a SELECT query) or until it has finished executing (if it's a different query type
    like DML). If the timeout expires we either cancel the query or report the timeout.
    Return True in the first case or False in the second (timeout) case."""
    # Loop until the query gets to the right state or a timeout expires.
    sleep_secs = 0.1
    secs_since_log = 0
    # True if we incremented num_queries_started_running_or_cancelled for this query.
    started_running_or_cancelled = False
    while True:
      query_state = cursor.status()
      # Check if the query got past the PENDING/INITIALIZED states, either because
      # it's executing or hit an error.
      if (not started_running_or_cancelled and query_state not in ('PENDING_STATE',
                                                      'INITIALIZED_STATE')):
        started_running_or_cancelled = True
        increment(self._metrics[NUM_QUERIES_STARTED_RUNNING_OR_CANCELLED])
      # Return if we're ready to fetch results (in the FINISHED state) or we are in
      # another terminal state like EXCEPTION.
      if query_state not in ('PENDING_STATE', 'INITIALIZED_STATE', 'RUNNING_STATE'):
        return True

      if time() > timeout_unix_time:
        if not should_cancel:
          fetch_and_set_profile(cursor, report)
        self._cancel(cursor, report)
        if not started_running_or_cancelled:
          increment(self._metrics[NUM_QUERIES_STARTED_RUNNING_OR_CANCELLED])
        return False
      if secs_since_log > 5:
        secs_since_log = 0
        LOG.debug("Waiting for query to execute")
      sleep(sleep_secs)
      secs_since_log += sleep_secs

  def update_from_query_report(self, report):
    LOG.debug("Updating runtime stats (Query Runner PID: {0})".format(self.proc.pid))
    increment(self._metrics[NUM_QUERIES_FINISHED])
    if report.not_enough_memory:
      increment(self._metrics[NUM_QUERIES_EXCEEDED_MEM_LIMIT])
    if report.ac_rejected:
      increment(self._metrics[NUM_QUERIES_AC_REJECTED])
    if report.ac_timedout:
      increment(self._metrics[NUM_QUERIES_AC_TIMEDOUT])
    if report.was_cancelled:
      increment(self._metrics[NUM_QUERIES_CANCELLED])

  def _cancel(self, cursor, report):
    report.timed_out = True

    if not report.query_id:
      return

    try:
      LOG.debug("Attempting cancellation of query with id %s", report.query_id)
      cursor.cancel_operation()
      LOG.debug("Sent cancellation request for query with id %s", report.query_id)
    except Exception as e:
      LOG.debug("Error cancelling query with id %s: %s", report.query_id, e)
      try:
        LOG.debug("Attempting to cancel query through the web server.")
        self.impalad.cancel_query(report.query_id)
      except Exception as e:
        LOG.debug("Error cancelling query %s through the web server: %s",
                  report.query_id, e)

  def _check_for_memory_errors(self, report, cursor, caught_exception):
    """To be called after a query failure to check for signs of failed due to a
    mem limit or admission control rejection/timeout. The report will be updated
    accordingly.
    """
    fetch_and_set_profile(cursor, report)
    caught_msg = str(caught_exception).lower().strip()
    # Distinguish error conditions based on string fragments. The AC rejection and
    # out-of-memory conditions actually overlap (since some memory checks happen in
    # admission control) so check the out-of-memory conditions first.
    if "memory limit exceeded" in caught_msg or \
       "repartitioning did not reduce the size of a spilled partition" in caught_msg or \
       "failed to get minimum memory reservation" in caught_msg or \
       "minimum memory reservation is greater than" in caught_msg or \
       "minimum memory reservation needed is greater than" in caught_msg:
      report.not_enough_memory = True
      return
    if "rejected query from pool" in caught_msg:
      report.ac_rejected = True
      return
    if "admission for query exceeded timeout" in caught_msg:
      report.ac_timedout = True
      return

    LOG.debug("Non-mem limit error for query with id %s: %s", report.query_id,
              caught_exception, exc_info=True)
    report.other_error = caught_exception

  def _hash_result(self, cursor, timeout_unix_time, query):
    """Returns a hash that is independent of row order. 'query' is only used for debug
    logging purposes (if the result is not as expected a log file will be left for
    investigations).
    """
    query_id = op_handle_to_query_id(cursor._last_operation.handle if
                                     cursor._last_operation else None)

    # A value of 1 indicates that the hash thread should continue to work.
    should_continue = Value("i", 1)

    def hash_result_impl():
      result_log = None
      try:
        file_name = '_'.join([query.logical_query_id, query_id.replace(":", "_")])
        if query.result_hash is None:
          file_name += "_initial"
        file_name += "_results.txt"
        result_log = open(os.path.join(self.results_dir, RESULT_HASHES_DIR, file_name),
                          "w")
        result_log.write(query.sql)
        result_log.write("\n")
        current_thread().result = 1
        while should_continue.value:
          LOG.debug(
              "Fetching result for query with id %s",
              op_handle_to_query_id(
                  cursor._last_operation.handle if cursor._last_operation else None))
          rows = cursor.fetchmany(self.BATCH_SIZE)
          if not rows:
            LOG.debug(
                "No more results for query with id %s",
                op_handle_to_query_id(
                    cursor._last_operation.handle if cursor._last_operation else None))
            return
          for row in rows:
            for idx, val in enumerate(row):
              if val is None:
                # The hash() of None can change from run to run since it's based on
                # a memory address. A chosen value will be used instead.
                val = 38463209
              elif isinstance(val, float):
                # Floats returned by Impala may not be deterministic, the ending
                # insignificant digits may differ. Only the first 6 digits will be used
                # after rounding.
                sval = "%f" % val
                dot_idx = sval.find(".")
                val = round(val, 6 - dot_idx)
              current_thread().result += (idx + 1) * hash(val)
              # Modulo the result to keep it "small" otherwise the math ops can be slow
              # since python does infinite precision math.
              current_thread().result %= maxint
              if result_log:
                result_log.write(str(val))
                result_log.write("\t")
                result_log.write(str(current_thread().result))
                result_log.write("\n")
      except Exception as e:
        current_thread().error = e
      finally:
        if result_log is not None:
          result_log.close()
          if (
              current_thread().error is not None and
              current_thread().result == query.result_hash
          ):
            os.remove(result_log.name)

    hash_thread = create_and_start_daemon_thread(
        hash_result_impl, "Fetch Results %s" % query_id)
    hash_thread.join(max(timeout_unix_time - time(), 0))
    if hash_thread.is_alive():
      should_continue.value = 0
      raise QueryTimeout()
    if hash_thread.error:
      raise hash_thread.error
    return hash_thread.result

  def get_metric_val(self, name):
    """Get the current value of the metric called 'name'."""
    return self._metrics[name].value

  def get_metric_vals(self):
    """Get the current values of the all metrics as a list of (k, v) pairs."""
    return [(k, v.value) for k, v in self._metrics.iteritems()]

  def increment_metric(self, name):
    """Increment the current value of the metric called 'name'."""
    increment(self._metrics[name])


class QueryReport(object):
  """Holds information about a single query run."""

  def __init__(self, query):
    self.query = query

    self.result_hash = None
    self.runtime_secs = None
    self.mem_was_spilled = False
    # not_enough_memory includes conditions like "Memory limit exceeded", admission
    # control rejecting because not enough memory, etc.
    self.not_enough_memory = False
    # ac_rejected is true if the query was rejected by admission control.
    # It is mutually exclusive with not_enough_memory - if the query is rejected by
    # admission control because the memory limit is too low, it is counted as
    # not_enough_memory.
    # TODO: reconsider whether they should be mutually exclusive
    self.ac_rejected = False
    self.ac_timedout = False
    self.other_error = None
    self.timed_out = False
    self.was_cancelled = False
    self.profile = None
    self.query_id = None

  def __str__(self):
    return dedent("""
        <QueryReport
        result_hash: %(result_hash)s
        runtime_secs: %(runtime_secs)s
        mem_was_spilled: %(mem_was_spilled)s
        not_enough_memory: %(not_enough_memory)s
        ac_rejected: %(ac_rejected)s
        ac_timedout: %(ac_timedout)s
        other_error: %(other_error)s
        timed_out: %(timed_out)s
        was_cancelled: %(was_cancelled)s
        query_id: %(query_id)s
        >
        """.strip() % self.__dict__)

  def has_query_error(self):
    """Return true if any kind of error status was returned from the query (i.e.
    the query didn't run to completion, time out or get cancelled)."""
    return (self.not_enough_memory or self.ac_rejected or self.ac_timedout
            or self.other_error)

  def write_query_profile(self, directory, prefix=None):
    """
    Write out the query profile bound to this object to a given directory.

    The file name is generated and will contain the query ID. Use the optional prefix
    parameter to set a prefix on the filename.

    Example return:
      tpcds_300_decimal_parquet_q21_00000001_a38c8331_profile.txt

    Parameters:
      directory (str): Directory to write profile.
      prefix (str): Prefix for filename.
    """
    if not (self.profile and self.query_id):
      return
    if prefix is not None:
      file_name = prefix + '_'
    else:
      file_name = ''
    file_name += self.query.logical_query_id + '_'
    file_name += self.query_id.replace(":", "_") + "_profile.txt"
    profile_log_path = os.path.join(directory, file_name)
    with open(profile_log_path, "w") as profile_log:
      profile_log.write(self.profile)


def fetch_and_set_profile(cursor, report):
  """Set the report's query profile using the given cursor.
  Producing a query profile can be somewhat expensive. A v-tune profile of
  impalad showed 10% of cpu time spent generating query profiles.
  """
  if not report.profile and cursor._last_operation:
    try:
      report.profile = cursor.get_profile()
    except Exception as e:
      LOG.debug("Error getting profile for query with id %s: %s", report.query_id, e)
