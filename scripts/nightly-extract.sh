#!/usr/bin/env bash
# Extraction, run by the instance on itself.
#
# The GPU box does its own work rather than being driven over SSH from a CI
# runner. Three reasons, in order of how much they matter:
#
#   1. A runner has no stable address. GitHub's ranges are thousands of
#      rotating IPs, so a security group cannot allow them without effectively
#      opening port 22 to the internet -- and SGLang has no authentication.
#   2. The instance stops itself. A workflow that dies mid-run cannot leave a
#      $0.53/hour instance up, because nothing outside this script is
#      responsible for turning it off.
#   3. It survives GitHub being down. The pipeline is a cron job with a GPU,
#      not a CI job that happens to use one.
#
# The cost of that: logs live here rather than in the Actions UI, so they are
# copied to S3 next to the output before shutdown.

set -uo pipefail   # not -e: the shutdown at the end must run even on failure

DATE="${1:-$(date -u -d 'yesterday' +%F)}"
BUCKET="${STORAGE_URI:-s3://inhouse-edgar}"
REPO="${REPO_DIR:-/home/ubuntu/inhouse}"
LOG="/tmp/extract-${DATE}.log"

exec > >(tee "$LOG") 2>&1
echo "=== extract $DATE  $(date -u +%FT%TZ) ==="

cd "$REPO" || { echo "no repo at $REPO"; exit 1; }

# Take the latest code. The schema and prompt are versioned with it, and an
# extraction run against a stale prompt would be silently wrong rather than
# broken.
git pull --ff-only || echo "warning: could not update repo, running what is here"

# SGLang starts independently at boot; this waits rather than assuming. Model
# load is two to three minutes from cold.
echo "waiting for SGLang..."
for i in $(seq 1 90); do
    if curl -sf localhost:30000/health >/dev/null 2>&1; then
        echo "ready after ${i}0s"
        break
    fi
    [ "$i" = 90 ] && { echo "SGLang never became healthy"; STATUS=1; }
    sleep 10
done

if [ "${STATUS:-0}" = 0 ]; then
    # Concurrency 32 is the measured knee on a T4: 135 docs/hour sequential,
    # 380 at 32, 381 at 64. Past 32 the card is memory-bandwidth-bound and
    # extra requests only queue.
    /opt/pytorch/bin/python -m inhouse extract \
        --date "$DATE" --concurrency 32 --storage "$BUCKET"
    STATUS=$?
fi

echo "=== extract finished status=$STATUS  $(date -u +%FT%TZ) ==="

# Ship the log before the machine goes away, so a failed night is diagnosable
# without starting the instance again to read journalctl.
aws s3 cp "$LOG" "$BUCKET/logs/extract-${DATE}.log" --only-show-errors || true

# A marker file the loader waits on: its presence means extraction reached the
# end, and its contents say whether it succeeded. Without it a workflow polling
# S3 cannot distinguish "still running" from "failed and stopped".
echo "{\"date\":\"$DATE\",\"status\":$STATUS,\"finished_at\":\"$(date -u +%FT%TZ)\"}" \
    | aws s3 cp - "$BUCKET/logs/extract-${DATE}.done" --only-show-errors || true

# Always. This line is the difference between ~$6/month and ~$380/month, and it
# runs whether extraction worked, failed, or never started.
echo "shutting down"
sudo shutdown -h now
