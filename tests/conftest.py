"""Point every test at a throwaway state dir and dashboard dir.

The probe derives each state path (SPECIAL_LOG, MARKS, MEETINGS, ...) from STATE
at import time, and most test files import their own copy of the module. A test
that patches only `wp.STATE` therefore still writes to the real files -- the
real special.jsonl got an `off` row after every `end_session` test, switching
special time back to main within seconds of it being turned on. Setting the
environment here, before any test module is collected, makes the leak
impossible rather than something each test has to remember.
"""

import os
import tempfile

_ROOT = tempfile.mkdtemp(prefix="worktime-test-")
os.environ["WORKTIME_STATE"] = os.path.join(_ROOT, "state")
os.environ["WORKTIME_DASHBOARD"] = os.path.join(_ROOT, "dashboard")
os.makedirs(os.environ["WORKTIME_STATE"])
os.makedirs(os.environ["WORKTIME_DASHBOARD"])
