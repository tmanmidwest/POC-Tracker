#!/bin/bash
# =============================================================================
# deploy.sh — POC-Tracker ECS Fargate Deployment
# =============================================================================
# Usage:  ./deploy.sh
# Requires: AWS CLI v2 configured with appropriate permissions
# =============================================================================

set -euo pipefail

# ── CONFIGURATION (edit these if needed) ─────────────────────────────────────
# Default instance name. You'll be prompted to confirm or change it at deploy
# time; set INSTANCE=<name> in the environment to skip the prompt. Each instance
# is a fully isolated stack, so you can run several in one AWS account.
APP_NAME="poc-tracker"
INSTANCE="${INSTANCE:-}"
GITHUB_REPO="https://github.com/tmanmidwest/POC-Tracker.git"
CONTAINER_IMAGE=""  # Set automatically — built from source and pushed to ECR
CONTAINER_PORT=8010   # web app
# MCP server (second container in the same task). Default 8443 because that is one
# of the few HTTPS ports Cloudflare's proxy (orange cloud) will forward — so the
# MCP endpoint works behind Cloudflare exactly like the app on 443. Cloudflare's
# proxied HTTPS ports are 443, 2053, 2083, 2087, 2096, 8443; a value outside that
# set (e.g. 8011) only works with the domain set to "DNS only" (grey cloud).
MCP_PORT=8443
# Two containers share one task, so size it for both. 0.5 vCPU / 1 GB is a safe
# baseline; drop to 256/512 if you disable the MCP container below.
CPU=512        # 0.5 vCPU
MEMORY=1024    # 1 GB
LOG_LEVEL="INFO"

# Deploy the MCP server as a second container alongside the web app (mirrors the
# repo's docker-compose). It shares the same EFS /data volume and reaches the app
# over localhost. Set DEPLOY_MCP=false to deploy the web app only.
DEPLOY_MCP="${DEPLOY_MCP:-true}"

# HTTPS / custom domain (optional). Leave disabled for the default HTTP-only
# deployment on the raw ALB DNS name. When enabled, the script provisions a free
# AWS-managed (ACM) certificate, adds an HTTPS:443 listener to the load balancer,
# and redirects HTTP→HTTPS. You point a Cloudflare CNAME at the ALB. Both values
# can also be supplied as environment variables, e.g.:
#   ENABLE_HTTPS=true DOMAIN_NAME=poc.trevorcombs.com ./deploy.sh
ENABLE_HTTPS="${ENABLE_HTTPS:-false}"
DOMAIN_NAME="${DOMAIN_NAME:-}"
# ─────────────────────────────────────────────────────────────────────────────

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m' # No Color

CHECKMARK="${GREEN}✔${NC}"
ARROW="${BLUE}▶${NC}"
WARNING="${YELLOW}⚠${NC}"

# State file naming — saves all resource IDs so manage.sh/teardown.sh can find
# them later. The default instance keeps the original ".poc-tracker-state" name for
# backward compatibility; other instances are namespaced as
# ".poc-tracker-state.<instance>" so several deployments can coexist. Management
# scripts discover them all with the glob ".poc-tracker-state*".
state_file_for() {
  if [ "$1" = "poc-tracker" ]; then echo ".poc-tracker-state"; else echo ".poc-tracker-state.$1"; fi
}

log()     { echo -e "${ARROW}  $1"; }
success() { echo -e "${CHECKMARK}  $1"; }
warn()    { echo -e "${WARNING}  ${YELLOW}$1${NC}"; }
error()   { echo -e "${RED}✖  ERROR: $1${NC}" >&2; exit 1; }
header()  { echo -e "\n${BOLD}${BLUE}── $1 ${NC}"; }

wait_for() {
  local description="$1"
  local check_cmd="$2"
  local expected="$3"
  local max_attempts="${4:-30}"
  local attempt=0
  log "Waiting for $description..."
  while [ $attempt -lt $max_attempts ]; do
    result=$(eval "$check_cmd" 2>/dev/null || echo "")
    if echo "$result" | grep -q "$expected"; then
      success "$description is ready"
      return 0
    fi
    sleep 5
    attempt=$((attempt + 1))
    echo -n "."
  done
  echo ""
  error "Timed out waiting for $description"
}

# Idempotently ensure a CloudWatch log group exists. Fails loudly if it can't
# be created — a missing log group makes ECS tasks fail to start (the awslogs
# driver does NOT auto-create the group), which is hard to diagnose otherwise.
ensure_log_group() {
  local group="$1"

  if aws logs describe-log-groups \
      --log-group-name-prefix "$group" \
      --region "$REGION" \
      --query "logGroups[?logGroupName=='${group}'] | [0].logGroupName" \
      --output text 2>/dev/null | grep -qx "$group"; then
    return 0
  fi

  # Not present — create it. Treat an already-exists race as success; any other
  # failure is fatal.
  local create_err
  if ! create_err=$(aws logs create-log-group \
      --log-group-name "$group" \
      --region "$REGION" 2>&1); then
    if echo "$create_err" | grep -q "ResourceAlreadyExistsException"; then
      return 0
    fi
    error "Could not create CloudWatch log group '$group': $create_err"
  fi

  # Verify it now exists; if not, fail rather than register a task def that
  # points at a group ECS can't write to.
  aws logs describe-log-groups \
    --log-group-name-prefix "$group" \
    --region "$REGION" \
    --query "logGroups[?logGroupName=='${group}'] | [0].logGroupName" \
    --output text 2>/dev/null | grep -qx "$group" \
    || error "Log group '$group' still does not exist after creation attempt."
}

# ── PRE-FLIGHT CHECKS ─────────────────────────────────────────────────────────
header "Pre-flight checks"

command -v aws >/dev/null 2>&1 || error "AWS CLI not found. Install from https://aws.amazon.com/cli/"

CALLER=$(aws sts get-caller-identity --output json 2>/dev/null) \
  || error "Not logged in to AWS. Run 'aws configure' or refresh your session and try again."

ACCOUNT_ID=$(echo "$CALLER" | python3 -c "import sys,json; print(json.load(sys.stdin)['Account'])")
SESSION_USER=$(echo "$CALLER" | python3 -c "import sys,json; print(json.load(sys.stdin)['Arn'].split('/')[-1])")
success "Logged in as: $SESSION_USER (Account: $ACCOUNT_ID)"

# ── ECR IMAGE BUILD ──────────────────────────────────────────────────────────
header "Container image"

