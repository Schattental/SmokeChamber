# GitHub upload and read-only Pi connection

This guide covers source upload and read-only device access. The public repository
is <https://github.com/Schattental/SmokeChamber>, with `main` as its default branch.
See [website updates](UPDATES.md) for the dashboard update button and service setup.

## 1. Choose the destination

Choose a GitHub owner, repository name, and public/private visibility. If creating
a new repository for this folder, leave it empty: do not initialize it with a
README, license, or gitignore because these files are prepared locally.
If the destination already has commits, inspect them and reconcile histories
before pushing. Do not force-push over an existing repository.

Only source and supporting files should be uploaded. Review the prepared list:

```sh
cd /home/smoke/smokechamber
git status --short
git diff --cached --stat
git ls-files
```

The repository excludes `env/`, recordings in `data/` and `Files/`, logs,
credentials, caches, and local settings. `.gitignore` does not remove any files
from the Pi, and it does not protect a file that someone explicitly force-adds.
The current private-LAN sensor addresses and serial device paths remain in the
source. The original application is available under PolyForm Noncommercial
1.0.0 in `LICENSE`, with required attribution in `NOTICE`. These terms restrict
commercial use and include explicit permissions for the organizations listed in
the license. Include these files and preserve the third-party notices when
uploading or redistributing the source.

## 2. Initial commit and upload use maintainer credentials

The Pi's eventual read-only credential cannot upload this first commit. Use a
maintainer's authenticated GitHub connection, or a trusted workstation/temporary
SSH agent forwarding session with repository write permission. Do not give the
device's deploy key write access to bootstrap the repository.

Set an author name/email locally if not already configured. A GitHub noreply
email can be used. Replace all uppercase placeholders below before running:

```sh
git config --local user.name "YOUR NAME"
git config --local user.email "YOUR GITHUB COMMIT EMAIL"
git add .
git diff --cached --stat
git commit -m "Import smoke chamber application and deployment documentation"
git remote add origin git@github.com:OWNER/REPOSITORY.git
git push -u origin main
```

The local repository is already initialized on `main`. If `origin` already
exists, inspect it with `git remote -v` instead of adding it again. The push
requires a maintainer's authenticated SSH connection. An authenticated HTTPS
remote is another upload option; do not put tokens in URLs, files, or chat.

After uploading, confirm on GitHub that the source files are present and that
recordings, logs, the virtual environment, and private credentials are absent.

## 3. Connect the Pi for reads

### Public repository: no key required

Use an HTTPS fetch URL to let the device read without stored credentials:

```sh
git remote set-url origin https://github.com/OWNER/REPOSITORY.git
git ls-remote --exit-code origin refs/heads/main
git fetch origin
```

Changing the local fetch URL does not change repository visibility or permissions.
Do not make a private repository public merely to avoid configuring credentials.

### Private repository: dedicated read-only deploy key

On the Pi, as `smoke`, generate a new dedicated key **only if that path does not
already exist**. Do not overwrite an existing key. The empty passphrase allows
future unattended reads; keep the private file readable only by its owner.

```sh
ssh-keygen -t ed25519 -f /home/smoke/.ssh/smokechamber_github_readonly -C "smokechamber Pi read-only" -N ''
chmod 600 /home/smoke/.ssh/smokechamber_github_readonly
cat /home/smoke/.ssh/smokechamber_github_readonly.pub
```

On GitHub, open this repository's **Settings → Deploy keys → Add deploy key**.
Use a title such as `Smoke chamber Pi`, paste only the `.pub` file contents,
and leave **Allow write access unchecked**. Do not add the private file to GitHub
or register this key as a broad personal-account SSH key. Organization policy
may restrict deploy keys; resolve that with the repository administrator.

Before using unattended strict host verification, establish GitHub's host key
with an interactive SSH connection. Verify the displayed fingerprint against
[GitHub's published fingerprints](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints)
before accepting it:

```sh
ssh -T -i /home/smoke/.ssh/smokechamber_github_readonly -o IdentitiesOnly=yes git@github.com
```

GitHub's successful authentication message also says it does not provide shell
access; the SSH command may exit with status 1 even when authentication succeeds.
Do not disable host-key verification to suppress errors.

Configure this repository alone to use the dedicated key for subsequent reads:

```sh
cd /home/smoke/smokechamber
git remote set-url origin git@github.com:OWNER/REPOSITORY.git
git config --local core.sshCommand 'ssh -i /home/smoke/.ssh/smokechamber_github_readonly -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes'
git ls-remote --exit-code origin refs/heads/main
git fetch origin
```

Check that GitHub lists the deploy key as read-only. `ls-remote` and `fetch`
verify read access without replacing the working files or restarting the service.
Do not test permissions by attempting an upload. The local SSH command applies
to pushes too; ongoing development uploads should use the maintainer connection
or a separate development checkout.

## 4. Website updater

The website's **Update from GitHub** button fetches the public `main` branch
without credentials and uses a separate systemd service to install updates and
recover from failed startup. See [UPDATES.md](UPDATES.md) for installation,
collection checks, local configuration, and recovery. No periodic update job is
installed; an operator starts updates explicitly from the website.

References: [GitHub's local code upload guide](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github?platform=linux)
and [deploy key guide](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys).
