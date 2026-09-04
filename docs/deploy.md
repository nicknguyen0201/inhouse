# Deployment

Two schedules, opposite requirements.

| | what | where | when |
|---|---|---|---|
| **Pipeline** | ingest → extract → load | GitHub Actions + the GPU instance | nightly, ~90 min |
| **Dashboard** | FastAPI reading Postgres | anywhere with a network | always on |

The pipeline needs a GPU for twenty minutes of ninety. The dashboard needs no
GPU at all. Keeping them separate is what makes the cost argument work.

---

## The nightly pipeline

`.github/workflows/nightly.yml` runs at 22:05 UTC, Tuesday to Saturday — filings
land after the US market closes and EDGAR publishes its daily index shortly
after.

### Why the job is split into three

The instance is $0.53/hour and is needed for one step of three:

```
ingest    ~60 min   GPU stopped    rate-limited at 8 req/s by the SEC
extract   ~22 min   GPU RUNNING    181 filings at 547 docs/hour
load       ~2 min   GPU stopped
```

Starting the GPU before the hour-long ingest would cost four times as much for
nothing. The stop step runs under `if: always()`, so a failure anywhere still
turns the meter off — that single line is the difference between $6/month and
$380/month.

### Credentials: OIDC, not stored keys

> Placeholders below (`<ACCOUNT_ID>`, `<BUCKET>`, `<INSTANCE_ID>`) are yours to
> fill in. They are not secrets, but they are reconnaissance -- an account id
> and a bucket name are where someone probing for misconfigurations starts, and
> the documentation reads the same without them.

The workflow assumes an IAM role using a token GitHub mints per run. Nothing
long-lived is stored, and the trust policy pins the role to this repository, so
a leaked workflow file grants nobody anything.

**1. Register GitHub as an identity provider** (once per AWS account):

IAM → Identity providers → Add provider → OpenID Connect

```
Provider URL:  https://token.actions.githubusercontent.com
Audience:      sts.amazonaws.com
```

**2. Create the role** with this trust policy, replacing the account id and repo:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": {
      "Federated": "arn:aws:iam::<ACCOUNT_ID>:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
      },
      "StringLike": {
        "token.actions.githubusercontent.com:sub": "repo:<OWNER>/<REPO>:*"
      }
    }
  }]
}
```

The `sub` condition is the security boundary. Without it, any GitHub repository
in the world could assume this role.

**3. Attach a permissions policy** scoped to exactly what the workflow does:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:ListBucket",
        "s3:DeleteObject"
      ],
      "Resource": [
        "arn:aws:s3:::<BUCKET>",
        "arn:aws:s3:::<BUCKET>/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "ec2:StartInstances",
        "ec2:StopInstances",
        "ec2:CreateTags"
      ],
      "Resource": "arn:aws:ec2:us-east-2:<ACCOUNT_ID>:instance/<INSTANCE_ID>"
    },
    {
      "Effect": "Allow",
      "Action": "ec2:DescribeInstances",
      "Resource": "*"
    }
  ]
}
```

`DeleteObject` is only for clearing the previous night's completion marker
before a run. Without it a stale marker makes the poll return immediately with
yesterday's status, which looks like success.

`CreateTags` is how the workflow tells the instance which date to extract: there
is no SSH session to pass an argument through, so it sets an `ExtractDate` tag
before starting the box and the script reads it back from instance metadata.

`DescribeInstances` cannot be resource-scoped — AWS does not support it — so it
is the one wildcard, and it only reads.

### Repository configuration

Settings → Secrets and variables → Actions.

**Variables** (not secret — they appear in logs anyway):

| name | example |
|---|---|
| `AWS_ROLE_ARN` | `arn:aws:iam::<ACCOUNT_ID>:role/inhouse-github-actions` |
| `GPU_INSTANCE_ID` | `<INSTANCE_ID>` |
| `STORAGE_URI` | `s3://<BUCKET>` |

**Secrets:**

| name | why |
|---|---|
| `SEC_USER_AGENT` | `Name email@example.com` — the SEC blocks requests without it |
| `GPU_SSH_KEY` | contents of the `.pem`, for the tunnel |
| `DATABASE_URL` | Supabase transaction pooler string |

### What runs on the instance

Two systemd units. The workflow starts the box and polls S3 — it never SSHes,
because a GitHub runner has no stable address and SGLang has no authentication,
so admitting one would mean opening port 22 to everyone.

