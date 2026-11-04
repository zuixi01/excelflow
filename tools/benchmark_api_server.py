"""HTTP benchmark launcher with optional process/thread diagnostics."""
import faulthandler
import os

import uvicorn

if os.environ.get("BENCHMARK_DEBUG") == "1":
    faulthandler.enable()
    faulthandler.dump_traceback_later(30, repeat=True)

if __name__ == "__main__":
    uvicorn.run("excel_auditor.api:app", host="127.0.0.1", port=int(os.environ["BENCHMARK_PORT"]),
                workers=int(os.environ.get("BENCHMARK_API_WORKERS", "2")))
