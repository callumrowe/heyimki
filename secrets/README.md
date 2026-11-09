# secrets/

Optional files that are mounted read-only into the containers. Everything in
this directory except this README is gitignored.

| File | Used by | When you need it |
|---|---|---|
| `vault_deploy_key` | api | Your vault is a private git repo cloned over SSH, and you want Ki to pull/push it |

Generate a deploy key with `ssh-keygen -t ed25519 -f secrets/vault_deploy_key -N ""`,
then add `secrets/vault_deploy_key.pub` as a deploy key (with write access) on the repo.