command -v docker >/dev/null 2>&1 || error "Docker not found. Install Docker Desktop from https://www.docker.com/products/docker-desktop/"
docker info >/dev/null 2>&1 || error "Docker is not running. Please start Docker Desktop and try again."
success "Docker is running"

# ── REGION SELECTION ──────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}  Select an AWS region to deploy to:${NC}"
echo ""
echo -e "  ${BOLD} 1)${NC} us-east-1       US East (N. Virginia)      ${YELLOW}— most services, lowest cost${NC}"
echo -e "  ${BOLD} 2)${NC} us-east-2       US East (Ohio)"
echo -e "  ${BOLD} 3)${NC} us-west-1       US West (N. California)"
echo -e "  ${BOLD} 4)${NC} us-west-2       US West (Oregon)"
echo -e "  ${BOLD} 5)${NC} eu-west-1       Europe (Ireland)"
echo -e "  ${BOLD} 6)${NC} eu-west-2       Europe (London)"
echo -e "  ${BOLD} 7)${NC} eu-west-3       Europe (Paris)"
echo -e "  ${BOLD} 8)${NC} eu-central-1    Europe (Frankfurt)"
echo -e "  ${BOLD} 9)${NC} eu-north-1      Europe (Stockholm)"
echo -e "  ${BOLD}10)${NC} ap-southeast-1  Asia Pacific (Singapore)"
echo -e "  ${BOLD}11)${NC} ap-southeast-2  Asia Pacific (Sydney)"
echo -e "  ${BOLD}12)${NC} ap-northeast-1  Asia Pacific (Tokyo)"
echo -e "  ${BOLD}13)${NC} ap-northeast-2  Asia Pacific (Seoul)"
echo -e "  ${BOLD}14)${NC} ap-south-1      Asia Pacific (Mumbai)"
echo -e "  ${BOLD}15)${NC} ca-central-1    Canada (Central)"
echo -e "  ${BOLD}16)${NC} sa-east-1       South America (São Paulo)"
echo ""

DEFAULT_REGION=$(aws configure get region 2>/dev/null || echo "us-east-1")
read -rp "  Enter number or region name [default: $DEFAULT_REGION]: " REGION_INPUT
echo ""

case "$REGION_INPUT" in
  1)  REGION="us-east-1" ;;
  2)  REGION="us-east-2" ;;
  3)  REGION="us-west-1" ;;
  4)  REGION="us-west-2" ;;
  5)  REGION="eu-west-1" ;;
  6)  REGION="eu-west-2" ;;
  7)  REGION="eu-west-3" ;;
  8)  REGION="eu-central-1" ;;
  9)  REGION="eu-north-1" ;;
  10) REGION="ap-southeast-1" ;;
  11) REGION="ap-southeast-2" ;;
  12) REGION="ap-northeast-1" ;;
  13) REGION="ap-northeast-2" ;;
  14) REGION="ap-south-1" ;;
  15) REGION="ca-central-1" ;;
  16) REGION="sa-east-1" ;;
  "") REGION="$DEFAULT_REGION" ;;
  *)  REGION="$REGION_INPUT" ;;
esac

# Validate the region
aws ec2 describe-regions --region-names "$REGION" --query 'Regions[0].RegionName' \
  --output text >/dev/null 2>&1 \
  || error "Invalid or inaccessible region: '$REGION'. Check the name and that your account has access."

success "Region: $REGION"

# ── DEPLOYMENT INSTANCE NAME ────────────────────────────────────────────────────
header "Deployment instance"

echo -e "  Each instance is a fully isolated deployment — its own load balancer,"
echo -e "  storage, container, certificate, and URL. Pick distinct names to run"
echo -e "  more than one in the same AWS account (e.g. ${BOLD}poc-tracker${NC}, ${BOLD}perm-test${NC})."
echo ""

DEFAULT_INSTANCE="${INSTANCE:-$APP_NAME}"
while true; do
  if [ -n "${INSTANCE:-}" ]; then
    APP_NAME="$INSTANCE"
  else
    read -rp "  Instance name [default: $DEFAULT_INSTANCE]: " INSTANCE_INPUT
    APP_NAME="${INSTANCE_INPUT:-$DEFAULT_INSTANCE}"
  fi
  # Validate for AWS resource names: lowercase letters/digits/hyphens, must start
  # with a letter, no trailing hyphen, <=28 chars (leaves room for "-alb" etc).
  if echo "$APP_NAME" | grep -Eq '^[a-z][a-z0-9-]{0,27}$' && [[ "$APP_NAME" != *- ]]; then
    break
  fi
  warn "Invalid name '$APP_NAME' — use lowercase letters, digits, and hyphens;"
  warn "start with a letter, no trailing hyphen, 28 characters max."
  INSTANCE=""  # clear a bad preset so we fall through to the prompt
done

STATE_FILE=$(state_file_for "$APP_NAME")
success "Instance: $APP_NAME"

# Note any other instances already deployed from this machine (each ALB bills separately)
shopt -s nullglob
EXISTING_STATES=( .poc-tracker-state* )
shopt -u nullglob
OTHER_COUNT=0
# Guard the loop: on macOS bash 3.2, referencing an empty array under `set -u`
# ("${EXISTING_STATES[@]}") errors with "unbound variable". ${#arr[@]} is safe.
if [ "${#EXISTING_STATES[@]}" -gt 0 ]; then
  for f in "${EXISTING_STATES[@]}"; do
    [ "$f" = "$STATE_FILE" ] || OTHER_COUNT=$((OTHER_COUNT + 1))
  done
fi
if [ "$OTHER_COUNT" -gt 0 ]; then
  warn "$OTHER_COUNT other instance(s) already tracked on this machine — each running"
  warn "instance has its own load balancer (~\$16/month). Run ./teardown.sh to remove one."
fi

# ── HTTPS / CUSTOM DOMAIN SELECTION ────────────────────────────────────────────
header "HTTPS / custom domain"

if [ "$ENABLE_HTTPS" != "true" ]; then
  echo -e "  Serve the app over ${BOLD}HTTPS on a custom domain${NC} (recommended if you'll"
  echo -e "  integrate OAuth later)? Choosing ${BOLD}No${NC} gives the default HTTP-only"
  echo -e "  deployment on the generated ALB DNS name."
  echo ""
  read -rp "  Enable HTTPS? [y/N] " https_confirm
  echo ""
  [[ "$https_confirm" =~ ^[Yy]$ ]] && ENABLE_HTTPS="true"
fi

