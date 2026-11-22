"""pytest plugin: record failed/passed node ids to $OUTCOME_OUT (dev harness)."""
import json, os
FAILED, PASSED, COLLERR = set(), set(), []
def pytest_runtest_logreport(report):
    if report.failed:
        FAILED.add(report.nodeid)
    elif report.when == "call" and report.passed:
        PASSED.add(report.nodeid)
def pytest_collectreport(report):
    if report.failed:
        COLLERR.append(report.nodeid)
def pytest_sessionfinish(session, exitstatus):
    with open(os.environ["OUTCOME_OUT"], "w") as f:
        json.dump({"failed": sorted(FAILED), "passed": sorted(PASSED), "collerr": COLLERR, "exit": int(exitstatus)}, f)
