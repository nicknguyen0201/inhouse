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
      "Federated": "arn:aws:iam::401418592700:oidc-provider/token.actions.githubusercontent.com"
    },
    "Action": "sts:AssumeRoleWithWebIdentity",
    "Condition": {
      "StringEquals": {
        "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
      },
      "StringLike": {
        "token.actions.githubusercontent.com:sub": "repo:nicknguyen0201/inhouse:*"
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
      "Action": ["s3:GetObject", "s3:PutObject", "s3:ListBucket"],
      "Resource": [
        "arn:aws:s3:::inhouse-edgar",
        "arn:aws:s3:::inhouse-edgar/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": ["ec2:StartInstances", "ec2:StopInstances"],
      "Resource": "arn:aws:ec2:us-east-2:401418592700:instance/i-02d5b786e9fe18e25"
    },
    {
      "Effect": "Allow",
      "Action": "ec2:DescribeInstances",
      "Resource": "*"
    }
  ]
}
```

`DescribeInstances` cannot be resource-scoped — AWS does not support it — so it
is the one wildcard, and it only reads.

### Repository configuration

Settings → Secrets and variables → Actions.

**Variables** (not secret — they appear in logs anyway):

| name | example |
|---|---|
| `AWS_ROLE_ARN` | `arn:aws:iam::401418592700:role/inhouse-github-actions` |
| `GPU_INSTANCE_ID` | `i-02d5b786e9fe18e25` |
| `STORAGE_URI` | `s3://inhouse-edgar` |

**Secrets:**

| name | why |
|---|---|
| `SEC_USER_AGENT` | `Name email@example.com` — the SEC blocks requests without it |
| `GPU_SSH_KEY` | contents of the `.pem`, for the tunnel |
| `DATABASE_URL` | Supabase transaction pooler string |

### The instance must autostart SGLang

The workflow starts the instance and waits for `/health`; it does not install or
launch anything. Put this on the box so the server comes up on boot:

```ini
# /etc/systemd/system/sglang.service
[Unit]
Description=SGLang inference server
After=network-online.target

[Service]
User=ubuntu
Environment=LIBRARY_PATH=/opt/pytorch/lib/python3.13/site-packages/nvidia/cu13/lib:/opt/pytorch/cuda/lib
Environment=LD_LIBRARY_PATH=/opt/pytorch/lib/python3.13/site-packages/nvidia/cu13/lib:/opt/pytorch/cuda/lib
ExecStart=/opt/pytorch/bin/python -m sglang.launch_server \
  --model-path Qwen/Qwen2.5-7B-Instruct-AWQ \
  --host 0.0.0.0 --port 30000 \
  --mem-fraction-static 0.75 \
  --attention-backend triton \
  --cuda-graph-max-bs 32 \
  --enable-metrics
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now sglang
```

`--mem-fraction-static 0.75` rather than the 0.85 in most examples: at 0.85 the
grammar-constraint kernel had no room to load and the server died on the first
schema-constrained request, several minutes after appearing healthy.

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