if [ "$ENABLE_HTTPS" = "true" ]; then
  while [ -z "$DOMAIN_NAME" ]; do
    read -rp "  Enter the domain name (e.g. testhr.trevorcombs.com): " DOMAIN_NAME
  done
  success "HTTPS enabled for: $DOMAIN_NAME"
  warn "You'll add two CNAME records in your Cloudflare DNS during this run:"
  warn "  1) a one-time certificate-validation record, then"
  warn "  2) a record pointing $DOMAIN_NAME at the load balancer."
else
  success "HTTPS disabled — deploying HTTP only on the ALB DNS name"
fi


if [ -f "$STATE_FILE" ]; then
  warn "A previous deployment state file exists ($STATE_FILE)."
  warn "This suggests the app may already be deployed."
  read -rp "  Continue anyway and overwrite? [y/N] " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi

# ── NETWORKING ────────────────────────────────────────────────────────────────
header "Networking"

VPC_ID=$(aws ec2 describe-vpcs \
  --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' \
  --output text)
[ -z "$VPC_ID" ] || [ "$VPC_ID" = "None" ] && error "No default VPC found. Please create one in the AWS console."
success "Default VPC: $VPC_ID"

# Grab up to 2 subnets from different AZs
SUBNET_IDS=$(aws ec2 describe-subnets \
  --filters Name=vpc-id,Values="$VPC_ID" \
  --query 'Subnets[*].SubnetId' \
  --output text | tr '\t' '\n' | head -2 | tr '\n' ' ' | xargs)
SUBNET_COUNT=$(echo "$SUBNET_IDS" | wc -w | xargs)
[ "$SUBNET_COUNT" -lt 2 ] && error "Need at least 2 subnets in your default VPC. Found: $SUBNET_COUNT"
SUBNET_1=$(echo "$SUBNET_IDS" | awk '{print $1}')
SUBNET_2=$(echo "$SUBNET_IDS" | awk '{print $2}')
success "Subnets: $SUBNET_1, $SUBNET_2"

# ── SECURITY GROUPS ───────────────────────────────────────────────────────────
header "Security groups"

# ALB security group
log "Creating ALB security group (or reusing if it exists)..."
ALB_SG_ID=$(aws ec2 describe-security-groups \
  --filters Name=group-name,Values="${APP_NAME}-alb-sg" Name=vpc-id,Values="$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "")

if [ -z "$ALB_SG_ID" ] || [ "$ALB_SG_ID" = "None" ]; then
  ALB_SG_ID=$(aws ec2 create-security-group \
    --group-name "${APP_NAME}-alb-sg" \
    --description "POC Tracker ALB - HTTP from internet" \
    --vpc-id "$VPC_ID" \
    --query 'GroupId' --output text)
  aws ec2 authorize-security-group-ingress \
    --group-id "$ALB_SG_ID" \
    --protocol tcp --port 80 --cidr 0.0.0.0/0 >/dev/null
fi

# MCP: expose the MCP port on the ALB to the internet (the endpoint is auth-gated
# by the gateway token you generate in the app UI). Idempotent across re-runs.
if [ "$DEPLOY_MCP" = "true" ]; then
  aws ec2 authorize-security-group-ingress \
    --group-id "$ALB_SG_ID" \
    --protocol tcp --port "$MCP_PORT" --cidr 0.0.0.0/0 >/dev/null 2>&1 || true
fi
success "ALB security group: $ALB_SG_ID"

# ECS task security group
log "Creating ECS task security group (or reusing if it exists)..."
ECS_SG_ID=$(aws ec2 describe-security-groups \
  --filters Name=group-name,Values="${APP_NAME}-ecs-sg" Name=vpc-id,Values="$VPC_ID" \
  --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "")

if [ -z "$ECS_SG_ID" ] || [ "$ECS_SG_ID" = "None" ]; then
  ECS_SG_ID=$(aws ec2 create-security-group \
    --group-name "${APP_NAME}-ecs-sg" \
    --description "POC Tracker ECS task - traffic from ALB only" \
    --vpc-id "$VPC_ID" \
    --query 'GroupId' --output text)
  # Allow the web app port from ALB SG only
  aws ec2 authorize-security-group-ingress \
    --group-id "$ECS_SG_ID" \
    --protocol tcp --port $CONTAINER_PORT \
    --source-group "$ALB_SG_ID" >/dev/null
  # Allow EFS (NFS port 2049) within the ECS SG
  aws ec2 authorize-security-group-ingress \
    --group-id "$ECS_SG_ID" \
    --protocol tcp --port 2049 \
    --source-group "$ECS_SG_ID" >/dev/null
fi

# MCP: allow the MCP port from the ALB SG (idempotent — also covers re-runs that
# flip DEPLOY_MCP on after the SG already existed). An already-exists rule is fine.
if [ "$DEPLOY_MCP" = "true" ]; then
  aws ec2 authorize-security-group-ingress \
    --group-id "$ECS_SG_ID" \
    --protocol tcp --port "$MCP_PORT" \
    --source-group "$ALB_SG_ID" >/dev/null 2>&1 || true
fi
success "ECS task security group: $ECS_SG_ID"

# ── EFS (PERSISTENT STORAGE) ──────────────────────────────────────────────────
header "EFS filesystem (persistent storage)"

log "Creating EFS filesystem (or reusing if it exists)..."
EFS_ID=$(aws efs describe-file-systems \
  --query "FileSystems[?Tags[?Key=='Name'&&Value=='${APP_NAME}-data']].FileSystemId" \
  --output text 2>/dev/null || echo "")

if [ -z "$EFS_ID" ] || [ "$EFS_ID" = "None" ]; then
  EFS_ID=$(aws efs create-file-system \
    --performance-mode generalPurpose \
    --encrypted \
    --tags Key=Name,Value="${APP_NAME}-data" \
    --query 'FileSystemId' --output text)
fi
success "EFS filesystem: $EFS_ID"

wait_for "EFS filesystem" \
  "aws efs describe-file-systems --file-system-id $EFS_ID --query 'FileSystems[0].LifeCycleState' --output text" \
  "available"

log "Creating EFS mount targets (or reusing if they exist)..."
MT_COUNT=$(aws efs describe-mount-targets \
  --file-system-id "$EFS_ID" \
  --query 'MountTargets | length(@)' \
  --output text --region "$REGION" 2>/dev/null || echo "0")

if [ "$MT_COUNT" = "0" ]; then
  aws efs create-mount-target \
    --file-system-id "$EFS_ID" \
    --subnet-id "$SUBNET_1" \
    --security-groups "$ECS_SG_ID" \
    --region "$REGION" >/dev/null

  aws efs create-mount-target \
    --file-system-id "$EFS_ID" \
    --subnet-id "$SUBNET_2" \
    --security-groups "$ECS_SG_ID" \
    --region "$REGION" >/dev/null
fi

log "Waiting for EFS mount targets to become available..."
attempt=0
while [ $attempt -lt 40 ]; do
  STATES=$(aws efs describe-mount-targets \
    --file-system-id "$EFS_ID" \
    --query 'MountTargets[*].LifeCycleState' \
    --output text \
    --region "$REGION" 2>/dev/null || echo "")
  TOTAL=$(echo "$STATES" | wc -w | xargs)
  READY=$(echo "$STATES" | tr '\t' '\n' | grep -c "^available$" || true)
  echo -ne "  Mount targets ready: ${READY} / ${TOTAL}\r"
  if [ "$TOTAL" -ge 1 ] && [ "$READY" = "$TOTAL" ]; then
    echo ""
    break
  fi
  sleep 8
  attempt=$((attempt + 1))
done
echo ""
success "EFS mount targets ready"


log "Creating EFS access point..."
ACCESS_POINT_ID=$(aws efs create-access-point \
  --file-system-id "$EFS_ID" \
  --posix-user Uid=1000,Gid=1000 \
  --root-directory "Path=/data,CreationInfo={OwnerUid=1000,OwnerGid=1000,Permissions=755}" \
  --tags Key=Name,Value="${APP_NAME}-access-point" \
  --query 'AccessPointId' --output text)
success "EFS access point: $ACCESS_POINT_ID"

# ── IAM TASK EXECUTION ROLE ───────────────────────────────────────────────────
header "IAM task execution role"

ROLE_ARN=$(aws iam get-role --role-name ecsTaskExecutionRole \
  --query 'Role.Arn' --output text 2>/dev/null || true)

if [ -z "$ROLE_ARN" ]; then
  log "Creating ecsTaskExecutionRole..."
  ROLE_ARN=$(aws iam create-role \
    --role-name ecsTaskExecutionRole \
    --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{
        "Effect":"Allow",
        "Principal":{"Service":"ecs-tasks.amazonaws.com"},
        "Action":"sts:AssumeRole"
      }]
    }' \
    --query 'Role.Arn' --output text)
  aws iam attach-role-policy \
    --role-name ecsTaskExecutionRole \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
  success "Created ecsTaskExecutionRole"
else
  success "ecsTaskExecutionRole already exists"
fi

# ── ECS SERVICE-LINKED ROLE ──────────────────────────────────────────────────────────────
header "ECS service-linked role"

log "Ensuring ECS service-linked role exists..."
aws iam create-service-linked-role \
  --aws-service-name ecs.amazonaws.com 2>/dev/null || true
success "ECS service-linked role ready"

# ── ECS CLUSTER ───────────────────────────────────────────────────────────────
header "ECS cluster"

log "Creating ECS cluster (or reusing if it exists)..."
aws ecs create-cluster --cluster-name "$APP_NAME" >/dev/null 2>/dev/null || true
success "Cluster: $APP_NAME"

# ── CLOUDWATCH LOGS ───────────────────────────────────────────────────────────
header "CloudWatch log group"

LOG_GROUP="/ecs/${APP_NAME}-webapp"
ensure_log_group "$LOG_GROUP"
success "Log group: $LOG_GROUP"

# ── ECR IMAGE BUILD & PUSH ────────────────────────────────────────────────────
header "Building and pushing container image to ECR"

ECR_REPO="${APP_NAME}-webapp"
CONTAINER_IMAGE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${ECR_REPO}:latest"

log "Creating ECR repository (or reusing if it exists)..."
EXISTING_REPO=$(aws ecr describe-repositories \
  --repository-names "$ECR_REPO" \
  --region "$REGION" \
  --query 'repositories[0].repositoryUri' \
  --output text 2>/dev/null || echo "")

if [ -z "$EXISTING_REPO" ] || [ "$EXISTING_REPO" = "None" ]; then
  aws ecr create-repository \
    --repository-name "$ECR_REPO" \
    --region "$REGION" >/dev/null
fi
success "ECR repository ready: $CONTAINER_IMAGE"

# Check if image already exists in ECR — skip build if so
EXISTING_IMAGE=$(aws ecr describe-images \
  --repository-name "$ECR_REPO" \
  --image-ids imageTag=latest \
  --region "$REGION" \
  --query 'imageDetails[0].imageTags[0]' \
  --output text 2>/dev/null || echo "")

if [ "$EXISTING_IMAGE" = "latest" ]; then
  success "Image already exists in ECR — skipping build"
else
  log "Logging Docker into ECR..."
  aws ecr get-login-password --region "$REGION" | \
    docker login --username AWS --password-stdin \
    "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com" 2>/dev/null
  success "Docker logged into ECR"

  log "Cloning repo from GitHub..."
  BUILD_DIR=$(mktemp -d)
  git clone $GITHUB_REPO "$BUILD_DIR" --depth 1 --quiet
  success "Repo cloned"

  log "Building Docker image (this takes 3-5 minutes)..."
  docker buildx build --platform linux/amd64 --push -t "${CONTAINER_IMAGE}" "$BUILD_DIR" --quiet
  rm -rf "$BUILD_DIR"
  success "Image built"

    success "Image built and pushed to ECR: $CONTAINER_IMAGE"
fi

# ── TASK DEFINITION ───────────────────────────────────────────────────────────
header "Task definition"

# Pin the public base URL so OIDC/OAuth redirect URIs exactly match what's
# registered at the identity provider. Only set it here for HTTPS custom domains
# (the value is known up front); for plain HTTP the ALB DNS name isn't created
# yet at this point, so the app derives the redirect from forwarded request
# headers instead, and ./update.sh pins it on subsequent deploys.
PUBLIC_BASE_URL_ENV=""
if [ "$ENABLE_HTTPS" = "true" ] && [ -n "$DOMAIN_NAME" ]; then
  PUBLIC_BASE_URL_ENV=",
        { \"name\": \"POCT_PUBLIC_BASE_URL\", \"value\": \"https://${DOMAIN_NAME}\" }"
fi

# Web app container. Note the trailing comma/brace is closed below so we can
# optionally append the MCP container before the array closes.
WEBAPP_CONTAINER="
    {
      \"name\": \"${APP_NAME}-webapp\",
      \"image\": \"${CONTAINER_IMAGE}\",
      \"essential\": true,
      \"portMappings\": [{ \"containerPort\": ${CONTAINER_PORT}, \"protocol\": \"tcp\" }],
      \"environment\": [
        { \"name\": \"POCT_LOG_LEVEL\", \"value\": \"${LOG_LEVEL}\" },
        { \"name\": \"POCT_BIND_HOST\", \"value\": \"0.0.0.0\" },
        { \"name\": \"POCT_BIND_PORT\", \"value\": \"${CONTAINER_PORT}\" }${PUBLIC_BASE_URL_ENV}
      ],
      \"mountPoints\": [{
        \"sourceVolume\": \"${APP_NAME}-data\",
        \"containerPath\": \"/data\",
        \"readOnly\": false
      }],
      \"healthCheck\": {
        \"command\": [\"CMD-SHELL\", \"curl -f http://localhost:${CONTAINER_PORT}/health || exit 1\"],
        \"interval\": 30,
        \"timeout\": 5,
        \"retries\": 3,
        \"startPeriod\": 10
      },
      \"logConfiguration\": {
        \"logDriver\": \"awslogs\",
        \"options\": {
          \"awslogs-group\": \"${LOG_GROUP}\",
          \"awslogs-region\": \"${REGION}\",
          \"awslogs-stream-prefix\": \"ecs\"
        }
      }
    }"

