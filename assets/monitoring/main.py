import os
import time
import datetime
import boto3

CLUSTER_NAME = os.environ.get("CLUSTER_NAME")
SYS_ADMIN_EMAIL = os.environ.get("SYS_ADMIN_EMAIL")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
ASG_NAME = os.environ.get("ASG_NAME")

def main():
    if not CLUSTER_NAME or not SYS_ADMIN_EMAIL or not ASG_NAME:
        print("Missing required environment variables.")
        return

    cw_client = boto3.client("cloudwatch", region_name=AWS_REGION)
    ses_client = boto3.client("ses", region_name=AWS_REGION)

    cpu_readings = []

    print(f"Starting monitoring for cluster {CLUSTER_NAME}...")

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

    if cpu_readings:
        avg_utilization = sum(cpu_readings) / len(cpu_readings)
        report_body = f"Monitoring Summary for Cluster: {CLUSTER_NAME}\n\nAverage CPU Usage across cluster nodes (6 samples over ~1 minute): {avg_utilization:.2f}%"
    else:
        report_body = f"Monitoring Summary for Cluster: {CLUSTER_NAME}\n\nNo CPU metric data could be retrieved."

    print("Sending email report...")
    try:
        ses_client.send_email(
            Source=SYS_ADMIN_EMAIL,
            Destination={"ToAddresses": [SYS_ADMIN_EMAIL]},
            Message={
                "Subject": {"Data": f"ECS Cluster Monitoring Report - {CLUSTER_NAME}"},
                "Body": {"Text": {"Data": report_body}}
            }
        )
        print("Email sent successfully.")
    except Exception as e:
        print(f"Failed to send email: {e}")

if __name__ == "__main__":
    main()