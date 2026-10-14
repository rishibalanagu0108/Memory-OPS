#!/usr/bin/env bash
set -euo pipefail
set +x

region=us-east-2
project_id=cold-silence-99209400
parent_branch=testing
instance_type=t3.small
suffix="$(date +%s)-$$"
branch_name="m8-20-capacity-${suffix}"
key_name="m8-20-${suffix}"
security_group_name="m8-20-${suffix}"
run_dir="$(mktemp -d)"
private_key="${run_dir}/id_ed25519"
archive="${run_dir}/source.tar.gz"
runtime_env="${run_dir}/runtime.env"
known_hosts="${run_dir}/known_hosts"
branches_json="${run_dir}/branches.json"
endpoints_json="${run_dir}/endpoints.json"
consumption_json="${run_dir}/consumption.json"
cloudwatch_dir="${run_dir}/cloudwatch"
instance_id=""
security_group_id=""
branch_id=""
endpoint_id=""
cleaned=0

cleanup_local() {
  if [[ -f "${runtime_env}" ]]; then shred -u "${runtime_env}"; fi
  if [[ -f "${private_key}" ]]; then shred -u "${private_key}"; fi
  if [[ -f "${private_key}.pub" ]]; then rm "${private_key}.pub"; fi
  if [[ -f "${archive}" ]]; then rm "${archive}"; fi
  if [[ -f "${known_hosts}" ]]; then rm "${known_hosts}"; fi
  if [[ -d "${cloudwatch_dir}" ]]; then rm -r "${cloudwatch_dir}"; fi
  for path in "${branches_json}" "${endpoints_json}" "${consumption_json}"; do
    if [[ -f "${path}" ]]; then rm "${path}"; fi
  done
  rmdir "${run_dir}" 2>/dev/null || true
}

cleanup_cloud() {
  if [[ "${cleaned}" == 1 ]]; then return; fi
  if [[ -n "${instance_id}" ]]; then
    aws ec2 terminate-instances --region "${region}" --instance-ids "${instance_id}" >/dev/null 2>&1 || true
    aws ec2 wait instance-terminated --region "${region}" --instance-ids "${instance_id}" >/dev/null 2>&1 || true
  fi
  aws ec2 delete-key-pair --region "${region}" --key-name "${key_name}" >/dev/null 2>&1 || true
  if [[ -n "${security_group_id}" ]]; then
    for _attempt in $(seq 1 24); do
      if aws ec2 delete-security-group --region "${region}" --group-id "${security_group_id}" >/dev/null 2>&1; then
        break
      fi
      sleep 5
    done
  fi
  if [[ -n "${branch_id}" ]]; then
    printf 'y\n' | neon branches delete "${branch_id}" --project-id "${project_id}" >/dev/null 2>&1 || true
  fi
  cleaned=1
}

cleanup() {
  cleanup_cloud
  cleanup_local
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

for command_name in aws neon ssh scp curl uv; do
  command -v "${command_name}" >/dev/null
done

public_ip="$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')"
[[ "${public_ip}" =~ ^[0-9.]+$ ]] || { echo "could not resolve public IPv4 address" >&2; exit 1; }

expires_at="$(date -u -d '+1 hour' '+%Y-%m-%dT%H:%M:%SZ')"
branch_id="$(neon branches create \
  --project-id "${project_id}" \
  --name "${branch_name}" \
  --parent "${parent_branch}" \
  --expires-at "${expires_at}" \
  --no-secrets \
  --output json | uv run python -c 'import json,sys; data=json.load(sys.stdin); print(data.get("branch", data)["id"])')"

pooled_url="$(neon connection-string "${branch_id}" --project-id "${project_id}" --role-name neondb_owner --database-name neondb --pooled)"
direct_url="$(neon connection-string "${branch_id}" --project-id "${project_id}" --role-name neondb_owner --database-name neondb)"
umask 077
printf 'DATABASE_URL=%s\nDATABASE_URL_UNPOOLED=%s\nM8_DATABASE_URL_POOLED=%s\nM8_DATABASE_URL_UNPOOLED=%s\n' \
  "${pooled_url}" "${direct_url}" "${pooled_url}" "${direct_url}" >"${runtime_env}"

vpc_id="$(aws ec2 describe-vpcs --region "${region}" --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)"
subnet_id="$(aws ec2 describe-subnets --region "${region}" --filters "Name=vpc-id,Values=${vpc_id}" "Name=default-for-az,Values=true" --query 'Subnets[0].SubnetId' --output text)"
ami_id="$(aws ssm get-parameter --region "${region}" --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id --query 'Parameter.Value' --output text)"
[[ "${vpc_id}" != "None" && "${subnet_id}" != "None" && "${ami_id}" != "None" ]] || { echo "default VPC, subnet, or Ubuntu AMI is unavailable" >&2; exit 1; }

ssh-keygen -q -t ed25519 -N '' -f "${private_key}"
chmod 600 "${private_key}"
aws ec2 import-key-pair --region "${region}" --key-name "${key_name}" --public-key-material "fileb://${private_key}.pub" >/dev/null

security_group_id="$(aws ec2 create-security-group --region "${region}" --group-name "${security_group_name}" --description "Temporary M8-20 capacity diagnostic" --vpc-id "${vpc_id}" --query 'GroupId' --output text)"
aws ec2 authorize-security-group-ingress --region "${region}" --group-id "${security_group_id}" --protocol tcp --port 22 --cidr "${public_ip}/32" >/dev/null

run_started="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
instance_id="$(aws ec2 run-instances \
  --region "${region}" \
  --image-id "${ami_id}" \
  --instance-type "${instance_type}" \
  --key-name "${key_name}" \
  --subnet-id "${subnet_id}" \
  --security-group-ids "${security_group_id}" \
  --associate-public-ip-address \
  --instance-initiated-shutdown-behavior terminate \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=M8-20-${suffix}},{Key=Purpose,Value=temporary-capacity-diagnostic}]" \
  --query 'Instances[0].InstanceId' \
  --output text)"