# MCP server container (same image, run as `poct-mcp`). essential=false so a MCP
# crash never takes the web app task down. It waits for the web app to be HEALTHY
# (dependsOn), reaches it over localhost (awsvpc shares the task's network
# namespace), and shares the same /data volume so it reads the UI-managed gateway
# token live. Its health check treats any HTTP response as "up" (the endpoint
# returns 401/503 until you generate the token), matching docker-compose.
MCP_CONTAINER=""
if [ "$DEPLOY_MCP" = "true" ]; then
  MCP_CONTAINER=",
    {
      \"name\": \"${APP_NAME}-mcp\",
      \"image\": \"${CONTAINER_IMAGE}\",
      \"command\": [\"poct-mcp\"],
      \"essential\": false,
      \"dependsOn\": [{ \"containerName\": \"${APP_NAME}-webapp\", \"condition\": \"HEALTHY\" }],
      \"portMappings\": [{ \"containerPort\": ${MCP_PORT}, \"protocol\": \"tcp\" }],
      \"environment\": [
        { \"name\": \"POCT_LOG_LEVEL\", \"value\": \"${LOG_LEVEL}\" },
        { \"name\": \"POCT_MCP_TRANSPORT\", \"value\": \"streamable-http\" },
        { \"name\": \"POCT_MCP_HOST\", \"value\": \"0.0.0.0\" },
        { \"name\": \"POCT_MCP_PORT\", \"value\": \"${MCP_PORT}\" },
        { \"name\": \"POCT_MCP_BASE_URL\", \"value\": \"http://localhost:${CONTAINER_PORT}\" }
      ],
      \"mountPoints\": [{
        \"sourceVolume\": \"${APP_NAME}-data\",
        \"containerPath\": \"/data\",
        \"readOnly\": false
      }],
      \"healthCheck\": {
        \"command\": [\"CMD-SHELL\", \"curl -s -o /dev/null http://localhost:${MCP_PORT}/ || exit 1\"],
        \"interval\": 30,
        \"timeout\": 5,
        \"retries\": 3,
        \"startPeriod\": 15
      },
      \"logConfiguration\": {
        \"logDriver\": \"awslogs\",
        \"options\": {
          \"awslogs-group\": \"${LOG_GROUP}\",
          \"awslogs-region\": \"${REGION}\",
          \"awslogs-stream-prefix\": \"mcp\"
        }
      }
    }"
