"""
Launches the arcade app on 127.0.0.1:8001 for the portal, WITHOUT touching
the arcade project. arcade/app.py hardcodes app.run(0.0.0.0:5000, debug=True)
in its __main__ block; importing the module instead gives us the bare Flask
object, which we serve ourselves — localhost-only, debugger off. gunicorn
would do the same job but isn't installed in the p312 env.

Run:  conda run -n p312 python run_arcade.py
"""
import os
import sys

ARCADE_DIR = os.path.expanduser("~/Documents/arcade")

# arcade/app.py calls load_dotenv() with no path (reads .env from the CWD)
# and serves its games with paths relative to its own directory.
os.chdir(ARCADE_DIR)
sys.path.insert(0, ARCADE_DIR)

from app import app  # noqa: E402

if __name__ == "__main__":
    app.run(host=os.environ.get("BIND_HOST", "127.0.0.1"), port=8001,
            debug=False, threaded=True)
