"""
Launches the AI_prep interview-practice web app on 127.0.0.1:8006 for the
portal, without touching the AI_prep project. Its own run() binds 0.0.0.0 on
WEB_PORT from the shared .env (8765, a name owned by another project), so we
build the app via its create_app() factory and serve it ourselves on the
PORT_AI_PREP convention port.

Run:  conda run -n p312 python run_aiprep.py
"""
import os
import sys

AIPREP_DIR = os.path.expanduser("~/Documents/AI_prep")

os.chdir(AIPREP_DIR)  # python-dotenv walks up from CWD; data paths are relative
sys.path.insert(0, AIPREP_DIR)

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from interview_practice.web.app import create_app  # noqa: E402

app = create_app()

if __name__ == "__main__":
    app.run(host=os.environ.get("BIND_HOST", "127.0.0.1"), port=8006,
            debug=False, threaded=True)
