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
