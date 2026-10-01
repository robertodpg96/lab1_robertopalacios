# Lab 1 — Containerized API Server with Monitoring: Full Documentation

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Project Structure](#2-project-structure)
3. [CDK Entry Point — app.py](#3-cdk-entry-point--apppy)
4. [CDK Infrastructure Stack — lab1/main_stack.py](#4-cdk-infrastructure-stack--lab1main_stackpy)
5. [API Server — assets/api_server/main.py](#5-api-server--assetsapi_servermainpy)
6. [Monitoring Application — assets/monitoring/main.py](#6-monitoring-application--assetsmonitoringmainpy)
7. [Dockerfiles](#7-dockerfiles)
8. [How Each Lab Requirement Was Satisfied](#8-how-each-lab-requirement-was-satisfied)

---

## 1. Architecture Overview

The solution implements a containerized URL shortener API server backed by a shared file system, with a separate batch monitoring application. Both run on Amazon ECS using EC2 capacity (not Fargate). The full data flow is:

1. A client sends HTTP requests to an **Application Load Balancer (ALB)**.
2. The ALB routes the request to one of the **API Server Tasks** running across EC2 instances in the ECS cluster.
3. The API server reads and writes a JSON file stored on **Amazon EFS**, which is mounted on every EC2 node, ensuring that all replicas of the API server see the same URL data regardless of which node the request lands on.
4. When the client calls the `/monitor` endpoint, the API server uses the **boto3 ECS client** to programmatically launch a one-off **Monitoring Task**.
5. The Monitoring Task runs for approximately 60 seconds, querying **Amazon CloudWatch** every 10 seconds for EC2 node CPU utilization, then sends a summary email via **Amazon SES**.

---

## 2. Project Structure

```
lab1_robertopalacios/
├── app.py                        # CDK application entry point
├── cdk.json                      # CDK CLI configuration
├── requirements.txt              # CDK Python dependencies
├── lab1/
│   └── main_stack.py             # Full infrastructure definition (CDK stack)
└── assets/
    ├── api_server/
    │   ├── main.py               # FastAPI URL shortener application
    │   ├── requirements.txt      # API server Python dependencies
    │   └── Dockerfile            # Container image definition
    └── monitoring/
        ├── main.py               # Monitoring batch script
        ├── requirements.txt      # Monitoring Python dependencies
        └── Dockerfile            # Container image definition
```

---

## 3. CDK Entry Point — `app.py`

### Libraries used

| Library | Purpose |
|---|---|
| `aws_cdk` | Core CDK library; provides the `App` and `Environment` classes |
| `os` | Standard library; reads environment variables for account and region |

### How it works

```python
import aws_cdk as cdk
import os

from lab1.main_stack import Lab1Stack

app = cdk.App()

env = cdk.Environment(
    account=app.node.try_get_context("account") or os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=app.node.try_get_context("region") or os.environ.get("CDK_DEFAULT_REGION"),
)

Lab1Stack(app, "Lab1Stack", env=env)

app.synth()
```

**`cdk.App()`** creates the root CDK application object. All stacks must be added to it.

**`cdk.Environment`** binds the stack to a specific AWS account and region. The values are read in order of priority:
1. CDK context flags passed at the CLI (`--context account=123`).
2. The environment variables `CDK_DEFAULT_ACCOUNT` and `CDK_DEFAULT_REGION`, which are automatically set by the CDK CLI when you have an AWS profile configured.

This pattern makes the project **account-agnostic**: the values come from the environment at deploy time, not from hardcoded strings in the code.

**`app.synth()`** triggers CDK to synthesize all stacks into CloudFormation templates and write them to the `cdk.out/` directory.

---

## 4. CDK Infrastructure Stack — `lab1/main_stack.py`

### Libraries used

| Library | Module alias | Purpose |
|---|---|---|
| `aws_cdk` | — | Core constructs: `Stack`, `CfnParameter`, `Duration` |
| `aws_cdk.aws_ec2` | `ec2` | VPC lookup, Security Groups, Launch Template, instance types, subnet selection |
| `aws_cdk.aws_autoscaling` | `autoscaling` | Auto Scaling Group for EC2 nodes |
| `aws_cdk.aws_ecs` | `ecs` | ECS Cluster, Task Definitions, Service, capacity providers, placement strategies, scaling |
| `aws_cdk.aws_ecr_assets` | `ecr_assets` | Builds Docker images locally and pushes them to ECR automatically |
| `aws_cdk.aws_iam` | `iam` | IAM Roles and Policy Statements (least privilege) |
| `aws_cdk.aws_efs` | `efs` | Elastic File System for shared persistent storage |
| `aws_cdk.aws_ses` | `ses` | SES Email Identity registration |
| `aws_cdk.aws_elasticloadbalancingv2` | `elbv2` | Application Load Balancer, Listener, Target Group |
| `constructs` | — | Base `Construct` class required by all CDK constructs |

---

### 4.1 Stack class and constants

```python
class Lab1Stack(Stack):
    API_SERVER_PORT = 8080  # Must match the one exposed in the Dockerfile
    MIN_TASK_COUNT = 3
```

`Lab1Stack` extends `Stack`, the CDK unit of deployment that maps directly to a CloudFormation stack. The two class-level constants avoid magic numbers and keep the port value consistent across the ALB target group, the container port mapping, and the security group rule.

---

### 4.2 Stack parameter — `CfnParameter`

```python
admin_email_param = CfnParameter(
    self, "SysAdminAddress",
    type="String",
    description="The email address of the system administrator."
)
```

`CfnParameter` models a CloudFormation parameter. It does not embed a value into the template at synth time; instead, it leaves a `{ "Ref": "SysAdminAddress" }` placeholder that CloudFormation resolves at deploy time when the caller provides `--parameters SysAdminAddress=<email>`. This keeps the sysadmin email out of source code.

---

### 4.3 SES Email Identity

```python
ses.EmailIdentity(
    self, "SysAdminEmailIdentity",
    identity=ses.Identity.email(admin_email_param.value_as_string)
)
```

Amazon SES requires every sender and recipient address to be verified before emails can be sent (in sandbox mode). `ses.EmailIdentity` creates an `AWS::SES::EmailIdentity` CloudFormation resource that triggers a verification email to the supplied address at deploy time. `admin_email_param.value_as_string` returns the CloudFormation `Ref` token for the parameter, so the identity is registered for whatever address the deployer provides.

---

### 4.4 VPC lookup

```python
vpc = ec2.Vpc.from_lookup(self, "Vpc", is_default=True)
```

`ec2.Vpc.from_lookup` does not create a new VPC — it retrieves an existing one. The `is_default=True` filter selects the account's default VPC (present in every AWS account by default). CDK performs this lookup during `cdk synth` by querying the AWS account and caching the result in `cdk.context.json`.

---

### 4.5 EC2 Instance Role, Security Group, and Launch Template

```python
ec2_instance_role = iam.Role(
    self, "AsgInstanceRole",
    assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
    managed_policies=[
        iam.ManagedPolicy.from_aws_managed_policy_name(
            "service-role/AmazonEC2ContainerServiceforEC2Role"
        )
    ]
)

ec2_security_group = ec2.SecurityGroup(
    self, "AsgSecurityGroup",
    vpc=vpc,
    allow_all_outbound=True,
)

ec2_launch_template = ec2.LaunchTemplate(
    self, "AsgLaunchTemplate",
    instance_type=ec2.InstanceType("t4g.micro"),
    machine_image=ecs.EcsOptimizedImage.amazon_linux2023(hardware_type=ecs.AmiHardwareType.ARM),
    role=ec2_instance_role,
    security_group=ec2_security_group,
    user_data=ec2.UserData.for_linux(),
    detailed_monitoring=True,
)
```

The EC2 nodes that back the ECS cluster are configured via a Launch Template, separate from the ASG itself:

- **`ec2_instance_role`** — attached to every EC2 instance via the launch template. The AWS-managed policy `AmazonEC2ContainerServiceforEC2Role` grants the ECS agent on each instance permission to register with the cluster, pull task definitions, and report container status.
- **`ec2_security_group`** — controls inbound/outbound network traffic at the instance level. Outbound is allowed by default; inbound rules are added later (ALB → port 8080, EFS NFS port 2049).
- **`instance_type=ec2.InstanceType("t4g.micro")`** — small ARM64 (AWS Graviton2) burstable instance, sufficient for a lab workload and Free Tier eligible. The ARM choice keeps the architecture consistent with the do-not-modify API server image asset (built for `LINUX_ARM64`).
- **`machine_image=ecs.EcsOptimizedImage.amazon_linux2023(hardware_type=ecs.AmiHardwareType.ARM)`** — selects the AWS-managed ECS-optimized AMI for Amazon Linux 2023 on ARM. This AMI comes pre-installed with the ECS agent, Docker, and required tooling. The `hardware_type=ARM` parameter is required because the default resolves to x86_64 and would not boot on a `t4g` instance. CDK resolves the exact AMI ID via SSM at deploy time.
- **`detailed_monitoring=True`** — enables 1-minute granularity CloudWatch metrics for the EC2 instances (instead of the default 5-minute granularity). This is required so the monitoring task can retrieve fresh CPU readings on its sub-minute polling cadence.

---

### 4.6 Auto Scaling Group

```python
ec2_asg = autoscaling.AutoScalingGroup(
    self, "Asg",
    vpc=vpc,
    launch_template=ec2_launch_template,
    min_capacity=3,
    max_capacity=5,
    vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC)
)
```

`autoscaling.AutoScalingGroup` creates the pool of EC2 instances that back the ECS cluster:

- **`launch_template`** — references the launch template defined above, so every instance launched by the ASG inherits the configured AMI, instance type, role, security group, and detailed monitoring setting.
- **`vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC)`** — distributes instances across all public subnets in the VPC. Since the default VPC has one public subnet per Availability Zone, this effectively spreads EC2 nodes across all AZs, satisfying the "nodes spread across AZs" rubric criterion.
- **`min_capacity=3, max_capacity=5`** — sets the ASG bounds. A minimum of 3 ensures there is always enough capacity to host the 3 required API server tasks across multiple AZs.

---

### 4.7 ASG Capacity Provider

```python
ec2_capacity_provider = ecs.AsgCapacityProvider(
    self, "AsgCapacityProvider",
    auto_scaling_group=ec2_asg,
    enable_managed_termination_protection=False,
)
```

`ecs.AsgCapacityProvider` links an ASG to ECS as a **capacity provider**. With managed scaling enabled (the default), ECS automatically scales the ASG up and down to ensure there is always enough EC2 capacity to place the requested number of tasks. This is the mechanism that makes the cluster use EC2 instead of Fargate.

`enable_managed_termination_protection=False` is set explicitly to keep stack teardown straightforward — managed termination protection prevents the ASG from terminating instances that host tasks, which can block `cdk destroy`.

---

### 4.8 ECS Cluster

```python
ecs_cluster = ecs.Cluster(
    self, "Cluster",
    vpc=vpc,
    cluster_name="Lab1Cluster"
)
ecs_cluster.add_asg_capacity_provider(ec2_capacity_provider)
```

`ecs.Cluster` creates the ECS cluster scoped to the VPC. `add_asg_capacity_provider` registers the ASG capacity provider with the cluster, making EC2 instances available for task placement.

---

### 4.9 EFS File System

```python
shared_storage = efs.FileSystem(
    self, "EfsFileSystem",
    vpc=vpc,
    lifecycle_policy=efs.LifecyclePolicy.AFTER_14_DAYS,
    performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
    out_of_infrequent_access_policy=efs.OutOfInfrequentAccessPolicy.AFTER_1_ACCESS,
    removal_policy=RemovalPolicy.DESTROY,
)
shared_storage.connections.allow_default_port_from(ec2_asg)
```

`efs.FileSystem` creates an Amazon EFS volume. EFS is an NFS-based distributed file system that can be mounted simultaneously by multiple EC2 instances, so all API server replicas across all nodes read and write the same `urls.json` file.

- **`lifecycle_policy`** — moves files not accessed in 14 days to the cheaper Infrequent Access storage class.
- **`performance_mode=GENERAL_PURPOSE`** — appropriate for latency-sensitive workloads like a web API.
- **`removal_policy=RemovalPolicy.DESTROY`** — overrides the CDK default of `RETAIN` for EFS. CDK retains file systems by default to prevent accidental data loss, but for this lab the URL store is ephemeral and should be cleaned up on `cdk destroy`. Without this override, the EFS resource would be skipped during stack deletion and remain in the account.
- **`shared_storage.connections.allow_default_port_from(ec2_asg)`** — adds a Security Group ingress rule that allows NFS traffic (TCP port 2049) from the ASG instances to the EFS mount targets. Without this rule, the EC2 instances cannot connect to EFS.

---

### 4.10 Docker Image Assets

```python
api_server_image_asset = ecr_assets.DockerImageAsset(
    self, 'ApiServerImageAsset',
    directory='assets/api_server',
    asset_name='ApiServerImageAsset',
    platform=ecr_assets.Platform.LINUX_ARM64
)
```

`ecr_assets.DockerImageAsset` tells CDK to:
1. Build the Docker image from the `Dockerfile` in `assets/api_server/` during `cdk deploy`.
2. Push the resulting image to the CDK-managed ECR repository in the account.
3. Return an image reference that can be used in task definitions.

This block is provided unchanged by the lab template (marked "do not modify"). **`platform=LINUX_ARM64`** targets ARM64, which is why the EC2 launch template uses a Graviton (`t4g`) instance type and an ARM AMI. The monitoring image asset uses the same `LINUX_ARM64` platform for consistency.

---

### 4.11 IAM Roles

#### API Server Task Role
```python
api_task_role = iam.Role(
    self, "ApiServerTaskRole",
    assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
)
```

Permissions are added after the monitoring task definition is created (so its ARN can be referenced). The **task role** is assumed by the application code running inside the API server container.

#### Execution Role
```python
ecs_exec_role = iam.Role(
    self, "ExecutionRole",
    assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
    managed_policies=[
        iam.ManagedPolicy.from_aws_managed_policy_name(
            "service-role/AmazonECSTaskExecutionRolePolicy"
        )
    ],
)
```

The **execution role** is assumed by the ECS agent (not the application). It grants the agent permission to pull the container image from ECR and write logs to CloudWatch Logs. The AWS-managed policy `AmazonECSTaskExecutionRolePolicy` provides exactly those permissions. The same execution role is reused by both the API server and monitoring task definitions.

#### Monitoring Task Role
```python
monitor_task_role = iam.Role(
    self, "MonitoringTaskRole",
    assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
)
monitor_task_role.add_to_policy(iam.PolicyStatement(
    actions=["cloudwatch:GetMetricStatistics"],
    resources=["*"]
))
monitor_task_role.add_to_policy(iam.PolicyStatement(
    actions=["ses:SendEmail"],
    resources=[f"arn:aws:ses:{self.region}:{self.account}:identity/{admin_email_param.value_as_string}"]
))
```

The monitoring task role grants only the two AWS API calls the monitoring script makes:

- **`cloudwatch:GetMetricStatistics`** — does not support resource-level ARN restrictions (CloudWatch enforces this at the API level), so `"*"` is the only valid resource. This is an inherent IAM limitation, not a least-privilege violation.
- **`ses:SendEmail`** — scoped to the **specific verified identity ARN** built from the stack's region, account, and the `SysAdminAddress` parameter. This applies the least-privilege principle exactly as taught: the monitoring task can only send email from the configured admin identity, not from any address.

---

### 4.12 API Server Task Definition

```python
api_task_def = ecs.Ec2TaskDefinition(
    self, "ApiServerTaskDef",
    task_role=api_task_role,
    execution_role=ecs_exec_role,
    network_mode=ecs.NetworkMode.BRIDGE
)

api_task_def.add_volume(
    name="efs-volume",
    efs_volume_configuration=ecs.EfsVolumeConfiguration(
        file_system_id=shared_storage.file_system_id
    )
)
```

`ecs.Ec2TaskDefinition` defines the blueprint for a task running on EC2 (as opposed to `FargateTaskDefinition`).

**`NetworkMode.BRIDGE`** uses the Docker bridge network on each EC2 host. Tasks share the host ENI rather than getting their own. This is simpler to operate than `awsvpc` mode and works directly with the EC2 launch type for this lab; it also means the ALB routes traffic to the EC2 instance's host port (8080), which is dynamically mapped to the container.

`add_volume` registers the EFS file system as a named volume on the task definition. This tells ECS which EFS to mount when a task starts; the actual mount path inside the container is set on the container definition.

---

### 4.13 Monitoring Task Definition

```python
monitor_task_def = ecs.Ec2TaskDefinition(
    self, "MonitoringTaskDef",
    task_role=monitor_task_role,
    execution_role=ecs_exec_role,
    network_mode=ecs.NetworkMode.BRIDGE
)

monitor_docker_image = ecr_assets.DockerImageAsset(
    self, 'MonitoringImageAsset',
    directory='assets/monitoring',
    asset_name='MonitoringImageAsset',
    platform=ecr_assets.Platform.LINUX_ARM64
)

monitor_task_def.add_container(
    "MonitoringContainer",
    image=ecs.ContainerImage.from_docker_image_asset(monitor_docker_image),
    memory_limit_mib=128,
    environment={
        "CLUSTER_NAME": ecs_cluster.cluster_name,
        "SYS_ADMIN_EMAIL": admin_email_param.value_as_string,
        "AWS_REGION": self.region,
        "ASG_NAME": ec2_asg.auto_scaling_group_name,
    },
    logging=ecs.LogDrivers.aws_logs(stream_prefix="Monitoring")
)
```

The monitoring task is a fully independent service from the API server — its own task definition, its own task role, its own container image. The container receives four environment variables at runtime: the cluster name (for logging context), the sysadmin email address (from the stack parameter), the region (for boto3 clients), and the ASG name (used as the CloudWatch metric dimension).

---

### 4.14 Granting the API Server Permission to Trigger Monitoring

```python
api_task_role.add_to_policy(iam.PolicyStatement(
    actions=["ecs:RunTask"],
    resources=[monitor_task_def.task_definition_arn]
))
api_task_role.add_to_policy(iam.PolicyStatement(
    actions=["iam:PassRole"],
    resources=[monitor_task_role.role_arn, ecs_exec_role.role_arn]
))
```

These two policy statements are added to the **API server's task role** after the monitoring task definition is created, so its ARN can be referenced:

- **`ecs:RunTask`** scoped to the monitoring task definition ARN — the API server cannot launch any other task.
- **`iam:PassRole`** scoped to exactly the two roles the monitoring task needs (its task role and the shared execution role). This is required by IAM whenever `RunTask` is called with explicit role ARNs in the task definition.

---

### 4.15 API Server Container Definition

```python
api_container = api_task_def.add_container(
    "ApiServerContainer",
    image=ecs.ContainerImage.from_docker_image_asset(api_server_image_asset),
    memory_limit_mib=256,
    environment={
        "CLUSTER_NAME": ecs_cluster.cluster_name,
        "MONITOR_TASK_DEF_ARN": monitor_task_def.task_definition_arn,
        "AWS_REGION": self.region,
    },
    logging=ecs.LogDrivers.aws_logs(stream_prefix="ApiServer")
)

api_container.add_mount_points(
    ecs.MountPoint(
        container_path="/mnt/efs",
        source_volume="efs-volume",
        read_only=False
    )
)

api_container.add_port_mappings(
    ecs.PortMapping(container_port=self.API_SERVER_PORT, host_port=self.API_SERVER_PORT)
)
```

- **`environment`** — injects the cluster name, monitoring task ARN, and region as environment variables. The application reads them at startup via `os.environ.get()`.
- **`add_mount_points`** — binds the `efs-volume` defined on the task definition to the path `/mnt/efs` inside the container. The API server writes `urls.json` to this path.
- **`add_port_mappings`** — declares that the container listens on port 8080 and maps it to host port 8080. With BRIDGE mode this is the host port the ALB target group reaches.

---

### 4.16 Application Load Balancer

```python
alb_security_group = ec2.SecurityGroup(self, "AlbSg", vpc=vpc, allow_all_outbound=True)
alb_security_group.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80))
ec2_asg.connections.allow_from(alb_security_group, ec2.Port.tcp(self.API_SERVER_PORT))

app_load_balancer = elbv2.ApplicationLoadBalancer(
    self, "Alb",
    vpc=vpc,
    internet_facing=True,
    security_group=alb_security_group,
    load_balancer_name='ApiServerServiceLB'
)

alb_listener = app_load_balancer.add_listener("Listener", port=80, open=False)
```

The ALB is internet-facing and accepts HTTP on port 80. Its security group allows inbound HTTP from any IPv4 address. A separate ingress rule on the ASG security group allows the ALB to reach the EC2 instances on port 8080. `open=False` on `add_listener` suppresses CDK's default behavior of creating an allow-all ingress rule on the listener, since the ALB security group already handles that.

---

### 4.17 ECS Service

```python
api_service = ecs.Ec2Service(
    self, "ApiServerService",
    cluster=ecs_cluster,
    task_definition=api_task_def,
    desired_count=self.MIN_TASK_COUNT,
    capacity_provider_strategies=[
        ecs.CapacityProviderStrategy(
            capacity_provider=ec2_capacity_provider.capacity_provider_name,
            weight=1
        )
    ],
    placement_strategies=[
        ecs.PlacementStrategy.spread_across("attribute:ecs.availability-zone")
    ]
)
```

`ecs.Ec2Service` keeps the specified number of task replicas running continuously.

- **`desired_count=3`** — starts 3 replicas.
- **`capacity_provider_strategies`** — directs ECS to use the ASG capacity provider for all tasks in this service.
- **`placement_strategies=[PlacementStrategy.spread_across("attribute:ecs.availability-zone")]`** — instructs ECS to distribute tasks as evenly as possible across the available Availability Zones.

---

### 4.18 Dynamic CPU-based Task Scaling

```python
task_autoscaling = api_service.auto_scale_task_count(
    min_capacity=self.MIN_TASK_COUNT,
    max_capacity=10
)
task_autoscaling.scale_on_cpu_utilization(
    "CpuScaling",
    target_utilization_percent=50
)
```

`auto_scale_task_count` creates an Application Auto Scaling scalable target for the ECS service. `scale_on_cpu_utilization` attaches a **target tracking scaling policy** that automatically increases or decreases the number of running tasks to keep average CPU utilization at 50%. When traffic spikes drive CPU above 50%, ECS adds tasks; when CPU drops, it removes tasks — always within the [3, 10] bounds. This satisfies the rubric's "Excellent" criterion for dynamic task scaling based on CPU usage.

---

### 4.19 ALB Target Group

```python
alb_listener.add_targets(
    "ServiceTarget",
    port=self.API_SERVER_PORT,
    targets=[api_service],
    health_check=elbv2.HealthCheck(
        path="/health",
        interval=Duration.seconds(30),
    ),
    deregistration_delay=Duration.seconds(30),
)
```

`add_targets` registers the ECS service as a target group for the ALB listener. The ALB pings `/health` on each task every 30 seconds; tasks that fail the check are taken out of rotation. `deregistration_delay` of 30 seconds gives in-flight requests time to complete before a task is removed.

---

## 5. API Server — `assets/api_server/main.py`

### Libraries used

| Library | Purpose |
|---|---|
| `fastapi` | Web framework; handles routing, request parsing, and response serialization |
| `uvicorn` | ASGI server that runs the FastAPI application |
| `pydantic` | Data validation; `BaseModel` validates incoming JSON request bodies |
| `boto3` | AWS SDK for Python; used to call the ECS `run_task` API |
| `os` | Reads environment variables injected by ECS at container startup |
| `json` | Serializes and deserializes the URL data store file |
| `uuid` | Generates random short IDs for shortened URLs |

---

### 5.1 Configuration block

```python
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8080"))
DATA_FILE = os.environ.get("DATA_FILE", "/mnt/efs/urls.json")
CLUSTER_NAME = os.environ.get("CLUSTER_NAME")
MONITOR_TASK_DEF_ARN = os.environ.get("MONITOR_TASK_DEF_ARN")
```

All configuration is read from environment variables at module load time. This follows the twelve-factor app methodology and avoids hardcoding values that differ between environments. The CDK stack injects all values except `HOST` and `PORT` (which use their defaults) via the container's `environment` map.

---

### 5.2 `UrlRequest` model

```python
class UrlRequest(BaseModel):
    url: str
```

A Pydantic `BaseModel` that declares the expected shape of the request body for the `/shorten` endpoint. FastAPI automatically validates incoming JSON against this model and returns a descriptive 422 error if the body is missing or malformed.

---

### 5.3 `load_data()`

```python
def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}
```

Reads the JSON data file from the EFS mount path and returns its contents as a Python dictionary mapping short IDs to original URLs. If the file does not yet exist (first run) or is unreadable, it returns an empty dictionary rather than raising, so the application starts cleanly on a fresh EFS volume.

---

### 5.4 `save_data(data)`

```python
def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f)
```

Writes the full dictionary back to the JSON file. `os.makedirs(..., exist_ok=True)` ensures the `/mnt/efs` directory exists before writing, which is necessary on the very first write if EFS was just mounted. Because all API server replicas share the same EFS volume, every write is immediately visible to all other replicas.

---

### 5.5 `GET /health`

```python
@app.get("/health")
def health():
    return {"status": "healthy"}
```

A lightweight health check endpoint that always returns HTTP 200. The ALB target group polls this endpoint every 30 seconds.

---

### 5.6 `POST /shorten`

```python
@app.post("/shorten")
def shorten_url(req: UrlRequest):
    data = load_data()
    short_id = str(uuid.uuid4())[:8]
    data[short_id] = req.url
    save_data(data)
    return {"short_id": short_id, "short_url": f"http://{HOST}:{PORT}/{short_id}"}
```

Accepts a JSON body `{"url": "<long_url>"}`, generates an 8-character random ID by slicing a UUID4 string, stores the mapping, and returns the short ID with a constructed short URL.

---

### 5.7 `GET /hello`

```python
@app.get("/hello")
def root():
    return {"message": "Welcome to the Simple URL shortener server!"}
```

A simple welcome endpoint. It is defined **before** the `/{short_id}` wildcard route so FastAPI's route matching registers it first.

---

### 5.8 `GET /{short_id}`

```python
@app.get("/{short_id}")
def expand_url(short_id: str):
    data = load_data()
    if short_id not in data:
        raise HTTPException(status_code=404, detail="URL not found")
    return RedirectResponse(url=data[short_id])
```

Looks up the short ID in the JSON file and returns an HTTP 307 redirect to the original URL. If the ID is not found, it returns 404. `RedirectResponse` sets the `Location` header automatically.

---

### 5.9 `DELETE /{short_id}`

```python
@app.delete("/{short_id}")
def delete_url(short_id: str):
    data = load_data()
    if short_id not in data:
        raise HTTPException(status_code=404, detail="URL not found")
    del data[short_id]
    save_data(data)
    return {"status": "deleted", "short_id": short_id}
```

Removes a previously shortened URL from the data store. Returns 200 with a confirmation payload on success, or 404 if the ID does not exist. This endpoint shares the same path pattern as the expand endpoint but is matched separately by FastAPI because they use different HTTP methods (DELETE vs GET). Implementing delete is **optional but encouraged** per the lab instructions.

---

### 5.10 `POST /monitor`

```python
@app.post("/monitor")
def trigger_monitoring():
    if not CLUSTER_NAME or not MONITOR_TASK_DEF_ARN:
        raise HTTPException(status_code=500, detail="Monitoring configuration missing")

    ecs = boto3.client("ecs", region_name=os.environ.get("AWS_REGION", "us-east-1"))
    try:
        response = ecs.run_task(
            cluster=CLUSTER_NAME,
            taskDefinition=MONITOR_TASK_DEF_ARN,
            launchType="EC2",
        )
        return {"status": "Monitoring task triggered", "failures": response.get("failures", [])}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

This endpoint programmatically launches the monitoring task as a one-off batch job using the **boto3 ECS client**.

- **`boto3.client("ecs")`** creates an ECS client authenticated using the task's IAM role credentials, automatically injected by ECS into the container environment.
- **`run_task`** starts a new task instance with the cluster name, the monitoring task definition ARN (from the environment), and `launchType="EC2"`. Because the task definition uses BRIDGE network mode, no `networkConfiguration` block is needed in the call.
- The endpoint returns immediately after `run_task` — it does not wait for the monitoring task to complete, which is the correct behavior for a batch trigger.

---

## 6. Monitoring Application — `assets/monitoring/main.py`

### Libraries used

| Library | Purpose |
|---|---|
| `boto3` | AWS SDK; used to call CloudWatch and SES APIs |
| `os` | Reads environment variables injected by ECS |
| `time` | Provides `sleep()` for the 10-second interval between samples |
| `datetime` | Constructs the time window for CloudWatch metric queries |

---

### 6.1 Configuration block

```python
CLUSTER_NAME = os.environ.get("CLUSTER_NAME")
SYS_ADMIN_EMAIL = os.environ.get("SYS_ADMIN_EMAIL")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
ASG_NAME = os.environ.get("ASG_NAME")
```

All values come from environment variables injected by CDK into the monitoring container definition. `ASG_NAME` is the Auto Scaling Group name, used as the dimension when querying CloudWatch for aggregate node CPU utilization.

---

### 6.2 `main()` — startup guard

```python
def main():
    if not CLUSTER_NAME or not SYS_ADMIN_EMAIL or not ASG_NAME:
        print("Missing required environment variables.")
        return
```

The function starts by validating that all required environment variables are present. If any are missing, the task exits cleanly with a log message rather than crashing.

---

### 6.3 Client initialization

```python
cw_client = boto3.client("cloudwatch", region_name=AWS_REGION)
ses_client = boto3.client("ses", region_name=AWS_REGION)
```

Two boto3 clients are created — one for CloudWatch (CPU metric queries) and one for SES (email sending). Both use the IAM role credentials injected by ECS.

---

### 6.4 Sampling loop

```python
for i in range(6):
    end_time = datetime.datetime.utcnow()
    start_time = end_time - datetime.timedelta(minutes=2)

    response = cw_client.get_metric_statistics(
        Namespace="AWS/EC2",
        MetricName="CPUUtilization",
        Dimensions=[
            {"Name": "AutoScalingGroupName", "Value": ASG_NAME}
        ],
        StartTime=start_time,
        EndTime=end_time,
        Period=60,
        Statistics=["Average"]
    )

    metric_points = response.get("Datapoints", [])
    if metric_points:
        newest_point = sorted(metric_points, key=lambda x: x["Timestamp"], reverse=True)[0]
        cpu_readings.append(newest_point["Average"])
        print(f"Sample {i+1}/6 - Average CPU across ASG: {newest_point['Average']:.2f}%")
    else:
        print(f"Sample {i+1}/6 - No CPU metric data found.")

    time.sleep(10)
```

The loop runs exactly **6 iterations with a 10-second sleep** between them, for a total runtime of approximately 60 seconds.

Each iteration calls `cloudwatch.get_metric_statistics`:

- **`Namespace="AWS/EC2"`** — targets the EC2 metrics namespace. This queries the actual EC2 node CPU, not the ECS task CPU.
- **`MetricName="CPUUtilization"`** — the standard EC2 CPU metric.
- **`Dimensions=[{"Name": "AutoScalingGroupName", "Value": ASG_NAME}]`** — scopes the query to all EC2 instances in the ASG, so CloudWatch returns the aggregate average CPU across all cluster nodes in a single API call.
- **`StartTime / EndTime`** — a 2-minute trailing window. This is short enough to keep the sampled datapoints close to "live" while still wide enough to tolerate CloudWatch's publication lag.
- **`Period=60`** — 1-minute granularity. Because the EC2 instances have **detailed monitoring enabled** in the launch template, fresh 1-minute datapoints are reliably available (instead of the default 5-minute granularity, which would give stale or empty results on a sub-minute cadence).
- **`Statistics=["Average"]`** — requests the average value across all instances in the dimension.

The response may contain multiple datapoints. The code sorts them by timestamp descending and takes the most recent one for that iteration. Sleeping 10 seconds between iterations and querying a 2-minute window ensures that within a 60-second monitoring run, the script picks up newly published datapoints as they appear.

---

### 6.5 Summary email

```python
if cpu_readings:
    avg_utilization = sum(cpu_readings) / len(cpu_readings)
    report_body = f"Monitoring Summary for Cluster: {CLUSTER_NAME}\n\nAverage CPU Usage across cluster nodes (6 samples over ~1 minute): {avg_utilization:.2f}%"
else:
    report_body = f"Monitoring Summary for Cluster: {CLUSTER_NAME}\n\nNo CPU metric data could be retrieved."

ses_client.send_email(
    Source=SYS_ADMIN_EMAIL,
    Destination={"ToAddresses": [SYS_ADMIN_EMAIL]},
    Message={
        "Subject": {"Data": f"ECS Cluster Monitoring Report - {CLUSTER_NAME}"},
        "Body": {"Text": {"Data": report_body}}
    }
)
```

After the loop completes, the script averages the collected CPU readings and composes a plain-text email body. `ses.send_email` sends the report from and to the `SYS_ADMIN_EMAIL` address. Both sender and recipient must be verified in SES — the CDK stack registers the address via `ses.EmailIdentity` at deploy time to satisfy this requirement.

---

## 7. Dockerfiles

Both Dockerfiles follow the same pattern.

### API Server Dockerfile

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 8080

CMD ["python", "main.py"]
```

- **`python:3.12-slim`** — a minimal Python 3.12 base image.
- **`COPY requirements.txt` before `COPY main.py`** — Docker builds layers in order. By copying and installing dependencies before copying application code, the dependency layer is cached and only rebuilt when `requirements.txt` changes.
- **`EXPOSE 8080`** — documents that the container listens on port 8080, matching `API_SERVER_PORT` in the CDK stack.
- **`CMD ["python", "main.py"]`** — starts the application. `main.py` calls `uvicorn.run(app, host=HOST, port=PORT)` at the bottom, so the uvicorn ASGI server starts accepting connections.

### Monitoring Dockerfile

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
CMD ["python", "main.py"]
```

Identical pattern without `EXPOSE`, since the monitoring task accepts no inbound connections — it only makes outbound calls to CloudWatch and SES.

---

## 8. How Each Lab Requirement Was Satisfied

| Requirement | Solution |
|---|---|
| **URL shortener: shorten endpoint** | `POST /shorten` in `api_server/main.py` accepts `{"url": "..."}` and returns a short ID |
| **URL shortener: expand endpoint** | `GET /{short_id}` in `api_server/main.py` returns a 307 redirect to the original URL |
| **URL shortener: delete endpoint (optional)** | `DELETE /{short_id}` in `api_server/main.py` removes the mapping from the JSON file |
| **Data store: JSON file, no external database** | `load_data()` and `save_data()` read/write `/mnt/efs/urls.json` directly |
| **Distributed storage via EFS** | EFS file system mounted at `/mnt/efs` on every API server task; all replicas share the same file |
| **Container orchestration: Amazon ECS** | `ecs.Ec2Service` with `ecs.Ec2TaskDefinition`; no Fargate constructs used anywhere |
| **Container registry: Amazon ECR** | `ecr_assets.DockerImageAsset` builds and pushes both images to ECR automatically on `cdk deploy` |
| **ECS capacity provider: not Fargate** | `ecs.AsgCapacityProvider` backed by an EC2 ASG; `Ec2TaskDefinition` confirms EC2 launch type |
| **`SysAdminAddress` as stack parameter** | `CfnParameter(self, "SysAdminAddress", ...)` creates a CloudFormation parameter; value never hardcoded |
| **SES identity registration** | `ses.EmailIdentity` registers the sysadmin address at deploy time |
| **Monitoring: separate independent service** | Monitoring has its own `Ec2TaskDefinition`, its own IAM role, its own Docker image, and is launched on demand via `run_task` |
| **Monitoring: runs for ~one minute** | `for i in range(6): ... time.sleep(10)` — 6 iterations × 10 seconds = 60 seconds |
| **Monitoring: CPU query every 10 seconds** | `cloudwatch.get_metric_statistics(...)` called once per loop iteration before each `sleep(10)` |
| **Monitoring: queries EC2 node CPU** | `Namespace="AWS/EC2"`, `Dimensions=[{"Name": "AutoScalingGroupName", ...}]` targets EC2 instances |
| **Monitoring: 1-minute CloudWatch granularity** | `detailed_monitoring=True` on the launch template so 1-min datapoints are available for sub-minute sampling |
| **Monitoring: email summary after all intervals** | `ses.send_email(...)` called after the loop exits, not during it |
| **Monitoring endpoint triggers monitoring task** | `POST /monitor` calls `boto3 ecs.run_task(...)` with the monitoring task definition ARN |
| **Dynamic CPU-based task scaling** | `service.auto_scale_task_count(...).scale_on_cpu_utilization("CpuScaling", target_utilization_percent=50)` |
| **Nodes forcedly spread across AZs** | ASG spans all public subnets in the default VPC (one per AZ); tasks placed with `PlacementStrategy.spread_across("attribute:ecs.availability-zone")` |
| **Least privilege IAM** | Each role has only the specific actions it needs; `ecs:RunTask` scoped to the monitoring task definition ARN, `iam:PassRole` scoped to the two required role ARNs, `ses:SendEmail` scoped to the verified identity ARN; `cloudwatch:GetMetricStatistics` is `"*"` only because the API does not support resource-level restrictions |
| **Account-agnostic CDK project** | Account and region read from `CDK_DEFAULT_ACCOUNT` / `CDK_DEFAULT_REGION` in `app.py`; no hardcoded IDs in source |
| **Dockerized applications** | Each application has its own `Dockerfile`; CDK builds and pushes images via `DockerImageAsset` |