fi

log "Registering task definition..."
[ "$DEPLOY_MCP" = "true" ] && log "Including MCP server as a second container (port ${MCP_PORT})"
TASK_DEF_ARN=$(aws ecs register-task-definition \
  --family "${APP_NAME}-webapp" \
  --network-mode awsvpc \
  --requires-compatibilities FARGATE \
  --cpu "$CPU" \
  --memory "$MEMORY" \
  --execution-role-arn "$ROLE_ARN" \
  --container-definitions "[${WEBAPP_CONTAINER}${MCP_CONTAINER}
  ]" \
  --volumes "[
    {
      \"name\": \"${APP_NAME}-data\",
      \"efsVolumeConfiguration\": {
        \"fileSystemId\": \"${EFS_ID}\",
        \"transitEncryption\": \"ENABLED\",
        \"authorizationConfig\": {
          \"accessPointId\": \"${ACCESS_POINT_ID}\",
          \"iam\": \"DISABLED\"
        }
      }
    }
  ]" \
  --query 'taskDefinition.taskDefinitionArn' --output text)
success "Task definition: $TASK_DEF_ARN"

# ── APPLICATION LOAD BALANCER ─────────────────────────────────────────────────
header "Application Load Balancer"

log "Creating ALB (or reusing if it exists)..."
ALB_ARN=$(aws elbv2 describe-load-balancers \
  --names "${APP_NAME}-alb" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || echo "")

if [ -z "$ALB_ARN" ] || [ "$ALB_ARN" = "None" ]; then
  ALB_ARN=$(aws elbv2 create-load-balancer \
    --name "${APP_NAME}-alb" \
    --subnets "$SUBNET_1" "$SUBNET_2" \
    --security-groups "$ALB_SG_ID" \
    --scheme internet-facing \
    --type application \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text)
fi

ALB_DNS=$(aws elbv2 describe-load-balancers \
  --load-balancer-arns "$ALB_ARN" \
  --query 'LoadBalancers[0].DNSName' --output text)
success "ALB: $ALB_DNS"

log "Creating target group (or reusing if it exists)..."
TG_ARN=$(aws elbv2 describe-target-groups \
  --names "${APP_NAME}-tg" \
  --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || echo "")

if [ -z "$TG_ARN" ] || [ "$TG_ARN" = "None" ]; then
  TG_ARN=$(aws elbv2 create-target-group \
    --name "${APP_NAME}-tg" \
    --protocol HTTP \
    --port $CONTAINER_PORT \
    --vpc-id "$VPC_ID" \
    --target-type ip \
    --health-check-path /health \
    --health-check-interval-seconds 30 \
    --health-check-timeout-seconds 5 \
    --healthy-threshold-count 2 \
    --unhealthy-threshold-count 3 \
    --query 'TargetGroups[0].TargetGroupArn' --output text)
