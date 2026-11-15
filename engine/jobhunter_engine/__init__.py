"""JobHunter engine: сбор вакансий с 24+ площадок поверх JobSpy и dream-job-bot."""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.join(os.path.dirname(_HERE), "vendor")
for _p in (os.path.join(_VENDOR, "jobspy_repo"), os.path.join(_VENDOR, "dreamjob")):
    if _p not in sys.path:
        sys.path.insert(0, _p)
