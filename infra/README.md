# Cloud collection VM

Data collection takes ≈ 65 min per episode, which is too long and too memory-hungry for the 7.4 GB laptop.
These files run the same testbed on **one rented Ubuntu VM** (x86_64 or ARM64). Several isolated
stacks run in parallel, raw runs are pulled back, and episodes are built locally.

| File | Runs on | Purpose |
|---|---|---|
| `cloud-init.yaml` | VM (first boot) | installs Docker Engine + compose plugin, uv |
| `push.sh` | laptop | copies committed code (`git archive`) to the VM and runs `vm_setup.sh` |
| `vm_setup.sh` | VM | Python 3.12 venv, dependencies, sanity tests, suggested stack count |
| `bootstrap.sh` | VM | fallback when the provider did not apply cloud-init (used automatically by `vm_setup.sh` after 10 min) |
| `collect.sh` | VM | N parallel stacks share the scenario × seed list; then builds episodes |
| `pull.sh` | laptop | downloads `data/raw` + logs, builds episodes locally |

## Which (free) VM?

Free-tier terms checked 2026-10-12. They change often, so confirm on the provider's page.

| Option | What is free | Size to pick | Parallel stacks | Notes |
|---|---|---|---|---|
| **Oracle Cloud Always Free** | permanent Ampere A1 allowance, reportedly reduced to **2 OCPU / 12 GB** in 2026 (4 / 24 GB on some accounts) | `VM.Standard.A1.Flex`, all free OCPUs/RAM, Ubuntu 24.04 aarch64, 50–100 GB boot volume | 1 (2 OCPU) · 2 (4 OCPU) | zero cost, can run for weeks; new accounts sometimes see "out of host capacity" (retry or another availability domain); card needed for verification |
| **Google Cloud free trial** | **$300 credit, 90 days** | `n2-standard-8` (8 vCPU, 32 GB) Ubuntu 24.04 x86_64, 50 GB disk | 4 | ~150 episodes in ≈ 2 days ≈ $20 of credit at ~$0.4/h (check current pricing); **delete the VM when done** |
| AWS Free Plan | $100–200 credit, 6 months | `m7i.2xlarge` / `m7g.2xlarge` | 4 | works the same; smaller credit |

Sizing rule: **one stack per 2 vCPUs**. Each stack is capped at ≈ 1.1 GB RAM and uses ≈ 0.5–1.5 cores
depending on the scenario. More stacks than that adds CPU contention, which is measurement noise. Other
stacks on the same VM are recorded per episode in `host_context`, as on the laptop.

> Research note: the VM's hardware differs from the laptop's. Collect **all** episodes of a dataset on
> the same machine type, and record it. Do not mix laptop and VM episodes in one evaluation without
> reporting it.

## Step by step

### 1. SSH key (laptop, once)
```bash
ssh-keygen -t ed25519 -f ~/.ssh/logsentinel -C logsentinel   # Enter twice for no passphrase, or set one
cat ~/.ssh/logsentinel.pub                                    # paste this when creating the VM
```

### 2a. Oracle Cloud Always Free
1. Sign up at oracle.com/cloud/free and choose a home region close to you (it cannot be changed later).
2. Compute → Instances → **Create instance**.
   - Image: **Canonical Ubuntu 24.04** (aarch64). Shape: **Ampere → VM.Standard.A1.Flex**, set
     OCPUs/memory to your Always Free maximum (the console marks it "Always Free-eligible").
   - Networking: default VCN with a public IPv4 address.
   - SSH keys: paste `~/.ssh/logsentinel.pub`.
   - Advanced options → Management → **cloud-init script**: upload `infra/cloud-init.yaml`.
   - Boot volume: 50–100 GB (up to 200 GB is free in total).
3. Note the public IP. The login user is `ubuntu`.

### 2b. Google Cloud ($300 trial)
1. Activate the trial at cloud.google.com/free. Create a project. **Billing → Budgets & alerts:** add a
   budget (e.g. $50) with email alerts.
2. Compute Engine → VM instances → **Create instance**.
   - Machine: **n2-standard-8**. Boot disk: **Ubuntu 24.04 LTS (x86/64)**, 50 GB balanced PD.
   - Advanced → Management → **Automation / startup script**: GCP does not read cloud-init from that
     field. Instead, under **Metadata**, add key `user-data` with the contents of `infra/cloud-init.yaml`
     (Ubuntu images run cloud-init from it).
   - Security → Manage access → **Add SSH key**: `ubuntu:<contents of logsentinel.pub>`.
3. Note the external IP. The login user is `ubuntu`.

### 3. Push code and set up (laptop)
```bash
git commit ...                       # push.sh sends the committed HEAD only
bash infra/push.sh ubuntu@<VM_IP>    # waits for cloud-init, installs, runs a few tests
```

### 4. Collect (on the VM, detached)
```bash
ssh -i ~/.ssh/logsentinel ubuntu@<VM_IP>
cd ~/LogSentinel
# pilot: one episode per scenario
STACKS=1 SEEDS=0 nohup bash infra/collect.sh > data/collect.log 2>&1 &
# full dataset later (after thresholds are frozen), e.g. 4 stacks, seeds 0-15:
# STACKS=4 SEEDS=0-15 nohup bash infra/collect.sh > data/collect.log 2>&1 &
exit                                  # safe: the run continues
```
Progress: `ssh ... 'tail -n 20 ~/LogSentinel/data/collect.log ~/LogSentinel/data/collect_stack*.log'`.
Grafana/Prometheus are bound to localhost on the VM; tunnel if needed:
`ssh -i ~/.ssh/logsentinel -L 9090:127.0.0.1:9090 ubuntu@<VM_IP>`.

### 5. Pull results (laptop)
```bash
bash infra/pull.sh ubuntu@<VM_IP>    # data/raw + logs -> laptop, then builds episodes locally
.venv/Scripts/python.exe -m streamlit run dashboard/app.py
```

### 6. Stop paying
- **GCP / AWS:** *delete* the VM (stopping still bills the disk), or at least stop it between runs.
  Check the billing page afterwards.
- **Oracle Always Free:** nothing to do. Oracle may reclaim Always Free instances that sit idle for long
  periods, so pull your data after each run.

## Security notes
- Only SSH (22) needs to be reachable. All testbed ports bind to 127.0.0.1 on the VM.
- `push.sh` sends committed files only. Never commit credentials.
