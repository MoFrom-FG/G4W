import os
import unittest

from G4W.cli.main import _pid_is_running


class ProcessControlTests(unittest.TestCase):
    def test_current_process_check_is_non_destructive(self):
        self.assertTrue(_pid_is_running(os.getpid()))
        self.assertTrue(_pid_is_running(os.getpid()))

    def test_invalid_pid_is_not_running(self):
        self.assertFalse(_pid_is_running(0))


if __name__ == "__main__":
    unittest.main()