**1. `sglang.service`** — the inference server, up on every boot.

```ini
# /etc/systemd/system/sglang.service
[Unit]
Description=SGLang inference server
After=network-online.target

[Service]
User=ubuntu
# systemd starts with a nearly empty environment. An interactive shell picks
# these up from the login profile, which is why launching by hand worked and
# the service did not: SGLang JIT-compiles a flashinfer kernel on the first
# forward pass and needs nvcc on PATH plus CUDA_HOME to find the toolkit.
Environment=CUDA_HOME=/opt/pytorch/cuda
Environment=PATH=/opt/pytorch/cuda/bin:/opt/pytorch/bin:/usr/local/bin:/usr/bin:/bin
Environment=LIBRARY_PATH=/opt/pytorch/lib/python3.13/site-packages/nvidia/cu13/lib:/opt/pytorch/cuda/lib
Environment=LD_LIBRARY_PATH=/opt/pytorch/lib/python3.13/site-packages/nvidia/cu13/lib:/opt/pytorch/cuda/lib
ExecStart=/opt/pytorch/bin/python -m sglang.launch_server \
  --model-path Qwen/Qwen2.5-7B-Instruct-AWQ \
  --host 0.0.0.0 --port 30000 \
  --mem-fraction-static 0.75 \
  --attention-backend triton \
  --enable-metrics
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

`--mem-fraction-static 0.75` rather than the 0.85 most examples use: at 0.85 the
grammar-constraint kernel had no room to load and the server died on the first
schema-constrained request, several minutes after appearing healthy.

**2. `nightly-extract.service`** — the job, which powers the machine off when
done. `Type=oneshot` so systemd knows it is a task rather than a daemon.

```ini
# /etc/systemd/system/nightly-extract.service
[Unit]
Description=Nightly 8-K extraction
After=network-online.target sglang.service
Wants=sglang.service

[Service]
Type=oneshot
User=ubuntu
Environment=STORAGE_URI=s3://<BUCKET>
Environment=REPO_DIR=/home/ubuntu/inhouse
ExecStart=/home/ubuntu/inhouse/scripts/nightly-extract.sh
TimeoutStartSec=3600

[Install]
WantedBy=multi-user.target
```

Install both:

```bash
git clone https://github.com/<OWNER>/<REPO>.git ~/inhouse
/opt/pytorch/bin/pip install -e ~/inhouse
sudo systemctl daemon-reload
sudo systemctl enable --now sglang
sudo systemctl enable nightly-extract     # enable, not start: it runs at boot
```

The instance also needs an IAM instance profile with S3 read/write on the
bucket — it fetches filings and writes extractions itself now.

**Verify by rebooting.** "It started when I ran systemctl" and "it starts on
boot" are different claims, and only the second is what the workflow depends on:

```bash
sudo reboot
# wait ~4 minutes
curl -s localhost:30000/health && echo " READY"
```

### How the workflow and the instance communicate

There is no shell session between them, so they pass messages through S3 and
instance tags:

| | |
|---|---|
| workflow → instance | an `ExtractDate` tag, set before `start-instances` |
| instance → workflow | `logs/extract-<date>.done`, a JSON marker with the exit status |
| instance → operator | `logs/extract-<date>.log`, uploaded before shutdown |

The workflow deletes the marker before starting, then polls for it. Without
that deletion a stale marker from the previous night would make the job return
immediately with yesterday's result.

### Running it by hand

Actions → nightly → Run workflow. Takes a date, and a `skip_extract` toggle that
never starts the GPU — useful for re-loading a day whose extractions already
exist in S3.

---

## The dashboard

Stateless, reads Supabase over the network, needs no AWS. Anywhere that runs a
container will do:

```bash
python -m uvicorn inhouse.web:app --host 0.0.0.0 --port 8080
```

with `DATABASE_URL` in the environment.

On a `t3.micro` that is a systemd unit and a security group rule. On Fly, Railway
or Render it is a push. The dashboard has no reason to sit in the same VPC as the
GPU — it talks to Supabase either way — so the simplest host wins.

---

## Cost

| | |
|---|---|
| GitHub Actions | free (public repository) |
| GPU, ~22 min/night | ~$6/month |
| S3, ~330 MB/day | ~$1/month |
| Supabase | free tier |
| Dashboard host | $0–8/month |

The comparison that matters is against leaving the instance up: **$380/month**.
Everything above is a consequence of one `if: always()` stop step and the order
of three jobs.
