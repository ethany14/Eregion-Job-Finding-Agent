"""Browser automation driver behind ApplicationBrowser: Playwright (default) or Patchright.

JOB_AGENT_BROWSER_DRIVER selects it; both expose the same sync API. Only the launch layer
changes: field handling, model decisions, budgets and hand-offs are driver-independent.
A selected driver that is not installed is an explicit error, never a silent switch, and
changing the setting back is the rollback."""
import os

DRIVERS = ('playwright', 'patchright')


def driver_name():
    name = (os.getenv('JOB_AGENT_BROWSER_DRIVER') or 'playwright').strip().casefold()
    if name not in DRIVERS:
        raise ValueError(f"JOB_AGENT_BROWSER_DRIVER must be one of {', '.join(DRIVERS)}, not {name!r}.")
    return name


def sync_api(name):
    # Plain import statements (not importlib) so import hooks, e.g. a test simulating a missing
    # dependency, apply exactly as they did to the previous direct Playwright import.
    if name == 'patchright':
        import patchright.sync_api as api
    else:
        import playwright.sync_api as api
    return api


def is_driver_error(error):
    """A failure raised by either driver (their exception classes live in different packages)."""
    return type(error).__module__.split('.', 1)[0] in DRIVERS
