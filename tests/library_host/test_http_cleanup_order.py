"""Actual EOF precedes scope retirement; tests must wait for that real event."""
import queue
import threading
import unittest
from unittest.mock import patch

from tests.library_host import test_business_errors as business_errors
from tests.library_host.support import Raw


class CleanupOrderTests(unittest.TestCase):
    setUp=business_errors.BusinessErrorHttpTests.setUp
    finish=business_errors.BusinessErrorHttpTests.finish
    request=business_errors.BusinessErrorHttpTests.request
    call=business_errors.BusinessErrorHttpTests.call
    args=business_errors.BusinessErrorHttpTests.args

    def test_eof_call_waits_until_actual_request_scope_and_resources_retire(self):
        self.assertEqual(200,self.call(self.args())[0])
        completed=self.cleaned
        self.assertTrue(completed.is_set())  # deliberately retain the prior event
        events=queue.Queue();cleared_threads=[]
        class CompletionEvent:
            def clear(inner):cleared_threads.append(threading.get_ident());completed.clear()
            def set(inner):completed.set()
            def is_set(inner):return completed.is_set()
            def wait(inner,timeout=None):
                events.put('cleanup_wait')
                return completed.wait(timeout)
        self.cleaned=CompletionEvent()
        closing=threading.Event();release=threading.Event();cancelled=threading.Event()
        caller_done=threading.Event();caller_id=[];results=[];errors=[]
        original=Raw.close
        def held_close(raw):
            if raw.port is self.port and raw.db is not None and not raw.closed and cancelled.is_set():
                closing.set()
                if not release.wait(4):raise AssertionError('controlled close was not released')
            return original(raw)
        def cancel(raw,sql,params):
            if raw.db is not None and 'SELECT payload_hash,result_json' in sql:
                cancelled.set();self.host._state().guard.cancel()
        self.port.on_sql=cancel
        def caller():
            caller_id.append(threading.get_ident())
            try:results.append(self.call(dict(self.args(),value='Synthetic cancellation conflict.')))
            except BaseException as exc:errors.append(exc)
            finally:caller_done.set();events.put('caller_returned')
        thread=threading.Thread(target=caller,daemon=True)
        with patch.object(Raw,'close',held_close):
            thread.start()
            try:
                self.assertTrue(closing.wait(2),'actual close did not reach the controlled boundary')
                # The old helper reports caller_returned here; no sleep or
                # scheduler-duration assumption is used to distinguish them.
                self.assertEqual('cleanup_wait',events.get(timeout=2))
                self.assertIn(caller_id[0],cleared_threads,'caller did not clear the previous request event')
                self.assertFalse(caller_done.is_set())
                self.assertFalse(completed.is_set())
                self.assertTrue(any(c.db is not None and not c.closed for c in self.port.connections))
            finally:
                release.set()
                self.assertTrue(completed.wait(2),'real request scope never completed')
                thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual([],errors)
        self.assertEqual([(None,None)],results)
        self.assertTrue(all(c.closed for c in self.port.connections))
        self.assertEqual(0,self.host.active_requests)


if __name__=='__main__':unittest.main()