fi
success "Target group: $TG_ARN"

# MCP target group. The MCP endpoint is auth-gated (returns 401/503 without a
# token), so a strict /health check would fail — we health-check "/" and accept
# any 200-499 response as healthy.
MCP_TG_ARN=""
if [ "$DEPLOY_MCP" = "true" ]; then
  log "Creating MCP target group (or reusing if it exists)..."
  MCP_TG_ARN=$(aws elbv2 describe-target-groups \
    --names "${APP_NAME}-mcp-tg" \
    --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || echo "")

  if [ -z "$MCP_TG_ARN" ] || [ "$MCP_TG_ARN" = "None" ]; then
    MCP_TG_ARN=$(aws elbv2 create-target-group \
      --name "${APP_NAME}-mcp-tg" \
      --protocol HTTP \
      --port "$MCP_PORT" \
      --vpc-id "$VPC_ID" \
      --target-type ip \
      --health-check-path / \
      --health-check-interval-seconds 30 \
      --health-check-timeout-seconds 5 \
      --healthy-threshold-count 2 \
      --unhealthy-threshold-count 3 \
      --matcher HttpCode=200-499 \
      --query 'TargetGroups[0].TargetGroupArn' --output text)
  fi
  success "MCP target group: $MCP_TG_ARN"
fi

# ── TLS CERTIFICATE (ACM) ─────────────────────────────────────────────────────
CERT_ARN=""
if [ "$ENABLE_HTTPS" = "true" ]; then
  header "TLS certificate (AWS Certificate Manager)"

  # Reuse an existing certificate for this domain if one was already requested
  CERT_ARN=$(aws acm list-certificates \
    --region "$REGION" \
    --query "CertificateSummaryList[?DomainName=='${DOMAIN_NAME}'].CertificateArn | [0]" \
    --output text 2>/dev/null || echo "")

  if [ -z "$CERT_ARN" ] || [ "$CERT_ARN" = "None" ]; then
    log "Requesting ACM certificate for $DOMAIN_NAME (DNS validation)..."
    CERT_ARN=$(aws acm request-certificate \
      --domain-name "$DOMAIN_NAME" \
      --validation-method DNS \
      --region "$REGION" \
      --query 'CertificateArn' --output text)
  fi
  success "Certificate: $CERT_ARN"

  CERT_STATUS=$(aws acm describe-certificate --certificate-arn "$CERT_ARN" --region "$REGION" \
    --query 'Certificate.Status' --output text 2>/dev/null || echo "")

  if [ "$CERT_STATUS" != "ISSUED" ]; then
    # Fetch the DNS validation record (takes a few seconds to populate)
    log "Retrieving the DNS validation record from ACM..."
    VAL_NAME=""; VAL_VALUE=""
    attempt=0
    while [ $attempt -lt 12 ]; do
      VAL_NAME=$(aws acm describe-certificate --certificate-arn "$CERT_ARN" --region "$REGION" \
        --query 'Certificate.DomainValidationOptions[0].ResourceRecord.Name' --output text 2>/dev/null || echo "")
      VAL_VALUE=$(aws acm describe-certificate --certificate-arn "$CERT_ARN" --region "$REGION" \
        --query 'Certificate.DomainValidationOptions[0].ResourceRecord.Value' --output text 2>/dev/null || echo "")
      [ -n "$VAL_NAME" ] && [ "$VAL_NAME" != "None" ] && break
      sleep 5; attempt=$((attempt + 1))
    done
    [ -n "$VAL_NAME" ] && [ "$VAL_NAME" != "None" ] || error "ACM did not return a validation record. Re-run ./deploy.sh — it will reuse this certificate."

    echo ""
    echo -e "  ${BOLD}${YELLOW}ACTION REQUIRED (record 1 of 2) — add this CNAME in Cloudflare to validate the certificate:${NC}"
    echo ""
    echo -e "    ${BOLD}Type:${NC}    CNAME"
    echo -e "    ${BOLD}Name:${NC}    ${VAL_NAME}"
    echo -e "    ${BOLD}Target:${NC}  ${VAL_VALUE}"
    echo -e "    ${BOLD}Proxy:${NC}   DNS only (grey cloud)  ${YELLOW}— required for validation${NC}"
    echo ""
    echo -e "  ${YELLOW}In Cloudflare you can paste the full Name; it won't double-append the zone.${NC}"
    echo -e "  ${YELLOW}This record can stay in place permanently so ACM auto-renews the cert.${NC}"
    echo -e "  ${YELLOW}This one cert secures both the app (443) and the MCP server (${MCP_PORT}).${NC}"
    echo -e "  ${YELLOW}The second record (pointing the domain at the load balancer) comes at the end.${NC}"
    echo ""
    read -rp "  Press Enter once the record is added to continue..." _

    log "Waiting for ACM to validate the certificate (typically 2-5 minutes)..."
    attempt=0
    while [ $attempt -lt 60 ]; do
      CERT_STATUS=$(aws acm describe-certificate --certificate-arn "$CERT_ARN" --region "$REGION" \
        --query 'Certificate.Status' --output text 2>/dev/null || echo "PENDING_VALIDATION")
      echo -ne "  Certificate status: ${CERT_STATUS}        \r"
      [ "$CERT_STATUS" = "ISSUED" ] && { echo ""; break; }
      [ "$CERT_STATUS" = "FAILED" ] && { echo ""; error "Certificate validation failed. Check the Cloudflare record, then re-run ./deploy.sh."; }
      sleep 10; attempt=$((attempt + 1))
    done
    echo ""
  fi

  [ "$CERT_STATUS" = "ISSUED" ] \
    || error "Certificate not validated in time. Once the Cloudflare CNAME propagates, re-run ./deploy.sh — it reuses this certificate and continues."
  success "Certificate issued"

  # Open HTTPS on the ALB security group (idempotent)
  aws ec2 authorize-security-group-ingress \
    --group-id "$ALB_SG_ID" --protocol tcp --port 443 --cidr 0.0.0.0/0 >/dev/null 2>&1 || true
  success "ALB security group allows HTTPS (443)"
fi

# ── ALB LISTENERS ─────────────────────────────────────────────────────────────
header "ALB listeners"

