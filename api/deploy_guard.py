"""
Container entrypoint for the api: roll back a self-deploy that won't start,
then run the command (uvicorn by default).

Before restarting onto new code, src/self_deploy.py writes pending.json with
the previous commit. A successful startup deletes it. If the container has
started more than MAX_ATTEMPTS times with it still there, the new code keeps
crashing, so put the checkout back to the previous commit.

This file is baked into the image (not bind-mounted with src/), so broken code
in src/ can't break the rollback.
"""
import json
import os
import subprocess
import sys

STATE = "/var/lib/ki-deploy"
REPO = "/repo"
MAX_ATTEMPTS = 3


def _rollback(base: str) -> tuple[bool, str]:
    st = os.stat(REPO)
    kwargs = {"env": {**os.environ, "HOME": "/tmp"}}
    if os.geteuid() == 0 and st.st_uid != 0:
        kwargs.update(user=st.st_uid, group=st.st_gid, extra_groups=[])
    r = subprocess.run(["git", "-C", REPO, "reset", "--keep", base],
                       capture_output=True, text=True, timeout=60, **kwargs)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def main() -> None:
    pending = os.path.join(STATE, "pending.json")
    attempts_file = os.path.join(STATE, "attempts")
    if os.path.exists(pending):
        try:
            attempts = int(open(attempts_file).read().strip() or 0) + 1
        except (OSError, ValueError):
            attempts = 1
        with open(attempts_file, "w") as f:
            f.write(str(attempts))
        if attempts > MAX_ATTEMPTS:
            info = json.load(open(pending))
            ok, output = _rollback(info["base"])
            print(f"[deploy-guard] {info['head'][:7]} failed to start {MAX_ATTEMPTS} times; "
                  f"rollback to {info['base'][:7]} {'done' if ok else 'FAILED: ' + output}", flush=True)
            info.update(rollback_ok=ok, rollback_output=output)
            with open(os.path.join(STATE, "rolled-back.json"), "w") as f:
                json.dump(info, f)
            os.remove(pending)
            os.remove(attempts_file)

    args = sys.argv[1:]
    if args:
        os.execvp(args[0], args)


if __name__ == "__main__":
    main()
