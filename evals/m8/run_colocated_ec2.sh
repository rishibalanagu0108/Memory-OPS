#!/usr/bin/env bash
set -euo pipefail
set +x

region=us-east-2
instance_type=t3.small
suffix="$(date +%s)-$$"
key_name="m8-17-${suffix}"
security_group_name="m8-17-${suffix}"
run_dir="$(mktemp -d)"
private_key="${run_dir}/id_ed25519"
archive="${run_dir}/source.tar.gz"
known_hosts="${run_dir}/known_hosts"
instance_id=""
security_group_id=""
cleaned=0

cleanup_local() {
  if [[ -f "${private_key}" ]]; then shred -u "${private_key}"; fi
  if [[ -f "${private_key}.pub" ]]; then rm "${private_key}.pub"; fi
  if [[ -f "${archive}" ]]; then rm "${archive}"; fi
  if [[ -f "${known_hosts}" ]]; then rm "${known_hosts}"; fi
  rmdir "${run_dir}" 2>/dev/null || true
}

cleanup_aws() {
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
  cleaned=1
}

cleanup() {
  cleanup_aws
  cleanup_local
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

[[ -f .env.m8-10 ]] || { echo ".env.m8-10 is required" >&2; exit 1; }
command -v aws >/dev/null
command -v ssh >/dev/null
command -v scp >/dev/null
command -v curl >/dev/null

public_ip="$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')"
[[ "${public_ip}" =~ ^[0-9.]+$ ]] || { echo "could not resolve public IPv4 address" >&2; exit 1; }

vpc_id="$(aws ec2 describe-vpcs --region "${region}" --filters Name=is-default,Values=true --query 'Vpcs[0].VpcId' --output text)"
subnet_id="$(aws ec2 describe-subnets --region "${region}" --filters "Name=vpc-id,Values=${vpc_id}" "Name=default-for-az,Values=true" --query 'Subnets[0].SubnetId' --output text)"
ami_id="$(aws ssm get-parameter --region "${region}" --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id --query 'Parameter.Value' --output text)"
[[ "${vpc_id}" != "None" && "${subnet_id}" != "None" && "${ami_id}" != "None" ]] || { echo "default VPC, subnet, or Ubuntu AMI is unavailable" >&2; exit 1; }

ssh-keygen -q -t ed25519 -N '' -f "${private_key}"
chmod 600 "${private_key}"
aws ec2 import-key-pair --region "${region}" --key-name "${key_name}" --public-key-material "fileb://${private_key}.pub" >/dev/null

security_group_id="$(aws ec2 create-security-group --region "${region}" --group-name "${security_group_name}" --description "Temporary M8-17 same-region diagnostic" --vpc-id "${vpc_id}" --query 'GroupId' --output text)"
aws ec2 authorize-security-group-ingress --region "${region}" --group-id "${security_group_id}" --protocol tcp --port 22 --cidr "${public_ip}/32" >/dev/null

instance_id="$(aws ec2 run-instances \
  --region "${region}" \
  --image-id "${ami_id}" \
  --instance-type "${instance_type}" \
  --key-name "${key_name}" \
  --subnet-id "${subnet_id}" \
  --security-group-ids "${security_group_id}" \
  --associate-public-ip-address \
  --instance-initiated-shutdown-behavior terminate \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=M8-17-${suffix}},{Key=Purpose,Value=temporary-latency-diagnostic}]" \
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
scp "${ssh_options[@]}" .env.m8-10 "ubuntu@${instance_ip}:~/memory-ops/.env.m8-17"

ssh "${ssh_options[@]}" "ubuntu@${instance_ip}" 'set -euo pipefail
  cd ~/memory-ops
  chmod 600 .env.m8-17
  tar -xzf source.tar.gz
  rm source.tar.gz
  sudo apt-get update -qq
  sudo apt-get install -y -qq python3-venv python3-pip >/dev/null
  python3 -m venv .bootstrap
  .bootstrap/bin/pip install -q uv
  .bootstrap/bin/uv sync --frozen
  status=0
  .bootstrap/bin/uv run --env-file .env.m8-17 python evals/m8/latency_attribution.py --seconds-per-level 30 --output evals/m8/colocated-load-result.json || status=$?
  shred -u .env.m8-17
  exit "${status}"'

scp "${ssh_options[@]}" "ubuntu@${instance_ip}:~/memory-ops/evals/m8/colocated-load-result.json" evals/m8/colocated-load-result.json
cleanup_aws

uv run python evals/m8/compare_colocated.py \
  --instance-id "${instance_id}" \
  --key-name "${key_name}" \
  --security-group-id "${security_group_id}" \
  --instance-type "${instance_type}" \
  --availability-zone "${availability_zone}"