aws ec2 wait instance-status-ok --region "${region}" --instance-ids "${instance_id}"
instance_ip="$(aws ec2 describe-instances --region "${region}" --instance-ids "${instance_id}" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"
availability_zone="$(aws ec2 describe-instances --region "${region}" --instance-ids "${instance_id}" --query 'Reservations[0].Instances[0].Placement.AvailabilityZone' --output text)"

ssh_options=(-i "${private_key}" -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=${known_hosts}" -o ConnectTimeout=10)
for _attempt in $(seq 1 30); do
  if ssh "${ssh_options[@]}" "ubuntu@${instance_ip}" true 2>/dev/null; then break; fi
  sleep 5
done
ssh "${ssh_options[@]}" "ubuntu@${instance_ip}" "sudo shutdown -h +55 >/dev/null"

tar --exclude='__pycache__' --exclude='*.pyc' -czf "${archive}" pyproject.toml uv.lock README.md src migrations evals/m8
ssh "${ssh_options[@]}" "ubuntu@${instance_ip}" "mkdir -p ~/memory-ops"
scp "${ssh_options[@]}" "${archive}" "ubuntu@${instance_ip}:~/memory-ops/source.tar.gz"
scp "${ssh_options[@]}" "${runtime_env}" "ubuntu@${instance_ip}:~/memory-ops/.env.m8-20"

ssh "${ssh_options[@]}" "ubuntu@${instance_ip}" 'set -euo pipefail
  cd ~/memory-ops
  cleanup_remote(){ if [[ -f .env.m8-20 ]]; then shred -u .env.m8-20; fi; }
  trap cleanup_remote EXIT
  chmod 600 .env.m8-20
  tar -xzf source.tar.gz
  rm source.tar.gz
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3-venv python3-pip >/dev/null
  python3 -m venv .bootstrap
  .bootstrap/bin/pip install -q uv
  .bootstrap/bin/uv sync --frozen
  .bootstrap/bin/uv run --env-file .env.m8-20 memory-ops-migrate
  .bootstrap/bin/uv run --env-file .env.m8-20 python evals/m8/fullscale.py seed
  .bootstrap/bin/uv run --env-file .env.m8-20 python evals/m8/fullscale.py seed-documents
  .bootstrap/bin/uv run --env-file .env.m8-20 python evals/m8/capacity_probe.py --seconds-per-level 30'

scp "${ssh_options[@]}" "ubuntu@${instance_ip}:~/memory-ops/evals/m8/capacity-load-result.json" evals/m8/capacity-load-result.json

mkdir -p "${cloudwatch_dir}"
run_ended="$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
for metric in CPUUtilization CPUCreditBalance StatusCheckFailed NetworkIn NetworkOut; do
  aws cloudwatch get-metric-statistics \
    --region "${region}" \
    --namespace AWS/EC2 \
    --metric-name "${metric}" \
    --dimensions "Name=InstanceId,Value=${instance_id}" \
    --start-time "${run_started}" \
    --end-time "${run_ended}" \
    --period 60 \
    --statistics Average Maximum \
    --output json >"${cloudwatch_dir}/${metric}.json"
done

neon branches list --project-id "${project_id}" --output json >"${branches_json}"
neon api "/projects/${project_id}/endpoints" >"${endpoints_json}"
endpoint_id="$(uv run python -c 'import json,sys; branch=sys.argv[1]; data=json.load(open(sys.argv[2])); print(next(row["id"] for row in data["endpoints"] if row["branch_id"] == branch))' "${branch_id}" "${endpoints_json}")"
org_id="$(neon projects list --output json | uv run python -c 'import json,sys; project=sys.argv[1]; print(next(row["org_id"] for row in json.load(sys.stdin) if row["id"] == project))' "${project_id}")"
hour_start="$(date -u '+%Y-%m-%dT%H:00:00Z')"
hour_end="$(date -u -d '+1 hour' '+%Y-%m-%dT%H:00:00Z')"
neon api /consumption_history/v2/branches \
  -Q "project_ids=${project_id}" \
  -Q "branch_ids=${branch_id}" \
  -Q "from=${hour_start}" \
  -Q "to=${hour_end}" \
  -Q granularity=hourly \
  -Q "org_id=${org_id}" \
  -Q metrics=compute_unit_seconds,public_network_transfer_bytes >"${consumption_json}" || printf '{"periods":[],"unavailable":true}\n' >"${consumption_json}"

shred -u "${runtime_env}"
cleanup_cloud

uv run python evals/m8/compare_capacity.py \
  --instance-id "${instance_id}" \
  --key-name "${key_name}" \
  --security-group-id "${security_group_id}" \
  --instance-type "${instance_type}" \
  --availability-zone "${availability_zone}" \
  --branch-id "${branch_id}" \
  --branch-name "${branch_name}" \
  --branches-json "${branches_json}" \
  --endpoints-json "${endpoints_json}" \
  --consumption-json "${consumption_json}" \
  --cloudwatch-dir "${cloudwatch_dir}" \
  --local-environment-path "${runtime_env}"