# Look up existing listeners by port so re-runs update rather than duplicate
HTTP_LISTENER_ARN=$(aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" \
  --query "Listeners[?Port==\`80\`].ListenerArn | [0]" --output text 2>/dev/null || echo "")
HTTPS_LISTENER_ARN=$(aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" \
  --query "Listeners[?Port==\`443\`].ListenerArn | [0]" --output text 2>/dev/null || echo "")

if [ "$ENABLE_HTTPS" = "true" ]; then
  # HTTPS:443 → forward to the app
  if [ -z "$HTTPS_LISTENER_ARN" ] || [ "$HTTPS_LISTENER_ARN" = "None" ]; then
    HTTPS_LISTENER_ARN=$(aws elbv2 create-listener \
      --load-balancer-arn "$ALB_ARN" \
      --protocol HTTPS --port 443 \
      --certificates "CertificateArn=${CERT_ARN}" \
      --ssl-policy ELBSecurityPolicy-TLS13-1-2-2021-06 \
      --default-actions "Type=forward,TargetGroupArn=${TG_ARN}" \
      --query 'Listeners[0].ListenerArn' --output text)
  else
    aws elbv2 modify-listener --listener-arn "$HTTPS_LISTENER_ARN" \
      --certificates "CertificateArn=${CERT_ARN}" \
      --default-actions "Type=forward,TargetGroupArn=${TG_ARN}" >/dev/null
  fi
  success "HTTPS listener ready (443 → container port $CONTAINER_PORT)"

  # HTTP:80 → permanent redirect to HTTPS
  REDIRECT_ACTION='Type=redirect,RedirectConfig={Protocol=HTTPS,Port=443,StatusCode=HTTP_301}'
  if [ -z "$HTTP_LISTENER_ARN" ] || [ "$HTTP_LISTENER_ARN" = "None" ]; then
    aws elbv2 create-listener \
      --load-balancer-arn "$ALB_ARN" --protocol HTTP --port 80 \
      --default-actions "$REDIRECT_ACTION" >/dev/null
  else
    aws elbv2 modify-listener --listener-arn "$HTTP_LISTENER_ARN" \
      --default-actions "$REDIRECT_ACTION" >/dev/null
  fi
  success "HTTP listener redirects to HTTPS (80 → 443)"
else
  # HTTP-only: 80 → forward to the app
  if [ -z "$HTTP_LISTENER_ARN" ] || [ "$HTTP_LISTENER_ARN" = "None" ]; then
    aws elbv2 create-listener \
      --load-balancer-arn "$ALB_ARN" --protocol HTTP --port 80 \
      --default-actions "Type=forward,TargetGroupArn=${TG_ARN}" >/dev/null
  else
    aws elbv2 modify-listener --listener-arn "$HTTP_LISTENER_ARN" \
      --default-actions "Type=forward,TargetGroupArn=${TG_ARN}" >/dev/null
  fi
  success "Listener created (port 80 → container port $CONTAINER_PORT)"
fi

# MCP listener — a dedicated port (8443, Cloudflare-proxyable) forwarding to the
# MCP target group. HTTPS reuses the same ACM cert when enabled; otherwise plain
# HTTP. Gateways (e.g. Saviynt) point at this port with their generated token.
if [ "$DEPLOY_MCP" = "true" ]; then
  MCP_LISTENER_ARN=$(aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" \
    --query "Listeners[?Port==\`${MCP_PORT}\`].ListenerArn | [0]" --output text 2>/dev/null || echo "")

  if [ "$ENABLE_HTTPS" = "true" ]; then
    if [ -z "$MCP_LISTENER_ARN" ] || [ "$MCP_LISTENER_ARN" = "None" ]; then
      aws elbv2 create-listener \
        --load-balancer-arn "$ALB_ARN" \
        --protocol HTTPS --port "$MCP_PORT" \
        --certificates "CertificateArn=${CERT_ARN}" \
        --ssl-policy ELBSecurityPolicy-TLS13-1-2-2021-06 \
        --default-actions "Type=forward,TargetGroupArn=${MCP_TG_ARN}" >/dev/null
    else
      aws elbv2 modify-listener --listener-arn "$MCP_LISTENER_ARN" \
        --certificates "CertificateArn=${CERT_ARN}" \
        --default-actions "Type=forward,TargetGroupArn=${MCP_TG_ARN}" >/dev/null
    fi
    success "MCP listener ready (HTTPS ${MCP_PORT} → container port $MCP_PORT)"
  else
    if [ -z "$MCP_LISTENER_ARN" ] || [ "$MCP_LISTENER_ARN" = "None" ]; then
      aws elbv2 create-listener \
        --load-balancer-arn "$ALB_ARN" --protocol HTTP --port "$MCP_PORT" \
        --default-actions "Type=forward,TargetGroupArn=${MCP_TG_ARN}" >/dev/null
    else
      aws elbv2 modify-listener --listener-arn "$MCP_LISTENER_ARN" \
        --default-actions "Type=forward,TargetGroupArn=${MCP_TG_ARN}" >/dev/null
    fi
    success "MCP listener created (HTTP ${MCP_PORT} → container port $MCP_PORT)"
  fi
fi

# ── ECS SERVICE ───────────────────────────────────────────────────────────────
header "ECS service"

# Load-balancer mappings: the web app always; the MCP target group too when
# enabled. Left unquoted on the CLI below so it splits into separate --load-balancers
# entries. Passing it on both create and update means a re-run reconciles the MCP
# wiring even if the service was first created web-app-only.
LB_MAPPINGS="targetGroupArn=${TG_ARN},containerName=${APP_NAME}-webapp,containerPort=${CONTAINER_PORT}"
if [ "$DEPLOY_MCP" = "true" ] && [ -n "$MCP_TG_ARN" ]; then
  LB_MAPPINGS="$LB_MAPPINGS targetGroupArn=${MCP_TG_ARN},containerName=${APP_NAME}-mcp,containerPort=${MCP_PORT}"
fi

log "Creating ECS service (or updating if it exists)..."
EXISTING_SVC=$(aws ecs describe-services \
  --cluster "$APP_NAME" --services "${APP_NAME}-webapp" \
  --query 'services[?status!=`INACTIVE`].status' \
  --output text 2>/dev/null || echo "")

if [ -n "$EXISTING_SVC" ] && [ "$EXISTING_SVC" != "None" ]; then
  # shellcheck disable=SC2086
  aws ecs update-service \
    --cluster "$APP_NAME" \
    --service "${APP_NAME}-webapp" \
    --task-definition "${APP_NAME}-webapp" \
    --desired-count 1 \
    --load-balancers $LB_MAPPINGS \
    --force-new-deployment >/dev/null
else
  # shellcheck disable=SC2086
  aws ecs create-service \
    --cluster "$APP_NAME" \
    --service-name "${APP_NAME}-webapp" \
    --task-definition "${APP_NAME}-webapp" \
    --desired-count 1 \
    --launch-type FARGATE \
    --network-configuration "awsvpcConfiguration={
      subnets=[$SUBNET_1,$SUBNET_2],
      securityGroups=[$ECS_SG_ID],
      assignPublicIp=ENABLED
    }" \
    --load-balancers $LB_MAPPINGS \
    --health-check-grace-period-seconds 30 >/dev/null
fi
success "ECS service created"

# ── SAVE STATE ────────────────────────────────────────────────────────────────
cat > "$STATE_FILE" <<EOF
# POC-Tracker deployment state — generated by deploy.sh
# Used by teardown.sh and manage.sh — do not delete
APP_NAME=$APP_NAME
REGION=$REGION
ACCOUNT_ID=$ACCOUNT_ID
VPC_ID=$VPC_ID
SUBNET_1=$SUBNET_1
SUBNET_2=$SUBNET_2
ALB_SG_ID=$ALB_SG_ID
ECS_SG_ID=$ECS_SG_ID
EFS_ID=$EFS_ID
ACCESS_POINT_ID=$ACCESS_POINT_ID
ALB_ARN=$ALB_ARN
ALB_DNS=$ALB_DNS
TG_ARN=$TG_ARN
LOG_GROUP=$LOG_GROUP
TASK_DEF_ARN=$TASK_DEF_ARN
ENABLE_HTTPS=$ENABLE_HTTPS
DOMAIN_NAME=$DOMAIN_NAME
CERT_ARN=$CERT_ARN
DEPLOY_MCP=$DEPLOY_MCP
MCP_PORT=$MCP_PORT
MCP_TG_ARN=$MCP_TG_ARN
EOF
success "State saved to $STATE_FILE"

# ── WAIT FOR HEALTHY ──────────────────────────────────────────────────────────
header "Waiting for app to become healthy"
log "This takes 3-5 minutes while the container starts and the ALB health checks pass..."
echo ""

attempt=0
max=40
while [ $attempt -lt $max ]; do
  RUNNING=$(aws ecs describe-services \
    --cluster "$APP_NAME" \
    --services "${APP_NAME}-webapp" \
    --query 'services[0].runningCount' \
    --output text 2>/dev/null || echo "0")
  HEALTH=$(aws elbv2 describe-target-health \
    --target-group-arn "$TG_ARN" \
    --query 'TargetHealthDescriptions[0].TargetHealth.State' \
    --output text 2>/dev/null || echo "unknown")
  echo -ne "  Running tasks: ${RUNNING} | ALB target health: ${HEALTH}\r"
  if [ "$RUNNING" = "1" ] && [ "$HEALTH" = "healthy" ]; then
    echo ""
    break
  fi
  sleep 10
  attempt=$((attempt + 1))
done

echo ""

if [ "$ENABLE_HTTPS" = "true" ]; then
  echo -e "${BOLD}${YELLOW}── LAST STEP — add ONE DNS record in Cloudflare ${NC}"
  echo ""
  echo -e "  Add exactly this record in your Cloudflare dashboard (DNS → Records → Add record)."
  echo -e "  This single record makes ${BOLD}both${NC} the app (port 443) and the MCP server"
  echo -e "  (port ${MCP_PORT}) reachable — they share the same hostname on the load balancer:"
  echo ""
  echo -e "    ${BOLD}Type:${NC}    CNAME"
  echo -e "    ${BOLD}Name:${NC}    ${DOMAIN_NAME}"
  echo -e "    ${BOLD}Target:${NC}  ${ALB_DNS}"
  if [ "$DEPLOY_MCP" = "true" ]; then
    echo -e "    ${BOLD}Proxy:${NC}   Proxied (orange cloud) ${YELLOW}— OK for both ports; 443 and ${MCP_PORT} are Cloudflare-proxyable${NC}"
    echo -e "    ${BOLD}TLS:${NC}     SSL/TLS → Overview → set encryption mode to ${BOLD}Full (strict)${NC}"
  else
    echo -e "    ${BOLD}Proxy:${NC}   Proxied (orange cloud) ${YELLOW}— set SSL/TLS mode to Full (strict)${NC}"
  fi
  echo ""
  echo -e "  ${YELLOW}Prefer to start unproxied? Set Proxy to \"DNS only\" (grey cloud) — the ACM${NC}"
  echo -e "  ${YELLOW}cert still makes both HTTPS URLs valid directly against the load balancer.${NC}"
  echo ""
  echo -e "  Once the record resolves, your endpoints are:"
  echo -e "    ${BOLD}App:${NC}  https://${DOMAIN_NAME}/"
  if [ "$DEPLOY_MCP" = "true" ]; then
    echo -e "    ${BOLD}MCP:${NC}  https://${DOMAIN_NAME}:${MCP_PORT}/mcp"
  fi
  echo ""
  APP_BASE="https://${DOMAIN_NAME}"
else
  APP_BASE="http://${ALB_DNS}"
fi

echo -e "${BOLD}${GREEN}═══════════════════════════════════════════════════${NC}"
echo -e "${BOLD}${GREEN}  Deployment complete!${NC}"
echo -e "${BOLD}${GREEN}═══════════════════════════════════════════════════${NC}"
echo ""
echo -e "  ${BOLD}App URL:${NC}   ${APP_BASE}/"
echo -e "  ${BOLD}API Docs:${NC}  ${APP_BASE}/docs"
echo -e "  ${BOLD}Health:${NC}    ${APP_BASE}/health"
if [ "$DEPLOY_MCP" = "true" ]; then
  if [ "$ENABLE_HTTPS" = "true" ]; then
    echo -e "  ${BOLD}MCP:${NC}       https://${DOMAIN_NAME}:${MCP_PORT}/mcp"
  else
    echo -e "  ${BOLD}MCP:${NC}       http://${ALB_DNS}:${MCP_PORT}/mcp"
  fi
  echo -e "             ${YELLOW}(generate the gateway token in Settings → MCP before use)${NC}"
fi
echo ""
echo -e "  ${BOLD}Username:${NC}  robbytheadmin"
echo -e "  ${BOLD}Password:${NC}  N0nPr0dF0r\$@viynt8"
echo ""
echo -e "  ${YELLOW}Change your password after first login!${NC}"
echo ""
echo -e "  Run ${BOLD}./manage.sh${NC} to stop, start, restart, or view logs."
echo -e "  Run ${BOLD}./teardown.sh${NC} to delete all AWS resources."
echo ""
