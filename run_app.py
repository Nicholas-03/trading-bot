import os
import runpy


mode = os.getenv("APP_MODE", "bot").strip().lower()

if mode == "analytics":
    runpy.run_module("analytics.server", run_name="__main__")
else:
    runpy.run_path("main.py", run_name="__main__")
