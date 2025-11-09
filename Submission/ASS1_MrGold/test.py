import sys
import unittest

try:
    from colorama import Fore, Style, init as colorama_init
except ImportError:
    colorama_init = lambda **_: None  # type: ignore[assignment]

    class _Fore:
        GREEN = "\033[32m"
        RED = "\033[31m"

    class _Style:
        RESET_ALL = "\033[0m"

    Fore = _Fore()  # type: ignore[assignment]
    Style = _Style()  # type: ignore[assignment]


class ColoredTextTestResult(unittest.TextTestResult):
    def _write_outcome(self, test, color, message, dot_symbol):
        if self.showAll:
            desc = self.getDescription(test)
            self.stream.writeln(f"{color}{desc}{Style.RESET_ALL} ... {message}")
        elif self.dots:
            self.stream.write(dot_symbol)

    def _quiet_super_call(self, method, *args, **kwargs):
        original_show = self.showAll
        original_dots = self.dots
        self.showAll = False
        self.dots = False
        try:
            method(*args, **kwargs)
        finally:
            self.showAll = original_show
            self.dots = original_dots

    def startTest(self, test):
        unittest.result.TestResult.startTest(self, test)

    def addSuccess(self, test):
        self._quiet_super_call(super().addSuccess, test)
        self._write_outcome(test, Fore.GREEN, "ok", ".")

    def addFailure(self, test, err):
        self._quiet_super_call(super().addFailure, test, err)
        self._write_outcome(test, Fore.RED, "FAIL", "F")

    def addError(self, test, err):
        self._quiet_super_call(super().addError, test, err)
        self._write_outcome(test, Fore.RED, "ERROR", "E")

    def addSkip(self, test, reason):
        self._quiet_super_call(super().addSkip, test, reason)
        message = f"skipped {reason!r}"
        self._write_outcome(test, Fore.GREEN, message, "s")

    def addExpectedFailure(self, test, err):
        self._quiet_super_call(super().addExpectedFailure, test, err)
        self._write_outcome(test, Fore.GREEN, "expected failure", "x")

    def addUnexpectedSuccess(self, test):
        self._quiet_super_call(super().addUnexpectedSuccess, test)
        self._write_outcome(test, Fore.RED, "unexpected success", "u")


class ColoredTextTestRunner(unittest.TextTestRunner):
    resultclass = ColoredTextTestResult


def main() -> int:
    colorama_init()

    class ColoredStream:
        def __init__(self, base):
            self.base = base

        def write(self, message):
            self.base.write(message)

        def flush(self):
            self.base.flush()

        def writeln(self, message=""):
            message = message or ""
            if message == "OK":
                self.base.write(f"{Fore.GREEN}{message}{Style.RESET_ALL}\n")
            elif message.startswith("FAILED"):
                self.base.write(f"{Fore.RED}{message}{Style.RESET_ALL}\n")
            else:
                self.base.write(f"{message}\n")

    loader = unittest.defaultTestLoader
    suite = loader.discover("tests")
    runner = ColoredTextTestRunner(stream=ColoredStream(sys.stdout), verbosity=2, buffer=False)
    result = runner.run(suite)

    if result.wasSuccessful():
        print(f"{Fore.GREEN}All tests passed successfully.{Style.RESET_ALL}")
        return 0
    print(f"{Fore.RED}Some tests failed.{Style.RESET_ALL}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
