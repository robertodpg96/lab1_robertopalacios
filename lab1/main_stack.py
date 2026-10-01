from aws_cdk import (
    Stack,
    aws_ecs as ecs,
    aws_ecr_assets as ecr_assets,
    aws_ec2 as ec2,
    aws_iam as iam,
    aws_efs as efs,
    aws_ses as ses,
    CfnParameter,
    Duration,
    RemovalPolicy,
    aws_elasticloadbalancingv2 as elbv2,
    aws_autoscaling as autoscaling,
)
from constructs import Construct


class Lab1Stack(Stack):
    API_SERVER_PORT = 8080 # Must match the one exposed in the Dockerfile
    MIN_TASK_COUNT = 3

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        admin_email_param = CfnParameter(
            self, "SysAdminAddress",
            type="String",
            description="The email address of the system administrator."
        )

        # Register sysadmin address as a verified SES identity so emails can be sent
        ses.EmailIdentity(
            self, "SysAdminEmailIdentity",
            identity=ses.Identity.email(admin_email_param.value_as_string)
        )

        # TODO: retrieve account's default VPC
        vpc = ec2.Vpc.from_lookup(self, "Vpc", is_default=True)

        # TODO: create Auto Scaling group
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

        asg = autoscaling.AutoScalingGroup(
            self, "Asg",
            vpc=vpc,
            launch_template=ec2_launch_template,
            min_capacity=3,
            max_capacity=5,
            vpc_subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC)
        )

        # TODO: create ASG capacity provider for ECS
        ec2_capacity_provider = ecs.AsgCapacityProvider(
            self, "AsgCapacityProvider",
            auto_scaling_group=asg,
            enable_managed_termination_protection=False,
        )

        # TODO: create ECS cluster
        ecs_cluster = ecs.Cluster(
            self, "Cluster",
            vpc=vpc,
            cluster_name="Lab1Cluster"
        )

        # TODO: add capacity provider to cluster
        ecs_cluster.add_asg_capacity_provider(ec2_capacity_provider)

        shared_storage = efs.FileSystem(
            self, "EfsFileSystem",
            vpc=vpc,
            lifecycle_policy=efs.LifecyclePolicy.AFTER_14_DAYS,
            performance_mode=efs.PerformanceMode.GENERAL_PURPOSE,
            out_of_infrequent_access_policy=efs.OutOfInfrequentAccessPolicy.AFTER_1_ACCESS,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # Allow ASG to access EFS
        shared_storage.connections.allow_default_port_from(asg)

        """
        Here we are defining a Docker image with the contents of the specified directory.
        CDK will build the image and push it to ECR automatically upon CDK deploy

        Note: don't modify this block of code
        """
        api_server_image_asset = ecr_assets.DockerImageAsset(
            self, 'ApiServerImageAsset',
            directory='assets/api_server',
            asset_name='ApiServerImageAsset',
            platform=ecr_assets.Platform.LINUX_ARM64
        )

        """
        Below we define the ECS task role that grants containers in the task permission to call AWS APIs on your behalf,
        and the execution role that grants the ECS agent permission to call AWS APIs on your behalf.

        Note: don't modify this block of code
        """
        task_role = iam.Role(
            self, "ApiServerTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )

        exec_role = iam.Role(
            self, "ExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                )
            ],
        )

        # TODO: create the ECS task definition with the two roles defined above
        api_task_def = ecs.Ec2TaskDefinition(
            self, "ApiServerTaskDef",
            task_role=task_role,
            execution_role=exec_role,
            network_mode=ecs.NetworkMode.HOST
        )

        api_task_def.add_volume(
            name="efs-volume",
            efs_volume_configuration=ecs.EfsVolumeConfiguration(
                file_system_id=shared_storage.file_system_id
            )
        )

        # Monitoring Task Definition
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

        monitor_task_def = ecs.Ec2TaskDefinition(
            self, "MonitoringTaskDef",
            task_role=monitor_task_role,
            execution_role=exec_role,
            network_mode=ecs.NetworkMode.HOST
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
                "ASG_NAME": asg.auto_scaling_group_name,
            },
            logging=ecs.LogDrivers.aws_logs(stream_prefix="Monitoring")
        )

        # allow api server to trigger the monitoring task
        task_role.add_to_policy(iam.PolicyStatement(
            actions=["ecs:RunTask"],
            resources=[monitor_task_def.task_definition_arn]
        ))
        task_role.add_to_policy(iam.PolicyStatement(
            actions=["iam:PassRole"],
            resources=[monitor_task_role.role_arn, exec_role.role_arn]
        ))

        # TODO: add a container to the task definition using the image asset defined earlier
        container = api_task_def.add_container(
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

        container.add_mount_points(
            ecs.MountPoint(
                container_path="/mnt/efs",
                source_volume="efs-volume",
                read_only=False
            )
        )

        """
        Here we define which ports the container exposes (those our app listens on)

        Note: don't modify this block of code
        """
        container.add_port_mappings(ecs.PortMapping(container_port=self.API_SERVER_PORT))

        """
        Here we create a security group for the ALB that will front the ECS service,
        allowing inbound HTTP requests, and all outbound traffic.
        We also allow the ALB to reach the ASG

        Note: don't modify this block of code
        """
        alb_sg = ec2.SecurityGroup(self, "AlbSg", vpc=vpc, allow_all_outbound=True)
        alb_sg.add_ingress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(80))
        asg.connections.allow_from(alb_sg, ec2.Port.tcp(self.API_SERVER_PORT))

        """
        Here we are creating the ALB that will front the ECS service,
        along with a listener on HTTP port
        """
        app_load_balancer = elbv2.ApplicationLoadBalancer(
            self, "Alb",
            vpc=vpc,
            internet_facing=True,
            security_group=alb_sg,
            load_balancer_name='ApiServerServiceLB'
        )

        listener = app_load_balancer.add_listener("Listener", port=80, open=False)

        # TODO: create the ECS service
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

        # scale tasks based on CPU utilization
        task_autoscaling = api_service.auto_scale_task_count(
            min_capacity=self.MIN_TASK_COUNT,
            max_capacity=10
        )
        task_autoscaling.scale_on_cpu_utilization(
            "CpuScaling",
            target_utilization_percent=50
        )

        """
        Here we define the service as a target for the listener.

        Note: don't modify this block of code
        """
        listener.add_targets(
            "ServiceTarget",
            port=self.API_SERVER_PORT,
            targets=[api_service],
            health_check=elbv2.HealthCheck(
                path="/health",
                interval=Duration.seconds(30),
            ),
            deregistration_delay=Duration.seconds(30),
        )