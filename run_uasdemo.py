"""
Serves the AI Ventures drone-tracking demo on 127.0.0.1:8012 for the portal,
WITHOUT touching that project: it imports the app from
ai-ventures/uas_demo/uas_demo_web.py (the built demo page, the proposal PDFs,
and "track your own video": /submit, /api/jobs, /jobs/<id>/...), the same way
run_arcade.py imports arcade's Flask app.

The GPU work for submitted videos is NOT done here: the uas-submit-worker user
unit (ai-ventures/uas_demo/systemd/) picks jobs up from uas_demo/submissions/.
Video files are served with werkzeug's send_file, which answers HTTP Range
requests (206), so MP4 seeking works through the proxy.

Run:  conda run -n p312 python run_uasdemo.py
"""
import argparse
import os
import sys

UAS_DEMO_DIR = os.path.expanduser("~/Documents/ai-ventures/uas_demo")
PORT = 8012
sys.path.insert(0, UAS_DEMO_DIR)

from uas_demo_web import create_app  # noqa: E402  (stdlib + Flask only; no torch in p312)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=PORT)
    args = ap.parse_args()
    create_app().run(host=os.environ.get("BIND_HOST", "127.0.0.1"), port=args.port,
                     debug=False, threaded=True)


if __name__ == "__main__":
    main()
