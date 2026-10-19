import os
import tempfile

# Safety net: any test (or code under test) that forgets to set TESTDESK_DB
# writes to this throwaway file, never to the real TestDesk/data/testdesk.sqlite.
os.environ.setdefault("TESTDESK_DB", os.path.join(tempfile.mkdtemp(prefix="testdesk_tests_"), "default.sqlite"))
